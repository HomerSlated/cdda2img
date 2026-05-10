"""
config.py — cdda2img user configuration.

Reads $XDG_CONFIG_HOME/cdda2img/cdda2img.toml
(default: ~/.config/cdda2img/cdda2img.toml).
When the file is absent and stdin is a TTY, the user is offered the option to
create it from the bundled example (conf/cdda2img.toml.example).
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]  # LINT-011

log = logging.getLogger(__name__)


def config_path() -> Path:
    """Return the path to the user config file."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "cdda2img" / "cdda2img.toml"


def _example_path() -> Path:
    # TODO: replace with importlib.resources when the package is properly installed
    return Path(__file__).parent.parent.parent / "conf" / "cdda2img.toml.example"


def _prompt_create_config(path: Path) -> bool:
    """Offer to create the config from the bundled example. Returns True if created."""
    if not sys.stdin.isatty():
        return False
    example = _example_path()
    if not example.exists():
        log.debug("Bundled example config not found at %s", example)
        return False
    print(f"  No config file found at {path}")
    try:
        answer = input("  Create from example? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if answer != "y":
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(example, path)
    print(f"  Created {path}")
    return True


def _load_raw() -> dict:
    path = config_path()
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        if _prompt_create_config(path):
            try:
                with open(path, "rb") as f:
                    return tomllib.load(f)
            except Exception as exc:
                log.warning("Failed to read config %s: %s", path, exc)
        return {}
    except Exception as exc:
        log.warning("Failed to read config %s: %s", path, exc)
        return {}


@dataclass
class DriveConfig:
    """Per-drive offset configuration stored in ``[[drives]]`` TOML blocks."""

    name: str
    offset: int


@dataclass
class Config:
    """Validated cdda2img configuration with typed fields and defaults."""

    drive_offset: int = 0
    cddb_server: str = "cddb.retrobridge.org:888"
    database_backups: int = 3
    database_backup_frequency: str = "1d"
    drives: list[DriveConfig] = field(default_factory=list)


def _parse_drives(raw_drives: object) -> list[DriveConfig]:
    """Parse a ``[[drives]]`` TOML value into a list of DriveConfig objects."""
    if not isinstance(raw_drives, list):
        return []
    result: list[DriveConfig] = []
    for entry in raw_drives:
        if not isinstance(entry, dict):
            continue
        # cast: isinstance narrows Unknown → dict[Never,Never]; give ty concrete types
        d = cast("dict[str, object]", entry)
        name = d.get("name")
        if not name or not isinstance(name, str):
            log.warning("Skipping drive entry with missing/invalid name: %r", entry)
            continue
        offset_raw = d.get("offset")
        try:
            result.append(DriveConfig(name=name, offset=int(offset_raw)))
        except (ValueError, TypeError):
            log.warning("Skipping drive %r: invalid offset %r", name, offset_raw)
    return result


def load_config() -> Config:
    """Load and return the user configuration from the TOML file."""
    data = _load_raw()

    raw = data.get("drive_offset", 0)
    try:
        offset = int(raw)
    except (ValueError, TypeError):
        log.warning("Invalid drive_offset %r in config; defaulting to 0", raw)
        offset = 0

    cddb_server = str(data.get("cddb_server", "cddb.retrobridge.org:888"))

    raw_backups = data.get("database_backups", 3)
    try:
        database_backups = max(0, int(raw_backups))
    except (ValueError, TypeError):
        log.warning(
            "Invalid database_backups %r in config; defaulting to 3", raw_backups
        )
        database_backups = 3

    database_backup_frequency = str(data.get("database_backup_frequency", "1d"))
    drives = _parse_drives(data.get("drives", []))

    return Config(
        drive_offset=offset,
        cddb_server=cddb_server,
        database_backups=database_backups,
        database_backup_frequency=database_backup_frequency,
        drives=drives,
    )


# ---------------------------------------------------------------------------
# Config write helpers
# ---------------------------------------------------------------------------


def _toml_quote(s: str) -> str:
    """Return *s* as a TOML basic-string literal (double-quoted, escapes applied)."""
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _rewrite_config_drives(text: str, drives: list[DriveConfig]) -> str:
    """Return *text* with all ``[[drives]]`` blocks replaced by *drives*.

    Existing ``[[drives]]`` entries (and all key-value lines inside them) are
    stripped.  The new entries are appended at the end of the file.  All other
    content is preserved verbatim.
    """
    lines_out: list[str] = []
    in_drives_block = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped == "[[drives]]":
            in_drives_block = True
            continue
        if in_drives_block:
            if stripped.startswith("["):
                in_drives_block = False
                lines_out.append(line)
            # else: skip — line belongs to a drives block being replaced
        else:
            lines_out.append(line)

    result = "".join(lines_out).rstrip("\n")
    if result:
        result += "\n"

    for drive in drives:
        result += "\n[[drives]]\n"
        result += f"name = {_toml_quote(drive.name)}\n"
        result += f"offset = {drive.offset}\n"

    return result


def save_drive(drive: DriveConfig, path: Path | None = None) -> None:
    """Upsert *drive* into the ``[[drives]]`` section of the config file.

    If a ``[[drives]]`` entry with the same name already exists it is replaced;
    otherwise the new entry is appended.  Writes are atomic (temp + rename).
    """
    cfg_path = path or config_path()

    try:
        text = cfg_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""

    try:
        data = tomllib.loads(text)
    except Exception:
        data = {}

    existing = _parse_drives(data.get("drives", []))
    updated = [d for d in existing if d.name != drive.name]
    updated.append(drive)

    new_text = _rewrite_config_drives(text, updated)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(cfg_path)
