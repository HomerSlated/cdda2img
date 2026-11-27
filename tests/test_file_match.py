"""create's file match: candidates in tiers (trust_model_design.md §14.4, kgr
2026-11-27). Pure: every fixture is built here."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdda2img import file_match as fm
from cdda2img import metadata
from cdda2img import release_match as rm
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.metadata import FileTags, files_order

TITLES = ["Another World", "Chinatown", "T.V. Age", "Target"]
RG = "0c55d762-c505-33fc-865b-5ac66cb90391"


def _rec(i: int) -> str:
    return f"00000000-0000-4000-8000-{i:012d}"


def _files(n: int = 4, *, fp: bool = True, extra: int = 0) -> fm.FolderEvidence:
    titles = TITLES[:n] + [f"Bonus {k}" for k in range(extra)]
    files = tuple(
        fm.FileTrack(
            position=i,
            name=f"{i + 1:02d} {t}.flac",
            title=t,
            duration_ms=240_000,
            fp_recordings=frozenset({_rec(i + 1)}) if fp else frozenset(),
            fp_titles=(t,) if fp else (),
        )
        for i, t in enumerate(titles)
    )
    return fm.FolderEvidence(files)


def _release(
    order: list[int] | None = None,
    *,
    rid: str = "e7ec55d9-0000-4000-8000-000000000000",
    rg: str = RG,
    date: str = "1985",
    first: str = "1982",
    barcode: str | None = None,
    recs: dict[int, str] | None = None,
) -> DiscMeta:
    """A release whose track *k* is the song TITLES[order[k] - 1]."""
    order = order or [1, 2, 3, 4]
    recs = recs or {}
    return DiscMeta(
        album="Night and Day",
        artist="Joe Jackson",
        mb_release_id=rid,
        mb_release_group_id=rg,
        release_date=date,
        original_release_date=first,
        barcode=barcode,
        tracks=[
            TrackMeta(
                number=k,
                title=TITLES[song - 1] if song <= len(TITLES) else f"Extra {song}",
                duration_ms=240_000,
                recording_id=recs.get(song, _rec(song)),
            )
            for k, song in enumerate(order, start=1)
        ],
    )


def test_the_folder_exactly_is_tier_a() -> None:
    c = fm.classify(_files(), _release())
    assert c is not None
    assert (c.tier, c.order, c.excluded) == ("A", "files", ())
    assert c.evidence["tracks"] == "recording"
    assert c.evidence["fingerprint"] == "match"


def test_a_release_listing_a_track_the_folder_lacks_is_rejected() -> None:
    """kgr: no way to build the archive with missing audio."""
    assert fm.classify(_files(), _release([1, 2, 3, 4, 5])) is None
    ranked = fm.rank(_files(), [_release([1, 2, 3, 4, 5])])
    assert ranked.candidates == [] and ranked.rejected == 1


def test_files_the_release_does_not_list_are_left_out() -> None:
    c = fm.classify(_files(extra=1), _release())
    assert c is not None
    assert (c.tier, c.excluded) == ("B", ("05 Bonus 0.flac",))


def test_another_order_is_offered_only_without_a_files_order_release() -> None:
    swapped = _release([2, 1, 3, 4], rid="swapped")
    alone = fm.rank(_files(), [swapped])
    assert [(c.meta.mb_release_id, c.tier, c.order) for c in alone.candidates] == [
        ("swapped", "B", "release")
    ]
    # A files'-order release, even a contradicted one, suppresses it (kgr).
    contradicted = _release(rid="files-order", date="1990", first="1990")
    folder = fm.FolderEvidence(_files().files, tag_year="1985")
    both = fm.rank(folder, [swapped, contradicted])
    assert [c.meta.mb_release_id for c in both.candidates] == ["files-order"]
    assert both.candidates[0].tier == "C"
    assert [c.meta.mb_release_id for c in both.contradicted] == ["files-order"]


def test_a_duplicate_recording_of_the_same_song_is_not_a_contradiction() -> None:
    """MusicBrainz holds duplicate recordings; AcoustID may name the other one."""
    dup = _release(recs={2: _rec(99)})  # the release lists another recording id
    c = fm.classify(_files(), dup)
    assert c is not None
    assert c.evidence["fingerprint"] == "match"  # track 2 found by title, not refuted
    assert c.tier == "A"


def test_a_fingerprint_of_another_song_contradicts() -> None:
    """The control: the file's fingerprint names a recording of a different title."""
    folder = _files()
    wrong = list(folder.files)
    wrong[1] = fm.FileTrack(
        position=1,
        name="02 Chinatown.flac",
        title="Chinatown",
        duration_ms=240_000,
        tag_recording=_rec(2),
        fp_recordings=frozenset({_rec(77)}),
        fp_titles=("Something Else Entirely",),
    )
    c = fm.classify(fm.FolderEvidence(tuple(wrong)), _release())
    assert c is not None
    assert c.evidence["fingerprint"] == "contradict" and c.tier == "C"


