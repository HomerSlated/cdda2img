"""Two-stage declarative validator for config files and recovery profiles.

Both consumers describe themselves as a :class:`Schema` — a table of
:class:`FieldSpec` plus an ordered list of :class:`SanityRule` — and share one
engine, so adding a field is a table edit rather than a code change.

The two stages are deliberately distinct (accudisc-migration-plan.md §9.5):

* **Stage 1, spec** — *structural*. Is the key known? Is the value the right type
  and shape? Is it in the enum? This is the stage that can be answered without
  understanding what the field means.
* **Stage 2, sanity** — *semantic*. Is the value, though well-formed, legal? A
  field can pass every structural check and still hold a value that cannot work.

The canonical example is AccuDisc's ``--retries 256``: it parses as a perfectly
good integer (stage 1 passes), and AccuDisc casts it through ``uint8_t``
unguarded, so it silently becomes ``0`` and then defaults back to ``2`` — the
opposite of the maximum effort the user asked for. Only a range check catches
that, and a range check is not a type check.

Stage 2 runs only on stage-1-valid data, so a rule may assume its fields exist
with the right type and never has to defend against ``None`` or a stray string.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

__all__ = [
    "CONFIG_SCHEMA",
    "PROFILE_SCHEMA",
    "Error",
    "FieldSpec",
    "SanityRule",
    "Schema",
    "apply_defaults",
    "validate",
    "validate_sanity",
    "validate_spec",
]


@dataclass(frozen=True)
class Error:
    """One validation failure. *where* is a dotted path so nested list-of-table
    entries report as e.g. ``drives[1].read_offset`` rather than just ``drives``."""

    where: str
    message: str
    stage: str  # "spec" | "sanity"

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


@dataclass(frozen=True)
class FieldSpec:
    """Structural description of one key.

    *types* is a tuple of accepted Python types. ``bool`` is never accepted for an
    ``int`` field even though Python makes it a subclass — a TOML ``true`` where a
    count belongs is a mistake, not a 1.
    """

    types: tuple[type, ...]
    required: bool = False
    default: Any = None
    enum: frozenset[str] | None = None
    pattern: Callable[[str], bool] | None = None
    pattern_desc: str = ""
    item_types: tuple[type, ...] | None = None  # element type for list fields
    item_schema: Schema | None = None  # element schema for list-of-table fields


@dataclass(frozen=True)
class SanityRule:
    """A semantic predicate over stage-1-valid data.

    *check* returns True when the data is acceptable. It may read any field in the
    schema; *where* only decides which key the error is filed under.
    """

    where: str
    describe: str
    check: Callable[[Mapping[str, Any]], bool]


@dataclass(frozen=True)
class Schema:
    name: str
    fields: Mapping[str, FieldSpec]
    rules: Sequence[SanityRule] = field(default_factory=tuple)


def _type_name(types: tuple[type, ...]) -> str:
    return " or ".join(t.__name__ for t in types)


def _type_ok(value: object, types: tuple[type, ...]) -> bool:
    """isinstance, minus Python's bool-is-an-int wart.

    ``int`` accepts a bool only when ``bool`` is itself in *types*; ``float``
    additionally accepts a genuine ``int`` (TOML writes ``5``, the field wants 5.0).
    """
    if isinstance(value, bool):
        return bool in types
    if isinstance(value, int) and float in types:
        return True
    return isinstance(value, types)


def _in_enum(value: object, enum: frozenset[str]) -> bool:
    """Enum membership, case-insensitively.

    Case folding is *normalisation*, not repair: a closed set of lowercase members
    cannot collide, so ``"OFF"`` unambiguously means ``off`` and accepting it costs
    nothing. This is deliberately unlike profile names (§9.7), where silent
    lowercasing is forbidden precisely because two distinct names could then map to
    one file. :func:`apply_defaults` folds the value to its canonical member, so
    nothing downstream ever sees the user's casing.
    """
    if isinstance(value, str):
        return value.lower() in enum
    return value in enum


def _check_value(where: str, value: object, spec: FieldSpec) -> list[Error]:
    if not _type_ok(value, spec.types):
        return [
            Error(
                where,
                f"expected {_type_name(spec.types)}, got "
                f"{type(value).__name__} ({value!r})",
                "spec",
            )
        ]

    errors: list[Error] = []
    if spec.enum is not None and not _in_enum(value, spec.enum):
        allowed = ", ".join(sorted(spec.enum))
        errors.append(Error(where, f"{value!r} is not one of: {allowed}", "spec"))
    if spec.pattern is not None and isinstance(value, str) and not spec.pattern(value):
        errors.append(Error(where, f"{value!r} {spec.pattern_desc}", "spec"))

    if isinstance(value, list):
        for i, item in enumerate(value):
            at = f"{where}[{i}]"
            if spec.item_schema is not None:
                if not isinstance(item, dict):
                    errors.append(Error(at, "expected a table", "spec"))
                else:
                    # cast: isinstance narrows Unknown -> dict[Never, Never]; give ty
                    # the concrete element types (same pattern as config._parse_drives).
                    nested = cast("dict[str, Any]", item)
                    errors.extend(validate(nested, spec.item_schema, prefix=at + "."))
            elif spec.item_types is not None and not _type_ok(item, spec.item_types):
                errors.append(
                    Error(
                        at,
                        f"expected {_type_name(spec.item_types)}, got "
                        f"{type(item).__name__} ({item!r})",
                        "spec",
                    )
                )
    return errors


def validate_spec(
    data: Mapping[str, Any], schema: Schema, prefix: str = ""
) -> list[Error]:
    """Stage 1. Unknown keys, missing required keys, wrong types, enum and pattern."""
    errors: list[Error] = []

    for key in data:
        if key not in schema.fields:
            errors.append(
                Error(f"{prefix}{key}", f"unknown key for {schema.name}", "spec")
            )

    for key, spec in schema.fields.items():
        if key not in data:
            if spec.required:
                errors.append(
                    Error(f"{prefix}{key}", "required key is missing", "spec")
                )
            continue
        errors.extend(_check_value(f"{prefix}{key}", data[key], spec))

    return errors


def apply_defaults(data: Mapping[str, Any], schema: Schema) -> dict[str, Any]:
    """Return *data* with every absent non-required key filled from its default.

    Sanity rules read a complete picture, so they run against this rather than the
    raw file — otherwise ``ladder="single"`` in a profile that omits ``speed``
    would be judged against a missing key instead of the default.
    """
    out = dict(data)
    for key, spec in schema.fields.items():
        if key in out:
            # Fold an enum value to its canonical member so callers never have to
            # think about the user's casing (see :func:`_in_enum`).
            v = out[key]
            if spec.enum is not None and isinstance(v, str):
                out[key] = v.lower()
            continue
        if spec.required:
            continue
        # Sequence defaults are declared as tuples so the schema itself cannot be
        # mutated, and are handed out as a fresh list per call. Returning the schema's
        # own object by reference would let one caller appending a drive corrupt the
        # default seen by every later load — a shared-mutable-default bug wearing a
        # frozen dataclass as a disguise.
        default = spec.default
        out[key] = list(default) if isinstance(default, tuple) else default
    return out


def validate_sanity(
    data: Mapping[str, Any], schema: Schema, prefix: str = ""
) -> list[Error]:
    """Stage 2. Ordered semantic rules over stage-1-valid, default-filled data."""
    filled = apply_defaults(data, schema)
    errors = [
        Error(f"{prefix}{rule.where}", rule.describe, "sanity")
        for rule in schema.rules
        if not rule.check(filled)
    ]

    for key, spec in schema.fields.items():
        if spec.item_schema is None or not isinstance(filled.get(key), list):
            continue
        for i, item in enumerate(filled[key]):
            if isinstance(item, dict):
                errors.extend(
                    validate_sanity(item, spec.item_schema, f"{prefix}{key}[{i}].")
                )
    return errors


def validate(data: Mapping[str, Any], schema: Schema, prefix: str = "") -> list[Error]:
    """Stage 1, then stage 2 only if stage 1 was clean.

    Short-circuiting is not an optimisation: a sanity rule reading a field that
    failed its type check would compare a string against an int and raise, so
    stage 2 is only meaningful on structurally valid data.
    """
    errors = validate_spec(data, schema, prefix)
    if errors:
        return errors
    return validate_sanity(data, schema, prefix)


# --------------------------------------------------------------------------
# Profile schema (accudisc-migration-plan.md §9.2)
# --------------------------------------------------------------------------

_NAME_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


def _is_profile_name(s: str) -> bool:
    return bool(s) and set(s) <= _NAME_OK


_SPEED_WORDS = frozenset({"max", "mid", "min"})


def _speed_ok(value: object) -> bool:
    """``max``/``mid``/``min``, or a fraction of maximum in [0.0, 1.0]."""
    if isinstance(value, str):
        return value in _SPEED_WORDS
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return 0.0 <= float(value) <= 1.0
    return False


PROFILE_SCHEMA = Schema(
    name="profile",
    fields={
        "name": FieldSpec(
            (str,),
            required=True,
            pattern=_is_profile_name,
            pattern_desc="must be non-empty and use only [a-z0-9_-]",
        ),
        "experimental": FieldSpec((bool,), default=False),
        # capture (pass 1)
        "sub": FieldSpec((bool,), default=True),
        "c2": FieldSpec((bool,), default=True),
        # recovery re-read (pass 2 — the bench axis)
        "granularity": FieldSpec(
            (str,), default="track", enum=frozenset({"sector", "track", "whole-disc"})
        ),
        "ladder": FieldSpec((str,), default="full", enum=frozenset({"full", "single"})),
        "speed": FieldSpec((str, float), default="max"),
        "passes": FieldSpec((int,), default=3),
        "run_up": FieldSpec((int,), default=0),
        "span": FieldSpec((int,), default=0),
        "variation": FieldSpec(
            (str,), default="none", enum=frozenset({"none", "speed", "full"})
        ),
        # recovery adjuncts
        "ctdb": FieldSpec((str,), default="auto", enum=frozenset({"off", "auto"})),
        "verify": FieldSpec((bool,), default=False),
        "budget_s": FieldSpec((int,), default=300),
    },
    rules=(
        SanityRule("passes", "must be at least 1", lambda d: d["passes"] >= 1),
        SanityRule("budget_s", "must be greater than 0", lambda d: d["budget_s"] > 0),
        SanityRule("run_up", "must not be negative", lambda d: d["run_up"] >= 0),
        SanityRule("span", "must not be negative", lambda d: d["span"] >= 0),
        SanityRule(
            "speed",
            'must be "max", "mid", "min", or a fraction of maximum in [0.0, 1.0]',
            lambda d: _speed_ok(d["speed"]),
        ),
        # Coherence. A speed-varying sweep needs rungs to vary over, and a
        # sector-shaped field is meaningless at track or whole-disc granularity —
        # silently ignoring either would make the profile lie about what it does.
        SanityRule(
            "variation",
            'variation="speed" requires ladder="full" (there is nothing to vary '
            "over on a single rung)",
            lambda d: d["variation"] != "speed" or d["ladder"] == "full",
        ),
        SanityRule(
            "span",
            'span > 0 requires granularity="sector"',
            lambda d: d["span"] <= 0 or d["granularity"] == "sector",
        ),
        SanityRule(
            "run_up",
            'run_up > 0 requires granularity="sector"',
            lambda d: d["run_up"] <= 0 or d["granularity"] == "sector",
        ),
        SanityRule(
            "ladder",
            'ladder="single" requires a speed selector',
            lambda d: d["ladder"] != "single" or d.get("speed") is not None,
        ),
    ),
)


# --------------------------------------------------------------------------
# Config schema (accudisc-migration-plan.md §9.6)
# --------------------------------------------------------------------------

DRIVE_SCHEMA = Schema(
    name="drive",
    fields={
        "name": FieldSpec((str,), required=True),
        "read_offset": FieldSpec((int,), required=True),
        "write_offset": FieldSpec((int,), default=None),
    },
    rules=(SanityRule("name", "must not be blank", lambda d: bool(d["name"].strip())),),
)

# ISO-3166 alpha-2, plus the MusicBrainz pseudo-codes XE (Europe) and XW
# (worldwide). Shape only — membership of the real ISO list is not checked, since
# a code we do not know is a lookup miss, not a config error.
_COUNTRY_LEN = 2

_FREQ_SUFFIX = frozenset("smhdw")


def _is_frequency(s: str) -> bool:
    """``db.parse_frequency`` shape: a positive integer plus a unit suffix, e.g. ``4w``."""
    return len(s) > 1 and s[-1] in _FREQ_SUFFIX and s[:-1].isdigit() and int(s[:-1]) > 0


CONFIG_SCHEMA = Schema(
    name="config",
    fields={
        "cddb_server": FieldSpec((str,), default="gnudb.gnudb.org:8880"),
        "contact_email": FieldSpec((str,), default=""),
        "database_backups": FieldSpec((int,), default=3),
        "database_backup_frequency": FieldSpec((str,), default="4w"),
        "catalogue_backups": FieldSpec((int,), default=3),
        "catalogue_backup_frequency": FieldSpec((str,), default="1d"),
        "drives": FieldSpec((list,), default=(), item_schema=DRIVE_SCHEMA),
        "catalogue_path": FieldSpec((str,), default=None),
        "enable_catalogue": FieldSpec((bool,), default=True),
        "duplicate_catalogue_entry": FieldSpec(
            (str,), default="ask", enum=frozenset({"ask", "skip", "replace", "add"})
        ),
        "default_device": FieldSpec((str,), default="/dev/sr0"),
        "silence_threshold": FieldSpec((int,), default=55),
        "capacity": FieldSpec((int,), default=80),
        "preview": FieldSpec((bool,), default=True),
        "tui": FieldSpec((bool,), default=True),
        "low_dr_threshold": FieldSpec((float,), default=5.0),
        "auto": FieldSpec((bool,), default=False),
        "embedart": FieldSpec((bool,), default=False),
        "recovery_passes": FieldSpec((int,), default=3),
        "c2_recovery": FieldSpec(
            (str,), default="auto", enum=frozenset({"auto", "on", "off"})
        ),
        "preferred_country": FieldSpec((list,), default=(), item_types=(str,)),
        # §9.4 rung 3: the profile used when no --profile and no --ad-* flag is given.
        "default_profile": FieldSpec((str,), default=None),
    },
    rules=(
        SanityRule(
            "database_backups",
            "must not be negative",
            lambda d: d["database_backups"] >= 0,
        ),
        SanityRule(
            "catalogue_backups",
            "must not be negative",
            lambda d: d["catalogue_backups"] >= 0,
        ),
        SanityRule(
            "database_backup_frequency",
            'must be a count plus one of s/m/h/d/w, e.g. "4w"',
            lambda d: _is_frequency(d["database_backup_frequency"]),
        ),
        SanityRule(
            "catalogue_backup_frequency",
            'must be a count plus one of s/m/h/d/w, e.g. "1d"',
            lambda d: _is_frequency(d["catalogue_backup_frequency"]),
        ),
        # A dBFS threshold stored as a positive magnitude; 0 would trim everything
        # and >120 is past the format's dynamic range either way.
        SanityRule(
            "silence_threshold",
            "must be between 1 and 120 (dBFS below full scale, as a magnitude)",
            lambda d: 1 <= d["silence_threshold"] <= 120,
        ),
        SanityRule(
            "capacity",
            "must be between 1 and 99 minutes (Red Book tops out at 80)",
            lambda d: 1 <= d["capacity"] <= 99,
        ),
        SanityRule(
            "low_dr_threshold",
            "must be between 0 and 30 LU",
            lambda d: 0 <= d["low_dr_threshold"] <= 30,
        ),
        # 0 disables the AR-recovery ladder entirely (documented); the ceiling is a
        # guard against a typo committing the drive to a multi-day sweep.
        SanityRule(
            "recovery_passes",
            "must be between 0 (disabled) and 100",
            lambda d: 0 <= d["recovery_passes"] <= 100,
        ),
        SanityRule(
            "preferred_country",
            "entries must be 2-letter codes (ISO-3166 alpha-2, or MB's XE/XW)",
            lambda d: all(
                len(c) == _COUNTRY_LEN and c.isalpha() for c in d["preferred_country"]
            ),
        ),
        SanityRule(
            "default_profile",
            "must be non-empty and use only [a-z0-9_-]",
            lambda d: d["default_profile"] is None
            or _is_profile_name(d["default_profile"]),
        ),
    ),
)
