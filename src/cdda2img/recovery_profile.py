"""Recovery profiles: load, validate, resolve, and bind to a drive.

A *profile* is a complete recovery specification — what to capture, what to re-read
and how, and which adjuncts to allow — named after the strategy the bench actually
measured (accudisc-migration-plan.md §9.1/§9.2). The seven shipped profiles are the
seven arms of that bench; their success rates live in each file's header comment.

Resolution has exactly four rungs (§9.4), in priority order:

    1. any --ad-* flag present  -> honour AccuDisc's flags ONLY, no profile, no merge
    2. --profile NAME           -> load and validate NAME
    3. cfg.default_profile      -> load and validate that
    4. nothing                  -> the built-in "track-ladder"

Rung 1 is deliberately exclusive rather than a merge. `--ad-*` is an escape hatch for
driving AccuDisc directly; silently blending it with a profile would produce a
configuration neither the user nor the profile author asked for, and PROV would have
no honest way to name it.

Rung 4 is a profile, not bare flags. The original plan assumed AccuDisc's flags carry
no defaults, so "no profile" could safely mean "pass nothing" — wrong for `--retries`,
which defaults to 2. Bare flags are therefore AccuDisc's R0 rung, a real floor but well
below track-ladder's measured 19/20, so a rip with no profile still gets our best
measured strategy rather than falling through to the floor.

:class:`ResolvedStrategy` is the only object the rip path sees, and it records which
rung produced it so PROV can say so (`recovery_source`).
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cdda2img.validation import PROFILE_SCHEMA, Error, apply_defaults, validate

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - 3.10 fallback
    import tomli as tomllib

log = logging.getLogger(__name__)

BUILTIN_PROFILE = "track-ladder"


class ProfileError(Exception):
    """A profile could not be found, parsed, or validated."""


@dataclass(frozen=True)
class Profile:
    """One validated recovery profile (§9.2 schema)."""

    name: str
    experimental: bool = False
    sub: bool = True
    c2: bool = True
    granularity: str = "track"
    ladder: str = "full"
    speed: str | float = "max"
    passes: int = 3
    run_up: int = 0
    span: int = 0
    variation: str = "none"
    ctdb: str = "auto"
    verify: bool = False
    budget_s: int = 300

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        """Build from a *validated* dict, filling schema defaults for absent keys."""
        return cls(**apply_defaults(data, PROFILE_SCHEMA))


def shipped_profiles_dir() -> Path:
    """The immutable profiles that ship with the package."""
    import contextlib
    import importlib.resources

    with contextlib.suppress(Exception):
        ref = importlib.resources.files("cdda2img").joinpath("../../conf/profiles")
        p = Path(str(ref))
        if p.is_dir():
            return p
    return Path(__file__).parent.parent.parent / "conf" / "profiles"


def user_profiles_dir() -> Path:
    """Where `setup` writes user-created profiles."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "cdda2img" / "profiles"


def list_profiles() -> dict[str, Path]:
    """Every resolvable profile name -> its file.

    User profiles shadow shipped ones of the same name, but `setup` refuses to create
    such a name in the first place (§9.7), so this is a safety net rather than a
    feature: a user who plants the file by hand gets their version, not a silent
    ignore.
    """
    found: dict[str, Path] = {}
    for directory in (shipped_profiles_dir(), user_profiles_dir()):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.toml")):
            found[path.stem] = path
    return found


def _describe(errors: list[Error]) -> str:
    return "; ".join(str(e) for e in errors)


def load_profile(name: str, *, profiles: dict[str, Path] | None = None) -> Profile:
    """Load, parse and two-stage-validate *name*. Raises :class:`ProfileError`.

    Never falls back to a default: a user who asked for a specific profile and got a
    silently different one has no way to notice, and every recovery measurement taken
    afterwards would be mislabelled.
    """
    available = list_profiles() if profiles is None else profiles
    path = available.get(name)
    if path is None:
        known = ", ".join(sorted(available)) or "none found"
        msg = f"unknown recovery profile {name!r}; available: {known}"
        raise ProfileError(msg)

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"could not read profile {name!r} ({path}): {exc}"
        raise ProfileError(msg) from exc

    errors = validate(data, PROFILE_SCHEMA)
    if errors:
        msg = f"profile {name!r} ({path}) is invalid: {_describe(errors)}"
        raise ProfileError(msg)

    profile = Profile.from_dict(data)
    if profile.name != name:
        msg = (
            f"profile {name!r} ({path}) declares name={profile.name!r}; "
            "the filename and the name field must agree"
        )
        raise ProfileError(msg)
    return profile


