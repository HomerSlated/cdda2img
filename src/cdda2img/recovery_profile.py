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

**`--ad-speed` is the one exception, and it is not an inconsistency** (kgr,
2026-08-09). Every other `--ad-*` flag describes *recovery* — how to re-read a sector
that failed — which is exactly what a profile describes, so the two genuinely compete
and one of them has to win outright. `--ad-speed` sets the rate of the **initial
pass**, which no profile has ever had an opinion about: `Profile.speed` is a selector
over the drive's admitted ladder, consumed only by `rungs_for` under
`ladder="single"`, and it never reaches the whole-disc read. So there is nothing for
`--ad-speed` to conflict with, and making it exclusive had a perverse effect —
`cdda2img rip --ad-speed 8` silently disabled AccurateRip recovery, because rung 1
returns no profile and the ladder is derived from one. It is carried on
:class:`ResolvedStrategy` as ``read_speed`` on **every** rung.

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
    """The immutable profiles that ship with the package.

    They live *inside* the package (``cdda2img/profiles/``) rather than beside it in
    a top-level ``conf/``, because only the former is addressable without escaping
    the package root. The previous arrangement did escape it — ``files("cdda2img")``
    joined with ``"../../conf/profiles"`` — and so resolved in a source checkout and
    nowhere else: the wheel ships ``src/cdda2img`` alone, and two levels up from an
    installed package is site-packages, which has no ``conf/``. Every install was
    therefore missing all seven profiles, and since rung 4 of :func:`resolve_recovery`
    loads ``track-ladder`` unconditionally, `rip` failed outright with "unknown
    recovery profile" rather than degrading.

    Both branches are kept. ``importlib.resources`` is correct for a zipped or
    otherwise non-filesystem distribution; the ``__file__`` fallback covers the case
    where the traversable cannot be materialised as a real path. Neither uses ``..``
    now, so a wrong answer is no longer reachable by construction.
    """
    import contextlib
    import importlib.resources

    with contextlib.suppress(Exception):
        ref = importlib.resources.files("cdda2img").joinpath("profiles")
        p = Path(str(ref))
        if p.is_dir():
            return p
    return Path(__file__).parent / "profiles"


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
    #: Initial-pass read speed in X from ``--ad-speed``; ``None`` leaves the drive
    #: at its own management. Orthogonal to *profile* and *ladder* — see the module
    #: docstring on why this one flag composes rather than excluding.
    read_speed: int | None = None

    def with_ladder(self, ladder: list[int]) -> ResolvedStrategy:
        return ResolvedStrategy(
            self.source,
            self.profile,
            self.ad_flags,
            tuple(ladder),
            self.read_speed,
        )


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

    ``speed`` is lifted out before the rung-1 test and carried on every result — it
    describes the initial pass, which no profile speaks for. See the module docstring.
    """
    supplied = {k: v for k, v in (ad_flags or {}).items() if v is not None}
    unknown = sorted(set(supplied) - set(AD_FLAGS))
    if unknown:
        msg = f"unknown --ad-* flag(s): {', '.join(unknown)}"
        raise ProfileError(msg)

    # Validated as an --ad-* flag, then removed from the set that decides the rung.
    # Order matters: taking it out before the unknown-key check above would make
    # `--ad-speed` the one flag nobody validates.
    read_speed = _read_speed_from(supplied.pop("speed", None))

    if supplied:
        log.info(
            "recovery: AccuDisc flags supplied (%s) — profiles are bypassed entirely",
            ", ".join(sorted(supplied)),
        )
        return ResolvedStrategy("ad-flags", ad_flags=supplied, read_speed=read_speed)

    if profile_name:
        return ResolvedStrategy(
            "profile",
            profile=load_profile(profile_name, profiles=profiles),
            read_speed=read_speed,
        )
    if config_default:
        return ResolvedStrategy(
            "config-default",
            profile=load_profile(config_default, profiles=profiles),
            read_speed=read_speed,
        )
    return ResolvedStrategy(
        "builtin",
        profile=load_profile(BUILTIN_PROFILE, profiles=profiles),
        read_speed=read_speed,
    )


#: Upper bound on ``--ad-speed``. AccuDisc's ``read_req.speed_x`` is a ``uint16_t``,
#: so the field itself would take far more; the bound is on what can be a real CD
#: rate. 72x is above every drive ever shipped (the fastest CD-ROM mechanisms topped
#: out at 56x), so this rejects typos and unit confusion — someone passing kB/s, or
#: a shell expansion gone wrong — without ever rejecting a drive.
_MAX_SPEED_X = 72


def _read_speed_from(value: object) -> int | None:
    """Validate ``--ad-speed`` into an ``int`` in ``[1, 72]``, or ``None``.

    Rejects rather than clamps. A clamp here would be the ``--ad-retries 256`` defect
    wearing different clothes: that value parses as a fine integer, is silently cut to
    a ``uint8_t``, and arrives at the drive as 2 — the opposite of what was asked, with
    the rip carrying on and every measurement taken under it mislabelled. When a
    number cannot be honoured the only safe answer is to say so before the drive spins.

    ``0`` is refused too, even though the library reads it as "fastest possible":
    ``--ad-speed 0`` from a human is a mistake, not a request for maximum, and the way
    to ask for maximum is to not pass the flag.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"--ad-speed must be an integer, got {value!r}"
        raise ProfileError(msg)
    if not (1 <= value <= _MAX_SPEED_X):
        msg = f"--ad-speed must be between 1 and {_MAX_SPEED_X}X, got {value}"
        raise ProfileError(msg)
    return value


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


