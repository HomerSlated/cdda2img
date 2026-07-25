"""Strict config loading (accudisc-migration-plan.md §9.6).

The behaviour being replaced is "warn about a bad value and substitute a default".
That is worse than no validation: a user who mistypes `recovery_passes` gets a rip
that quietly did something other than what they asked for, and the warning scrolls
past. These tests pin the new contract — refuse, list everything, and exempt only
`setup`, which has to be able to open a broken config in order to fix it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cdda2img import config as C


def _expect_error() -> C.ConfigError:
    """Call load_config and return the ConfigError it must raise.

    `pytest.raises(...).value` is typed as BaseException, so reaching into
    `.errors` / `.describe()` through it does not type-check. Catching explicitly
    keeps the assertions typed.
    """
    try:
        C.load_config()
    except C.ConfigError as err:
        return err
    msg = "expected ConfigError, but load_config succeeded"
    raise AssertionError(msg)


@pytest.fixture
def _cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point config_path() at a temp file and return a writer for it."""
    path = tmp_path / "cdda2img.toml"
    monkeypatch.setattr(C, "config_path", lambda: path)
    return lambda text: path.write_text(text)


def test_a_valid_config_loads(_cfg) -> None:
    _cfg('default_device = "/dev/sr1"\nrecovery_passes = 5\n')
    cfg = C.load_config()
    assert cfg.default_device == "/dev/sr1"
    assert cfg.recovery_passes == 5


def test_an_absent_config_is_all_defaults_not_an_error(tmp_path, monkeypatch) -> None:
    """Never having written a config is not a mistake."""
    monkeypatch.setattr(C, "config_path", lambda: tmp_path / "nope.toml")
    assert C.load_config().default_device == "/dev/sr0"


def test_an_out_of_range_value_raises_instead_of_defaulting(_cfg) -> None:
    """The whole point. `recovery_passes = 500` parses fine and means nothing good."""
    _cfg("recovery_passes = 500\n")
    with pytest.raises(C.ConfigError) as exc:
        C.load_config()
    assert "recovery_passes" in str(exc.value)


def test_an_unknown_key_raises(_cfg) -> None:
    """A typo'd key silently doing nothing is the failure strict mode removes."""
    _cfg("recovry_passes = 3\n")
    with pytest.raises(C.ConfigError, match="recovry_passes"):
        C.load_config()


def test_every_problem_in_a_stage_is_listed_not_just_the_first(_cfg) -> None:
    """Fixing a config one error per run is a bad experience."""
    _cfg("recovery_passes = 500\ncapacity = 0\nlow_dr_threshold = 99.0\n")
    err = _expect_error()
    text = str(err)
    assert all(k in text for k in ("recovery_passes", "capacity", "low_dr_threshold"))
    assert len(err.errors) >= 3


def test_structural_failures_are_reported_before_semantic_ones(_cfg) -> None:
    """Cross-stage short-circuiting is deliberate, so the report is stage-1 only
    while any structural fault remains. A sanity rule reading a field that failed
    its type check would compare a string to an int and raise, so stage 2 is only
    meaningful once the shape is right — the user fixes structure, then values."""
    _cfg('c2_recovery = "sometimes"\nrecovery_passes = 500\n')
    err = _expect_error()
    assert [e.stage for e in err.errors] == ["spec"]
    assert "c2_recovery" in str(err)

    # With the structural fault fixed, the semantic one surfaces.
    _cfg('c2_recovery = "auto"\nrecovery_passes = 500\n')
    err = _expect_error()
    assert [e.stage for e in err.errors] == ["sanity"]
    assert "recovery_passes" in str(err)


def test_recovery_passes_zero_is_accepted_because_zero_means_disabled(_cfg) -> None:
    """Documented behaviour, not a mistake — it must not be validated away."""
    _cfg("recovery_passes = 0\n")
    assert C.load_config().recovery_passes == 0


def test_lenient_mode_drops_the_bad_key_and_keeps_the_rest(_cfg) -> None:
    """`setup` must be able to open a broken config; the valid keys still apply."""
    _cfg('recovery_passes = 500\ndefault_device = "/dev/sr1"\n')
    cfg = C.load_config(strict=False)
    assert cfg.default_device == "/dev/sr1"
    assert cfg.recovery_passes == 3  # schema default, not the rejected 500


def test_lenient_mode_discards_rather_than_keeps_an_invalid_value(_cfg) -> None:
    """Keeping a value we have just declared invalid is the old bug, restated."""
    _cfg("capacity = 999\n")
    assert C.load_config(strict=False).capacity == 80


def test_a_bad_drive_entry_is_reported_with_its_index(_cfg) -> None:
    _cfg(
        '[[drives]]\nname = "A"\nread_offset = 30\n\n'
        '[[drives]]\nname = "B"\nread_offset = "thirty"\n'
    )
    with pytest.raises(C.ConfigError, match=r"drives\[1\]\.read_offset"):
        C.load_config()


def test_drives_and_country_codes_are_still_normalised(_cfg) -> None:
    """Normalisation survives the move to schema validation: rejection moved to the
    schema, but turning tables into objects and codes into upper case did not."""
    _cfg(
        'preferred_country = ["gb", "xe", "gb"]\n\n'
        '[[drives]]\nname = "PLEXTOR"\nread_offset = 30\nwrite_offset = -30\n'
    )
    cfg = C.load_config()
    assert cfg.preferred_country == ["GB", "XE"]  # upper-cased, de-duplicated
    assert cfg.drives[0].name == "PLEXTOR"
    assert (cfg.drives[0].read_offset, cfg.drives[0].write_offset) == (30, -30)


def test_the_error_names_the_file_so_it_can_be_found(_cfg) -> None:
    _cfg("capacity = 0\n")
    assert str(C.config_path()) in _expect_error().describe()


def test_the_renamed_silence_key_still_warns(_cfg, caplog) -> None:
    """`silence` -> `silence_threshold` predates the schema; the guidance must not
    be lost to a generic 'unknown key' message."""
    _cfg("silence = 55\n")
    with pytest.raises(C.ConfigError):
        C.load_config()
    assert "silence_threshold" in caplog.text


def test_default_profile_reaches_the_config(_cfg) -> None:
    """§9.4 rung 3 — without this the built-in is the only reachable default."""
    _cfg('default_profile = "max-variation"\n')
    assert C.load_config().default_profile == "max-variation"


def test_an_illegal_profile_name_in_config_is_refused(_cfg) -> None:
    _cfg('default_profile = "Max Variation"\n')
    with pytest.raises(C.ConfigError, match="default_profile"):
        C.load_config()
