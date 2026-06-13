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

    cddb_server: str = "gnudb.gnudb.org:8880"
    contact_email: str = ""
    database_backups: int = 3
    database_backup_frequency: str = "4w"
    catalogue_backups: int = 3
    catalogue_backup_frequency: str = "1d"
    drives: list[DriveConfig] = field(default_factory=list)
    catalogue_path: Path | None = None
    enable_catalogue: bool = True
    duplicate_catalogue_entry: str = "ask"
    default_device: str = "/dev/sr0"
    silence_threshold: int = 55
    capacity: int = 80
    preview: bool = True
    tui: bool = True
    low_dr_threshold: float = 5.0  # album LRA (LU) below which low_dynamic_range=YES
    # R10: when True, every remote metadata lookup (CDDB / MB / Discogs /
    # AcoustID / AccurateRip) short-circuits to "unavailable" semantics.
    # Combined with R7's SQLite cache, lets a re-run reproduce a prior
    # rip's metadata without network access.
    no_network_services: bool = False


_no_network_override: bool | None = None


def is_no_network_active() -> bool:
    """R10: True iff offline mode is active.

    Honours, in order:
      1. The process-wide override set by ``set_no_network_override``
         (used by the CLI flag and by tests).
      2. The ``no_network_services`` key from the loaded TOML config.

    Module-level cache is intentionally absent — config load is cheap
    and a cache would otherwise race with the CLI-set override.
    """
    if _no_network_override is not None:
        return _no_network_override
    try:
        return load_config().no_network_services
    except Exception:
        return False


def set_no_network_override(value: bool | None) -> None:
    """Set or clear the process-wide R10 override (None = use config value)."""
    global _no_network_override
    _no_network_override = value


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


_DUP_POLICY_VALID = {"ask", "skip", "replace", "add"}


def _parse_dup_policy(raw: object) -> str:
    value = str(raw).lower()
    if value not in _DUP_POLICY_VALID:
        log.warning(
            "Invalid duplicate_catalogue_entry %r in config; defaulting to 'ask'", raw
        )
        return "ask"
    return value


def load_config() -> Config:
    """Load and return the user configuration from the TOML file."""
    data = _load_raw()

    cddb_server = str(data.get("cddb_server", "gnudb.gnudb.org:8880"))
    contact_email = str(data.get("contact_email", ""))

    raw_backups = data.get("database_backups", 3)
    try:
        database_backups = max(0, int(raw_backups))
    except (ValueError, TypeError):
        log.warning(
            "Invalid database_backups %r in config; defaulting to 3", raw_backups
        )
        database_backups = 3

    database_backup_frequency = str(data.get("database_backup_frequency", "4w"))

    raw_cat_backups = data.get("catalogue_backups", 3)
    try:
        catalogue_backups = max(0, int(raw_cat_backups))
    except (ValueError, TypeError):
        log.warning(
            "Invalid catalogue_backups %r in config; defaulting to 3", raw_cat_backups
        )
        catalogue_backups = 3

    catalogue_backup_frequency = str(data.get("catalogue_backup_frequency", "1d"))
    drives = _parse_drives(data.get("drives", []))

    raw_catalogue_path = data.get("catalogue_path")
    catalogue_path = (
        Path(str(raw_catalogue_path)) if raw_catalogue_path is not None else None
    )

    enable_catalogue = bool(data.get("enable_catalogue", True))
    duplicate_catalogue_entry = _parse_dup_policy(
        data.get("duplicate_catalogue_entry", "ask")
    )
    default_device = str(data.get("default_device", "/dev/sr0"))

    if "silence" in data and "silence_threshold" not in data:
        log.warning(
            "Config key 'silence' was renamed to 'silence_threshold' — "
            "the old key is ignored. Update %s to silence the warning.",
            config_path(),
        )
    raw_silence_threshold = data.get("silence_threshold", 55)
    try:
        silence_threshold = int(raw_silence_threshold)
    except (ValueError, TypeError):
        silence_threshold = 0
    if not 1 <= silence_threshold <= 90:
        log.warning(
            "Invalid silence_threshold %r in config; defaulting to 55",
            raw_silence_threshold,
        )
        silence_threshold = 55

    raw_capacity = data.get("capacity", 80)
    try:
        capacity = int(raw_capacity)
    except (ValueError, TypeError):
        capacity = 0
    if not 1 <= capacity <= 99:
        log.warning("Invalid capacity %r in config; defaulting to 80", raw_capacity)
        capacity = 80

    preview = bool(data.get("preview", True))
    tui = bool(data.get("tui", True))
    # R10: offline mode toggle.
    no_network_services = bool(data.get("no_network_services", False))

    raw_low_dr = data.get("low_dr_threshold", 5.0)
    try:
        low_dr_threshold = float(raw_low_dr)
    except (ValueError, TypeError):
        low_dr_threshold = 0.0
    if not 0.5 <= low_dr_threshold <= 20.0:
        log.warning(
            "Invalid low_dr_threshold %r in config; defaulting to 5.0", raw_low_dr
        )
        low_dr_threshold = 5.0

    return Config(
        cddb_server=cddb_server,
        contact_email=contact_email,
        database_backups=database_backups,
        database_backup_frequency=database_backup_frequency,
        catalogue_backups=catalogue_backups,
        catalogue_backup_frequency=catalogue_backup_frequency,
        drives=drives,
        catalogue_path=catalogue_path,
        enable_catalogue=enable_catalogue,
        duplicate_catalogue_entry=duplicate_catalogue_entry,
        default_device=default_device,
        silence_threshold=silence_threshold,
        capacity=capacity,
        preview=preview,
        tui=tui,
        low_dr_threshold=low_dr_threshold,
        no_network_services=no_network_services,
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
