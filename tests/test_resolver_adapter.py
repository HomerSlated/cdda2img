"""
test_resolver_adapter.py — B4 B-1: the reproduce-today adapter (keystone).

The central guarantee: ``disc_from_resolution(resolve(baseline + meta proposals))``
reproduces ``_merge_into_disc`` EXACTLY, across **every** merged field — not just
the B0 characterization subset. If this holds, the resolver can replace the merge
fold without any behaviour change (the actual ranking change is deferred to B-6).

Also pinned here: the C2 strictness boundary (recording-level source + non-empty
mb_release_id raises, where the merge would leak) and the §11.5 skip trace.
"""

import pytest

from cdda2img.field_resolver import (
    Field,
    Source,
    Trust,
    disc_from_resolution,
    resolve,
)
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.mb_lookup import _merge_into_disc, strip_pressing_mbid
from cdda2img.rbi_format import RBIDisc, RBITocEntry
from cdda2img.resolver_adapter import (
    baseline_proposals,
    meta_to_proposals,
    trust_for,
)


def _equiv(
    meta: DiscMeta, disc: RBIDisc, source: Source = Source.MB_DISC_ID
) -> RBIDisc:
    """Assert the resolver reproduces ``_merge_into_disc(meta, disc)`` and return it.

    The equivalence is also order-independent: the same proposals resolve to the
    same disc regardless of collection order (baseline-first vs meta-first).
    """
    base_props = baseline_proposals(disc)
    meta_props = meta_to_proposals(meta, source)
    via_resolver = disc_from_resolution(resolve(base_props + meta_props), disc)
    via_reversed = disc_from_resolution(resolve(meta_props + base_props), disc)
    via_merge = _merge_into_disc(meta, disc)
    assert via_resolver == via_merge
    assert via_reversed == via_merge
    return via_resolver


def _disc(**kw: object) -> RBIDisc:
    base: dict = {"album": "", "artist": "", "tracks": []}
    base.update(kw)
    return RBIDisc(**base)  # type: ignore[arg-type]


def _toc(n: int, **kw: object) -> RBITocEntry:
    base: dict = {
        "track_number": n,
        "title": "",
        "performer": "",
        "start_frame": 0,
        "duration_frames": 100,
        "pregap_frames": 0,
        "isrc": None,
    }
    base.update(kw)
    return RBITocEntry(**base)  # type: ignore[arg-type]


def _full_disc() -> RBIDisc:
    """A disc with every merged field populated (+ physical fields, for C1)."""
    return RBIDisc(
        album="Disc Album",
        artist="Disc Artist",
        catalog="0000000000000",
        disc_number=1,
        disc_total=1,
        release_date="1987",
        catalog_number="CID U2 6",
        label="Island",
        country="GB",
        original_release_date="1987",
        mb_release_id="disc-rel",
        mb_release_group_id="disc-rg",
        discogs_release_id=111,
        set_title="Disc Set",
        tracks=[
            _toc(1, title="D1", performer="DP1", isrc="GBAYE0000123"),
            _toc(2, title="D2", performer="DP2", isrc=None),
        ],
        pre_emphasis=True,
        low_dynamic_range=True,
        cdtext_catalog_ref="REF",
    )


def _full_meta() -> DiscMeta:
    """A meta with every merged field populated, all values distinct from the disc."""
    return DiscMeta(
        album="Meta Album",
        artist="Meta Artist",
        catalog="1111111111111",
        disc_number=2,
        disc_total=3,
        release_date="1990",
        catalog_number="MCN-2",
        label="Meta Label",
        country="US",
        original_release_date="1985",
        mb_release_id="meta-rel",
        mb_release_group_id="meta-rg",
        discogs_release_id=222,
        set_title="Meta Set",
        tracks=[
            TrackMeta(number=1, title="M1", performer="MP1", isrc="USAR10400486"),
            TrackMeta(number=2, title="M2", performer="MP2", isrc="USAR10400487"),
        ],
    )


# === Full-coverage equivalence — every merged field, both polarities =========


def test_equiv_full_disc_full_meta_disc_priority_everywhere():
    """All fields populated both sides: disc-priority wins everywhere EXCEPT
    disc_number/disc_total (meta-priority). One assertion proves the whole map."""
    out = _equiv(_full_meta(), _full_disc())
    # disc-priority fields kept from the disc
    assert out.album == "Disc Album"
    assert out.artist == "Disc Artist"
    assert out.catalog == "0000000000000"
    assert out.release_date == "1987"
    assert out.catalog_number == "CID U2 6"
    assert out.label == "Island"
    assert out.country == "GB"
    assert out.original_release_date == "1987"
    assert out.mb_release_id == "disc-rel"
    assert out.mb_release_group_id == "disc-rg"
    assert out.discogs_release_id == 111
    assert out.set_title == "Disc Set"
    assert [t.title for t in out.tracks] == ["D1", "D2"]
    assert [t.performer for t in out.tracks] == ["DP1", "DP2"]
    assert [t.isrc for t in out.tracks] == ["GBAYE0000123", "USAR10400487"]
    # the meta-priority exception
    assert out.disc_number == 2
    assert out.disc_total == 3
    # C1: physical fields survive
    assert out.pre_emphasis is True
    assert out.cdtext_catalog_ref == "REF"


