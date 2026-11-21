"""Metadata consistent enough to survive a round trip (kgr, 2026-11-21).

A CD-R burned from an RBI carries the RBI's own ISRCs and MCN, so ripping it
again must identify the same release. Two things broke that on ABBA *Gold*:
MusicBrainz holds several ISRCs per recording and only the first was compared,
so the RBI's (valid, second) ISRCs "contradicted" all four disc-ID releases;
and two pressings tied on every rung were split by the alphabetical ``mbid``
sort, landing on the one with no Discogs release.
"""

from __future__ import annotations

from cdda2img import mb_lookup
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.rbi_format import RBIDisc, RBITocEntry

_DANCING_QUEEN = ("SEA017600030", "SEAYD7601020", "SEUV71000390")


def _disc(isrc: str | None) -> RBIDisc:
    return RBIDisc(
        album="Gold",
        artist="ABBA",
        tracks=[
            RBITocEntry(
                track_number=1,
                title="",
                performer="",
                start_frame=0,
                duration_frames=1,
                isrc=isrc,
            )
        ],
    )


def _release(isrcs=_DANCING_QUEEN) -> DiscMeta:
    return DiscMeta(
        tracks=[TrackMeta(number=1, isrc=isrcs[0] if isrcs else None, isrcs=isrcs)]
    )


def test_every_isrc_of_the_recording_is_kept() -> None:
    release = {
        "title": "Gold",
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {
                        "number": "1",
                        "position": "1",
                        "recording": {
                            "title": "Dancing Queen",
                            "isrc-list": list(_DANCING_QUEEN),
                        },
                    }
                ],
            }
        ],
    }
    (track,) = mb_lookup._parse_release(release).tracks
    assert track.isrcs == _DANCING_QUEEN
    assert track.isrc == "SEA017600030"  # the one written is unchanged


def test_a_second_registration_of_the_recording_is_not_a_contradiction() -> None:
    disc = _disc("SEAYD7601020")  # what our ABBA RBI carries
    assert mb_lookup._is_consistent(_release(), disc)
    assert mb_lookup._score_candidate_by_isrcs(_release(), disc) == 1


def test_an_isrc_of_another_recording_still_contradicts() -> None:
    """The control: Unit G still rejects a genuinely different record."""
    disc = _disc("GBXXX1234567")
    assert not mb_lookup._is_consistent(_release(), disc)
    assert mb_lookup._score_candidate_by_isrcs(_release(), disc) == 0


def test_a_track_without_the_full_list_compares_its_one_isrc() -> None:
    """A TrackMeta built elsewhere (no ``isrcs``) behaves as before."""
    meta = DiscMeta(tracks=[TrackMeta(number=1, isrc="SEA017600030")])
    assert mb_lookup._is_consistent(meta, _disc("SEA017600030"))
    assert not mb_lookup._is_consistent(meta, _disc("SEAYD7601020"))


# ---- the ladder's discogs_link rung -----------------------------------------------


def _pressing(mbid: str, **kw) -> DiscMeta:
    return DiscMeta(
        mb_release_id=mbid,
        barcode=kw.get("barcode", "0731451700729"),
        country=kw.get("country", "XE"),
        release_date=kw.get("date", "1992"),
    )


_09CC = "09cc6e86-9901-40f0-a53e-a638e95744ba"
_159D = "159d12a8-f7d2-371e-9482-606d50b968ff"


def test_a_tie_goes_to_the_pressing_linked_to_discogs() -> None:
    asked: list[str] = []

    def linked(rid: str) -> bool:
        asked.append(rid)
        return rid == _159D

    sel = mb_lookup._select_release_lexicographic(
        [_pressing(_09CC), _pressing(_159D)], _disc(None), [], has_discogs_link=linked
    )
    assert sel.winner is not None
    assert sel.winner.mb_release_id == _159D
    assert sel.via == "discogs_link"
    assert sel.tied_after == "discogs_link:1"
    assert sorted(asked) == sorted([_09CC, _159D])


def test_without_the_lookup_the_mbid_sort_decides_as_before() -> None:
    """The control: the rung is what moved the pick."""
    sel = mb_lookup._select_release_lexicographic(
        [_pressing(_09CC), _pressing(_159D)], _disc(None), []
    )
    assert sel.winner is not None
    assert sel.winner.mb_release_id == _09CC


def test_only_candidates_still_tied_are_asked() -> None:
    """An earlier rung that decides leaves the network alone."""
    asked: list[str] = []

    def linked(rid: str) -> bool:
        asked.append(rid)
        return True

    sel = mb_lookup._select_release_lexicographic(
        [_pressing(_09CC, date="1992"), _pressing(_159D, date="1993")],
        _disc(None),
        [],
        has_discogs_link=linked,
    )
    assert sel.winner is not None
    assert sel.winner.mb_release_id == _09CC
    assert asked == []


def test_the_menu_still_offers_every_tied_pressing() -> None:
    """``discogs_link`` is not evidence about the disc, so it narrows the
    automatic pick only, never what the user is shown."""
    sel = mb_lookup._select_release_lexicographic(
        [_pressing(_09CC), _pressing(_159D)],
        _disc(None),
        [],
        has_discogs_link=lambda rid: rid == _159D,
    )
    assert {c.mb_release_id for c in sel.menu_candidates} == {_09CC, _159D}


def test_album_titles_differing_only_in_punctuation_match() -> None:
    from cdda2img.cdda2img import _albums_match

    assert _albums_match("Gold: Greatest Hits", "Gold (Greatest Hits)")
    # the controls: the separator guard and a different album still refuse
    assert not _albums_match("Eliminator", "Afterburner / Eliminator")
    assert not _albums_match("Gold: Greatest Hits", "More ABBA Gold")


def test_the_disc_id_path_asks_the_link_rung(monkeypatch) -> None:
    """End to end through prepopulate_from_mb: the wiring, not just the ladder."""

    def candidate(mbid: str) -> DiscMeta:
        return DiscMeta(
            album="Gold: Greatest Hits",
            artist="ABBA",
            mb_release_id=mbid,
            mb_release_group_id="rg-gold",
            barcode="0731451700729",
            country="XE",
            release_date="1992",
        )

    monkeypatch.setattr(
        mb_lookup, "lookup_disc_id", lambda _d: [candidate(_09CC), candidate(_159D)]
    )
    monkeypatch.setattr(mb_lookup, "_has_discogs_link", lambda rid: rid == _159D)
    r = mb_lookup.prepopulate_from_mb(_disc(None), verbose=False)
    assert r.selected_release_id == _159D
    assert r.release_selected_via == "discogs_link"
