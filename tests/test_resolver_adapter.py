"""
test_resolver_adapter.py — B4 B-1: the reproduce-today adapter (keystone).

The central guarantee: ``disc_from_resolution(resolve(baseline + meta proposals))``
reproduces ``_merge_into_disc`` across **every** merged field (not just the B0
characterization subset) on the live in-domain space — so the resolver can replace
the merge fold without behaviour change (the ranking change is deferred to B-6).

Three divergences are known and partitioned out of that space: two pinned as
strict-xfail examples (invalid-disc-ISRC scrub; duplicate track numbers) and one
representational class (falsy-but-present ``""``/``0`` on optional fields) excluded
by the Hypothesis strategy, which models RBIDisc's None-when-absent invariant and
is exact within it. Also pinned: the C2 strictness boundary (recording-level source
+ non-empty mb_release_id raises, where the merge would leak) and the §11.5 trace.
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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


# === Property-based equivalence (Hypothesis) ================================
# Generate (disc, meta) pairs in the CLEAN in-domain space where the resolver must
# reproduce _merge_into_disc EXACTLY, and assert it. The three documented
# divergences are excluded by construction (so a failure here is a *new* bug):
#   - ISRCs are valid-or-None         -> no invalid-disc-ISRC scrub divergence
#   - track numbers are unique        -> no duplicate-track-number divergence
#   - discogs_release_id is 1..N      -> no `or`-truthiness 0 edge
#   - meta disc-level strings are None-or-nonempty (never "")
#                                     -> no falsy-but-present edge on the three
#                                        guard-less fields (catalog / set_title /
#                                        discogs_release_id), where _merge keeps
#                                        ""/0 but the resolver canonicalises to
#                                        None. Representational, not behavioural.
# Source is MB_DISC_ID throughout, so meta may carry mb_release_id (no C2 raise).
# The excluded regions are pinned by the strict-xfail examples above; together they
# partition the input space (fuzzed-exact where exact, example-pinned where it
# knowingly diverges).

_LETTERS2 = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=2)
_ALNUM3 = st.text(
    alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=3, max_size=3
)
_DIGITS7 = st.text(alphabet="0123456789", min_size=7, max_size=7)
_valid_isrc = st.builds(lambda a, b, c: a + b + c, _LETTERS2, _ALNUM3, _DIGITS7)
_opt_isrc = st.none() | _valid_isrc

_opt_text = st.none() | st.text(max_size=8)
_int = st.integers(min_value=0, max_value=99)  # disc-side disc_number/total (int)
_opt_int = st.none() | st.integers(min_value=0, max_value=99)  # meta-side (int|None)
_opt_discogs = st.none() | st.integers(min_value=1, max_value=10**8)
# disc-side album/artist + track strings are real strings (possibly ""), matching
# RBIDisc's required str fields and how the rip/import builds tracks.
_str_text = st.text(max_size=8)
_str_artist = st.text(max_size=8) | st.just("Unknown Artist")
# Optional disc-level merged strings are None-or-nonempty on BOTH sides: RBIDisc
# defaults them to None (never "") so absent==None is the live invariant, and the
# falsy-but-present "" would only exercise the documented representational edge.
_opt_clean = st.none() | st.text(min_size=1, max_size=8)
_opt_artist_meta = (
    st.none() | st.text(min_size=1, max_size=8) | st.just("Unknown Artist")
)


@st.composite
def _disc_and_meta(draw):
    n = draw(st.integers(min_value=0, max_value=4))
    disc_tracks = []
    meta_tracks = []
    for num in range(1, n + 1):
        disc_tracks.append(
            RBITocEntry(
                track_number=num,
                title=draw(_str_text),
                performer=draw(_str_artist),
                start_frame=0,
                duration_frames=100,
                pregap_frames=0,
                isrc=draw(_opt_isrc),
            )
        )
        if draw(st.booleans()):  # meta may or may not match this track number
            meta_tracks.append(
                TrackMeta(
                    number=num,
                    title=draw(_opt_text),
                    performer=draw(_opt_text),
                    isrc=draw(_opt_isrc),
                )
            )
    disc = RBIDisc(
        album=draw(_str_text),
        artist=draw(_str_artist),
        catalog=draw(_opt_clean),
        disc_number=draw(_int),
        disc_total=draw(_int),
        release_date=draw(_opt_clean),
        catalog_number=draw(_opt_clean),
        label=draw(_opt_clean),
        country=draw(_opt_clean),
        original_release_date=draw(_opt_clean),
        mb_release_id=draw(_opt_clean),
        mb_release_group_id=draw(_opt_clean),
        discogs_release_id=draw(_opt_discogs),
        set_title=draw(_opt_clean),
        tracks=disc_tracks,
        pre_emphasis=draw(st.none() | st.booleans()),
        low_dynamic_range=draw(st.none() | st.booleans()),
        cdtext_catalog_ref=draw(_opt_text),  # physical: preserved, value irrelevant
    )
    meta = DiscMeta(
        album=draw(_opt_clean),
        artist=draw(_opt_artist_meta),
        catalog=draw(_opt_clean),
        disc_number=draw(_opt_int),
        disc_total=draw(_opt_int),
        release_date=draw(_opt_clean),
        catalog_number=draw(_opt_clean),
        label=draw(_opt_clean),
        country=draw(_opt_clean),
        original_release_date=draw(_opt_clean),
        mb_release_id=draw(_opt_clean),
        mb_release_group_id=draw(_opt_clean),
        discogs_release_id=draw(_opt_discogs),
        set_title=draw(_opt_clean),
        tracks=meta_tracks,
    )
    return disc, meta


@settings(max_examples=300)
@given(_disc_and_meta())
def test_property_resolver_equals_merge_on_clean_domain(dm):
    disc, meta = dm
    _equiv(meta, disc, source=Source.MB_DISC_ID)
