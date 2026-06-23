"""
test_menu_apply_resolver.py — B4 B-5: the menu-apply resolver keystone.

The interactive metadata menu applies a user-selected search result (MB / Discogs /
AcoustID) onto ``ctl.disc`` in one of two modes: "update missing fields only"
(``_merge_into_disc``, disc-priority) or "overwrite all" (``_overwrite_disc``,
meta-priority). B-5 routes both through the trust resolver via
``resolver_adapter.apply_menu_selection``, with ``ctl.disc`` as the live baseline:

- **update** is the B-1 keystone with ``disc = ctl.disc`` — the selection emitted at
  the below-baseline ``Source.MENU`` trust fills blanks only;
- **overwrite** is the new keystone — the selection emitted at ``Trust.MANUAL``
  (the user endorses it) outranks the OBJECTIVE baseline and wins, baseline filling
  its blanks.

Equivalence holds on the clean live domain (None-or-nonempty fields, valid canonical
/ None ISRCs, unique track numbers). The accepted divergences (resolver-cleaner) are
pinned as examples: an "Unknown Artist" sentinel in the *selection* under overwrite
(demoted below a real baseline value, where ``_overwrite_disc`` takes it), and the
shared ISRC carve-outs (``sanitize_base`` + baseline normalisation). Also pinned: a
user-selected release's ``mb_release_id`` is KEPT (``Source.MENU`` is not
recording-level — no C2 strip), matching today's bake.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.mb_lookup import _merge_into_disc, _overwrite_disc
from cdda2img.rbi_format import RBIDisc, RBITocEntry
from cdda2img.resolver_adapter import apply_menu_selection

# === keystones — explicit ====================================================


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


def test_update_fills_blanks_like_merge():
    """Menu 'update' == _merge_into_disc (disc-priority fill-blank)."""
    disc = _disc(album="On Disc", artist="", tracks=[_toc(1, title="D1")])
    sel = DiscMeta(album="From MB", artist="MB Artist", label="MB Label")
    out = apply_menu_selection(disc, sel, overwrite=False)
    assert out == _merge_into_disc(sel, disc)
    assert out.album == "On Disc"  # existing wins
    assert out.artist == "MB Artist"  # blank filled
    assert out.label == "MB Label"  # blank filled


def test_overwrite_replaces_like_overwrite_disc():
    """Menu 'overwrite' == _overwrite_disc (meta-priority, baseline fills blanks)."""
    disc = _disc(album="On Disc", artist="On Disc Artist", catalog="CAT")
    sel = DiscMeta(album="From MB", artist="MB Artist")  # no catalog
    out = apply_menu_selection(disc, sel, overwrite=True)
    assert out == _overwrite_disc(sel, disc)
    assert out.album == "From MB"  # selection wins
    assert out.artist == "MB Artist"  # selection wins
    assert out.catalog == "CAT"  # baseline fills the selection's blank


def test_overwrite_keeps_user_selected_mb_release_id_no_c2():
    """Source.MENU is not recording-level: a user-selected release's mb_release_id is
    kept (user-confirmed, authoritative), not C2-stripped — and construction does not
    raise. Reproduces today's _overwrite_disc, which bakes it."""
    disc = _disc(album="A", artist="B", tracks=[_toc(1)])
    sel = DiscMeta(
        album="A",
        artist="B",
        mb_release_id="rid-user-picked",
        mb_release_group_id="rg",
        tracks=[TrackMeta(number=1, title="t")],
    )
    out = apply_menu_selection(disc, sel, overwrite=True)
    assert out.mb_release_id == "rid-user-picked"
    assert out == _overwrite_disc(sel, disc)


# === accepted divergences (resolver-cleaner), pinned =========================


