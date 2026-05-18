"""
ddp_reader.py — Import a DDP 2.0 image as an RBI-ready disc object.

DDP (Disc Description Protocol) 2.0 image directories contain:
  DDPID      128-byte identifier block: version magic + MCN
  PQDESCR    Q-channel index records (64 bytes each): track/index positions + ISRCs
  CDTEXT.BIN Raw IEC 60908 CD-TEXT packs (18 bytes each): titles, performers, DISC_ID
  TRACK*.DAT Per-track audio sectors (2352 bytes/sector, s16le, subchannel-stripped)
  TRACK*.RW  Per-track R-W subchannel data (96 bytes/sector; not used here)

Import always uses master mode: audio is imported 1:1 (no byte-order conversion).
GEAR Pro (the only verified DDP source) writes s16le to TRACK*.DAT files — the same
byte order as the RBI PCM block — so no swap is needed.

The standard 150-frame lead-in that precedes Track 1's index 1 in TRACK01.DAT is
skipped during PCM assembly.  This matches the cdrdao master-mode convention (the
cdrdao BIN also starts at Track 1's first music frame, not the lead-in).  If the
disc has a non-silent Track 1 hidden area (index 0 content before index 1), that
content will be silently dropped — this is accepted behaviour for the prototype.
"""

from dataclasses import dataclass
from pathlib import Path

from cdda2img.rbi_format import FLAG_MASTER_MODE, RBIDisc, RBITocEntry

_SECTOR_AUDIO_BYTES = 2352
_STANDARD_PREGAP_FRAMES = 150  # 2-second lead-in per IEC 60908
_PQ_RECORD_SIZE = 64
_LEADOUT_TRACK = 0xAA  # sentinel; "AA" in PQDESCR


# ---------------------------------------------------------------------------
# DDPID
# ---------------------------------------------------------------------------


def _parse_ddpid(path: Path) -> str:
    """Return the 13-digit MCN from DDPID, or '' if not present / not digits."""
    data = path.read_bytes()
    if not data[:6].startswith(b"DDP 2."):
        msg = f"{path.name}: unrecognised DDPID magic {data[:8]!r}"
        raise ValueError(msg)
    raw = data[8:21].rstrip(b" \x00").decode("ascii", errors="ignore")
    return raw if raw.isdigit() and len(raw) == 13 else ""


# ---------------------------------------------------------------------------
# PQDESCR
# ---------------------------------------------------------------------------


@dataclass
class _PQEntry:
    track: int  # 0 = lead-in, 1-99 = audio track, _LEADOUT_TRACK = lead-out
    index: int  # 0 = pre-gap start, 1 = audio start
    abs_frame: int  # absolute CD frame position (from disc start)
    isrc: str | None


def _mmssff_to_frames(s: str) -> int:
    return int(s[0:2]) * 75 * 60 + int(s[2:4]) * 75 + int(s[4:6])


def _parse_pqdescr(path: Path) -> list[_PQEntry]:
    """Parse all 64-byte PQDESCR records into a list of _PQEntry objects."""
    data = path.read_bytes()
    entries: list[_PQEntry] = []
    for off in range(0, len(data), _PQ_RECORD_SIZE):
        rec = data[off : off + _PQ_RECORD_SIZE]
        if len(rec) < _PQ_RECORD_SIZE or rec[:4] != b"VVVS":
            continue
        track_raw = rec[4:6].decode("ascii", errors="ignore")
        if track_raw == "AA":
            track = _LEADOUT_TRACK
        elif track_raw.strip().isdigit():
            track = int(track_raw)
        else:
            continue
        index = int(rec[6:8])
        abs_frame = _mmssff_to_frames(rec[10:16].decode("ascii"))
        isrc_raw = rec[20:32].rstrip(b" ").decode("ascii", errors="ignore")
        isrc = isrc_raw if len(isrc_raw) == 12 else None
        entries.append(
            _PQEntry(track=track, index=index, abs_frame=abs_frame, isrc=isrc)
        )
    return entries


# ---------------------------------------------------------------------------
# CDTEXT.BIN
# ---------------------------------------------------------------------------


