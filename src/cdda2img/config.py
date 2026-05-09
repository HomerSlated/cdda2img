"""
config.py — cdda2img user configuration.

Reads $XDG_CONFIG_HOME/cdda2img/cdda2img.toml
(default: ~/.config/cdda2img/cdda2img.toml).
A missing file is silently ignored; all settings use built-in defaults.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]  # LINT-011

log = logging.getLogger(__name__)


def config_path() -> Path:
    """Return the path to the user config file."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "cdda2img" / "cdda2img.toml"


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
class Config:
    """Validated cdda2img configuration with typed fields and defaults."""

    drive_offset: int = 0
    cddb_server: str = "cddb.retrobridge.org:888"


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

    return Config(drive_offset=offset, cddb_server=cddb_server)
