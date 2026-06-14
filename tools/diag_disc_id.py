"""Diagnostic: compute MB disc-ID from an RBI file and show track offsets."""

import sys
from pathlib import Path

from cdda2img.cdrdao_reader import parsed_to_rbi_disc
from cdda2img.container import read_header
from cdda2img.mb_lookup import disc_id_from_rbi
from cdda2img.rbi_format import BLOCK_TYPE_TOC
from cdda2img.toc_parser import parse_toc

p = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ABBA's Greatest Hits 24.rbi")
h = read_header(p)
e = h.find_block(BLOCK_TYPE_TOC)
if e is None:
    msg = "TOC block not found in RBI"
    raise SystemExit(msg)
with open(p, "rb") as f:
    f.seek(e.offset)
    toc = f.read(e.length)

disc = parsed_to_rbi_disc(parse_toc(toc))
computed = disc_id_from_rbi(disc)
expected = "xu6JNKjjqvue0dEfEKJ5d7Ffipw-"
print(f"disc-ID from RBI: {computed}")
print(f"whipper disc-ID:  {expected}")
print(f"match: {computed == expected}")
print()
for t in disc.tracks[:4]:
    lba = t.start_frame + t.pregap_frames + 150
    print(
        f"  track {t.track_number:2d}: start={t.start_frame:6d}  pregap={t.pregap_frames:4d}  -> LBA={lba}"
    )
