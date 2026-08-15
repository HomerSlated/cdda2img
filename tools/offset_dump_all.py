#!/usr/bin/env python3
"""offset_dump_all.py — dump every AccurateRip and REDUMP drive-offset entry, raw.

No deduplication, no vendor aliasing, no submission-max resolution: every row from
both corpora exactly as stored, tagged with its source. Sorted by name so entries
for the same drive from the two sources sit adjacent.

    uv run python tools/offset_dump_all.py
"""

from __future__ import annotations

import csv
import datetime
import re
import sqlite3
from pathlib import Path

IXX = Path("private/code/redumper/offsets.ixx")
DB = Path.home() / ".data/cdda2img/drive_offsets.db"
OUT = Path("private/research/incoming/offsets_all_raw.tsv")

_ENTRY = re.compile(
    r'^\s*\{\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*,\s*([+-]?\d+)\s*\}'
)


def main() -> None:
    rows: list[tuple[str, str, str, str, int, str]] = []

    for line in IXX.read_text(encoding="utf-8").splitlines():
        m = _ENTRY.match(line)
        if m:
            vendor, product, off = m.group(1), m.group(2), int(m.group(3))
            name = f"{vendor} {product}".strip()
            rows.append(("Redump", vendor, product, name, off, ""))

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    for r in conn.execute("SELECT ar_name, offset, submissions FROM ar_drives"):
        rows.append(("AR", "", "", r["ar_name"], r["offset"], str(r["submissions"])))
    conn.close()

    rows.sort(key=lambda t: (t[3].upper(), t[0]))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        f.write(
            f"# Generated {datetime.date.today().isoformat()} by tools/offset_dump_all.py\n"
        )
        f.write(f"# Redump : {IXX} (upstream redumper table, verbatim)\n")
        f.write(f"# AR     : {DB} ar_drives, verbatim\n")
        f.write("# NO dedup, NO aliasing, NO submission resolution. Sorted by name.\n")
        f.write(
            "# vendor/product are populated for Redump only -- AccurateRip stores a\n"
        )
        f.write(
            "# single joined string; its vendor/model split is discarded at import by\n"
        )
        f.write(
            "# drive_info._normalize_ar_name and is not recoverable from the database.\n"
        )
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["source", "vendor", "product", "name", "offset", "submissions"])
        for src, vendor, product, name, off, subs in rows:
            w.writerow([src, vendor, product, name, f"{off:+d}", subs])

    n_rd = sum(1 for r in rows if r[0] == "Redump")
    n_ar = len(rows) - n_rd
    print(f"wrote {OUT}")
    print(f"  Redump rows : {n_rd}")
    print(f"  AR rows     : {n_ar}")
    print(f"  total       : {len(rows)}")


if __name__ == "__main__":
    main()