def test_equiv_blank_disc_full_meta_meta_fills_everything():
    """All disc fields blank: meta fills every field."""
    blank = _disc(tracks=[_toc(1), _toc(2)])
    out = _equiv(_full_meta(), blank)
    assert out.album == "Meta Album"
    assert out.artist == "Meta Artist"
    assert out.catalog == "1111111111111"
    assert out.disc_number == 2
    assert out.disc_total == 3
    assert out.label == "Meta Label"
    assert out.mb_release_id == "meta-rel"
    assert out.discogs_release_id == 222
    assert [t.title for t in out.tracks] == ["M1", "M2"]
    assert [t.isrc for t in out.tracks] == ["USAR10400486", "USAR10400487"]


def test_equiv_empty_meta_is_identity_on_full_disc():
    """An all-None meta leaves a populated disc unchanged."""
    out = _equiv(DiscMeta(), _full_disc())
    assert out == _full_disc()


# === The meta-priority abstention path (disc_number / disc_total) ============


def test_equiv_disc_total_meta_wins_over_present_disc():
    out = _equiv(DiscMeta(disc_total=4), _disc(disc_total=1))
    assert out.disc_total == 4


def test_equiv_disc_total_base_preserved_when_meta_none():
    out = _equiv(DiscMeta(disc_total=None), _disc(disc_total=7))
    assert out.disc_total == 7


def test_equiv_disc_number_meta_wins_over_present_disc():
    out = _equiv(DiscMeta(disc_number=5), _disc(disc_number=1))
    assert out.disc_number == 5


# === mb_release_id: proposed from DISC_ID, skipped from recording-level ======


def test_equiv_mb_release_id_from_disc_id_source():
    """A DISC_ID source may propose mb_release_id; disc-priority still applies."""
    disc = _disc(mb_release_id="on-disc")
    out = _equiv(DiscMeta(mb_release_id="from-mb"), disc, source=Source.MB_DISC_ID)
    assert out.mb_release_id == "on-disc"


def test_equiv_mb_release_id_recording_level_post_strip():
    """Live domain: a recording-level meta has been stripped, so mb_release_id is
    None and is skipped — no C2 raise — and equivalence holds."""
    disc = _full_disc()  # disc.mb_release_id = "disc-rel"
    meta = strip_pressing_mbid(_full_meta())  # meta.mb_release_id -> None
    out = _equiv(meta, disc, source=Source.DURATION)
    assert out.mb_release_id == "disc-rel"  # disc value preserved
    assert out.mb_release_group_id == "disc-rg"  # group id (not stripped) still disc


def test_equiv_mb_release_id_both_none_recording_level():
    disc = _disc(mb_release_id=None)
    meta = strip_pressing_mbid(DiscMeta(mb_release_group_id="rg"))
    out = _equiv(meta, disc, source=Source.ACOUSTID)
    assert out.mb_release_id is None
    assert out.mb_release_group_id == "rg"


def test_recording_level_nonempty_mb_release_id_raises_c2():
    """OFF-domain strictness: a recording-level source with a *non-empty*
    mb_release_id raises (the resolver is stricter than the leaky merge)."""
    leaky = DiscMeta(mb_release_id="leaked-pressing")
    for src in (Source.DURATION, Source.ACOUSTID, Source.ISRC):
        with pytest.raises(ValueError, match="C2 violation"):
            meta_to_proposals(leaky, src)


# === ISRC: validation + normalisation on the disc side =======================


def test_equiv_isrc_disc_valid_wins():
    disc = _disc(tracks=[_toc(1, isrc="USAR10400486")])
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="GBAYE0000123")])
    out = _equiv(meta, disc)
    assert out.tracks[0].isrc == "USAR10400486"


def test_equiv_isrc_disc_absent_uses_meta():
    disc = _disc(tracks=[_toc(1, isrc=None)])
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="GBAYE0000123")])
    assert _equiv(meta, disc).tracks[0].isrc == "GBAYE0000123"


def test_equiv_isrc_disc_invalid_falls_back_to_meta():
    disc = _disc(tracks=[_toc(1, isrc="NOTVALID")])
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="GBAYE0000123")])
    assert _equiv(meta, disc).tracks[0].isrc == "GBAYE0000123"


def test_equiv_isrc_disc_normalised_form_proposed():
    """A hyphenated/lowercase disc ISRC is normalised by _merge; the resolver must
    propose the same normalised value to stay equivalent."""
    disc = _disc(tracks=[_toc(1, isrc="gb-aye-0000123")])
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="USAR10400486")])
    assert _equiv(meta, disc).tracks[0].isrc == "GBAYE0000123"