@pytest.mark.parametrize(
    ("tag_year", "result"),
    [("1985", "match"), ("1982", "match"), ("1990", "contradict"), (None, "absent")],
)
def test_the_tag_year_against_both_dates(tag_year: str | None, result: str) -> None:
    """beets writes the release date, many taggers the original year: a 1982 tag on
    a 1985 pressing of a 1982 album is no contradiction."""
    folder = fm.FolderEvidence(_files().files, tag_year=tag_year)
    c = fm.classify(folder, _release(date="1985", first="1982"))
    assert c is not None and c.evidence["year"] == result


def test_a_tag_barcode_against_the_release() -> None:
    tags = rm.ReleaseRecord("tags", "", barcodes=("082839490627",))
    folder = fm.FolderEvidence(_files().files, tags=tags)
    same = fm.classify(folder, _release(barcode="0082839490627"))
    other = fm.classify(folder, _release(barcode="0075021490628"))
    assert same is not None and same.evidence["barcode"] == "match"
    assert other is not None and other.evidence["barcode"] == "contradict"
    assert other.tier == "C"


def test_titles_alone_find_the_tracks() -> None:
    c = fm.classify(_files(fp=False), _release())
    assert c is not None
    assert c.evidence["tracks"] == "title" and c.tier == "A"


def test_titles_that_do_not_match_find_nothing() -> None:
    folder = fm.FolderEvidence(
        tuple(
            fm.FileTrack(position=i, name=f"{i}.flac", title=f"Track {i}")
            for i in range(4)
        )
    )
    assert fm.classify(folder, _release()) is None


def test_a_single_never_outranks_the_album() -> None:
    single = _release([2], rid="single")  # Chinatown on its own
    album = _release(rid="album")
    folder = _files(extra=1)  # the album is tier B too: one bonus file
    ranked = fm.rank(folder, [single, album])
    assert [c.meta.mb_release_id for c in ranked.candidates] == ["album", "single"]


def _ranked(*cs: DiscMeta, tagged: str | None = None) -> fm.Decision:
    return fm.decide_unattended(fm.rank(_files(), list(cs)), tagged)


def test_unattended_decisions() -> None:
    a = _release(rid="a")
    b = _release(rid="b")
    assert _ranked(a).outcome == "unique"
    assert _ranked(a, b).outcome == "release_group_only"
    d = _ranked(a, b, tagged="b")
    assert (d.outcome, d.chosen and d.chosen.meta.mb_release_id) == ("unique", "b")
    other_album = _release(rid="c", rg="another-group")
    assert _ranked(a, other_album).reason == "ambiguous_album"
    assert _ranked(_release([1, 2, 3, 4, 5])).reason == "missing_tracks"
    assert _ranked().reason == "no_candidates"
    swapped = _release([2, 1, 3, 4], rid="swapped")
    assert _ranked(swapped).reason == "no_tier_a"


# ── metadata.files_order / read_file_tracks ─────────────────────────────────


def _ft(name: str, tracknumber: int | None, name_number: int | None) -> FileTags:
    return FileTags(Path(name), name, tracknumber, name_number, None)


def test_files_order_takes_track_numbers_then_names() -> None:
    by_tag = [_ft("a", 2, 1), _ft("b", 1, 2)]
    assert files_order(by_tag) == [1, 0]
    by_name = [_ft("a", None, 2), _ft("b", None, 1)]
    assert files_order(by_name) == [1, 0]
    clash = [_ft("a", 1, None), _ft("b", 1, None)]
    assert files_order(clash) == [0, 1]


def test_read_file_tracks(monkeypatch: pytest.MonkeyPatch) -> None:
    tags = {
        "/s/01 Another World.flac": {
            "title": ["Another World"],
            "tracknumber": ["1/9"],
            "musicbrainz_trackid": ["21511631-0D17-4490-9AB6-04AE95202A89"],
            "date": ["1985-04-11"],
            "barcode": ["082839490627"],
        },
        "/s/02 chinatown.flac": {"date": ["1986"], "barcode": ["082839490627"]},
    }
    monkeypatch.setattr(metadata, "File", lambda name, easy=True: tags[name])
    files, album = metadata.read_file_tracks([
        Path("/s/01 Another World.flac"),
        Path("/s/02 chinatown.flac"),
    ])
    assert files[0].recording_id == "21511631-0d17-4490-9ab6-04ae95202a89"
    assert (files[0].tracknumber, files[1].name_number) == (1, 2)
    assert files[1].title == "chinatown"  # the name without its leading number
    assert album.barcode == "082839490627"
    assert album.date is None  # two files disagree: not the album's
