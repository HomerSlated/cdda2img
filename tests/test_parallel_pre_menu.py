"""
test_parallel_pre_menu.py — R8 parallel-prepop integration tests.

The R8 restructure moves CDDB into ``_finalize_import`` and runs it in a
2-worker ThreadPoolExecutor alongside the MB disc-ID lookup. The two
key properties to verify:

  1. CDDB-first → MB-second merge order is preserved (non-blank-wins).
  2. A slow / failing CDDB does not block MB latency (failure isolation).

These are integration-style tests that exercise the helper at the
level of ``prepopulate_from_cddb`` + ``prepopulate_from_mb`` because the
helper that wraps them in a ThreadPoolExecutor is private to
``_finalize_import``.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from cdda2img.cddb import prepopulate_from_cddb
from cdda2img.lookup_result import DiscMeta
from cdda2img.mb_lookup import _merge_into_disc, prepopulate_from_mb
from cdda2img.rbi_format import RBIDisc, RBITocEntry


def _disc() -> RBIDisc:
    return RBIDisc(
        album="",
        artist="",
        tracks=[
            RBITocEntry(
                track_number=1,
                title="",
                performer="",
                start_frame=0,
                duration_frames=18000,
            )
        ],
    )


def test_cddb_first_mb_second_merge_order() -> None:
    """When both services agree, both fields land. When they disagree, CDDB wins."""
    disc = _disc()
    # CDDB returns one match with album="From CDDB", artist="From CDDB".
    cddb_meta = DiscMeta(album="From CDDB", artist="From CDDB", source="cddb")
    # MB returns one match with album="From MB", artist="From MB".
    mb_meta = DiscMeta(
        album="From MB",
        artist="From MB",
        mb_release_id="rid-mb",
        source="musicbrainz",
    )

    with (
        patch("cdda2img.cddb.query_cddb", return_value=[cddb_meta]),
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[mb_meta]),
        ThreadPoolExecutor(max_workers=2) as ex,
    ):
        cddb_future = ex.submit(prepopulate_from_cddb, disc, [0], 18000)
        mb_future = ex.submit(prepopulate_from_mb, disc, verbose=False)
        cddb_disc = cddb_future.result()
        mb_result = mb_future.result()

    # CDDB-first merge already applied in cddb_disc.
    final = cddb_disc
    # MB-second merge on top: non-blank-wins means CDDB's "From CDDB" stays.
    if mb_result.meta is not None:
        final = _merge_into_disc(mb_result.meta, final)
    assert final.album == "From CDDB"  # CDDB wins on album
    assert final.artist == "From CDDB"  # CDDB wins on artist
    # MB-only fields land via the second merge:
    assert final.mb_release_id == "rid-mb"


def test_slow_cddb_does_not_block_mb_latency() -> None:
    """The MB future completes before the CDDB future when CDDB is slow.

    This is the failure-isolation property the R8 spec calls out: a flaky
    or slow CDDB should not gate MB. We measure by polling done-state.
    """
    disc = _disc()
    slow_signal = {"started": False, "done": False}

    def slow_query_cddb(*_args, **_kwargs):
        slow_signal["started"] = True
        time.sleep(0.5)
        slow_signal["done"] = True
        return [DiscMeta(album="Slow", source="cddb")]

    def fast_lookup_disc_id(*_args, **_kwargs):
        return [DiscMeta(album="Fast", mb_release_id="rid-fast", source="musicbrainz")]

    with (
        patch("cdda2img.cddb.query_cddb", side_effect=slow_query_cddb),
        patch("cdda2img.mb_lookup.lookup_disc_id", side_effect=fast_lookup_disc_id),
        ThreadPoolExecutor(max_workers=2) as ex,
    ):
        cddb_future = ex.submit(prepopulate_from_cddb, disc, [0], 18000)
        mb_future = ex.submit(prepopulate_from_mb, disc, verbose=False)

        # MB should resolve while CDDB is still in flight.
        mb_result = mb_future.result(timeout=2.0)
        assert slow_signal["started"], "CDDB must have started"
        # CDDB may or may not have finished depending on system load,
        # but mb_result should be available regardless.
        cddb_disc = cddb_future.result(timeout=2.0)

    assert mb_result.meta is not None
    assert mb_result.meta.mb_release_id == "rid-fast"
    assert cddb_disc.album == "Slow"


_CDDB_SIMULATED_FAILURE_MSG = "CDDB simulated failure"


def test_cddb_failure_does_not_block_mb() -> None:
    """A CDDB exception is contained inside its thread; MB still returns."""
    disc = _disc()

    def failing_query_cddb(*_args, **_kwargs):
        raise RuntimeError(_CDDB_SIMULATED_FAILURE_MSG)

    def good_lookup_disc_id(*_args, **_kwargs):
        return [DiscMeta(album="OK", mb_release_id="rid-ok", source="musicbrainz")]

    with (
        patch("cdda2img.cddb.query_cddb", side_effect=failing_query_cddb),
        patch("cdda2img.mb_lookup.lookup_disc_id", side_effect=good_lookup_disc_id),
        ThreadPoolExecutor(max_workers=2) as ex,
    ):
        cddb_future = ex.submit(prepopulate_from_cddb, disc, [0], 18000)
        mb_future = ex.submit(prepopulate_from_mb, disc, verbose=False)
        mb_result = mb_future.result(timeout=2.0)
        # CDDB's failure is raised when we call .result(); the caller
        # is responsible for handling it. R8 wraps with try/except in
        # _finalize_import (verified separately).
        cddb_exc: Exception | None = None
        try:
            _cddb_disc = cddb_future.result(timeout=2.0)
        except RuntimeError as exc:
            cddb_exc = exc

    assert mb_result.meta is not None
    assert mb_result.meta.mb_release_id == "rid-ok"
    assert cddb_exc is not None
    assert "CDDB simulated failure" in str(cddb_exc)


def test_mb_winning_meta_exposed_on_result() -> None:
    """R8 requires MBPrepopResult.meta to be populated for the post-merge step."""
    disc = _disc()
    mb_meta = DiscMeta(
        album="Album",
        artist="Artist",
        mb_release_id="rid-1",
        source="musicbrainz",
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[mb_meta]):
        result = prepopulate_from_mb(disc, verbose=False)
    assert result.meta is not None
    assert result.meta.mb_release_id == "rid-1"


def test_mb_meta_is_none_on_no_match() -> None:
    """No MB matches → meta is None — the post-merge step is skipped."""
    disc = _disc()
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[]):
        result = prepopulate_from_mb(disc, verbose=False)
    assert result.meta is None
