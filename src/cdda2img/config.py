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
    read_offset: int
    write_offset: int | None = None


@dataclass
class Config:
    """Validated cdda2img configuration with typed fields and defaults."""

    cddb_server: str = "cddb.retrobridge.org:888"
    contact_email: str = ""
    database_backups: int = 3
    database_backup_frequency: str = "1d"
    drives: list[DriveConfig] = field(default_factory=list)
    catalogue_path: Path | None = None
    enable_catalogue: bool = True


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
        read_offset_raw = d.get("read_offset")
        try:
            read_offset = int(read_offset_raw)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            log.warning(
                "Skipping drive %r: invalid read_offset %r", name, read_offset_raw
            )
            continue
        write_offset: int | None = None
        write_offset_raw = d.get("write_offset")
        if write_offset_raw is not None:
            try:
                write_offset = int(write_offset_raw)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                log.warning(
                    "Drive %r: invalid write_offset %r; ignoring",
                    name,
                    write_offset_raw,
                )
        result.append(
            DriveConfig(name=name, read_offset=read_offset, write_offset=write_offset)
        )
    return result


def load_config() -> Config:
    """Load and return the user configuration from the TOML file."""
    data = _load_raw()

    cddb_server = str(data.get("cddb_server", "cddb.retrobridge.org:888"))
    contact_email = str(data.get("contact_email", ""))

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

    raw_catalogue_path = data.get("catalogue_path")
    catalogue_path = (
        Path(str(raw_catalogue_path)) if raw_catalogue_path is not None else None
    )

    enable_catalogue = bool(data.get("enable_catalogue", True))

    return Config(
        cddb_server=cddb_server,
        contact_email=contact_email,
        database_backups=database_backups,
        database_backup_frequency=database_backup_frequency,
        drives=drives,
        catalogue_path=catalogue_path,
        enable_catalogue=enable_catalogue,
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
        result += f"read_offset = {drive.read_offset}\n"
        if drive.write_offset is not None:
            result += f"write_offset = {drive.write_offset}\n"

    return result


def _read_config_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _parse_config_text(text: str) -> dict:
    try:
        return tomllib.loads(text)
    except Exception:
        return {}


def _write_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def save_drive_read_offset(
    name: str, read_offset: int, path: Path | None = None
) -> None:
    """Upsert ``read_offset`` for drive *name*, preserving any existing ``write_offset``."""
    cfg_path = path or config_path()
    text = _read_config_text(cfg_path)
    existing = _parse_drives(_parse_config_text(text).get("drives", []))
    old = next((d for d in existing if d.name == name), None)
    drive = DriveConfig(
        name=name,
        read_offset=read_offset,
        write_offset=old.write_offset if old else None,
    )
    updated = [d for d in existing if d.name != name]
    updated.append(drive)
    _write_config(cfg_path, _rewrite_config_drives(text, updated))


def save_drive_write_offset(
    name: str, write_offset: int, path: Path | None = None
) -> None:
    """Upsert ``write_offset`` for drive *name*, preserving any existing ``read_offset``."""
    cfg_path = path or config_path()
    text = _read_config_text(cfg_path)
    existing = _parse_drives(_parse_config_text(text).get("drives", []))
    old = next((d for d in existing if d.name == name), None)
    drive = DriveConfig(
        name=name,
        read_offset=old.read_offset if old else 0,
        write_offset=write_offset,
    )
    updated = [d for d in existing if d.name != name]
    updated.append(drive)
    _write_config(cfg_path, _rewrite_config_drives(text, updated))


def save_drive(drive: DriveConfig, path: Path | None = None) -> None:
    """Upsert *drive* into the ``[[drives]]`` section of the config file.

    Replaces any existing entry with the same name; preserves all other content.
    Writes are atomic (temp + rename).
    """
    cfg_path = path or config_path()
    text = _read_config_text(cfg_path)
    existing = _parse_drives(_parse_config_text(text).get("drives", []))
    updated = [d for d in existing if d.name != drive.name]
    updated.append(drive)
    _write_config(cfg_path, _rewrite_config_drives(text, updated))
