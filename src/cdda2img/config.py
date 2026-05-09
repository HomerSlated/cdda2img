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
