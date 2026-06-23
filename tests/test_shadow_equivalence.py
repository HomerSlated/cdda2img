"""
test_shadow_equivalence.py — B4 B-3 part 2 / B-4: resolver-vs-legacy equivalence.

B-3 part 1 proved the resolver reproduces a *reconstruction* of the
``_run_metadata_lookups`` merge sequence (``test_merge_sequence``). This proves it
reproduces the **real running function**, and survives the B-4 flip as a
non-tautological check.

Post-B-4 (the flip landed): ``_run_metadata_lookups`` returns the resolver's
committed disc, and the legacy per-source merge chain is retained as the live
equivalence ORACLE, stashed at ``_shadow_out["merged"]``. These tests drive the
real function with ``_shadow_out`` enabled and assert the returned (resolver) disc
equals that legacy oracle (``live == shadow["merged"]``) — proving the flip did not
change the committed output on clean data. Comparing against ``shadow["disc"]``
would be tautological now (it *is* the returned disc), so the oracle is the merge.

What this newly tests against real code (vs. the part-1 reconstruction, advisor
2026-06-23):

- the **full** ``_r6_acoustid_corroborate`` wrapper is field-neutral — not just the
  inner ``_r6_tally_and_merge`` (covered by ``test_merge_sequence``). Here it runs
  for real, forced down its ``is_available()==False`` early-return (the live path on
  any tokenless host), and the disc must be untouched;
- ``_prepopulate_from_discogs`` (phase A canonical-MCN overwrite + phase B Discogs
  merge) runs for real and the captured ``(chosen, hit)`` reproduce its disc effect;
- MB's disc effect equals ``_merge_into_disc(mb_result.meta, baseline)`` — i.e.
  ``prepopulate_from_mb`` does nothing to the disc beyond merging the exposed meta;
- the baseline snapshot + stage-7 / CDDB capture are wired correctly.

The fixtures are clean ``DiscMeta`` (None-or-nonempty fields, valid/None ISRCs,
unique track numbers), so exact ``==`` holds and neither of the two strict-xfail
divergences pinned in ``test_resolver_adapter`` is reachable — no allow-list needed.
``_discogs_barcode_corroborate`` is PROV-only (typed ``-> None``, return ignored at
the call site), so it cannot affect the disc; its network is neutralised, not stubbed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from cdda2img.cdda2img import _run_metadata_lookups
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.rbi_format import RBIDisc, RBITocEntry

_PCM = Path("/nonexistent.pcm")  # never read: every network source is stubbed/off


def _disc(album: str = "", artist: str = "", *, catalog: str | None = None) -> RBIDisc:
    return RBIDisc(
        album=album,
        artist=artist,
        catalog=catalog,
        tracks=[
            RBITocEntry(
                track_number=1,
                title="",
                performer="",
                start_frame=0,
                duration_frames=18000,
            ),
            RBITocEntry(
                track_number=2,
                title="",
                performer="",
                start_frame=18000,
                duration_frames=18000,
            ),
        ],
    )


def _run_with_shadow(disc: RBIDisc) -> tuple[RBIDisc, dict[str, object]]:
    """Drive the real helper with the shadow seam enabled; return (live, shadow)."""
    shadow: dict[str, object] = {}
    prov: dict[str, str] = {}
    live, _mb = _run_metadata_lookups(
        disc,
        _PCM,
        prov,
        do_cddb=True,
        cddb_track_lsns=[0],
        cddb_disc_last_lsn=36000,
        cddb_server=None,
        cddb_verbose=False,
        mb_verbose=False,
        preferred_country=[],
        ui=None,
        _shadow_out=shadow,
    )
    return live, shadow


def _assert_equiv(live: RBIDisc, shadow: dict[str, object]) -> None:
    """Post-B-4-flip equivalence check (non-tautological).

    After the flip ``live`` (the returned disc) IS the resolver's committed output,
    so comparing it to ``shadow["disc"]`` (also the resolver) proves nothing. The
    real oracle is the retained legacy merge chain, stashed at ``shadow["merged"]``;
    asserting ``live == merged`` checks the resolver still reproduces the fold on
    clean data. ``Resolution.contenders`` surfaces *which* field/source diverged.
    """
    merged = shadow["merged"]
    assert isinstance(merged, RBIDisc)
    if live != merged:
        # contenders is the decision trace: per field, every proposal that competed
        # (trust-descending) — so a mismatch names the diverging source.
        from cdda2img.field_resolver import Resolution

        resolution = shadow["resolution"]
        assert isinstance(resolution, Resolution)
        diffs = [
            f"{f}: resolver={getattr(live, f)!r} merged={getattr(merged, f)!r}"
            for f in (
                "album",
                "artist",
                "catalog",
                "label",
                "country",
                "release_date",
                "mb_release_id",
                "discogs_release_id",
            )
            if getattr(live, f) != getattr(merged, f)
        ]
        tdiffs = [
            f"track {lt.track_number}: resolver={lt!r} merged={rt!r}"
            for lt, rt in zip(live.tracks, merged.tracks, strict=False)
            if lt != rt
        ]
        msg = (
            "resolver (committed) != legacy merge oracle\n"
            + "\n".join(diffs + tdiffs)
            + f"\ncontenders={resolution.contenders}"
        )
        raise AssertionError(msg)


def test_shadow_multi_source_mb_discogs_cddb() -> None:
    """MB + canonical MCN + Discogs merge + CDDB gap-fill — the full happy path.

    MB pins a release (so stage-7 does not fire), the on-disc valid MCN drives the
    §10 canonical-MCN pick, Discogs returns a single album-matching hit that
    merges, and CDDB fills a field nothing richer supplied. The real AcoustID and
    Discogs-barcode corroborate wrappers run (network off) and must not perturb
    the disc.
    """
    mcn = "0075992377423"
    disc = _disc("Eliminator", "ZZ Top", catalog=mcn)
    mb_meta = DiscMeta(
        album="Eliminator",
        artist="ZZ Top",
        mb_release_id="rid-mb",
        mb_release_group_id="rg-mb",
        source="musicbrainz",
        tracks=[
            TrackMeta(number=1, title="Gimme All Your Lovin'", isrc="USRC18300001"),
            TrackMeta(number=2, title="Got Me Under Pressure"),
        ],
    )
    discogs_hit = DiscMeta(
        album="Eliminator",
        artist="ZZ Top",
        label="Warner Bros.",
        country="US",
        catalog=mcn,
    )
    cddb_meta = DiscMeta(
        album="From CDDB",  # loses to MB
        release_date="1983",  # nothing richer set it → CDDB fills it
        source="cddb",
        tracks=[TrackMeta(number=2, performer="ZZ Top")],
    )
    with (
        patch("cdda2img.cddb.query_cddb", return_value=[cddb_meta]),
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[mb_meta]),
        patch("cdda2img.acoustid_lookup.is_available", return_value=False),
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch("cdda2img.discogs_lookup.search_by_barcode", return_value=[discogs_hit]),
        # Keep the (real, PROV-only) barcode corroborate network-free.
        patch("cdda2img.mb_lookup.discogs_link_and_barcode", return_value=(None, None)),
    ):
        live, shadow = _run_with_shadow(disc)

    # Sanity: the live merge actually did what the scenario intends.
    assert live.album == "Eliminator"  # MB won
    assert live.label == "Warner Bros."  # Discogs filled
    assert live.release_date == "1983"  # CDDB filled the last gap
    assert live.catalog == mcn
    _assert_equiv(live, shadow)


def test_shadow_stage7_fires_discogs_off() -> None:
    """MB finds nothing → stage-7 duration match merges; Discogs unavailable; CDDB last.

    Exercises the stage-7 capture (``strip_pressing_mbid``) and the no-Discogs
    branch of ``_prepopulate_from_discogs`` (chosen=None, hit=None).
    """
    disc = _disc("Seed Album", "Seed Artist")  # embedded seed lets stage-7 fire
    dur_meta = DiscMeta(
        album="Duration Match",
        artist="Seed Artist",
        mb_release_id="rid-dur",  # stripped before merge
        mb_release_group_id="rg-dur",
        release_date="2004",
        source="musicbrainz",
        tracks=[TrackMeta(number=1, title="Track One")],
    )
    cddb_meta = DiscMeta(
        album="From CDDB",  # loses to the duration match
        label="CDDB Label",  # nothing else set it → CDDB fills
        source="cddb",
    )
    with (
        patch("cdda2img.cddb.query_cddb", return_value=[cddb_meta]),
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[]),
        patch("cdda2img.mb_lookup.duration_match_lookup", return_value=dur_meta),
        patch("cdda2img.acoustid_lookup.is_available", return_value=False),
        patch("cdda2img.discogs_lookup.is_available", return_value=False),
    ):
        live, shadow = _run_with_shadow(disc)

    # The seed album survives (fill-blank: baseline non-blank wins); stage-7 fills
    # the blank release_date, outranking CDDB; CDDB fills the last blank.
    assert live.album == "Seed Album"
    assert live.release_date == "2004"  # stage-7 filled, ahead of CDDB
    assert live.label == "CDDB Label"  # CDDB filled the gap
    assert live.mb_release_id is None  # pressing MBID never baked in (C2)
    _assert_equiv(live, shadow)


def test_shadow_mb_only_no_other_sources() -> None:
    """Degenerate path: MB match, no Discogs, no CDDB, no stage-7.

    Proves the baseline-snapshot + MB-meta capture alone reproduce the disc, and
    that the real AcoustID wrapper (network off) is a no-op.
    """
    disc = _disc("Album", "Artist")
    mb_meta = DiscMeta(
        album="Album",
        artist="Artist",
        mb_release_id="rid-only",
        mb_release_group_id="rg-only",
        source="musicbrainz",
        tracks=[TrackMeta(number=1, title="One"), TrackMeta(number=2, title="Two")],
    )
    with (
        patch("cdda2img.cddb.query_cddb", return_value=[]),
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[mb_meta]),
        patch("cdda2img.acoustid_lookup.is_available", return_value=False),
        patch("cdda2img.discogs_lookup.is_available", return_value=False),
    ):
        live, shadow = _run_with_shadow(disc)

    assert live.mb_release_id == "rid-only"
    assert [t.title for t in live.tracks] == ["One", "Two"]
    _assert_equiv(live, shadow)


def test_resolver_failure_falls_back_to_legacy_merge() -> None:
    """B-4 never-fail guard: if the resolver path raises (an unforeseen C2, a
    malformed meta during proposal construction, etc.), _run_metadata_lookups must
    NOT abort the rip — it logs and falls back to the faithful legacy merge fold.

    The guard wraps the whole resolver path including the proposal BUILD, which is
    where C2 actually raises (FieldProposal.__post_init__). We inject the failure at
    _collect_metadata_proposals to prove the build is inside the net.
    """
    disc = _disc("Album", "Artist")
    mb_meta = DiscMeta(
        album="Album",
        artist="Artist",
        mb_release_id="rid",
        mb_release_group_id="rg",
        source="musicbrainz",
        tracks=[TrackMeta(number=1, title="One"), TrackMeta(number=2, title="Two")],
    )
    with (
        patch("cdda2img.cddb.query_cddb", return_value=[]),
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[mb_meta]),
        patch("cdda2img.acoustid_lookup.is_available", return_value=False),
        patch("cdda2img.discogs_lookup.is_available", return_value=False),
        patch(
            "cdda2img.cdda2img._collect_metadata_proposals",
            side_effect=RuntimeError("injected build failure"),
        ),
    ):
        live, shadow = _run_with_shadow(disc)  # must not raise

    # Fell back to the legacy fold (MB applied), not aborted or blanked.
    assert live.mb_release_id == "rid"
    assert live.album == "Album"
    assert [t.title for t in live.tracks] == ["One", "Two"]
    assert live == shadow["merged"]  # committed == fallback == legacy fold
    # Confirm the except branch was taken: no resolution, empty proposals.
    assert shadow["resolution"] is None
    assert shadow["proposals"] == []


def test_shadow_b6_discogs_catalogue_beats_mb_diverges_from_legacy() -> None:
    """B-6 in the real pipeline: when MB and Discogs both supply a catalogue field
    (label here), the committed (resolver) disc takes Discogs — and INTENTIONALLY
    diverges from the legacy merge oracle, which took MB by call order. This is the
    one case where ``returned != shadow["merged"]`` is correct, so it is asserted
    directly, not via ``_assert_equiv``."""
    mcn = "0075992377423"
    disc = _disc("Eliminator", "ZZ Top", catalog=mcn)
    mb_meta = DiscMeta(
        album="Eliminator",
        artist="ZZ Top",
        mb_release_id="rid-mb",
        mb_release_group_id="rg-mb",
        label="MB Label",  # MB also carries a label …
        country="DE",
        source="musicbrainz",
        tracks=[TrackMeta(number=1, title="Gimme All Your Lovin'")],
    )
    discogs_hit = DiscMeta(
        album="Eliminator",
        artist="ZZ Top",
        label="Warner Bros.",  # … and Discogs disagrees (the catalogue authority)
        country="GB",
        catalog=mcn,
    )
    with (
        patch("cdda2img.cddb.query_cddb", return_value=[]),
        patch("cdda2img.mb_lookup.lookup_disc_id", return_value=[mb_meta]),
        patch("cdda2img.acoustid_lookup.is_available", return_value=False),
        patch("cdda2img.discogs_lookup.is_available", return_value=True),
        patch("cdda2img.discogs_lookup.search_by_barcode", return_value=[discogs_hit]),
        patch("cdda2img.mb_lookup.discogs_link_and_barcode", return_value=(None, None)),
    ):
        live, shadow = _run_with_shadow(disc)

    merged = shadow["merged"]
    assert isinstance(merged, RBIDisc)
    # Resolver (committed): Discogs wins label (the catalogue pair).
    assert live.label == "Warner Bros."
    # Legacy oracle: MB won label by merge call order — the intended B-6 divergence.
    assert merged.label == "MB Label"
    assert live != merged
    # country is EXCLUDED from the override (couples with §10 preferred_country), so
    # MB wins it in BOTH paths — committed and oracle agree.
    assert live.country == "DE"
    assert merged.country == "DE"
    # Non-catalogue fields are unaffected: MB still wins album.
    assert live.album == "Eliminator"
