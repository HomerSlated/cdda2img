"""
ccd_reader.py — CloneCD CCD/IMG disc image importer.

CCD format:
  *.ccd  INI-style text TOC: [CloneCD], [Disc], [Session N], [Entry N], [TRACK N], [CDText]
  *.img  Raw sector data (2352 bytes/sector, s16le — no byteswap needed)
  *.sub  96-byte subchannel per sector (ignored)

Track layout:
  [Entry N] Point=0x01-0x63: PLBA is authoritative sector address in IMG.
  [Entry N] Point=0xA2: lead-out PLBA.
  [TRACK N]: INDEX 0 (optional pre-gap start sector), INDEX 1 (= PLBA), ISRC.

Byte order: s16le (Windows-native) — no byteswap needed.
Track 1's standard 150-sector lead-in pre-gap (IMG sectors 0-149) is skipped
during PCM assembly; inter-track pre-gaps (INDEX 0 < PLBA) are included.

NOTE: CD-Text parsing (CDTextLength > 0) uses the same 18-byte pack format as
DDP/NRG, but the CDText hex-line encoding in CCD has not been validated against
a real sample image.  The code path is exercised only when CDTextLength > 0.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO

from cdda2img.ddp_reader import parse_cdtext_packs
from cdda2img.rbi_format import FLAG_MASTER_MODE, RBIDisc, RBITocEntry

_CHUNK_BYTES = 1 << 20  # 1 MiB read buffer
_CDDA_SECTOR_BYTES = 2352
_STANDARD_PREGAP_SECTORS = 150  # track 1 lead-in: IMG sectors 0-149


# ---------------------------------------------------------------------------
# INI parser
# ---------------------------------------------------------------------------


def _parse_ccd(text: str) -> dict[str, dict[str, str]]:
    """Parse INI-style CCD text into {section_name_lower: {key_lower: value}}."""
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    current_name = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_name = line[1:-1].lower()
            current = {}
            sections[current_name] = current
        elif "=" in line:
            key, _, value = line.partition("=")
            current[key.strip().lower()] = value.strip()
    return sections


def _parse_int(value: str) -> int:
    """Parse decimal or 0x-prefixed hex integer."""
    return int(value, 0) if value.startswith(("0x", "0X")) else int(value)


# ---------------------------------------------------------------------------
# [Entry N] and [TRACK N] extraction
# ---------------------------------------------------------------------------


def _extract_entries(
    sections: dict[str, dict[str, str]],
) -> tuple[int | None, list[dict]]:
    """Return (lead_out_plba, sorted_track_entries) from [Entry N] sections.

    track_entries dicts: {point, control, plba, session}.
    Entries with Point=0xA0/0xA1 (disc info) are ignored.
    """
    lead_out_plba: int | None = None
    track_entries: list[dict] = []

    for name, fields in sections.items():
        if not name.startswith("entry "):
            continue
        try:
            point = _parse_int(fields.get("point", "0"))
            control = _parse_int(fields.get("control", "0"))
            plba = _parse_int(fields.get("plba", "0"))
            session = _parse_int(fields.get("session", "1"))
        except ValueError:
            continue

        if point == 0xA2:
            lead_out_plba = plba
        elif 0x01 <= point <= 0x63:
            track_entries.append({
                "point": point,
                "control": control,
                "plba": plba,
                "session": session,
            })

    track_entries.sort(key=lambda e: e["point"])
    return lead_out_plba, track_entries


def _extract_track_details(
    sections: dict[str, dict[str, str]], n_tracks: int
) -> list[dict]:
    """Return per-track dicts from [TRACK N] sections (index 0 = track 1).

    Each dict: {isrc: str|None, index0: int|None, index1: int|None}.
    """
    details: list[dict] = []
    for n in range(1, n_tracks + 1):
        sec = sections.get(f"track {n}", {})
        isrc_raw = sec.get("isrc", "").strip()
        isrc = isrc_raw if len(isrc_raw) == 12 else None

        index0_str = sec.get("index 0")
        index1_str = sec.get("index 1")
        try:
            index0 = _parse_int(index0_str) if index0_str is not None else None
        except ValueError:
            index0 = None
        try:
            index1 = _parse_int(index1_str) if index1_str is not None else None
        except ValueError:
            index1 = None

        details.append({"isrc": isrc, "index0": index0, "index1": index1})
    return details


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(
    ccd_name: str,
    disc_fields: dict[str, str],
    track_entries: list[dict],
    lead_out_plba: int,
    img_size: int,
) -> None:
    """Raise ValueError for unsupported or malformed CCD images."""
    try:
        sessions = _parse_int(disc_fields.get("sessions", "1"))
    except ValueError:
        sessions = 1
    if sessions != 1:
        msg = (
            f"{ccd_name}: multi-session images are not supported ({sessions} sessions)"
        )
        raise ValueError(msg)

    for e in track_entries:
        if e["session"] != 1:
            msg = f"{ccd_name}: multi-session images are not supported"
            raise ValueError(msg)
        if e["control"] & 0x04:
            msg = (
                f"{ccd_name}: track {e['point']:02d} is a data track"
                f" (Control=0x{e['control']:02x}); only CD-DA audio images are supported"
            )
            raise ValueError(msg)

    if not track_entries:
        msg = f"{ccd_name}: no audio tracks found in CCD"
        raise ValueError(msg)

    expected = lead_out_plba * _CDDA_SECTOR_BYTES
    if img_size != expected:
        msg = (
            f"{ccd_name}: IMG file size {img_size} B does not match"
            f" {lead_out_plba} sectors x {_CDDA_SECTOR_BYTES} = {expected} B"
        )
        raise ValueError(msg)


# ---------------------------------------------------------------------------
# CDText
# ---------------------------------------------------------------------------


def _parse_cdtext_section(
    sections: dict[str, dict[str, str]], cdtext_length: int
) -> bytes | None:
    """Extract raw CD-Text pack bytes from [CDText] section.

    CCD stores each 18-byte pack as a hex line:
      ``Entries N=MM NN PP ...``   (N = 0-based pack index)

    Returns ``cdtext_length`` bytes, or None if the section is absent/malformed.
    """
    if cdtext_length == 0:
        return None
    sec = sections.get("cdtext", {})
    if not sec:
        return None

    n_packs = cdtext_length // 18
    buf = bytearray()
    for i in range(n_packs):
        line = sec.get(f"entries {i}", "")
        if not line:
            return None
        try:
            buf.extend(int(b, 16) for b in line.split())
        except ValueError:
            return None

    return bytes(buf) if len(buf) == cdtext_length else None


# ---------------------------------------------------------------------------
# Disc construction and PCM extraction
# ---------------------------------------------------------------------------


def _build_disc_and_write_pcm(
    img: IO[bytes],
    track_entries: list[dict],
    track_details: list[dict],
    lead_out_plba: int,
    catalog: str | None,
    disc_title: str,
    disc_performer: str,
    disc_id: str | None,
    track_map: dict[int, tuple[str, str]],
    pcm_out: Path,
) -> RBIDisc:
    """Build RBIDisc and write PCM in one pass.

    Track 1's 150-sector lead-in pre-gap (IMG sectors 0-149) is skipped.
    Inter-track pre-gaps (INDEX 0 present and < PLBA) are included in the
    PCM block so the RBI TOC can reference them with START directives.
    """
    disc = RBIDisc(
        album=disc_title, artist=disc_performer, catalog=catalog, disc_id=disc_id
    )
    pcm_frame = 0
    n_tracks = len(track_entries)

    with open(pcm_out, "wb") as out:
        for n, (entry, detail) in enumerate(zip(track_entries, track_details), start=1):
            plba = entry["plba"]
            index0 = detail["index0"]

            # --- sector range to read from IMG for this track ---
            if n == 1:
                # Always skip the standard 150-sector lead-in pre-gap.
                img_start = _STANDARD_PREGAP_SECTORS if plba == 0 else plba
                audio_plba = img_start  # no pre-gap in PCM for track 1
                pregap_frames = 0
            else:
                audio_plba = plba
                if index0 is not None and 0 < index0 < plba:
                    img_start = index0  # include pre-gap in PCM
                    pregap_frames = plba - index0
                else:
                    img_start = plba
                    pregap_frames = 0

            # Upper bound: start of next track's content (or lead-out).
            if n < n_tracks:
                nxt_entry = track_entries[n]  # 0-indexed: n == current 1-indexed
                nxt_detail = track_details[n]
                nxt_index0 = nxt_detail["index0"]
                nxt_plba = nxt_entry["plba"]
                img_end = (
                    nxt_index0
                    if nxt_index0 is not None and 0 < nxt_index0 < nxt_plba
                    else nxt_plba
                )
            else:
                img_end = lead_out_plba

            duration_frames = img_end - audio_plba

            title, performer = track_map.get(n, (disc_title, disc_performer))
            disc.tracks.append(
                RBITocEntry(
                    track_number=n,
                    title=title,
                    performer=performer,
                    start_frame=pcm_frame,
                    duration_frames=duration_frames,
                    pregap_frames=pregap_frames,
                    isrc=detail["isrc"],
                )
            )
            pcm_frame += pregap_frames + duration_frames

            # Write IMG sectors [img_start, img_end) to PCM output.
            img.seek(img_start * _CDDA_SECTOR_BYTES)
            remaining = (img_end - img_start) * _CDDA_SECTOR_BYTES
            while remaining > 0:
                chunk = img.read(min(_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)

    return disc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def import_ccd(ccd_path: Path, pcm_out: Path) -> tuple[RBIDisc, int]:
    """Import a CloneCD CCD/IMG disc image as master-mode RBI.

    Reads ``ccd_path`` (*.ccd text TOC) and the paired *.img file.
    Returns ``(disc, FLAG_MASTER_MODE)``.

    Raises:
        FileNotFoundError: IMG file not found alongside the CCD.
        ValueError: multi-session image, data track, IMG size mismatch,
                    missing lead-out entry, or no audio tracks.
    """
    # Locate the paired IMG file (case-insensitive fallback).
    img_path = ccd_path.with_suffix(".img")
    if not img_path.exists():
        candidates = [
            p
            for p in ccd_path.parent.iterdir()
            if p.stem.lower() == ccd_path.stem.lower() and p.suffix.lower() == ".img"
        ]
        if not candidates:
            msg = f"IMG file not found: {img_path}"
            raise FileNotFoundError(msg)
        img_path = candidates[0]

    text = ccd_path.read_text(encoding="utf-8", errors="replace")
    sections = _parse_ccd(text)

    disc_fields = sections.get("disc", {})
    try:
        cdtext_length = _parse_int(disc_fields.get("cdtextlength", "0"))
    except ValueError:
        cdtext_length = 0

    lead_out_plba, track_entries = _extract_entries(sections)
    if lead_out_plba is None:
        msg = f"{ccd_path.name}: no lead-out entry (Point=0xA2) found"
        raise ValueError(msg)

    if not track_entries:
        msg = f"{ccd_path.name}: no track entries found"
        raise ValueError(msg)

    n_tracks = len(track_entries)
    track_details = _extract_track_details(sections, n_tracks)
    img_size = img_path.stat().st_size

    _validate(ccd_path.name, disc_fields, track_entries, lead_out_plba, img_size)

    disc_title = disc_performer = ""
    disc_id: str | None = None
    catalog: str | None = None
    track_map: dict[int, tuple[str, str]] = {}

    cdtext_bytes = _parse_cdtext_section(sections, cdtext_length)
    if cdtext_bytes:
        disc_title, disc_performer, disc_id, track_map = parse_cdtext_packs(
            cdtext_bytes
        )

    with open(img_path, "rb") as img:
        disc = _build_disc_and_write_pcm(
            img,
            track_entries,
            track_details,
            lead_out_plba,
            catalog,
            disc_title,
            disc_performer,
            disc_id,
            track_map,
            pcm_out,
        )

    return disc, FLAG_MASTER_MODE
