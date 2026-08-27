"""
test_resolve_drive_offsets.py — unit tests for _resolve_drive_offsets() in cdda2img.py.

Rung 2 stopped being a local SQLite scrape on 2026-08-27 and became a lookup into
AccuDisc's compiled table, so these mock ``_lookup_drive_offset`` where they used
to mock ``open_drive_offsets_db``. The behaviour under test is unchanged except
for one genuinely new outcome: the table can now say *the sources disagree*,
which the old one had no way to express.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from cdda2img.accudisc_reader import DriveOffsetLookup
from cdda2img.cdda2img import _resolve_drive_offsets
from cdda2img.config import Config, DriveConfig


def _cfg(**kwargs) -> Config:
    return Config(**kwargs)


def _info(
    read_offset: int | None,
    *,
    submissions: int = 0,
    sources: frozenset[str] = frozenset({"accuraterip"}),
    candidates: tuple = (),
    truncated: bool = False,
) -> DriveOffsetLookup:
    return DriveOffsetLookup(
        vendor="PLEXTOR",
        product="DVDR PX-716A",
        read_offset=read_offset,
        sources=sources,
        ar_submissions=submissions,
        ar_agree_pct=100,
        candidates=candidates,
        truncated=truncated,
        generic_product=False,
    )


# ---------------------------------------------------------------------------
# 1. cfg.drives hit — short-circuit: no lookup performed
# ---------------------------------------------------------------------------


def test_uses_config_drives_when_name_matches() -> None:
    cfg = _cfg(drives=[DriveConfig("PLEXTOR DVDR PX-716A", read_offset=42)])

    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch("cdda2img.cdda2img._lookup_drive_offset") as mock_lookup,
    ):
        result = _resolve_drive_offsets("/dev/sr0", cfg)

    assert result == (42, None, "PLEXTOR DVDR PX-716A")
    # Config is authoritative: the lookup must not even be attempted.
    mock_lookup.assert_not_called()


def test_uses_config_drives_returns_write_offset() -> None:
    cfg = _cfg(
        drives=[DriveConfig("PLEXTOR DVDR PX-716A", read_offset=30, write_offset=-30)]
    )

    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch("cdda2img.cdda2img._lookup_drive_offset") as mock_lookup,
    ):
        result = _resolve_drive_offsets("/dev/sr0", cfg)

    assert result == (30, -30, "PLEXTOR DVDR PX-716A")
    mock_lookup.assert_not_called()


def test_config_drives_ignores_non_matching_entries() -> None:
    cfg = _cfg(drives=[DriveConfig("SOME OTHER DRIVE", read_offset=99)])

    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch("cdda2img.cdda2img._lookup_drive_offset", return_value=None),
    ):
        offset, write_offset, name = _resolve_drive_offsets("/dev/sr0", cfg)

    assert offset == 0
    assert write_offset is None
    assert name == "PLEXTOR DVDR PX-716A"


# ---------------------------------------------------------------------------
# 2. AccuDisc table lookup
# ---------------------------------------------------------------------------


def test_auto_apply_high_confidence() -> None:
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch(
            "cdda2img.cdda2img._lookup_drive_offset",
            return_value=_info(30, submissions=99),
        ),
        patch("cdda2img.config.save_drive_read_offset"),
    ):
        offset, _wo, _name = _resolve_drive_offsets("/dev/sr0", _cfg())

    assert offset == 30


def test_auto_apply_saves_to_config() -> None:
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch(
            "cdda2img.cdda2img._lookup_drive_offset",
            return_value=_info(30, submissions=99),
        ),
        patch("cdda2img.config.save_drive_read_offset") as mock_save,
    ):
        _resolve_drive_offsets("/dev/sr0", _cfg())

    mock_save.assert_called_once_with("PLEXTOR DVDR PX-716A", 30)


def test_prompt_accepted() -> None:
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch(
            "cdda2img.cdda2img._lookup_drive_offset",
            return_value=_info(6, submissions=1),
        ),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="y"),
        patch("cdda2img.config.save_drive_read_offset"),
    ):
        offset, _wo, _name = _resolve_drive_offsets("/dev/sr0", _cfg())

    assert offset == 6


def test_prompt_rejected_returns_zero() -> None:
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch(
            "cdda2img.cdda2img._lookup_drive_offset",
            return_value=_info(6, submissions=1),
        ),
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", return_value="n"),
        patch("cdda2img.config.save_drive_read_offset") as mock_save,
    ):
        offset, _wo, _name = _resolve_drive_offsets("/dev/sr0", _cfg())

    assert offset == 0
    mock_save.assert_not_called()


def test_low_confidence_no_tty_returns_zero() -> None:
    """Without a TTY a thinly-evidenced offset is declined, not adopted.

    Nobody is there to say no, so silence must not read as consent.
    """
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch(
            "cdda2img.cdda2img._lookup_drive_offset",
            return_value=_info(6, submissions=1),
        ),
        patch("sys.stdin.isatty", return_value=False),
        patch("cdda2img.config.save_drive_read_offset") as mock_save,
    ):
        offset, _wo, _name = _resolve_drive_offsets("/dev/sr0", _cfg())

    assert offset == 0
    mock_save.assert_not_called()


def test_drive_not_in_table_returns_zero() -> None:
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch("cdda2img.cdda2img._lookup_drive_offset", return_value=None),
    ):
        offset, write_offset, name = _resolve_drive_offsets("/dev/sr0", _cfg())

    assert offset == 0
    assert write_offset is None
    assert name == "PLEXTOR DVDR PX-716A"


# ---------------------------------------------------------------------------
# 3. Sources disagree — the outcome the old catalogue could not express
# ---------------------------------------------------------------------------


def test_ambiguous_offset_is_never_applied_and_never_saved() -> None:
    """``read_offset is None`` means DISAGREE and must not collapse to a value.

    The trap this guards is flattening a refusal into its neighbouring falsy
    value: an offset of 0 is a real, applicable answer, while None is "we will
    not say". Returning 0 here is correct only because it means "unshifted, and
    you were told" -- what must never happen is SAVING it, which would bake the
    guess into config as though it had been confirmed.
    """
    info = _info(
        None,
        candidates=((6, frozenset({"accuraterip"})), (48, frozenset({"redump"}))),
        sources=frozenset({"accuraterip", "redump"}),
    )
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch("cdda2img.cdda2img._lookup_drive_offset", return_value=info),
        patch("cdda2img.config.save_drive_read_offset") as mock_save,
    ):
        offset, write_offset, name = _resolve_drive_offsets("/dev/sr0", _cfg())

    assert offset == 0
    assert write_offset is None
    assert name == "PLEXTOR DVDR PX-716A"
    mock_save.assert_not_called()


def test_ambiguous_offset_reports_every_candidate(capsys) -> None:
    """Both candidates must reach the user, or the refusal is unactionable."""
    info = _info(
        None,
        candidates=((6, frozenset({"accuraterip"})), (48, frozenset({"redump"}))),
    )
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch("cdda2img.cdda2img._lookup_drive_offset", return_value=info),
    ):
        _resolve_drive_offsets("/dev/sr0", _cfg())

    out = capsys.readouterr().out
    assert "DISAGREE" in out
    assert "+6" in out
    assert "+48" in out
    assert "read_offset" in out  # tells the user how to settle it


# ---------------------------------------------------------------------------
# 4. Failure paths
# ---------------------------------------------------------------------------


def test_probe_fails_returns_zero_without_lookup() -> None:
    with (
        patch("cdda2img.drive_info.probe_drive_name", return_value=None),
        patch("cdda2img.cdda2img._lookup_drive_offset") as mock_lookup,
    ):
        result = _resolve_drive_offsets("/dev/sr0", _cfg())

    assert result == (0, None, None)
    mock_lookup.assert_not_called()


def test_save_drive_read_offset_oserror_does_not_propagate() -> None:
    """A config write failure must not fail the rip -- the offset still applies."""
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch(
            "cdda2img.cdda2img._lookup_drive_offset",
            return_value=_info(30, submissions=99),
        ),
        patch(
            "cdda2img.config.save_drive_read_offset",
            side_effect=OSError("read-only fs"),
        ),
    ):
        offset, _wo, _name = _resolve_drive_offsets("/dev/sr0", _cfg())

    assert offset == 30


def test_lookup_failure_falls_through_to_zero() -> None:
    """A RuntimeError from the seam warns and degrades; it does not abort.

    _lookup_drive_offset swallows it, so this exercises the real helper rather
    than the mock the other tests use.
    """
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch(
            "cdda2img.drive_info.probe_drive_inquiry",
            return_value=("PLEXTOR", "DVDR PX-716A"),
        ),
        patch(
            "cdda2img.accudisc_reader.drive_offset_lookup",
            side_effect=RuntimeError("binding missing"),
        ),
    ):
        offset, _wo, name = _resolve_drive_offsets("/dev/sr0", _cfg())

    assert offset == 0
    assert name == "PLEXTOR DVDR PX-716A"


def test_lookup_receives_the_split_inquiry_pair() -> None:
    """The vendor/product boundary must survive to the lookup.

    Passing the joined "VENDOR MODEL" string as the product is the exact defect
    the retired _normalize_ar_name embodied, and it would still return an answer
    for some drives -- so this asserts the call, not the result.
    """
    mock_lookup = MagicMock(return_value=None)
    with (
        patch(
            "cdda2img.drive_info.probe_drive_name", return_value="PLEXTOR DVDR PX-716A"
        ),
        patch(
            "cdda2img.drive_info.probe_drive_inquiry",
            return_value=("PLEXTOR", "DVDR PX-716A"),
        ),
        patch("cdda2img.accudisc_reader.drive_offset_lookup", mock_lookup),
    ):
        _resolve_drive_offsets("/dev/sr0", _cfg())

    mock_lookup.assert_called_once_with("PLEXTOR", "DVDR PX-716A")