def parse_cdtext_packs(
    data: bytes,
) -> tuple[str, str, str | None, dict[int, tuple[str, str]]]:
    """Parse block-0 CD-TEXT packs from raw pack bytes.

    Returns ``(disc_title, disc_performer, disc_id, track_map)`` where
    *track_map* maps 1-based track numbers to ``(title, performer)`` pairs.

    Pack structure (18 bytes): ID1 ID2 ID3 ID4 text[12] CRC[2].
    All packs for a given PTI in block 0 are concatenated (text fields only)
    to form a stream of NUL-terminated ISO-8859-1 strings: disc first (index 0),
    then tracks 1, 2, … in order.
    """
    streams: dict[int, bytearray] = {}
    for off in range(0, len(data) - 17, 18):
        pti = data[off]
        block = (data[off + 3] >> 5) & 0x07
        if block != 0 or pti not in (0x80, 0x81, 0x86):
            continue
        streams.setdefault(pti, bytearray()).extend(data[off + 4 : off + 16])

    def _split(raw: bytearray) -> list[str]:
        return [
            part.decode("iso-8859-1").rstrip() for part in bytes(raw).split(b"\x00")
        ]

    titles = _split(streams.get(0x80, bytearray()))
    performers = _split(streams.get(0x81, bytearray()))
    disc_id_stream = streams.get(0x86, bytearray())
    disc_id = (
        bytes(disc_id_stream).split(b"\x00")[0].decode("iso-8859-1").strip() or None
    )

    disc_title = titles[0] if titles else ""
    disc_performer = performers[0] if performers else ""

    n_tracks = max(len(titles) - 1, len(performers) - 1)
    track_map: dict[int, tuple[str, str]] = {}
    for i in range(1, n_tracks + 1):
        t = titles[i] if i < len(titles) else disc_title
        p = performers[i] if i < len(performers) else disc_performer
        if t or p:
            track_map[i] = (t, p)

    return disc_title, disc_performer, disc_id, track_map


# ---------------------------------------------------------------------------
# PCM assembly
# ---------------------------------------------------------------------------


def _assemble_pcm(
    ddp_dir: Path,
    track_count: int,
    skip_frames: int,
    pcm_out: Path,
    chunk_frames: int = 75 * 60,
) -> None:
    """Concatenate TRACKxx.DAT files into a single s16le PCM file.

    The first *skip_frames* sectors (the disc lead-in) are dropped from
    TRACK01.DAT.  All subsequent track files are copied in full.  Each
    2352-byte sector is byte-swapped from s16be (disc-native) to s16le
    (RBI-native).  All track files are validated to exist before any write
    begins, so the output file is never left half-written.
    """
    # Pre-flight: verify every DAT file exists before opening the output
    dat_files = [ddp_dir / f"TRACK{n:02d}.DAT" for n in range(1, track_count + 1)]
    missing = [p.name for p in dat_files if not p.exists()]
    if missing:
        msg = f"Missing DAT file(s): {', '.join(missing)}"
        raise FileNotFoundError(msg)

    chunk_bytes = chunk_frames * _SECTOR_AUDIO_BYTES
    skip_bytes = skip_frames * _SECTOR_AUDIO_BYTES
    with open(pcm_out, "wb") as f_out:
        for n, dat_path in enumerate(dat_files, start=1):
            with open(dat_path, "rb") as f_in:
                if n == 1 and skip_bytes:
                    f_in.seek(skip_bytes)
                while True:
                    chunk = f_in.read(chunk_bytes)
                    if not chunk:
                        break
                    f_out.write(chunk)


# ---------------------------------------------------------------------------
# RBIDisc construction
# ---------------------------------------------------------------------------


