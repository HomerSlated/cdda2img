"""
test_merge_sequence.py — B4 B-3: multi-source merge-sequence equivalence (proof).

B-1 proved the resolver reproduces a *single* ``_merge_into_disc`` fold. This proves
it reproduces the **whole ``_run_metadata_lookups`` merge sequence** — baseline ->
MB -> §10 canonical-MCN overwrite -> Discogs -> stage-7 -> CDDB — which is the model
the B-3 production accumulator (and the B-4 flip) rely on. The proof is a Hypothesis
property test over the live in-domain space, the multi-source generalization of the
B-1 property test (which caught the falsy-but-present class on its first run).

Live constraints modelled in the domain (so equivalence is over what actually
reaches the merge, not arbitrary inputs):

- AcoustID contributes **no fields** (corroboration-only). Asserted separately, and
  therefore absent from the sequence.
- MB and stage-7 are **mutually exclusive** — stage-7 fires only when the MB disc-ID
  selection found nothing (``selected_release_id is None``). So at most one source
  carries ``disc_number``/``disc_total`` (Discogs/CDDB never do), which is *why* the
  meta-priority last-writer-vs-first-writer question is unreachable.
- stage-7's meta is always ``strip_pressing_mbid``-ed -> ``mb_release_id is None``.
- the falsy-but-present ``""``/``0`` class and the two strict-xfail divergences are
  excluded exactly as in B-1 (optional fields None-or-nonempty, valid/None ISRCs,
  unique track numbers, non-zero discogs id).
"""

from dataclasses import replace

from hypothesis import given, settings
from hypothesis import strategies as st

from cdda2img.field_resolver import Source, disc_from_resolution, resolve
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.mb_lookup import _merge_into_disc
from cdda2img.rbi_format import RBIDisc, RBITocEntry
from cdda2img.resolver_adapter import (
    baseline_proposals,
    canonical_mcn_proposal,
    meta_to_proposals,
    sanitize_base,
)

# === the two sequences =======================================================


def _live_sequence(
    baseline: RBIDisc,
    mb: DiscMeta | None,
    chosen: str | None,
    discogs: DiscMeta | None,
    stage7: DiscMeta | None,
    cddb: DiscMeta | None,
) -> RBIDisc:
    """The field effects of ``_run_metadata_lookups``, in order (AcoustID merges
    nothing, so it is absent). Phase A of ``_prepopulate_from_discogs`` overwrites
    ``catalog`` with the canonical MCN between the MB and Discogs merges."""
    disc = baseline
    if mb is not None:
        disc = _merge_into_disc(mb, disc)
    if chosen and disc.catalog != chosen:  # phase A — unconditional overwrite
        disc = replace(disc, catalog=chosen)
    if discogs is not None:
        disc = _merge_into_disc(discogs, disc)
    if stage7 is not None:
        disc = _merge_into_disc(stage7, disc)
    if cddb is not None:
        disc = _merge_into_disc(cddb, disc)
    return disc


def _resolver_sequence(
    baseline: RBIDisc,
    mb: DiscMeta | None,
    chosen: str | None,
    discogs: DiscMeta | None,
    stage7: DiscMeta | None,
    cddb: DiscMeta | None,
) -> RBIDisc:
    """Collect every source's proposals, resolve once, assemble on the baseline."""
    props = baseline_proposals(baseline)
    if mb is not None:
        props += meta_to_proposals(mb, Source.MB_DISC_ID)
    props += canonical_mcn_proposal(chosen)
    if discogs is not None:
        props += meta_to_proposals(discogs, Source.DISCOGS)
    if stage7 is not None:
        props += meta_to_proposals(stage7, Source.DURATION)
    if cddb is not None:
        props += meta_to_proposals(cddb, Source.CDDB)
    # sanitize_base: the committed-disc assembly contract (drops invalid on-disc
    # ISRCs uniformly). A no-op on this property test's clean domain (valid/None
    # ISRCs only), kept for parity with production.
    return disc_from_resolution(resolve(props), sanitize_base(baseline))


# === strategies (clean live domain) ==========================================

_L2 = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=2)
_A3 = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=3, max_size=3)
_D7 = st.text(alphabet="0123456789", min_size=7, max_size=7)
_valid_isrc = st.builds(lambda a, b, c: a + b + c, _L2, _A3, _D7)
_opt_isrc = st.none() | _valid_isrc

