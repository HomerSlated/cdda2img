"""
test_parallel_pre_menu.py — lookup precedence + parallel-prepop integration tests.

The remote-metadata lookups live in ``cdda2img._run_metadata_lookups``, which
runs the CDDB query in parallel with the MB lookup and merges every source into
the disc in precedence order. The properties verified here:

  1. CDDB is the LOWEST-precedence source — MusicBrainz (and any other source)
     overwrites it; CDDB only fills fields nothing richer provided.
  2. A slow or failing CDDB query never gates or breaks the rip (the query is
     best-effort; its result is applied last and swallowed on error).

These bind to the real helper (not an inline reconstruction of the merge), so a
regression in the actual ordering is caught.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from cdda2img.cdda2img import _run_metadata_lookups
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.mb_lookup import prepopulate_from_mb
from cdda2img.rbi_format import RBIDisc, RBITocEntry

_PCM = Path("/nonexistent.pcm")  # never read: Discogs/AcoustID are stubbed out


def _disc() -> RBIDisc:
    """A disc with no embedded (CD-Text) metadata — every field blank."""
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


def _run(disc: RBIDisc, prov: dict[str, str]):
    """Drive the real helper with Discogs/AcoustID stubbed to identity."""
    return _run_metadata_lookups(
        disc,
        _PCM,
        prov,
        do_cddb=True,
        cddb_track_lsns=[0],
        cddb_disc_last_lsn=18000,
        cddb_server=None,
        cddb_verbose=False,
        mb_verbose=False,
        ui=None,
    )


def test_cddb_is_lowest_precedence_mb_overwrites_it() -> None:
    """MB overwrites every field CDDB also provides; CDDB fills only the gaps.

    Mirrors the real symptom: CDDB Title-Cases ("Of"), MB is canonical ("of").
    With CDDB demoted to last, MB's value must win. The release_date assertion
    proves CDDB still works as a last-resort gap-filler for fields no higher
    source supplied.
    """
    disc = _disc()
    cddb_meta = DiscMeta(
        album="From CDDB",
        artist="From CDDB",
        release_date="2009",  # MB leaves this blank → CDDB fills it
        source="cddb",
        tracks=[TrackMeta(number=1, title="Boulevard Of Broken Dreams")],  # cap "Of"
    )
    mb_meta = DiscMeta(
        album="From MB",
        artist="From MB",
        mb_release_id="rid-mb",
        source="musicbrainz",
        tracks=[TrackMeta(number=1, title="Boulevard of Broken Dreams")],  # "of"
    )
    prov: dict[str, str] = {}
    with (
        patch("cdda2img.cddb.query_cddb", return_value=[cddb_meta]),
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[mb_meta]),
        patch(
            "cdda2img.cdda2img._prepopulate_from_discogs",
            side_effect=lambda d, *a, **k: d,
        ),
        patch(
            "cdda2img.cdda2img._r6_acoustid_corroborate",
            side_effect=lambda d, *a, **k: d,
        ),
    ):
        result, _mb_result = _run(disc, prov)

    # MB wins every contested field.
    assert result.album == "From MB"
    assert result.artist == "From MB"
    assert result.tracks[0].title == "Boulevard of Broken Dreams"
    assert result.mb_release_id == "rid-mb"
    # CDDB still fills a field nothing else provided.
    assert result.release_date == "2009"


def test_cddb_query_failure_does_not_break_lookups() -> None:
    """A CDDB query exception is swallowed; MB results still land."""
    disc = _disc()
    mb_meta = DiscMeta(
        album="From MB", artist="From MB", mb_release_id="rid-ok", source="musicbrainz"
    )

    def boom(*_a, **_k):
        msg = "CDDB simulated failure"
        raise RuntimeError(msg)

    prov: dict[str, str] = {}
    with (
        patch("cdda2img.cddb.query_cddb", side_effect=boom),
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[mb_meta]),
        patch(
            "cdda2img.cdda2img._prepopulate_from_discogs",
            side_effect=lambda d, *a, **k: d,
        ),
        patch(
            "cdda2img.cdda2img._r6_acoustid_corroborate",
            side_effect=lambda d, *a, **k: d,
        ),
    ):
        result, _mb_result = _run(disc, prov)

    assert result.album == "From MB"
    assert result.mb_release_id == "rid-ok"
    # R12: CDDB attempted but produced no usable data.
    assert prov["lookup_status_cddb"] == "empty"


def test_slow_cddb_does_not_gate_mb() -> None:
    """A slow CDDB query runs concurrently with MB, not serially before it.

    The helper blocks until both finish, but the two run in parallel — so the
    wall time is ~max(cddb, mb), not their sum. We assert MB data lands and the
    elapsed time is closer to the single CDDB delay than to double it.
    """
    disc = _disc()
    mb_meta = DiscMeta(
        album="From MB",
        artist="From MB",
        mb_release_id="rid-fast",
        source="musicbrainz",
    )

    def slow_cddb(*_a, **_k):
        time.sleep(0.3)
        return [DiscMeta(album="From CDDB", source="cddb")]

    def slow_mb(*_a, **_k):
        time.sleep(0.3)
        return [mb_meta]

    prov: dict[str, str] = {}
    with (
        patch("cdda2img.cddb.query_cddb", side_effect=slow_cddb),
        patch("cdda2img.mb_lookup.lookup_disc_id", side_effect=slow_mb),
        patch(
            "cdda2img.cdda2img._prepopulate_from_discogs",
            side_effect=lambda d, *a, **k: d,
        ),
        patch(
            "cdda2img.cdda2img._r6_acoustid_corroborate",
            side_effect=lambda d, *a, **k: d,
        ),
    ):
        start = time.monotonic()
        result, _mb_result = _run(disc, prov)
        elapsed = time.monotonic() - start

    assert result.mb_release_id == "rid-fast"
    assert elapsed < 0.55, "CDDB and MB must run concurrently, not serially"


def test_mb_winning_meta_exposed_on_result() -> None:
    """prepopulate_from_mb exposes the winning meta for the precedence merge."""
    disc = _disc()
    mb_meta = DiscMeta(
        album="Album", artist="Artist", mb_release_id="rid-1", source="musicbrainz"
    )
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[mb_meta]):
        result = prepopulate_from_mb(disc, verbose=False)
    assert result.meta is not None
    assert result.meta.mb_release_id == "rid-1"


def test_mb_meta_is_none_on_no_match() -> None:
    """No MB matches → meta is None."""
    disc = _disc()
    with patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[]):
        result = prepopulate_from_mb(disc, verbose=False)
    assert result.meta is None
