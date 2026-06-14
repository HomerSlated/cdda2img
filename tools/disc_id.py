"""Print the MusicBrainz disc ID computed from a .toc or .rbi file.

Usage:
    uv run python tools/disc_id.py <file.toc>
    uv run python tools/disc_id.py <file.rbi>
    uv run python tools/disc_id.py <file.toc> --lookup   # query MB too

Useful for comparing the disc-ID produced by a cdrdao fast-toc scan vs. a
full rip, or for diagnosing why a particular disc fails MB lookup.
"""

import sys
from pathlib import Path

from cdda2img.cdrdao_reader import parsed_to_rbi_disc
from cdda2img.mb_lookup import disc_id_from_rbi
from cdda2img.toc_parser import parse_toc


def _load_rbi(p: Path):
    from cdda2img.container import read_header
    from cdda2img.rbi_format import BLOCK_TYPE_TOC

    h = read_header(p)
    e = h.find_block(BLOCK_TYPE_TOC)
    if e is None:
        msg = f"{p}: TOC block not found"
        raise SystemExit(msg)
    with open(p, "rb") as f:
        f.seek(e.offset)
        return f.read(e.length)


def _load_toc(p: Path) -> bytes:
    return p.read_bytes()


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    do_lookup = "--lookup" in sys.argv

    if not args:
        msg = "Usage: disc_id.py <file.toc|file.rbi> [--lookup]"
        raise SystemExit(msg)

    p = Path(args[0])
    if not p.exists():
        msg = f"File not found: {p}"
        raise SystemExit(msg)

    toc_bytes = _load_rbi(p) if p.suffix.lower() == ".rbi" else _load_toc(p)

    disc = parsed_to_rbi_disc(parse_toc(toc_bytes))
    disc_id = disc_id_from_rbi(disc)

    print(f"source:   {p}")
    print(f"disc-ID:  {disc_id or '(none — empty disc?)'}")
    print(f"tracks:   {len(disc.tracks)}")
    print()
    print(f"  {'#':>2}  {'start':>8}  {'pregap':>7}  {'LBA (INDEX 01)':>14}")
    for t in disc.tracks:
        lba = t.start_frame + t.pregap_frames + 150
        print(
            f"  {t.track_number:>2}  {t.start_frame:>8}  {t.pregap_frames:>7}  {lba:>14}"
        )
    lead_out_lba = disc.total_frames + 150
    print(f"  {'lead-out':>10}                        {lead_out_lba:>14}")

    if do_lookup and disc_id:
        from cdda2img.mb_lookup import _setup_useragent, lookup_disc_id

        _setup_useragent()
        print()
        print("querying MusicBrainz…")
        matches = lookup_disc_id(disc)
        if not matches:
            print("  no matches")
        for m in matches:
            print(f"  {m.mb_release_id}  {m.album!r}  {m.artist!r}  {m.release_date}")


if __name__ == "__main__":
    main()