# --------------------------------------------------------------------------
# Resolution (§9.4)
# --------------------------------------------------------------------------

AD_FLAGS = ("speed", "retries", "c2", "recovery", "ladder", "verify", "overlap")


@dataclass(frozen=True)
class ResolvedStrategy:
    """What the rip path acts on. *source* is written to PROV as `recovery_source`."""

    source: str  # "ad-flags" | "profile" | "config-default" | "builtin"
    profile: Profile | None = None
    ad_flags: dict[str, object] = field(default_factory=dict)
    ladder: tuple[int, ...] = ()

    def with_ladder(self, ladder: list[int]) -> ResolvedStrategy:
        return ResolvedStrategy(self.source, self.profile, self.ad_flags, tuple(ladder))


def resolve_recovery(
    ad_flags: dict[str, object] | None = None,
    profile_name: str | None = None,
    config_default: str | None = None,
    *,
    profiles: dict[str, Path] | None = None,
) -> ResolvedStrategy:
    """The four-rung resolution. Pure: no device, no I/O beyond reading profile files.

    *ad_flags* holds only the `--ad-*` values the user actually supplied (absent flags
    must not appear, or rung 1 fires on every invocation).
    """
    supplied = {k: v for k, v in (ad_flags or {}).items() if v is not None}
    if supplied:
        unknown = sorted(set(supplied) - set(AD_FLAGS))
        if unknown:
            msg = f"unknown --ad-* flag(s): {', '.join(unknown)}"
            raise ProfileError(msg)
        log.info(
            "recovery: AccuDisc flags supplied (%s) — profiles are bypassed entirely",
            ", ".join(sorted(supplied)),
        )
        return ResolvedStrategy("ad-flags", ad_flags=supplied)

    if profile_name:
        return ResolvedStrategy(
            "profile", profile=load_profile(profile_name, profiles=profiles)
        )
    if config_default:
        return ResolvedStrategy(
            "config-default", profile=load_profile(config_default, profiles=profiles)
        )
    return ResolvedStrategy(
        "builtin", profile=load_profile(BUILTIN_PROFILE, profiles=profiles)
    )


# --------------------------------------------------------------------------
# Binding to a drive (§9.3)
# --------------------------------------------------------------------------


def rungs_for(profile: Profile, admitted: list[int]) -> list[int]:
    """Resolve the profile's ladder policy against the drive's admitted rungs.

    ``full`` takes the whole admitted list fastest->slowest; ``single`` picks the
    admitted rung nearest the selector. ``variation="full"`` also needs the whole
    list, because the sweep chooses a rung per attempt at random.
    """
    if not admitted:
        return []
    if profile.ladder == "full" or profile.variation == "full":
        return sorted(admitted, reverse=True)

    top = max(admitted)
    speed = profile.speed
    if speed == "max":
        target = float(top)
    elif speed == "min":
        target = float(min(admitted))
    elif speed == "mid":
        target = (top + min(admitted)) / 2
    else:
        target = float(speed) * top
    return [min(admitted, key=lambda r: (abs(r - target), -r))]


def bind_ladder(strategy: ResolvedStrategy, device: str) -> ResolvedStrategy:
    """Attach the drive's admitted speed ladder to *strategy*.

    Separate from :func:`resolve_recovery` so that resolution stays pure and testable
    without hardware. The ladder is a property of drive **and** disc — a governor caps
    a degraded disc regardless of drive capability — so this must be called per rip,
    never cached across discs.
    """
    from cdda2img import drive_speed

    if strategy.profile is None:
        return strategy
    admitted = drive_speed.admitted_ladder(device)
    rungs = rungs_for(strategy.profile, admitted)
    log.info(
        "recovery: profile %s (%s), drive admitted %s -> rungs %s",
        strategy.profile.name,
        strategy.source,
        admitted,
        rungs,
    )
    return strategy.with_ladder(rungs)
