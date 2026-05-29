"""
test_no_network_services.py — R10 offline-mode toggle tests.

Verifies that every remote-lookup module honours
``config.is_no_network_active()`` consistently, and that
``set_no_network_override`` propagates without depending on
``load_config()``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def _force_offline():
    """Activate R10 offline mode for the duration of the test."""
    from cdda2img.config import set_no_network_override

    set_no_network_override(True)
    try:
        yield
    finally:
        set_no_network_override(None)


@pytest.fixture
def _force_online():
    """Force R10 *off* regardless of the loaded config."""
    from cdda2img.config import set_no_network_override

    set_no_network_override(False)
    try:
        yield
    finally:
        set_no_network_override(None)


# ---------------------------------------------------------------------------
# Override mechanism
# ---------------------------------------------------------------------------


def test_override_true_forces_offline(_force_offline) -> None:
    from cdda2img.config import is_no_network_active

    assert is_no_network_active() is True


def test_override_false_forces_online(_force_online) -> None:
    from cdda2img.config import is_no_network_active

    assert is_no_network_active() is False


def test_override_none_falls_back_to_config_default() -> None:
    from cdda2img.config import is_no_network_active, set_no_network_override

    set_no_network_override(None)
    # In the test environment, no config file exists at the temp XDG home,
    # so Config defaults apply → no_network_services=False.
    assert is_no_network_active() is False


# ---------------------------------------------------------------------------
# Per-service R10 behaviour
# ---------------------------------------------------------------------------


def test_discogs_is_available_returns_false_when_offline(_force_offline) -> None:
    from cdda2img import discogs_lookup

    # Even with a token in env, offline mode wins.
    with patch.dict("os.environ", {"DISCOGS_TOKEN": "fake"}):
        assert discogs_lookup.is_available() is False


def test_acoustid_is_available_returns_false_when_offline(_force_offline) -> None:
    from cdda2img import acoustid_lookup

    with patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}):
        assert acoustid_lookup.is_available() is False


def test_cddb_query_returns_empty_when_offline(_force_offline) -> None:
    from cdda2img.cddb import query_cddb

    # Even with a valid TOC, no network call happens.
    assert query_cddb([0, 18000], 36000) == []


def test_mb_lookup_disc_id_skips_network_when_offline(_force_offline) -> None:
    from cdda2img.mb_lookup import lookup_disc_id
    from cdda2img.rbi_format import RBIDisc, RBITocEntry

    disc = RBIDisc(
        album="A",
        artist="B",
        tracks=[
            RBITocEntry(
                track_number=1,
                title="T",
                performer="P",
                start_frame=0,
                duration_frames=18000,
            )
        ],
    )
    # No mock for musicbrainzngs.get_releases_by_discid — if R10 lets
    # the call through, this test would attempt a real network call.
    assert lookup_disc_id(disc) == []


def test_mb_search_releases_skips_network_when_offline(_force_offline) -> None:
    from cdda2img.mb_lookup import search_releases

    assert search_releases("artist:foo") == []


def test_mb_lookup_release_returns_none_when_offline(_force_offline) -> None:
    from cdda2img.mb_lookup import lookup_release

    assert lookup_release("any-uuid") is None


def test_mb_lookup_isrc_returns_empty_when_offline(_force_offline) -> None:
    from cdda2img.mb_lookup import lookup_isrc

    assert lookup_isrc("USEE18300025") == []


def test_accuraterip_fetch_skipped_when_offline(_force_offline) -> None:
    from cdda2img.accuraterip import _fetch_ar

    body, transport = _fetch_ar(1, "aabbccdd", "11223344", 0)
    assert body is None
    assert transport is None
