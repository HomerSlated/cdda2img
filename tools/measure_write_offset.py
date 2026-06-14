#!/usr/bin/env python3
"""measure_write_offset.py — DEPRECATED: use `cdda2img setup --write-offset`."""

import sys

print(
    "measure_write_offset.py is deprecated.\n"
    "Use: uv run python -m cdda2img setup --write-offset",
    file=sys.stderr,
)
sys.exit(1)
