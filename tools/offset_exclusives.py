#!/usr/bin/env python3
"""offset_exclusives.py — the drives each offset corpus holds and the other does not.

Writes two TSVs to private/research/incoming/ listing the entries unique to REDUMP
and to AccurateRip, each with a "near miss" column naming a plausible counterpart in
the other corpus so a naming variant can be told from a genuinely absent drive.

Two rules are load-bearing and both were arrived at the hard way (TODO N9c):

* AccurateRip holds DUPLICATE ``ar_name`` rows at different offsets with very
  different submission counts.  They are resolved by HIGHEST submissions, matching
  ``drive_info.find_drive_offset``.  A ``{name: row}`` dict keeps the minority row.
* AccurateRip's vendor string for LG drives is ``LG Electronics``, not ``LG``.

    uv run python tools/offset_exclusives.py
"""

from __future__ import annotations

import csv
import datetime
import re
import sqlite3
from pathlib import Path

IXX = Path("private/code/redumper/offsets.ixx")
DB = Path.home() / ".data/cdda2img/drive_offsets.db"
OUT = Path("private/research/incoming")

# REDUMP vendor string -> the vendor string AccurateRip's page uses. Exact whole-field
# rewrites only, never substring matching: measured 649/650 and 375/375, all agreeing.
ALIASES = {"HL-DT-ST": "LG ELECTRONICS", "MATSHITA": "PANASONIC"}

_ENTRY = re.compile(
    r'^\s*\{\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*,\s*([+-]?\d+)\s*\}'
)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _read_redump() -> dict[tuple[str, str], list[int]]:
    rows: dict[tuple[str, str], list[int]] = {}
    for line in IXX.read_text(encoding="utf-8").splitlines():
        m = _ENTRY.match(line)
        if m:
            key = (_norm(m.group(1)), _norm(m.group(2)))
            rows.setdefault(key, []).append(int(m.group(3)))
    return rows


def _read_accuraterip() -> dict[str, tuple[str, int, int]]:
    """Map upper-cased name -> (name, offset, submissions), highest submissions wins."""
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        # ASC so the highest-submission row is written last and survives.
        rows = conn.execute(
            "SELECT ar_name, offset, submissions FROM ar_drives"
            " ORDER BY submissions ASC"
        ).fetchall()
    finally:
        conn.close()
    return {
        _norm(r["ar_name"]).upper(): (
            _norm(r["ar_name"]),
            r["offset"],
            r["submissions"],
        )
        for r in rows
    }


def _candidate_keys(vendor: str, product: str) -> list[str]:
    keys = [f"{vendor} {product}".strip().upper()]
    alias = ALIASES.get(vendor.upper())
    if alias:
        keys.append(f"{alias} {product}".strip().upper())
    return keys


def main() -> None:
    redump = _read_redump()
    accuraterip = _read_accuraterip()

    redump_only: list[tuple[str, str, int, bool]] = []
    matched: set[str] = set()
    for (vendor, product), offsets in redump.items():
        hit = next(
            (k for k in _candidate_keys(vendor, product) if k in accuraterip), None
        )
        if hit is not None:
            matched.add(hit)
        else:
            redump_only.append((vendor, product, offsets[0], len(set(offsets)) > 1))
    ar_only = sorted(
        (accuraterip[k] for k in accuraterip.keys() - matched),
        key=lambda t: t[0].upper(),
    )
    redump_only.sort(key=lambda t: (t[0].upper(), t[1].upper()))

    ar_names = set(accuraterip)
    redump_names = {f"{v} {p}".upper() for v, p in redump}

    def near_accuraterip(product: str) -> str:
        """An AccurateRip key containing this product string, if any."""
        needle = product.upper()
        return next((k for k in ar_names if needle and needle in k), "")

    def near_redump(name: str) -> str:
        """A REDUMP key sharing a trailing token run with this name, if any."""
        tokens = name.upper().split()
        for i in range(1, len(tokens)):
            tail = " ".join(tokens[i:])
            if len(tail) < 4:
                break
            hit = next((k for k in redump_names if tail in k), "")
            if hit:
                return hit
        return ""

    header = (
        f"# Generated {datetime.date.today().isoformat()} by tools/offset_exclusives.py\n"
        f"# REDUMP source : {IXX} (upstream, tuple-keyed)\n"
        f"# AccurateRip   : {DB} ar_drives, duplicate ar_name resolved by HIGHEST\n"
        f"#                 submissions (the rule in drive_info.find_drive_offset)\n"
        f"# Vendor aliases: "
        + ", ".join(f"{k} -> {v}" for k, v in ALIASES.items())
        + "\n"
        "# NOT applied   : FREECOM_ -> FREECOM (trailing underscore), left visible\n"
    )

    OUT.mkdir(parents=True, exist_ok=True)

    path_redump = OUT / "offsets_redump_only.tsv"
    with path_redump.open("w", newline="") as f:
        f.write(header)
        f.write(f"# {len(redump_only)} drives in REDUMP and absent from AccurateRip\n")
        f.write(
            "# near_ar = an AccurateRip key containing this product string, if any\n"
        )
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["vendor", "product", "offset", "redump_self_contested", "near_ar"])
        for vendor, product, offset, contested in redump_only:
            w.writerow([
                vendor,
                product,
                f"{offset:+d}",
                "YES" if contested else "",
                near_accuraterip(product),
            ])

    path_ar = OUT / "offsets_accuraterip_only.tsv"
    with path_ar.open("w", newline="") as f:
        f.write(header)
        f.write(f"# {len(ar_only)} drives in AccurateRip and absent from REDUMP\n")
        f.write("# near_rd = a REDUMP key sharing a trailing token run, if any\n")
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(["ar_name", "offset", "submissions", "near_rd"])
        for name, offset, submissions in ar_only:
            w.writerow([name, f"{offset:+d}", submissions, near_redump(name)])

    print(f"wrote {path_redump}  ({len(redump_only)} rows)")
    print(f"wrote {path_ar}  ({len(ar_only)} rows)")


if __name__ == "__main__":
    main()
