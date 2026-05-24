"""
import_eac_offsets.py — one-shot import of an EAC OffsetBase XML file into
the local drive_offsets.db (eac_drives table).

Usage:
    uv run python tools/import_eac_offsets.py <xml_path> [--conflicts-out <path>]

Conflicting entries are NOT imported; they are written to --conflicts-out
(default: private/research/incoming/offsets_check.xml) for manual review.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# Ensure the package is importable when run from the project root.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cdda2img import db
from cdda2img.config import load_config
from cdda2img.drive_info import import_eac_drives_xml


def _write_conflicts_xml(conflicts: list[dict[str, str | None]], path: Path) -> None:
    root = ET.Element(
        "offsetbase_conflicts",
        attrib={"source": "import_eac_offsets", "count": str(len(conflicts))},
    )
    for entry in conflicts:
        drive_el = ET.SubElement(root, "drive")
        for tag, value in entry.items():
            child = ET.SubElement(drive_el, tag)
            child.text = value or ""
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(path, encoding="utf-8", xml_declaration=True)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import EAC OffsetBase XML into drive_offsets.db"
    )
    parser.add_argument(
        "xml", type=Path, metavar="XML_FILE", help="offsetbase_combined.xml path"
    )
    parser.add_argument(
        "--conflicts-out",
        type=Path,
        default=Path("private/research/incoming/offsets_check.xml"),
        metavar="PATH",
        help="destination for conflicting entries (default: %(default)s)",
    )
    args = parser.parse_args()

    if not args.xml.exists():
        sys.exit(f"error: XML file not found: {args.xml}")

    cfg = load_config()
    conn = db.open_drive_offsets_db(cfg)
    try:
        result, conflict_entries = import_eac_drives_xml(conn, args.xml)
    finally:
        conn.close()

    print(
        f"Inserted: {result.inserted}  "
        f"Upgraded: {result.upgraded}  "
        f"Skipped: {result.skipped}  "
        f"Conflicts: {result.conflicts}"
    )

    if conflict_entries:
        _write_conflicts_xml(conflict_entries, args.conflicts_out)
        print(f"Conflicting entries written to: {args.conflicts_out}")
    else:
        print("No conflicts.")


if __name__ == "__main__":
    main()