def test_overwrite_selection_unknown_artist_sentinel_DIVERGES_from_overwrite():
    """A *selected* 'Unknown Artist' under overwrite is demoted below a real baseline
    artist (resolver-cleaner); _overwrite_disc takes the sentinel via
    ``meta.artist or disc.artist``. Same bucket as the B-1 sentinel handling."""
    disc = _disc(album="A", artist="Real Artist")
    sel = DiscMeta(album="A", artist="Unknown Artist")
    via_resolver = apply_menu_selection(disc, sel, overwrite=True)
    via_overwrite = _overwrite_disc(sel, disc)
    assert via_resolver.artist == "Real Artist"  # resolver: sentinel loses
    assert via_overwrite.artist == "Unknown Artist"  # _overwrite: sentinel taken


def test_overwrite_unmatched_noncanonical_isrc_DIVERGES_from_overwrite():
    """Shared ISRC carve-out in the menu path: an unmatched track's valid
    non-canonical on-disc ISRC is normalised by the resolver (baseline normalisation)
    but kept raw by _overwrite_disc. Resolver-cleaner; low reachability."""
    disc = _disc(album="A", artist="B", tracks=[_toc(1, isrc="gb-aye-0000123")])
    sel = DiscMeta(album="A", artist="B", tracks=[])  # unmatched
    via_resolver = apply_menu_selection(disc, sel, overwrite=True)
    via_overwrite = _overwrite_disc(sel, disc)
    assert via_resolver.tracks[0].isrc == "GBAYE0000123"  # normalised
    assert via_overwrite.tracks[0].isrc == "gb-aye-0000123"  # raw


# === Hypothesis property over the clean domain ===============================

_L2 = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ", min_size=2, max_size=2)
_A3 = st.text(alphabet="ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", min_size=3, max_size=3)
_D7 = st.text(alphabet="0123456789", min_size=7, max_size=7)
_valid_isrc = st.builds(lambda a, b, c: a + b + c, _L2, _A3, _D7)  # canonical
_opt_isrc = st.none() | _valid_isrc
_opt_clean = st.none() | st.text(min_size=1, max_size=6)
# Non-sentinel artist: the sentinel divergence is pinned separately above.
_opt_artist = st.none() | st.text(min_size=1, max_size=6)
_str_text = st.text(max_size=6)
_str_artist = st.text(max_size=6)  # baseline artist may be anything (incl. blank)


@st.composite
def _meta_tracks(draw, numbers):
    out = []
    for num in numbers:
        if draw(st.booleans()):
            out.append(
                TrackMeta(
                    number=num,
                    title=draw(_opt_clean),
                    performer=draw(_opt_artist),
                    isrc=draw(_opt_isrc),
                )
            )
    return out


@st.composite
def _scenario(draw):
    n = draw(st.integers(min_value=0, max_value=3))
    numbers = list(range(1, n + 1))
    disc = RBIDisc(
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
        tracks=[
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
        ],
        pre_emphasis=draw(st.none() | st.booleans()),
    )
    sel = DiscMeta(
        album=draw(_opt_clean),
        artist=draw(_opt_artist),
        catalog=draw(_opt_clean),
        disc_number=draw(st.none() | st.integers(min_value=0, max_value=9)),
        disc_total=draw(st.none() | st.integers(min_value=0, max_value=9)),
        release_date=draw(_opt_clean),
        catalog_number=draw(_opt_clean),
        label=draw(_opt_clean),
        country=draw(_opt_clean),
        original_release_date=draw(_opt_clean),
        mb_release_id=draw(_opt_clean),
        mb_release_group_id=draw(_opt_clean),
        discogs_release_id=draw(st.none() | st.integers(min_value=1, max_value=999)),
        set_title=draw(_opt_clean),
        tracks=draw(_meta_tracks(numbers)),
    )
    return disc, sel


@settings(max_examples=400)
@given(_scenario())
def test_property_update_reproduces_merge(scenario):
    disc, sel = scenario
    assert apply_menu_selection(disc, sel, overwrite=False) == _merge_into_disc(
        sel, disc
    )


@settings(max_examples=400)
@given(_scenario())
def test_property_overwrite_reproduces_overwrite_disc(scenario):
    disc, sel = scenario
    assert apply_menu_selection(disc, sel, overwrite=True) == _overwrite_disc(sel, disc)
