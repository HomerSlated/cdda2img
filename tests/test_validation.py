"""Two-stage validator tests (accudisc-migration-plan.md §9.5).

The stages are tested separately on purpose. Stage 1 answers "is this well-formed",
stage 2 answers "is this legal" — and the whole design rests on those being different
questions, so a test that only ever calls :func:`validate` would not notice them
collapsing into one.
"""

from __future__ import annotations

import pytest

from cdda2img.validation import (
    CONFIG_SCHEMA,
    PROFILE_SCHEMA,
    FieldSpec,
    SanityRule,
    Schema,
    apply_defaults,
    validate,
    validate_sanity,
    validate_spec,
)

_TOY = Schema(
    name="toy",
    fields={
        "name": FieldSpec((str,), required=True),
        "count": FieldSpec((int,), default=1),
        "mode": FieldSpec((str,), default="a", enum=frozenset({"a", "b"})),
        "ratio": FieldSpec((float,), default=0.5),
        "tags": FieldSpec((list,), default=[], item_types=(str,)),
    },
    rules=(SanityRule("count", "must be at least 1", lambda d: d["count"] >= 1),),
)


def _where(errors) -> list[str]:
    return [e.where for e in errors]


# ---- stage 1: structural ----------------------------------------------------


def test_a_valid_document_produces_no_errors() -> None:
    assert validate({"name": "x", "count": 2}, _TOY) == []


def test_a_missing_required_key_is_reported() -> None:
    errors = validate_spec({"count": 2}, _TOY)
    assert _where(errors) == ["name"]
    assert errors[0].stage == "spec"


def test_an_unknown_key_is_an_error_not_a_shrug() -> None:
    """The whole point of strict config: a typo'd key must not silently do nothing."""
    errors = validate_spec({"name": "x", "coutn": 2}, _TOY)
    assert _where(errors) == ["coutn"]
    assert "unknown key" in errors[0].message


def test_a_wrong_type_is_reported_with_both_types() -> None:
    errors = validate_spec({"name": 7}, _TOY)
    assert _where(errors) == ["name"]
    assert "expected str" in errors[0].message
    assert "int" in errors[0].message


def test_a_value_outside_the_enum_is_reported() -> None:
    errors = validate_spec({"name": "x", "mode": "z"}, _TOY)
    assert _where(errors) == ["mode"]
    assert "not one of: a, b" in errors[0].message


def test_a_bool_is_not_accepted_where_an_int_belongs() -> None:
    """Python makes bool a subclass of int; TOML users do not. `count = true` is a
    mistake, and a plain isinstance check would wave it through as 1."""
    errors = validate_spec({"name": "x", "count": True}, _TOY)
    assert _where(errors) == ["count"]


def test_an_int_is_accepted_where_a_float_belongs() -> None:
    """TOML writes `ratio = 1`, not `1.0`; rejecting that would be pedantry."""
    assert validate_spec({"name": "x", "ratio": 1}, _TOY) == []


def test_a_bool_is_still_rejected_for_a_float() -> None:
    assert _where(validate_spec({"name": "x", "ratio": True}, _TOY)) == ["ratio"]


def test_list_element_types_are_checked_and_indexed() -> None:
    errors = validate_spec({"name": "x", "tags": ["ok", 3]}, _TOY)
    assert _where(errors) == ["tags[1]"]


def test_every_structural_failure_is_reported_not_just_the_first() -> None:
    """A user fixing a config one error per run is a bad experience; strict mode
    promises the whole list."""
    errors = validate_spec({"count": "no", "mode": "z", "bogus": 1}, _TOY)
    assert set(_where(errors)) == {"name", "count", "mode", "bogus"}


# ---- stage 2: semantic ------------------------------------------------------


def test_a_well_formed_but_illegal_value_passes_stage_1_and_fails_stage_2() -> None:
    """The reason the stages exist. `count = 0` is a perfectly good integer."""
    doc = {"name": "x", "count": 0}
    assert validate_spec(doc, _TOY) == []
    errors = validate_sanity(doc, _TOY)
    assert _where(errors) == ["count"]
    assert errors[0].stage == "sanity"


def test_sanity_rules_see_defaults_for_absent_keys() -> None:
    """A rule must judge the value that will actually be used, not the absence."""
    assert apply_defaults({"name": "x"}, _TOY)["count"] == 1
    assert validate_sanity({"name": "x"}, _TOY) == []


def test_a_sequence_default_is_a_fresh_list_every_call() -> None:
    """A shared mutable default wearing a frozen dataclass as a disguise: `Schema`
    and `FieldSpec` are frozen, but a `[]` default is not, so handing it out by
    reference would let one caller appending a drive corrupt every later load."""
    first = apply_defaults({}, CONFIG_SCHEMA)
    first["drives"].append({"name": "X", "read_offset": 0})
    assert apply_defaults({}, CONFIG_SCHEMA)["drives"] == []
    assert isinstance(first["preferred_country"], list)


def test_stage_2_is_skipped_when_stage_1_failed() -> None:
    """Short-circuiting is correctness, not speed: a rule reading a field that failed
    its type check would compare str to int and raise."""
    errors = validate({"name": "x", "count": "seven"}, _TOY)
    assert all(e.stage == "spec" for e in errors)