_opt_clean = st.none() | st.text(min_size=1, max_size=6)  # None-or-nonempty
_opt_artist = st.none() | st.text(min_size=1, max_size=6) | st.just("Unknown Artist")
_opt_int = st.none() | st.integers(min_value=0, max_value=9)
_opt_barcode = st.none() | st.text(alphabet="0123456789", min_size=12, max_size=13)
_str_text = st.text(max_size=6)
_str_artist = st.text(max_size=6) | st.just("Unknown Artist")


@st.composite
def _meta_tracks(draw, numbers, *, with_isrc=True):
    out = []
    for num in numbers:
        if draw(st.booleans()):
            out.append(
                TrackMeta(
                    number=num,
                    title=draw(_opt_clean),
                    performer=draw(_opt_clean),
                    isrc=draw(_opt_isrc) if with_isrc else None,
                )
            )
    return out


@st.composite
def _scenario(draw):
    n = draw(st.integers(min_value=0, max_value=3))
    numbers = list(range(1, n + 1))
    disc_tracks = [
        RBITocEntry(
            track_number=num,
            title=draw(_str_text),
            performer=draw(_str_artist),
            start_frame=num * 1000,
            duration_frames=500,
            pregap_frames=0,
            isrc=draw(_opt_isrc),
        )
        for num in numbers
    ]
    baseline = RBIDisc(
        album=draw(_str_text),
        artist=draw(_str_artist),
        catalog=draw(_opt_clean),
        disc_number=draw(st.integers(min_value=0, max_value=9)),
        disc_total=draw(st.integers(min_value=0, max_value=9)),
        release_date=draw(_opt_clean),
        catalog_number=draw(_opt_clean),
        label=draw(_opt_clean),
        country=draw(_opt_clean),
        original_release_date=draw(_opt_clean),
        mb_release_id=draw(_opt_clean),
        mb_release_group_id=draw(_opt_clean),
        discogs_release_id=draw(st.none() | st.integers(min_value=1, max_value=999)),
        set_title=draw(_opt_clean),
        tracks=disc_tracks,
        pre_emphasis=draw(st.none() | st.booleans()),
    )

    def _disc_meta(*, mbid, disc_num, group_ok):
        return DiscMeta(
            album=draw(_opt_clean),
            artist=draw(_opt_artist),
            catalog=draw(_opt_clean),
            disc_number=disc_num,
            disc_total=disc_num,
            release_date=draw(_opt_clean),
            catalog_number=draw(_opt_clean),
            label=draw(_opt_clean),
            country=draw(_opt_clean),
            original_release_date=draw(_opt_clean),
            mb_release_id=mbid,
            mb_release_group_id=draw(_opt_clean) if group_ok else None,
            discogs_release_id=draw(
                st.none() | st.integers(min_value=1, max_value=999)
            ),
            set_title=draw(_opt_clean),
            tracks=draw(_meta_tracks(numbers)),
        )

    # MB ⊕ stage-7: at most one is present (live mutual exclusivity).
    which = draw(st.sampled_from(["mb", "stage7", "neither"]))
    mb = (
        _disc_meta(mbid=draw(_opt_clean), disc_num=draw(_opt_int), group_ok=True)
        if which == "mb"
        else None
    )
    # stage-7 meta is strip_pressing_mbid-ed -> mb_release_id None.
    stage7 = (
        _disc_meta(mbid=None, disc_num=draw(_opt_int), group_ok=True)
        if which == "stage7"
        else None
    )
    # Discogs / CDDB never carry disc_number/disc_total or an MB release id.
    discogs = (
        _disc_meta(mbid=None, disc_num=None, group_ok=False)
        if draw(st.booleans())
        else None
    )
    cddb = (
        _disc_meta(mbid=None, disc_num=None, group_ok=False)
        if draw(st.booleans())
        else None
    )
    chosen = draw(_opt_barcode)
    return baseline, mb, chosen, discogs, stage7, cddb


@settings(max_examples=400)
@given(_scenario())
def test_property_resolver_reproduces_merge_sequence(scenario):
    baseline, mb, chosen, discogs, stage7, cddb = scenario
    assert _resolver_sequence(
        baseline, mb, chosen, discogs, stage7, cddb
    ) == _live_sequence(baseline, mb, chosen, discogs, stage7, cddb)


