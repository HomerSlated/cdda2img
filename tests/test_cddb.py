"""Tests for `compute_cddb_disc_id` — frozen vectors + the floor-division
boundary that previously caused a 1-second-short total when track 1 had an
in-track pre-gap, producing an off-by-1 disc-ID and 404s at AccurateRip.
"""

from __future__ import annotations

from cdda2img import cddb
from cdda2img.cddb import _parse_xmcd, compute_cddb_disc_id
from cdda2img.lookup_result import DiscMeta

# Sheryl Crow — Sheryl Crow (1996), 14 tracks. Whipper and EAC both compute
# CDDB id e00e160e for this disc; AccurateRip serves a 1529-byte dBAR at the
# URL keyed on it. The 33-frame in-track pre-gap on track 1 makes this disc
# the canonical regression for the floor-division bug — track_lsns[0] mod 75
# (= 33) exceeds (disc_last_lsn + 1) mod 75 (= 25), so a subtract-then-floor
# would lose a second of total runtime and corrupt the id.
_SHERYL_CROW_LSNS = [
    33,
    22175,
    39415,
    61220,
    79218,
    103473,
    123455,
    137535,
    156828,
    178033,
    193910,
    216245,
    237583,
    255455,
]
_SHERYL_CROW_LAST_LSN = 270474


def test_parse_xmcd_splits_freedb_artist_title_ttitle() -> None:
    """gnudb/freedb store TTITLE as "Artist / Title"; the artist prefix must be
    stripped from the track title (regression: gnudb swap surfaced this)."""
    lines = [
        "DTITLE=Green Day / American Idiot",
        "TTITLE0=Green Day / American Idiot",
        "TTITLE2=Boulevard of Broken Dreams",
    ]
    meta = _parse_xmcd(lines, 3)
    assert meta.tracks[0].title == "American Idiot"
    assert meta.tracks[0].performer == "Green Day"
    # No " / " -> the whole value is the title, performer unset.
    assert meta.tracks[2].title == "Boulevard of Broken Dreams"
    assert meta.tracks[2].performer is None
    # Missing TTITLE1 -> blank title, no crash.
    assert meta.tracks[1].title is None


def test_parse_xmcd_preserves_medley_slash_in_title() -> None:
    """Split on the FIRST " / " only, so a medley title containing " / " keeps
    its internal separators in the title part, not just the leading artist."""
    lines = ["TTITLE0=Green Day / Jesus of Suburbia / City of the Damned"]
    meta = _parse_xmcd(lines, 1)
    assert meta.tracks[0].performer == "Green Day"
    assert meta.tracks[0].title == "Jesus of Suburbia / City of the Damned"


def test_compute_cddb_disc_id_matches_whipper_for_sheryl_crow() -> None:
    assert compute_cddb_disc_id(_SHERYL_CROW_LSNS, _SHERYL_CROW_LAST_LSN) == "e00e160e"


def test_compute_cddb_disc_id_track1_lsn_zero_no_regression() -> None:
    """Discs with no in-track pre-gap must keep the same id.

    With track_lsns[0] == 0 the two formulas coincide (0 // 75 = 0), so this
    pins the no-pregap path against any future formula change. Values mirror
    the Technotronic vector used in test_accuraterip.
    """
    lsns = [
        0,
        24337,
        49832,
        69982,
        93097,
        107660,
        132412,
        156647,
        178840,
        205097,
        226850,
        246345,
    ]
    last_lsn = 259511
    assert compute_cddb_disc_id(lsns, last_lsn) == "ac0d840c"


def test_compute_cddb_disc_id_floor_boundary_minimal() -> None:
    """Two-track disc engineered so subtract-then-floor under-counts by 1s.

    track_lsns[0] = 50  → 50 // 75 = 0
    leadout       = 100 → 100 // 75 = 1 → total = 1 s
    Subtract-first: (99 - 50 + 1) // 75 = 50 // 75 = 0 s (wrong).
    """
    lsns = [50, 75]
    last_lsn = 99  # leadout = 100
    # checksum  = digit_sum((50+150)//75) + digit_sum((75+150)//75)
    #           = digit_sum(2) + digit_sum(3) = 2 + 3 = 5
    # total_secs = 100//75 - 50//75 = 1 - 0 = 1
    # n_tracks  = 2
    # id = (5 << 24) | (1 << 8) | 2 = 0x05000102
    assert compute_cddb_disc_id(lsns, last_lsn) == "05000102"


def test_compute_cddb_disc_id_floor_boundary_no_bias() -> None:
    """Control: when both endpoints fall on the same side, the two formulas
    agree, so we keep the same id in either direction.
    """
    lsns = [0, 75]
    last_lsn = 149  # leadout = 150 → 2 s exact
    # checksum = digit_sum(150//75) + digit_sum(225//75) = digit_sum(2) + digit_sum(3) = 5
    # total_secs = 150//75 - 0//75 = 2
    # id = (5 << 24) | (2 << 8) | 2 = 0x05000202
    assert compute_cddb_disc_id(lsns, last_lsn) == "05000202"


# ---------------------------------------------------------------------------
# #3-d — transport-error retry (cold-connect TCP flake)
# ---------------------------------------------------------------------------


def _force_online_cache_miss(monkeypatch):
    monkeypatch.setattr(cddb.time, "sleep", lambda *_: None)
    monkeypatch.setattr("cdda2img.lookup_cache.get_cached_cddb_lookup", lambda *_: None)
    monkeypatch.setattr("cdda2img.config.is_no_network_active", lambda: False)


def test_query_cddb_retries_then_gives_up_on_transport_error(monkeypatch):
    _force_online_cache_miss(monkeypatch)
    calls = {"n": 0}

    def _boom(*_a, **_k):
        calls["n"] += 1
        raise OSError

    monkeypatch.setattr(cddb, "_query_cddb_session", _boom)
    assert cddb.query_cddb([0, 18000], 40000) == []
    assert calls["n"] == cddb._CONNECT_ATTEMPTS  # every attempt tried


def test_query_cddb_succeeds_after_transient_flake(monkeypatch):
    _force_online_cache_miss(monkeypatch)
    calls = {"n": 0}

    def _flaky(*_a, **_k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError
        return [DiscMeta(album="OK")]

    monkeypatch.setattr(cddb, "_query_cddb_session", _flaky)
    result = cddb.query_cddb([0, 18000], 40000)
    assert [m.album for m in result] == ["OK"]
    assert calls["n"] == 2  # one flake, then success
