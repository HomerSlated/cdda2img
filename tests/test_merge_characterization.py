"""
test_merge_characterization.py — B0: pin the CURRENT _merge_into_disc behaviour.

CHARACTERIZATION TESTS. These assert what the fill-blank merge does *today* —
quirks included — NOT what is necessarily desired. They are the regression gate
for B4 Phase B: the collect->resolve resolver, with its trust table encoding
today's call-order precedence, must reproduce exactly these outcomes. If a quirk
is later judged a bug and fixed, the matching test changes WITH the fix, making
the behavioural delta explicit in the diff.

Scope here: the headless fill-blank fold (`_merge_into_disc`). The interactive
menu apply (`_overwrite_disc` / Update-vs-Overwrite-All) and the per-pipeline
baselines (create = mutagen-high, rip = CD-Text-low) are pinned separately.
Physical-field preservation (C1) is already covered by test_merge_invariants.py.
"""

from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.mb_lookup import _merge_into_disc, _overwrite_disc
from cdda2img.rbi_format import RBIDisc, RBITocEntry


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


# --- album: DISC-priority — the baseline wins by position when present -------


def test_album_present_on_disc_wins_meta_ignored():
    """The OPT-4 defect, pinned: a present baseline album beats MB by position."""
    out = _merge_into_disc(DiscMeta(album="From MB"), _disc(album="From Disc"))
    assert out.album == "From Disc"


def test_album_empty_on_disc_filled_from_meta():
    out = _merge_into_disc(DiscMeta(album="From MB"), _disc(album=""))
    assert out.album == "From MB"


def test_album_empty_both_stays_empty():
    out = _merge_into_disc(DiscMeta(album=None), _disc(album=""))
    assert out.album == ""


# --- artist: DISC-priority EXCEPT the "Unknown Artist" sentinel -------------


def test_artist_present_on_disc_wins():
    out = _merge_into_disc(DiscMeta(artist="Real"), _disc(artist="On Disc"))
    assert out.artist == "On Disc"


def test_artist_unknown_sentinel_yields_to_meta():
    """'Unknown Artist' is treated as absent — meta fills it."""
    out = _merge_into_disc(DiscMeta(artist="Real"), _disc(artist="Unknown Artist"))
    assert out.artist == "Real"


# --- presence-semantics INCONSISTENCY (quirk, design doc §8.5) --------------


def test_disc_number_is_meta_priority_unlike_album():
    """album is disc-priority but disc_number is meta-priority (meta wins when
    not None) — opposite directions inside the same function. Pinned as a quirk
    so Phase B reproduces it and any future unification is a visible change."""
    out = _merge_into_disc(DiscMeta(disc_number=5), _disc(disc_number=1))
    assert out.disc_number == 5


# --- catalog (MCN): plain fill-blank (disc wins if present) -----------------


def test_barcode_fill_blank_then_disc_priority():
    # meta.barcode -> disc.barcode (fill-blank, disc-priority). The on-disc MCN
    # (catalog) is never filled from a service barcode.
    assert _merge_into_disc(DiscMeta(barcode="X"), _disc(barcode=None)).barcode == "X"
    assert _merge_into_disc(DiscMeta(barcode="X"), _disc(barcode="Y")).barcode == "Y"
    assert _merge_into_disc(DiscMeta(barcode="X"), _disc(catalog=None)).catalog is None


# --- per-track title / performer: same disc-priority + sentinel logic -------


def test_track_title_disc_priority_blank_filled():
    disc = _disc(tracks=[_toc(1, title="On Disc"), _toc(2, title="")])
    meta = DiscMeta(
        tracks=[TrackMeta(number=1, title="MB1"), TrackMeta(number=2, title="MB2")]
    )
    out = _merge_into_disc(meta, disc)
    assert [t.title for t in out.tracks] == ["On Disc", "MB2"]


def test_track_with_no_meta_match_kept_verbatim():
    disc = _disc(tracks=[_toc(1, title="Keep")])
    out = _merge_into_disc(DiscMeta(tracks=[]), disc)
    assert out.tracks[0].title == "Keep"


# --- ISRC fallback order: disc-side (validated) first, else meta ------------


def test_isrc_disc_side_valid_wins():
    disc = _disc(tracks=[_toc(1, isrc="USAR10400486")])
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="GBAYE0000123")])
    assert _merge_into_disc(meta, disc).tracks[0].isrc == "USAR10400486"


def test_isrc_disc_side_absent_uses_meta():
    disc = _disc(tracks=[_toc(1, isrc=None)])
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="GBAYE0000123")])
    assert _merge_into_disc(meta, disc).tracks[0].isrc == "GBAYE0000123"


def test_isrc_disc_side_invalid_falls_back_to_meta():
    """A malformed disc-side ISRC is dropped (R13) and the meta ISRC used."""
    disc = _disc(tracks=[_toc(1, isrc="NOTVALID")])
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="GBAYE0000123")])
    assert _merge_into_disc(meta, disc).tracks[0].isrc == "GBAYE0000123"


# === _overwrite_disc ("Overwrite All") — META-first, the opposite polarity ===
# The interactive menu's destructive apply. The resolver must subsume this: a
# user explicitly choosing "Overwrite All" is the high-trust (MANUAL) override.


def test_overwrite_album_meta_wins_over_present_disc():
    """Opposite of _merge_into_disc: a present meta album replaces the disc's."""
    out = _overwrite_disc(DiscMeta(album="From MB"), _disc(album="On Disc"))
    assert out.album == "From MB"


def test_overwrite_album_meta_blank_keeps_disc():
    """Overwrite is meta-first but still blank-safe: empty meta keeps the disc."""
    out = _overwrite_disc(DiscMeta(album=None), _disc(album="On Disc"))
    assert out.album == "On Disc"


def test_overwrite_track_title_meta_first():
    disc = _disc(tracks=[_toc(1, title="On Disc")])
    meta = DiscMeta(tracks=[TrackMeta(number=1, title="From MB")])
    assert _overwrite_disc(meta, disc).tracks[0].title == "From MB"


def test_overwrite_isrc_meta_first():
    """Overwrite prefers the meta ISRC even when the disc has a valid one."""
    disc = _disc(tracks=[_toc(1, isrc="USAR10400486")])
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="GBAYE0000123")])
    assert _overwrite_disc(meta, disc).tracks[0].isrc == "GBAYE0000123"