def _build_disc(
    pq_entries: list[_PQEntry],
    track_count: int,
    skip_frames: int,
    catalog: str,
    disc_title: str,
    disc_performer: str,
    disc_id: str | None,
    track_map: dict[int, tuple[str, str]],
) -> RBIDisc:
    """Convert parsed DDP metadata into an RBIDisc."""
    idx: dict[int, dict[int, int]] = {}
    for e in pq_entries:
        idx.setdefault(e.track, {})[e.index] = e.abs_frame

    isrc_by_track = {e.track: e.isrc for e in pq_entries if e.index == 1 and e.isrc}

    leadout = (
        idx.get(_LEADOUT_TRACK, {}).get(1) or idx.get(_LEADOUT_TRACK, {}).get(0) or 0
    )

    def _next_index0(n: int) -> int:
        if n + 1 in idx:
            return idx[n + 1].get(0, idx[n + 1].get(1, leadout))
        return leadout

    disc = RBIDisc(
        album=disc_title,
        artist=disc_performer,
        catalog=catalog or None,
        disc_id=disc_id,
    )
    for n in range(1, track_count + 1):
        t_idx = idx.get(n, {})
        index0_abs = t_idx.get(0, t_idx.get(1, skip_frames))
        index1_abs = t_idx.get(1, index0_abs)
        next_i0_abs = _next_index0(n)

        if n == 1:
            # Track 1 lead-in pre-gap is not stored in the PCM block
            start_frame = 0
            pregap = 0
        else:
            start_frame = index0_abs - skip_frames
            pregap = index1_abs - index0_abs

        duration = next_i0_abs - index1_abs
        title, performer = track_map.get(n, (disc_title, disc_performer))
        disc.tracks.append(
            RBITocEntry(
                track_number=n,
                title=title,
                performer=performer,
                start_frame=start_frame,
                duration_frames=duration,
                pregap_frames=pregap,
                isrc=isrc_by_track.get(n),
            )
        )
    return disc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _parse_ddp(ddp_dir: Path) -> tuple[RBIDisc, bool, int, int]:
    """Parse a DDP 2.0 directory without writing PCM.

    Returns ``(disc, has_cdtext, track_count, skip_frames)``.

    Raises:
        FileNotFoundError: DDPID or PQDESCR missing.
        ValueError: no audio tracks found.
    """
    for name in ("DDPID", "PQDESCR"):
        if not (ddp_dir / name).exists():
            msg = f"Not a DDP image directory (missing {name}): {ddp_dir}"
            raise FileNotFoundError(msg)

    catalog = _parse_ddpid(ddp_dir / "DDPID")
    pq_entries = _parse_pqdescr(ddp_dir / "PQDESCR")

    track_numbers = {e.track for e in pq_entries if 1 <= e.track <= 99}
    track_count = max(track_numbers, default=0)
    if track_count == 0:
        msg = f"No audio tracks found in PQDESCR: {ddp_dir}"
        raise ValueError(msg)

    skip_frames = next(
        (e.abs_frame for e in pq_entries if e.track == 1 and e.index == 1),
        _STANDARD_PREGAP_FRAMES,
    )

    cdtext_path = ddp_dir / "CDTEXT.BIN"
    disc_title = disc_performer = ""
    disc_id: str | None = None
    track_map: dict[int, tuple[str, str]] = {}
    has_cdtext = cdtext_path.exists()
    if has_cdtext:
        disc_title, disc_performer, disc_id, track_map = parse_cdtext_packs(
            cdtext_path.read_bytes()
        )

    disc = _build_disc(
        pq_entries,
        track_count,
        skip_frames,
        catalog,
        disc_title,
        disc_performer,
        disc_id,
        track_map,
    )
    return disc, has_cdtext, track_count, skip_frames


def info_ddp(ddp_dir: Path) -> tuple[RBIDisc, bool, int]:
    """Return ``(disc, has_cdtext, audio_bytes)`` for a DDP image without importing it."""
    disc, has_cdtext, track_count, _ = _parse_ddp(ddp_dir)
    dat_files = [ddp_dir / f"TRACK{n:02d}.DAT" for n in range(1, track_count + 1)]
    total_bytes = sum(p.stat().st_size for p in dat_files if p.exists())
    return disc, has_cdtext, total_bytes


def import_ddp(ddp_dir: Path, pcm_out: Path) -> tuple[RBIDisc, int]:
    """Parse a DDP 2.0 directory and write s16le PCM to *pcm_out*.

    Returns ``(disc, FLAG_MASTER_MODE)``.
    """
    disc, has_cdtext, track_count, skip_frames = _parse_ddp(ddp_dir)
    print(f"  CD-Text: {'YES' if has_cdtext else 'NO'}")
    _assemble_pcm(ddp_dir, track_count, skip_frames, pcm_out)
    return disc, FLAG_MASTER_MODE