# === KNOWN B-1 DIVERGENCES from _merge_into_disc (must resolve before B-4 flip)
# Root cause (advisor 2026-06-22): _merge *rebuilds* a meta-matched track entry,
# so a disc-side value that validates-to-empty gets nulled; the resolver *patches*
# only proposed fields and otherwise keeps the base. These are pinned as
# strict-xfail equivalence assertions so they (a) stay visible, (b) keep the suite
# green, and (c) self-trip the moment a fix or a conscious documented divergence
# (matching the discogs_release_id==0 treatment) lands and makes them pass.


@pytest.mark.xfail(
    strict=True,
    reason="B-1 divergence: invalid disc ISRC + matched meta track w/ empty ISRC. "
    "_merge rebuilds and nulls the bad ISRC; resolver patches and keeps it. "
    "Resolve before the B-4 flip.",
)
def test_equiv_isrc_invalid_disc_matched_meta_empty_isrc_DIVERGES():
    disc = _disc(tracks=[_toc(1, title="D1", isrc="GARBAGE")])
    # meta matches track 1 (so _merge rebuilds it) but carries no ISRC.
    meta = DiscMeta(tracks=[TrackMeta(number=1, title="M1", isrc=None)])
    # _merge -> isrc None (scrubbed); resolver -> "GARBAGE" (kept).
    _equiv(meta, disc)


@pytest.mark.xfail(
    strict=True,
    reason="B-1 divergence: duplicate track numbers in meta.tracks. _merge's "
    "meta_by_num dict is last-wins; resolver's equal-trust max is first-wins. "
    "Resolve before the B-4 flip.",
)
def test_equiv_duplicate_meta_track_number_DIVERGES():
    disc = _disc(tracks=[_toc(1, isrc=None)])
    meta = DiscMeta(
        tracks=[
            TrackMeta(number=1, isrc="AAAA10400486"),  # first
            TrackMeta(number=1, isrc="BBBB10400487"),  # last (merge picks this)
        ]
    )
    # _merge -> "BBBB10400487" (last); resolver -> "AAAA10400486" (first).
    _equiv(meta, disc)


# === sentinel + track-verbatim quirks via the equivalence path ==============


def test_equiv_artist_unknown_sentinel_yields_to_meta():
    out = _equiv(DiscMeta(artist="Real"), _disc(artist="Unknown Artist"))
    assert out.artist == "Real"


def test_equiv_track_without_meta_match_kept_verbatim():
    disc = _disc(tracks=[_toc(1, title="Keep", performer="Unknown Artist")])
    out = _equiv(DiscMeta(tracks=[]), disc)
    assert out.tracks[0].title == "Keep"
    assert out.tracks[0].performer == "Unknown Artist"  # no meta to fill it


def test_equiv_track_title_disc_priority_blank_filled():
    disc = _disc(tracks=[_toc(1, title="On Disc"), _toc(2, title="")])
    meta = DiscMeta(
        tracks=[TrackMeta(number=1, title="MB1"), TrackMeta(number=2, title="MB2")]
    )
    out = _equiv(meta, disc)
    assert [t.title for t in out.tracks] == ["On Disc", "MB2"]


# === trust_for — the flat two-tier reproduce map ============================


def test_trust_for_baseline_outranks_meta():
    assert trust_for(Source.BASELINE, Field.ALBUM, "rip") == Trust.MANUAL
    assert trust_for(Source.MB_DISC_ID, Field.ALBUM, "rip") < Trust.MANUAL


def test_trust_for_all_meta_sources_equal_in_b1():
    pipe = "create"
    levels = {
        trust_for(s, Field.ALBUM, pipe)
        for s in (Source.MB_DISC_ID, Source.DISCOGS, Source.CDDB, Source.DURATION)
    }
    assert len(levels) == 1  # flat: every meta source at the same tier (until B-6)


# === §11.5 skip trace — silently-dropped values become visible ==============


def test_baseline_skips_recorded_with_reasons():
    disc = _disc(
        album="",
        artist="Unknown Artist",
        disc_number=1,
        tracks=[_toc(1, title="", performer="Unknown Artist", isrc="NOTVALID")],
    )
    skips: list = []
    baseline_proposals(disc, skips=skips)
    reasons = {(s.field, s.reason) for s in skips}
    assert (Field.ALBUM, "empty") in reasons
    assert (Field.ARTIST, "unknown-artist-sentinel") in reasons
    assert (Field.DISC_NUMBER, "abstain-meta-priority") in reasons
    assert (Field.DISC_TOTAL, "abstain-meta-priority") in reasons
    assert (Field.TRACK_TITLE, "empty") in reasons
    assert (Field.TRACK_PERFORMER, "unknown-artist-sentinel") in reasons
    assert (Field.TRACK_ISRC, "invalid-isrc") in reasons


def test_meta_skips_empty_recorded():
    skips: list = []
    meta_to_proposals(DiscMeta(album="A", artist=None), Source.MB_DISC_ID, skips=skips)
    reasons = {(s.field, s.reason) for s in skips}
    assert (Field.ARTIST, "empty") in reasons
    # a present field is NOT skipped
    assert (Field.ALBUM, "empty") not in reasons