# ---- profile schema (§9.2) --------------------------------------------------


def test_the_default_profile_shape_validates() -> None:
    assert validate({"name": "track-ladder"}, PROFILE_SCHEMA) == []


@pytest.mark.parametrize("name", ["Track-Ladder", "track ladder", "trackladder!", ""])
def test_an_illegal_profile_name_is_rejected_never_mangled(name: str) -> None:
    """§9.7: any character outside [a-z0-9_-] is an error. Silent lowercasing would
    let two profiles collide under one filename."""
    assert _where(validate({"name": name}, PROFILE_SCHEMA)) == ["name"]


@pytest.mark.parametrize("speed", ["max", "mid", "min", 0.0, 1.0, 0.5])
def test_every_documented_speed_selector_is_accepted(speed: object) -> None:
    assert (
        validate({"name": "p", "ladder": "single", "speed": speed}, PROFILE_SCHEMA)
        == []
    )


@pytest.mark.parametrize("speed", ["fastest", 1.5, -0.1])
def test_an_out_of_range_or_unknown_speed_is_rejected(speed: object) -> None:
    errors = validate({"name": "p", "speed": speed}, PROFILE_SCHEMA)
    assert _where(errors) == ["speed"]


def test_speed_variation_requires_a_ladder_to_vary_over() -> None:
    errors = validate(
        {"name": "p", "variation": "speed", "ladder": "single"}, PROFILE_SCHEMA
    )
    assert _where(errors) == ["variation"]


@pytest.mark.parametrize("field_name", ["span", "run_up"])
def test_a_sector_shaped_field_requires_sector_granularity(field_name: str) -> None:
    """Accepting these at track granularity would make the profile lie: the value is
    read by nothing and the user believes it took effect."""
    errors = validate(
        {"name": "p", field_name: 4, "granularity": "track"}, PROFILE_SCHEMA
    )
    assert _where(errors) == [field_name]
    assert (
        validate({"name": "p", field_name: 4, "granularity": "sector"}, PROFILE_SCHEMA)
        == []
    )


def test_zero_passes_is_rejected_for_a_profile() -> None:
    assert _where(validate({"name": "p", "passes": 0}, PROFILE_SCHEMA)) == ["passes"]


# ---- config schema (§9.6) ---------------------------------------------------


def test_an_empty_config_is_valid() -> None:
    """Every config key has a default; a user who has never written one is not in error."""
    assert validate({}, CONFIG_SCHEMA) == []


def test_a_drives_table_is_validated_per_entry_with_an_index() -> None:
    doc = {
        "drives": [
            {"name": "PLEXTOR PX-716A", "read_offset": 30, "write_offset": -30},
            {"name": "OTHER", "read_offset": "nope"},
        ]
    }
    errors = validate(doc, CONFIG_SCHEMA)
    assert _where(errors) == ["drives[1].read_offset"]


def test_a_drive_entry_missing_its_offset_is_reported() -> None:
    errors = validate({"drives": [{"name": "X"}]}, CONFIG_SCHEMA)
    assert _where(errors) == ["drives[0].read_offset"]


def test_recovery_passes_may_be_zero_because_zero_means_disabled() -> None:
    """Documented behaviour: 0 turns the AR-recovery ladder off. It is a setting,
    not a mistake, and must not be validated away."""
    assert validate({"recovery_passes": 0}, CONFIG_SCHEMA) == []
    assert _where(validate({"recovery_passes": -1}, CONFIG_SCHEMA)) == [
        "recovery_passes"
    ]


@pytest.mark.parametrize("freq", ["4w", "1d", "30m", "12h", "90s"])
def test_backup_frequency_shapes_accepted_by_parse_frequency(freq: str) -> None:
    assert validate({"database_backup_frequency": freq}, CONFIG_SCHEMA) == []


@pytest.mark.parametrize("freq", ["4", "w", "0d", "-1w", "4y", ""])
def test_a_malformed_backup_frequency_is_rejected(freq: str) -> None:
    errors = validate({"database_backup_frequency": freq}, CONFIG_SCHEMA)
    assert _where(errors) == ["database_backup_frequency"]


def test_preferred_country_accepts_iso_codes_and_mb_pseudo_codes() -> None:
    assert validate({"preferred_country": ["GB", "XE", "XW"]}, CONFIG_SCHEMA) == []


def test_a_malformed_country_code_is_rejected() -> None:
    errors = validate({"preferred_country": ["GBR"]}, CONFIG_SCHEMA)
    assert _where(errors) == ["preferred_country"]


def test_the_out_of_range_uint8_case_the_two_stages_exist_for() -> None:
    """AccuDisc casts --retries through uint8_t unguarded, so 256 becomes 0 and then
    defaults back to 2 — the opposite of the maximum effort requested. It is a
    perfectly good integer, which is exactly why stage 1 cannot catch it."""
    schema = Schema(
        name="ad",
        fields={"retries": FieldSpec((int,), default=2)},
        rules=(
            SanityRule("retries", "must be 1..255", lambda d: 1 <= d["retries"] <= 255),
        ),
    )
    assert validate_spec({"retries": 256}, schema) == []
    assert _where(validate_sanity({"retries": 256}, schema)) == ["retries"]
