"""inspect_discid.py — print the MusicBrainz disc-ID inputs for an RBI file.

Run from project root after a rip:

    uv run python tools/inspect_discid.py path/to/Eliminator.rbi

Output shows every byte that goes into the SHA-1 hash, the resulting disc-ID,
and the URLs you can open to compare against MusicBrainz's submitted disc-IDs
for a known release.

The point of this tool is debugging "MB returned 0 matches" — if the disc-ID
we compute doesn't match any disc-ID submitted for the release page you expect,
either (a) the pressing was never submitted or (b) our offset calculation is
off in some way (extra lead-in, pregap mis-attribution, etc.).
"""

from __future__ import annotations

import sys
from pathlib import Path

from cdda2img.container import read_header
from cdda2img.mb_lookup import compute_disc_id, disc_id_from_rbi
from cdda2img.rbi_format import BLOCK_TYPE_TOC
from cdda2img.toc_parser import parse_toc

_LEAD_IN_SECTORS = 150  # mirror of mb_lookup._LEAD_IN_SECTORS


def _load_disc_from_rbi(rbi_path: Path):
    """Read the RBI, parse its embedded TOC, return an RBIDisc-shaped surrogate."""
    header = read_header(rbi_path)
    toc_entry = next((e for e in header.directory if e.type_id == BLOCK_TYPE_TOC), None)
    if toc_entry is None:
        msg = f"{rbi_path}: no TOC block in directory"
        raise ValueError(msg)
    with rbi_path.open("rb") as f:
        f.seek(toc_entry.offset)
        toc_bytes = f.read(toc_entry.length)
    parsed = parse_toc(toc_bytes)

    # Build a minimal RBIDisc-shaped object for disc_id_from_rbi(). We replicate
    # the fields it reads rather than dragging in the whole rbi_format module
    # construction logic.
    from cdda2img.rbi_format import RBIDisc, RBITocEntry

    entries: list[RBITocEntry] = []
    cumulative = 0
    for t in parsed.tracks:
        entries.append(
            RBITocEntry(
                track_number=t.track_number,
                title=t.title or "",
                performer="",
                start_frame=t.start_frame,
                duration_frames=t.duration_frames,
                pregap_frames=t.pregap_frames,
            )
        )
        cumulative += t.duration_frames + t.pregap_frames
    disc = RBIDisc(album=parsed.title or "", artist=parsed.performer or "")
    disc.tracks = entries
    return disc


def _hex_dump(buf: bytes) -> str:
    lines = []
    for off in range(0, len(buf), 16):
        chunk = buf[off : off + 16]
        hexpart = " ".join(f"{b:02x}" for b in chunk)
        lines.append(f"  {off:04x}: {hexpart}")
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <file.rbi>", file=sys.stderr)
        return 2
    rbi_path = Path(sys.argv[1])
    if not rbi_path.exists():
        print(f"not found: {rbi_path}", file=sys.stderr)
        return 1

    disc = _load_disc_from_rbi(rbi_path)
    n = len(disc.tracks)
    tracks = sorted(disc.tracks, key=lambda t: t.track_number)
    offsets = [t.start_frame + t.pregap_frames + _LEAD_IN_SECTORS for t in tracks]
    # Match RBIDisc.total_frames: includes pregap *plus* audio duration per track.
    total_frames = sum(t.pregap_frames + t.duration_frames for t in tracks)
    lead_out = total_frames + _LEAD_IN_SECTORS

    print(f"File:           {rbi_path}")
    print(f"Tracks:         {n}")
    print(f"Total frames:   {total_frames}  ({total_frames / 75:.2f} s)")
    print(f"Lead-in:        {_LEAD_IN_SECTORS} sectors (2.00 s, Red Book standard)")
    print()
    print("Per-track inputs (LBA = start_frame + pregap_frames + 150 lead-in):")
    print("  trk  start_frame  pregap  → LBA offset")
    for t, off in zip(tracks, offsets):
        print(
            f"  {t.track_number:>3}  {t.start_frame:>11}  {t.pregap_frames:>6}  → {off}"
        )
    print()
    print(f"first_track:    {tracks[0].track_number}")
    print(f"last_track:     {tracks[-1].track_number}")
    print(f"lead_out:       {lead_out}")
    print()
    # Build the exact ASCII hex string that gets SHA-1'd (MB spec — NOT raw bytes)
    parts = [
        f"{tracks[0].track_number:02X}",
        f"{tracks[-1].track_number:02X}",
        f"{lead_out:08X}",
    ]
    for i in range(99):
        parts.append(f"{(offsets[i] if i < len(offsets) else 0):08X}")
    hex_input = "".join(parts)
    print(f"Hash input ({len(hex_input)} ASCII chars):")
    # Show the used portion: 2 + 2 + 8 + n*8 chars
    used_chars = 2 + 2 + 8 + 8 * n
    print(f"  {hex_input[:used_chars]}")
    print(f"  ... + {99 - n} x 8 zero chars for unused track slots")
    print()
    disc_id = compute_disc_id(
        tracks[0].track_number, tracks[-1].track_number, offsets, lead_out
    )
    via_rbi = disc_id_from_rbi(disc)
    if disc_id != via_rbi:
        msg = f"compute_disc_id / disc_id_from_rbi disagree: {disc_id!r} vs {via_rbi!r}"
        raise RuntimeError(msg)
    print(f"Disc ID:        {disc_id}")
    print()
    print("Verify:")
    print(f"  MB disc-ID page:    https://musicbrainz.org/cdtoc/{disc_id}")
    print(
        f"  MB API (JSON):      https://musicbrainz.org/ws/2/discid/{disc_id}?fmt=json"
    )
    print()
    print("On a known release page, click the 'CD' tab to see submitted disc-IDs;")
    print("compare to the value above.  If the computed ID isn't in MB's list,")
    print("either (a) this pressing was never submitted, or (b) one of our inputs")
    print("(per-track LBA / lead-out / pregap handling) differs from MB's expected")
    print("form.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