# === AcoustID contributes no fields (load-bearing for its absence) ===========


def test_acoustid_tally_merge_changes_no_fields():
    """The whole reproduce model omits AcoustID from the sequence. That is only
    valid because ``_r6_tally_and_merge`` merges no fields — verify it for both
    branches (disc has a selected release / disc has none)."""
    from types import SimpleNamespace

    from cdda2img.cdda2img import _r6_tally_and_merge

    def _hit(rid, rgid=None):
        return SimpleNamespace(mb_release_id=rid, mb_release_group_id=rgid)

    disc = RBIDisc(
        album="A",
        artist="B",
        catalog="X",
        mb_release_group_id="RG",
        tracks=[
            RBITocEntry(
                track_number=1,
                title="t",
                performer="p",
                start_frame=0,
                duration_frames=100,
                pregap_frames=0,
                isrc=None,
            )
        ],
    )
    hits = [[_hit("R-OTHER", "RG-OTHER")], [_hit("R-OTHER", "RG-OTHER")]]
    # has-id branch
    out1 = _r6_tally_and_merge(hits, disc, {}, selected_release_id="R-SEL")
    # no-id branch
    out2 = _r6_tally_and_merge(hits, disc, {}, selected_release_id=None)
    for out in (out1, out2):
        assert out.album == "A"
        assert out.artist == "B"
        assert out.catalog == "X"
        assert out.mb_release_id == disc.mb_release_id
        assert [t.title for t in out.tracks] == ["t"]


# === explicit examples — the multi-source behaviours, named ==================


def _meta(**kw) -> DiscMeta:
    return DiscMeta(**kw)


def _bare_disc(**kw) -> RBIDisc:
    base = {"album": "", "artist": "", "tracks": []}
    base.update(kw)
    return RBIDisc(**base)  # type: ignore[arg-type]


def test_first_writer_wins_mb_over_discogs_over_cddb():
    """For a field all three carry, the earliest in call order wins (MB)."""
    baseline = _bare_disc()  # blank album
    mb = _meta(album="MB Album")
    discogs = _meta(album="Discogs Album")
    cddb = _meta(album="CDDB Album")
    out = _resolver_sequence(baseline, mb, None, discogs, None, cddb)
    assert out.album == "MB Album"
    assert out == _live_sequence(baseline, mb, None, discogs, None, cddb)


def test_discogs_fills_what_mb_left_blank():
    baseline = _bare_disc()
    mb = _meta(album="MB Album")  # no label
    discogs = _meta(album="Discogs Album", label="Discogs Label")
    out = _resolver_sequence(baseline, mb, None, discogs, None, None)
    assert out.album == "MB Album"  # MB wins the contested field
    assert out.label == "Discogs Label"  # Discogs fills the blank
    assert out == _live_sequence(baseline, mb, None, discogs, None, None)


def test_canonical_mcn_overrides_baseline_and_meta_catalog():
    baseline = _bare_disc(catalog="0042284229999")  # an on-disc catalog
    mb = _meta(catalog="1111111111111")
    chosen = "0042284229821"  # §10 verdict
    out = _resolver_sequence(baseline, mb, chosen, None, None, None)
    assert out.catalog == chosen
    assert out == _live_sequence(baseline, mb, chosen, None, None, None)


def test_no_canonical_mcn_falls_back_to_fill_blank_catalog():
    baseline = _bare_disc()  # no catalog
    mb = _meta(catalog="MB-CAT")
    out = _resolver_sequence(baseline, mb, None, None, None, None)
    assert out.catalog == "MB-CAT"  # MB fills the blank when no canonical MCN
    assert out == _live_sequence(baseline, mb, None, None, None, None)


def test_stage7_supplies_title_when_mb_absent():
    baseline = _bare_disc()
    stage7 = _meta(album="Duration Match", mb_release_id=None)
    cddb = _meta(album="CDDB Album")
    out = _resolver_sequence(baseline, None, None, None, stage7, cddb)
    assert out.album == "Duration Match"  # stage-7 outranks CDDB
    assert out == _live_sequence(baseline, None, None, None, stage7, cddb)
