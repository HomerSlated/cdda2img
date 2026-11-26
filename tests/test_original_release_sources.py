"""Where the original release comes from, and why MusicBrainz's answer was refused
(trust_model_design.md §13.8, kgr 2026-11-26)."""

from __future__ import annotations

import pytest

from cdda2img import original_release as o
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.rbi_format import RBIDisc, RBITocEntry


def _disc() -> RBIDisc:
    disc = RBIDisc(
        album="Brothers in Arms",
        artist="Dire Straits",
        tracks=[
            RBITocEntry(
                track_number=i,
                title=f"T{i}",
                performer="",
                start_frame=0,
                duration_frames=7500,
            )
            for i in (1, 2)
        ],
    )
    disc.mb_release_id = "584fc120"
    disc.mb_release_group_id = "b02f651e"
    return disc


@pytest.mark.parametrize(
    ("meta_tracks", "reason"),
    [
        ([TrackMeta(number=1, title="T1")], "track_count"),
        (
            [
                TrackMeta(number=1, title="T1", duration_ms=1),
                TrackMeta(number=2, title="T2", duration_ms=1),
            ],
            "durations",
        ),
        (
            [
                TrackMeta(number=1, title="Nothing alike"),
                TrackMeta(number=2, title="At all here"),
            ],
            "title_fuzz",
        ),
    ],
)
def test_each_r3_gate_names_itself(meta_tracks, reason) -> None:
    meta = DiscMeta(mb_release_id="584fc120", tracks=meta_tracks)
    assert o._r3_reject_reason(meta, _disc()) == reason
    assert o._verify_release_matches_disc(meta, _disc()) is False


def test_a_rejection_is_recorded_and_discogs_supplies_the_original(monkeypatch) -> None:
    monkeypatch.setattr(
        o, "_resolve_rg_original", lambda *_a, **_k: ("Brothers in Arms", 1985)
    )
    monkeypatch.setattr(o, "_rg_path_reject_reason", lambda *_a: "title_fuzz")
    monkeypatch.setattr(
        o, "find_original_release_fuzzy", lambda _d: (False, None, None)
    )
    monkeypatch.setattr("cdda2img.discogs_lookup.is_available", lambda: True)
    monkeypatch.setattr(
        "cdda2img.mb_lookup.discogs_master_of_release_group", lambda _g: 23684
    )
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.master_original", lambda _m: ("Brothers In Arms", 1985)
    )
    disc, prov = _disc(), {}
    o.populate_original_release(disc, provenance=prov)
    assert prov["original_release_rejected"] == "title_fuzz"
    assert prov["original_release_source"] == "discogs_master"
    assert (disc.original_release_title, disc.original_release_year) == (
        "Brothers In Arms",
        1985,
    )


def test_the_release_group_answer_records_its_source(monkeypatch) -> None:
    monkeypatch.setattr(
        o, "_resolve_rg_original", lambda *_a, **_k: ("Brothers in Arms", 1985)
    )
    monkeypatch.setattr(o, "_rg_path_reject_reason", lambda *_a: None)
    disc, prov = _disc(), {}
    o.populate_original_release(disc, provenance=prov)
    assert prov == {"original_release_source": "mb_release_group"}
    assert disc.original_release_year == 1985


def test_nothing_found_anywhere_leaves_it_unset(monkeypatch) -> None:
    monkeypatch.setattr(o, "_resolve_rg_original", lambda *_a, **_k: None)
    monkeypatch.setattr(
        o, "find_original_release_fuzzy", lambda _d: (False, None, None)
    )
    monkeypatch.setattr("cdda2img.discogs_lookup.is_available", lambda: True)
    monkeypatch.setattr(
        "cdda2img.mb_lookup.discogs_master_of_release_group", lambda _g: None
    )
    disc, prov = _disc(), {}
    o.populate_original_release(disc, provenance=prov)
    assert not disc.original_release_found
    assert prov == {"original_release_rejected": "release_group"}


def test_the_duration_gate_counts_the_next_tracks_pre_gap() -> None:
    """MusicBrainz measures INDEX 01 to INDEX 01, so its track 1 includes track 2's
    pre-gap. Tracy Chapman: 17 s of gaps were read as a 17 s mismatch."""
    gap = 3 * 75  # three seconds before track 2
    disc = RBIDisc(
        album="A",
        artist="B",
        tracks=[
            RBITocEntry(
                track_number=1,
                title="",
                performer="",
                start_frame=0,
                duration_frames=75 * 200,
            ),
            RBITocEntry(
                track_number=2,
                title="",
                performer="",
                start_frame=75 * 203,
                pregap_frames=gap,
                duration_frames=75 * 100,
            ),
        ],
    )
    # Both of MusicBrainz's lengths run INDEX 01 to INDEX 01: track 1 is 203 s.
    meta = DiscMeta(
        mb_release_id="x",
        tracks=[
            TrackMeta(number=1, duration_ms=203_000),
            TrackMeta(number=2, duration_ms=100_000),
        ],
    )
    assert o._r3_reject_reason(meta, disc) is None
    # Control: 20 s of real difference is still caught.
    meta_far = DiscMeta(
        mb_release_id="x",
        tracks=[
            TrackMeta(number=1, duration_ms=223_000),
            TrackMeta(number=2, duration_ms=100_000),
        ],
    )
    assert o._r3_reject_reason(meta_far, disc) == "durations"
