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
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
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
    import contextlib
    import importlib.resources

    with contextlib.suppress(Exception):
        ref = importlib.resources.files("cdda2img").joinpath(
            "../../conf/cdda2img.toml.example"
        )
        p = Path(str(ref))
        if p.is_file():
            return p
    return Path(__file__).parent.parent.parent / "conf" / "cdda2img.toml.example"


_DRIVE_KEY_RE = re.compile(r"^(name|read_offset|write_offset)\s*=")
_COMMENTED_DRIVE_LINE_RE = re.compile(
    r"^#\s*(\[\[drives\]\]|(name|read_offset|write_offset)\s*=)"
)


def _render_value(value: object) -> str:
    """Render a single Python value as a TOML literal.

    Handles the value types that appear in this config: bool (lowercase
    ``true``/``false``), int/float, str (double-quoted with escapes), and
    *arrays* of those (e.g. ``preferred_country``). Bool is checked before
    int because ``bool`` is a subclass of ``int``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_render_value(v) for v in value) + "]"
    return _toml_quote(str(value))


def _render_scalar(key: str, value: object) -> str:
    return f"{key} = {_render_value(value)}"


def _render_drive(drive: dict) -> str:
    lines = ["[[drives]]"]
    for k, v in drive.items():
        lines.append(_render_scalar(k, v))
    return "\n".join(lines)


def _overlay(example_text: str, user_data: dict) -> str:
    """Return *example_text* with user values substituted in place."""
    top_scalars = {k: v for k, v in user_data.items() if k != "drives"}
    drives = user_data.get("drives", [])

    out: list[str] = []
    lines = example_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped in ("[[drives]]", "# [[drives]]"):
            if out and out[-1].strip() == "#":
                out[-1] = ""
            j = i + 1
            while j < len(lines):
                s = lines[j].strip()
                if s == "[[drives]]" or _DRIVE_KEY_RE.match(s):
                    j += 1
                    continue
                if _COMMENTED_DRIVE_LINE_RE.match(s):
                    j += 1
                    continue
                break
            for d in drives:
                out.append(_render_drive(d))
                out.append("")
            if out and out[-1] == "":
                out.pop()
            i = j
            continue

        m = re.match(r"^\s*#?\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in top_scalars:
            out.append(_render_scalar(m.group(1), top_scalars[m.group(1)]))
        else:
            out.append(line)
        i += 1

    return "\n".join(out).rstrip() + "\n"


def update_config_from_template(path: Path | None = None) -> bool:
    """Overlay the user's config onto the bundled template and write it back.

    Makes a timestamped backup before writing.  Returns True on success.
    """
    dest = path or config_path()
    example = _example_path()
    if not example.is_file():
        log.warning("Template not found at %s — cannot update config", example)
        return False
    if not dest.is_file():
        log.warning("Config not found at %s — cannot update", dest)
        return False
    try:
        with open(dest, "rb") as f:
            user_data = tomllib.load(f)
        merged = _overlay(example.read_text(), user_data)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        bak = dest.with_suffix(f".toml.{stamp}.bak")
        shutil.copy(dest, bak)
        tmp = dest.with_suffix(".toml.tmp")
        tmp.write_text(merged)
        tmp.replace(dest)
    except Exception as exc:
        log.warning("Failed to update config from template: %s", exc)
        return False
    else:
        log.info("Config updated from template; backup at %s", bak)
        return True


def _load_raw() -> dict:
    path = config_path()
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
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
    auto: bool = False
    embedart: bool = False
    # AR-recovery: number of full sweeps of the drive's speed ladder to attempt per
    # failed track before giving up (total attempts = passes x ladder_steps).
    recovery_passes: int = 3
    # C2-erasure-assisted CTDB recovery gate (item 8). "off" (default) rips via cdrdao
    # read-cd as usual — CTDB error-only ctanalyse still repairs above cd-paranoia. "auto"
    # uses the drive's C2 error pointers as RS erasures when the drive advertises +
    # functionally supports C2; "on" forces it. C2 is a *modifier* to ctanalyse, not a
    # separate method, so it never disables recovery — only adds the erasure boost.
    # "auto"/"on" take the AccuDisc single-pass C2 read (audio + C2 + raw P-W sub;
    # metadata assembled by subq_toc, so no second cdrdao read-toc pass); the erasure
    # boost only helps discs too damaged for error-only ctanalyse. Default off is
    # conservative -- opt in for a troublesome disc, or for production testing.
    c2_recovery: str = "off"
    # Ordered priority ranking of release-country codes for the release-selection
    # rung (rbi_spec.md §6.3.2; trust_model_design.md §10.2). NOT a filter: listed
    # codes rank in order, unlisted codes share the lowest rank, empty = key skipped.
    preferred_country: list[str] = field(default_factory=list)
    # Recovery profile used when neither --profile nor any --ad-* flag is given
    # (accudisc-migration-plan.md §9.4, resolution rung 3). None falls through to
    # the built-in "track-ladder", the bench winner.
    default_profile: str | None = None


def _parse_preferred_country(raw: object) -> list[str]:
    """Parse the ``preferred_country`` TOML array into an ordered list of
    uppercased country codes (a priority ranking, not a filter). Non-string or
    empty entries are dropped and order is preserved; duplicates are collapsed.
    A non-list value degrades to an empty list with a warning.
    """
    if not isinstance(raw, list):
        if raw:
            log.warning(
                "Invalid preferred_country %r in config; expected a list of codes", raw
            )
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            code = item.strip().upper()
            if code not in out:
                out.append(code)
    return out


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


_C2_RECOVERY_VALID = {"auto", "on", "off"}


def _parse_c2_recovery(raw: object) -> str:
    value = str(raw).lower()
    if value not in _C2_RECOVERY_VALID:
        log.warning("Invalid c2_recovery %r in config; defaulting to 'auto'", raw)
        return "auto"
    return value


def _bounded_int(raw: object, default: int, lo: int, hi: int, name: str) -> int:
    """Parse *raw* as an int in [lo, hi]; warn and return *default* on bad/out-of-range."""
    try:
        val = int(raw)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        val = None  # type: ignore[assignment]
    if val is None or not lo <= val <= hi:
        log.warning("Invalid %s %r in config; defaulting to %d", name, raw, default)
        return default
    return val


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
    auto = bool(data.get("auto", False))
    embedart = bool(data.get("embedart", False))

    recovery_passes = _bounded_int(
        data.get("recovery_passes", 3), 3, 0, 20, "recovery_passes"
    )
    c2_recovery = _parse_c2_recovery(data.get("c2_recovery", "off"))

    preferred_country = _parse_preferred_country(data.get("preferred_country", []))

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

    raw_profile = data.get("default_profile")
    default_profile = str(raw_profile).strip() or None if raw_profile else None

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
        auto=auto,
        embedart=embedart,
        recovery_passes=recovery_passes,
        c2_recovery=c2_recovery,
        preferred_country=preferred_country,
        default_profile=default_profile,
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