def parse_ad_ladder(spec: object) -> list[int]:
    """``--ad-ladder "32,16,8"`` -> ``[32, 16, 8]``, in the order given.

    Order is preserved rather than sorted: the ladder is walked rung-by-rung per
    attempt, so ``"4,32"`` (start slow, then try fast) is a different experiment from
    ``"32,4"`` and a caller who wrote one must not silently get the other.

    Every rung is bounded exactly as ``--ad-speed`` is, and for the same reason — a
    ladder is a list of speed requests. Duplicates are kept: repeating a rung is how
    you spend more attempts at it, which is a real thing to want from a flag whose
    whole purpose is direct control.
    """
    if not isinstance(spec, str):
        msg = f"--ad-ladder must be a comma-separated list of speeds, got {spec!r}"
        raise ProfileError(msg)
    rungs: list[int] = []
    for token in spec.split(","):
        text = token.strip()
        if not text:
            msg = f"--ad-ladder has an empty rung: {spec!r}"
            raise ProfileError(msg)
        try:
            value = int(text)
        except ValueError:
            msg = f"--ad-ladder rung {text!r} is not an integer"
            raise ProfileError(msg) from None
        if not (1 <= value <= _MAX_SPEED_X):
            msg = f"--ad-ladder rung {value} is outside 1..{_MAX_SPEED_X}X"
            raise ProfileError(msg)
        rungs.append(value)
    if not rungs:
        msg = "--ad-ladder is empty"
        raise ProfileError(msg)
    return rungs


def bind_ladder(strategy: ResolvedStrategy, device: str) -> ResolvedStrategy:
    """Attach the speed ladder to *strategy*: explicit rungs, or the drive's admitted.

    Separate from :func:`resolve_recovery` so that resolution stays pure and testable
    without hardware. The ladder is a property of drive **and** disc — a governor caps
    a degraded disc regardless of drive capability — so this must be called per rip,
    never cached across discs.

    **The ad-flags rung gets a ladder too**, which it did not until 2026-08-09. Before
    that, this function returned early whenever ``profile is None`` — true by
    construction on that rung — so ``strategy.ladder`` stayed empty, the caller read
    that as "no recovery", and ``--ad-ladder 32,16,8`` produced *no ladder at all*.
    A flag whose only purpose is to name a ladder, turning the ladder off. The rungs
    are taken verbatim and are NOT filtered against ``admitted_ladder``: this rung is
    the escape hatch for driving the engine directly, and a user asking for a speed
    our admission policy would reject is exactly the case the hatch exists for.
    """
    from cdda2img import drive_speed

    if strategy.profile is None:
        explicit = strategy.ad_flags.get("ladder")
        if explicit is None:
            return strategy
        rungs = parse_ad_ladder(explicit)
        log.info("recovery: --ad-ladder %s -> rungs %s (not filtered)", explicit, rungs)
        return strategy.with_ladder(rungs)

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
