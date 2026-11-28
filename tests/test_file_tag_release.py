"""`create`: the release the files' tags name, and the file match that settles
which release, which files and which order the archive takes (kgr, 2026-11-27;
trust_model_design.md §14; rbi_spec.md ``file_tag_release``, ``file_match*``)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import av
import pytest
from mutagen.flac import FLAC

from cdda2img import cdda2img as cli
from cdda2img import file_match as fm
from cdda2img import metadata
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.metadata import read_file_tag_release

RID = "e7ec55d9-cf2c-3d46-925f-a0bad61e8a06"
OTHER = "0a19729e-0000-4000-8000-000000000000"


def _stub_tags(monkeypatch: pytest.MonkeyPatch, per_file: list[dict]) -> list[Path]:
    """Stub mutagen so file *i* carries the easy tags *per_file[i]*."""
    paths = [Path(f"/src/{i:02d}.flac") for i in range(1, len(per_file) + 1)]
    by_name = {str(p): tags for p, tags in zip(paths, per_file)}
    monkeypatch.setattr(metadata, "File", lambda name, easy=True: by_name[name])
    return paths


def test_one_release_named_by_every_file(monkeypatch: pytest.MonkeyPatch) -> None:
    tags = {"musicbrainz_albumid": [RID.upper()], "discnumber": ["2/2"]}
    paths = _stub_tags(monkeypatch, [tags, tags, tags])
    assert read_file_tag_release(paths) == (RID, RID, 2)


def test_no_file_names_a_release(monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _stub_tags(monkeypatch, [{"album": ["X"]}, {}])
    assert read_file_tag_release(paths) == (None, None, None)


@pytest.mark.parametrize(
    "second",
    [
        {"musicbrainz_albumid": [OTHER]},  # names another release
        {},  # names none: not one release's tracks either
    ],
)
def test_files_that_disagree_name_nothing(
    monkeypatch: pytest.MonkeyPatch, second: dict
) -> None:
    paths = _stub_tags(monkeypatch, [{"musicbrainz_albumid": [RID]}, second])
    assert read_file_tag_release(paths) == (None, "mixed", None)


def test_a_value_that_is_not_a_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    tags = {"musicbrainz_albumid": ["not-an-mbid"]}
    paths = _stub_tags(monkeypatch, [tags, tags])
    assert read_file_tag_release(paths) == (None, "invalid", None)


def test_disc_numbers_that_differ_select_no_medium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _stub_tags(
        monkeypatch,
        [
            {"musicbrainz_albumid": [RID], "discnumber": ["1"]},
            {"musicbrainz_albumid": [RID], "discnumber": ["2"]},
        ],
    )
    assert read_file_tag_release(paths) == (RID, RID, None)


def test_a_real_flac_tagged_by_picard(tmp_path: Path) -> None:
    """The tag name mutagen's easy interface reads is the one Picard writes:
    MUSICBRAINZ_ALBUMID in a Vorbis comment (the stubbed tests cannot see this)."""
    path = tmp_path / "01.flac"
    with av.open(str(path), "w", format="flac") as out:
        stream = out.add_stream("flac", rate=44100, layout="stereo")
        frame = av.AudioFrame(format="s16", layout="stereo", samples=588)
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        frame.sample_rate = 44100
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    flac = FLAC(str(path))
    flac["MUSICBRAINZ_ALBUMID"] = RID
    flac["DISCNUMBER"] = "1"
    flac.save()
    assert read_file_tag_release([path]) == (RID, RID, 1)


# ── _identify_files (§14.4) ─────────────────────────────────────────────────

TITLES = ["Another World", "Chinatown", "T.V. Age"]
RG = "0c55d762-c505-33fc-865b-5ac66cb90391"


def _rec(i: int) -> str:
    return f"00000000-0000-4000-8000-{i:012d}"


def _folder(n: int = 3, *, tagged: str | None = RID) -> fm.FolderEvidence:
    titles = TITLES + [f"Bonus {k}" for k in range(n - len(TITLES))]
    return fm.FolderEvidence(
        tuple(
            fm.FileTrack(
                position=i,
                name=f"{i + 1:02d} {t}.flac",
                title=t,
                duration_ms=240_000,
                tag_recording=_rec(i + 1),
            )
            for i, t in enumerate(titles[:n])
        ),
        tagged_release=tagged,
    )


def _release(rid: str = RID, order: tuple[int, ...] = (1, 2, 3)) -> DiscMeta:
    return DiscMeta(
        album="Night and Day",
        artist="Joe Jackson",
        mb_release_id=rid,
        mb_release_group_id=RG,
        label="A&M Records",
        tracks=[
            TrackMeta(number=k, title=TITLES[s - 1], recording_id=_rec(s))
            for k, s in enumerate(order, start=1)
        ],
    )


_CTX: dict = {}


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    folder: fm.FolderEvidence,
    order: list[int],
    metas: list[DiscMeta],
    formats: dict | None = None,
) -> None:
    """The whole input as *folder*, the tagged release's media as *metas*."""
    _CTX["folder"], _CTX["order"] = folder, order
    monkeypatch.setattr(cli, "_gather_tagged", lambda b, p: (metas, formats or {}))
    monkeypatch.setattr(
        cli, "_gather_candidates", lambda *a, **k: cli.Gathered([], {}, 0, 0, False)
    )


def _evidence(album: str | None = "Night and Day", artist: str | None = "Joe Jackson"):
    return cli.InputEvidence(
        {},
        "OK",
        album,
        artist,
        _CTX.get("folder", fm.FolderEvidence(())),
        _CTX.get("order", []),
    )


def _identify(
    prov: dict[str, str], *, interactive: bool = False, evidence=None
) -> cli.FileIdentity:
    return cli._identify_files(
        [],
        prov,
        evidence=evidence or _evidence(),
        interactive=interactive,
        tui=False,
    )


def test_the_tagged_release_in_tier_a_is_the_pressing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = _release("ede0869d-0000-4000-8000-000000000000")
    _wire(monkeypatch, _folder(), [2, 0, 1], [other, _release()])
    prov: dict[str, str] = {}
    ident = _identify(prov)
    assert ident.meta is not None and ident.meta.mb_release_id == RID
    assert ident.keep == [2, 0, 1]  # the files' order, as batch indices
    assert prov["mb_identified_via"] == "file_tags"
    assert prov["release_selection"] == "unique"
    assert (prov["file_match_tier"], prov["file_match_order"]) == ("A", "files")


def test_several_untagged_pressings_record_the_album_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kgr (§14.4): the files cannot choose between pressings under --auto."""
    a = _release("a0000000-0000-4000-8000-000000000000")
    b = _release("b0000000-0000-4000-8000-000000000000")
    _wire(monkeypatch, _folder(tagged=None), [0, 1, 2], [a, b])
    prov: dict[str, str] = {}
    ident = _identify(prov)
    assert ident.group_only and ident.meta is not None
    assert ident.meta.mb_release_id is None
    assert ident.meta.label == "A&M Records"  # stated alike by every candidate
    assert prov["release_selection"] == "release_group_only"


def test_unattended_never_takes_tier_b(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _folder(4), [0, 1, 2, 3], [_release()])  # a bonus file
    prov: dict[str, str] = {}
    ident = _identify(prov)
    assert ident.meta is None and ident.keep == [0, 1, 2, 3]
    assert prov["file_match_rejected"] == "no_tier_a"


def test_a_release_missing_a_folder_track_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _folder(2), [0, 1], [_release()])  # the folder lacks track 3
    prov: dict[str, str] = {}
    assert _identify(prov).meta is None
    assert prov["file_match_rejected"] == "missing_tracks"


def test_a_chosen_release_lays_out_its_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tier B: a bonus file left out, the release's order taken; the archive is
    built from the kept files in the release's order."""
    swapped = _release(order=(2, 1, 3))
    _wire(monkeypatch, _folder(4), [0, 1, 2, 3], [swapped])

    def pick(ranked, folder, *, interactive, tui):
        (c,) = ranked.candidates
        return c, "manual", ()

    monkeypatch.setattr(cli, "_choose_file_release", pick)
    prov: dict[str, str] = {}
    ident = _identify(prov, interactive=True)
    assert ident.keep == [1, 0, 2]
    assert prov["file_match_excluded"] == "04 Bonus 0.flac"
    assert (prov["file_match_tier"], prov["file_match_order"]) == ("B", "release")
    assert prov["release_selection"] == "manual"


def test_a_digital_release_lists_file_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _folder(), [0, 1, 2], [_release()], {RID: ("Digital Media",)})
    assert _identify({}).discogs_fmt == "File"


def test_create_batches_by_the_release_s_discs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """kgr, 2026-11-28, option 2: identify the whole input first; the chosen
    release's media are the discs. Five files, a two-disc release of 2 + 2
    tracks in another order, one file on neither disc."""
    names = [f"{n:02d}.flac" for n in range(1, 6)]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    disc1 = DiscMeta(mb_release_id=RID, album="A", artist="B", disc_number=1)
    disc2 = DiscMeta(mb_release_id=RID, album="A", artist="B", disc_number=2)
    ident = cli.FileIdentity(
        disc1,
        [3, 0, 1, 2],
        discs=(((3, 0), disc1), ((1, 2), disc2)),
    )
    seen: dict = {}

    class Stop(Exception):
        pass

    def limits(batches, capacity_minutes):
        seen["batches"] = [[p.name for p in b] for b in batches]
        raise Stop

    monkeypatch.setattr(cli, "get_audio_duration_minutes", lambda f: 4.0)
    monkeypatch.setattr(cli, "_input_evidence", lambda f: _evidence("A", "B"))
    monkeypatch.setattr(cli, "_identify_files", lambda *a, **k: ident)
    monkeypatch.setattr(cli, "_check_batch_limits", limits)
    with pytest.raises(Stop):
        cli.create_image(tmp_path, silence_mode="notrim")
    # Disc 1: files 4 and 1, in the release's order; disc 2: files 2 and 3;
    # 05.flac is on neither disc and in no batch.
    assert seen["batches"] == [["04.flac", "01.flac"], ["02.flac", "03.flac"]]


def test_without_a_release_the_strategy_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ident = cli.FileIdentity(None, [1, 0], album="A", artist="B")
    paths = [Path("a.flac"), Path("b.flac")]
    monkeypatch.setattr(
        cli, "select_batches", lambda files, s, capacity_minutes: [list(files)]
    )
    batches, metas, layout = cli._create_batches(paths, ident, "aatc", 80)
    assert [[p.name for p in b] for b in batches] == [["b.flac", "a.flac"]]
    assert (metas, layout) == ([None], "strategy:aatc")


def test_a_three_disc_release_lays_out_three_discs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Platinum Collection shape: media of 20, 18 and 19 tracks against the
    57 files of the input give three discs of exactly those sizes, in order."""
    sizes = (20, 18, 19)
    files = tuple(
        fm.FileTrack(position=i, name=f"{i:02d}.ogg", tag_recording=_rec(i + 1))
        for i in range(sum(sizes))
    )
    media, n = [], 0
    for d, size in enumerate(sizes, start=1):
        media.append(
            DiscMeta(
                mb_release_id=RID,
                mb_release_group_id=RG,
                album="The Platinum Collection",
                artist="David Bowie",
                disc_number=d,
                disc_total=3,
                tracks=[
                    TrackMeta(number=k, title=f"T{n + k}", recording_id=_rec(n + k))
                    for k in range(1, size + 1)
                ],
            )
        )
        n += size
    _wire(
        monkeypatch,
        fm.FolderEvidence(files, tagged_release=RID),
        list(range(57)),
        media,
    )
    ident = _identify({})
    assert [len(idx) for idx, _ in ident.discs] == [20, 18, 19]
    assert [m.disc_number for _, m in ident.discs] == [1, 2, 3]
    assert ident.discs[1][0][0] == 20  # disc 2 starts at the 21st file


def test_leaving_a_screen_of_contradicted_releases_chooses_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is marked from tier C, so leaving accepts nothing (§14.4)."""
    contradicted = _retitled(_release())
    folder = _folder()
    ranked = fm.rank(folder, [contradicted])
    assert [c.tier for c in ranked.candidates] == ["C"]
    seen = {}

    def run(self):
        seen["marked"] = self.disc.mb_release_id
        seen["outcome"] = self.pressing_outcome
        return self.disc

    monkeypatch.setattr("cdda2img.menu_state.MenuController.run", run)
    out = cli._choose_file_release(ranked, folder, interactive=True, tui=False)
    assert out == (None, None, ())
    assert seen == {"marked": None, "outcome": "none"}


def test_one_tier_a_release_needs_no_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    def never(self):
        msg = "the screen opened"
        raise AssertionError(msg)

    monkeypatch.setattr("cdda2img.menu_state.MenuController.run", never)
    ranked = fm.rank(_folder(), [_release()])
    chosen, selection, _ = cli._choose_file_release(
        ranked, _folder(), interactive=True, tui=False
    )
    assert chosen is not None and selection == "unique"


# ── gathering candidates (§14.2 steps 2 and 3) ──────────────────────────────


def _rel(rid: str, *counts: int) -> dict:
    return {"id": rid, "medium-list": [{"track-count": str(c)} for c in counts]}


def test_releases_holding_both_sampled_recordings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_rec = {
        _rec(1): [_rel("album", 3), _rel("single", 1), _rel("box", 20)],
        _rec(2): [_rel("album", 3), _rel("other", 3)],
    }
    monkeypatch.setattr(
        "cdda2img.acoustid_lookup.releases_for_recording", lambda r: by_rec.get(r, [])
    )
    # Every file has a tag recording (equally unambiguous): files 1 and 2 sampled.
    ids = cli._release_ids_by_recordings(_folder(tagged=None))
    # "album" is in both pools; the 20-track box set cannot fit; the rest follow.
    assert ids == ["album", "other", "single"]


def test_the_least_ambiguous_files_are_sampled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live, Night and Day: track 2's first AcoustID hit was another song's demo;
    a file with one recording names its song without doubt."""
    files = (
        fm.FileTrack(0, "01.flac", fp_recordings=frozenset({"x", "y"}), fp_best="x"),
        fm.FileTrack(
            1, "02.flac", fp_recordings=frozenset({"demo", "z"}), fp_best="demo"
        ),
        fm.FileTrack(2, "03.flac", fp_recordings=frozenset({"tv"}), fp_best="tv"),
        fm.FileTrack(
            3, "04.flac", fp_recordings=frozenset({"target"}), fp_best="target"
        ),
    )
    browsed: list[str] = []
    monkeypatch.setattr(
        "cdda2img.acoustid_lookup.releases_for_recording",
        lambda r: browsed.append(r) or [],
    )
    cli._release_ids_by_recordings(fm.FolderEvidence(files))
    assert browsed == ["tv", "target"]


def test_without_recordings_the_tags_are_searched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = fm.FolderEvidence(
        tuple(fm.FileTrack(position=i, name=f"{i}.flac") for i in range(3)),
        album="Night and Day",
        artist="Joe Jackson",
    )
    stubs = [
        DiscMeta(mb_release_id="nine", track_count=9),
        DiscMeta(mb_release_id="three", track_count=3),
    ]
    monkeypatch.setattr("cdda2img.mb_lookup.search_releases", lambda q: stubs)
    fetched: list[str] = []

    def media(rid: str):
        fetched.append(rid)
        return [DiscMeta(mb_release_id=rid)], ("CD",)

    monkeypatch.setattr("cdda2img.mb_lookup.release_media", media)
    g = cli._gather_candidates(folder, set())
    assert fetched == ["three"]  # nine tracks cannot all be in three files
    assert g.formats == {"three": ("CD",)}
    assert (g.found, g.fetched) == (1, 1)


def test_a_tagged_release_in_tier_a_ends_the_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _folder(), [0, 1, 2], [_release()])
    called = []
    monkeypatch.setattr(
        cli,
        "_gather_candidates",
        lambda *a: called.append(1) or cli.Gathered([], {}, 0, 0, False),
    )
    _identify({})
    assert called == []


def test_a_contradicted_tagged_release_looks_further(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the tags name a release the files contradict, so the search
    goes on, and the release it finds is taken."""
    folder = _folder()
    bad = _retitled(_release())  # the folder's title tags contradict it
    good = _release("6000d000-0000-4000-8000-000000000000")
    _wire(monkeypatch, folder, [0, 1, 2], [bad])
    monkeypatch.setattr(
        cli, "_gather_candidates", lambda *a: cli.Gathered([good], {}, 1, 1, False)
    )
    prov: dict[str, str] = {}
    ident = _identify(prov)
    assert ident.meta is not None and ident.meta.mb_release_id == good.mb_release_id
    assert prov["mb_identified_via"] == "file_match"


def test_every_file_is_fingerprinted(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fp(wav: Path):
        calls.append(wav.name)
        return None if wav.name == "b" else [(_rec(1), "Another World")]

    monkeypatch.setattr("cdda2img.acoustid_lookup.is_available", lambda: True)
    monkeypatch.setattr("cdda2img.acoustid_lookup.fingerprint_recordings", fp)
    out, status = cli._fingerprint_files([Path("c"), Path("a"), Path("b")])
    assert calls == ["c", "a", "b"]  # every file, in the order given
    assert set(out) == {Path("a"), Path("c")}
    # Some answered, one did not: neither an outage nor complete (Toca, live).
    assert status == "partial"


def test_the_release_browse_sets_the_user_agent_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On an untagged create the browse is the run's first MusicBrainz request;
    musicbrainzngs refuses it without a user agent, and the refusal read as "no
    releases" (live, 2026-11-27)."""
    from cdda2img import acoustid_lookup

    order: list[str] = []
    monkeypatch.setattr(
        "cdda2img.mb_lookup._setup_useragent", lambda: order.append("agent")
    )
    monkeypatch.setattr(
        acoustid_lookup,
        "_browse_releases_for_recording",
        lambda r: order.append("browse") or [],
    )
    acoustid_lookup.releases_for_recording(_rec(1))
    assert order == ["agent", "browse"]


def test_finding_nothing_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _folder(tagged=None), [0, 1, 2], [])
    prov: dict[str, str] = {}
    assert _identify(prov).meta is None
    assert prov["file_match_rejected"] == "no_candidates"


def test_the_album_s_pressings_are_fetched_first() -> None:
    """Live, Night and Day: the cap took ids alphabetically and left out the
    pressings the tags named."""
    pool = {
        "1other": {"rg": "2for1", "country": "ZA", "fit": 9, "hits": 2},
        "9cd-gb": {"rg": "album", "country": "GB", "fit": 9, "hits": 2},
        "5cd-de": {"rg": "album", "country": "DE", "fit": 9, "hits": 2},
    }
    assert cli._order_pool(pool, ["GB"]) == ["9cd-gb", "5cd-de", "1other"]


def _gathered(monkeypatch, g, metas):
    _wire(monkeypatch, _folder(tagged=None), [0, 1, 2], metas)
    monkeypatch.setattr(cli, "_gather_candidates", lambda *a: g)


def test_one_release_of_a_capped_set_is_not_a_pressing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One tier-A release among those compared is not one among all that exist."""
    only = replace(_release(), barcode="0082839490627")
    _gathered(monkeypatch, cli.Gathered([only], {}, 30, 25, False), [])
    prov: dict[str, str] = {}
    ident = _identify(prov)
    assert ident.group_only and ident.meta is not None
    assert ident.meta.mb_release_id is None
    assert ident.meta.barcode is None and ident.meta.label is None
    assert prov["file_match_incomplete"] == "capped"
    assert prov["file_match_fetched"] == "25/30"
    assert prov["release_selection"] == "release_group_only"


def test_one_release_of_a_complete_set_is_the_pressing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control."""
    _gathered(monkeypatch, cli.Gathered([_release()], {}, 1, 1, False), [])
    prov: dict[str, str] = {}
    ident = _identify(prov)
    assert ident.meta is not None and ident.meta.mb_release_id == RID
    assert prov["release_selection"] == "unique"


def test_an_outage_while_gathering_is_not_nothing_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _gathered(monkeypatch, cli.Gathered([], {}, 0, 0, True), [])
    prov: dict[str, str] = {}
    assert _identify(prov).meta is None
    assert prov["file_match_rejected"] == "unanswered"


def test_a_unanimous_barcode_never_becomes_the_mcn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§13.7: kept because every candidate states it, not a pressing's own."""
    a = replace(
        _release("a0000000-0000-4000-8000-000000000000"), barcode="0082839490627"
    )
    b = replace(
        _release("b0000000-0000-4000-8000-000000000000"), barcode="0082839490627"
    )
    _gathered(monkeypatch, cli.Gathered([a, b], {}, 2, 2, False), [])
    prov: dict[str, str] = {}
    ident = _identify(prov)
    assert ident.meta is not None and ident.meta.barcode == "0082839490627"
    assert prov["barcode_source"] == "unanimous"
    disc = cli.RBIDisc(album="A", artist="B", barcode=ident.meta.barcode)
    cli._finalize_identifiers(prov, disc)
    assert disc.catalog is None and "mcn_source" not in prov


def test_the_real_release_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    """The menu loop itself, not a stub: open row 2 and take it; leave the mark in
    place; answer none of these."""
    a = _release("a0000000-0000-4000-8000-000000000000")
    b = _release("b0000000-0000-4000-8000-000000000000")
    folder = _folder(tagged=None)
    ranked = fm.rank(folder, [a, b])
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    # Opening a row fetches its annotation; never from a test.
    monkeypatch.setattr("cdda2img.mb_lookup.fetch_annotation", lambda rid: "")

    def run(*keys: str):
        with patch("cdda2img.metadata_menu._prompt", side_effect=list(keys)):
            return cli._choose_file_release(ranked, folder, interactive=True, tui=False)

    chosen, selection, _ = run("2", "a", "s")
    assert selection == "manual" and chosen.meta.mb_release_id == b.mb_release_id
    chosen, selection, _ = run("s")
    assert selection == "accepted" and chosen.meta.mb_release_id == a.mb_release_id
    assert run("x") == (None, "rejected", ())


# ── kgr's rulings of 2026-11-28 ────────────────────────────────────────────


def test_nothing_named_aborts_unattended(monkeypatch: pytest.MonkeyPatch) -> None:
    """No release and no album/artist tags: --auto writes nothing (exit 3)."""
    from cdda2img.rip_abort import EXIT_INVESTIGATE, RipAborted

    _wire(monkeypatch, _folder(tagged=None), [0, 1, 2], [])
    with pytest.raises(RipAborted) as e:
        _identify({}, evidence=_evidence(None, None))
    abort = e.value
    assert isinstance(abort, RipAborted) and abort.code == EXIT_INVESTIGATE


def test_the_tags_name_the_album_when_no_release_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: album and artist tags are a deduction; no abort, no question."""
    _wire(monkeypatch, _folder(tagged=None), [0, 1, 2], [])
    prov: dict[str, str] = {}
    ident = _identify(prov, evidence=_evidence("Obscure", "Nobody"))
    assert (ident.album, ident.artist) == ("Obscure", "Nobody")
    assert prov["album_source"] == "tags"


def test_interactive_asks_at_the_end_and_searches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _folder(tagged=None), [0, 1, 2], [])
    searched: list[tuple] = []

    def gather(folder, have, pc=(), *, search_only=False):
        searched.append((folder.album, folder.artist, search_only))
        found = [_release()] if search_only else []  # only the search finds it
        return cli.Gathered(found, {}, len(found), len(found), False)

    monkeypatch.setattr(cli, "_gather_candidates", gather)
    with patch(
        "cdda2img.metadata_menu._prompt", side_effect=["Night and Day", "Joe Jackson"]
    ):
        prov: dict[str, str] = {}
        ident = _identify(prov, interactive=True, evidence=_evidence(None, None))
    assert searched[-1] == ("Night and Day", "Joe Jackson", True)
    assert ident.meta is not None and ident.meta.mb_release_id == RID
    assert prov["album_source"] == "release"


def test_interactive_keeps_the_users_words_when_nothing_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _folder(tagged=None), [0, 1, 2], [])
    with patch("cdda2img.metadata_menu._prompt", side_effect=["Home Tape", "Me"]):
        prov: dict[str, str] = {}
        ident = _identify(prov, interactive=True, evidence=_evidence(None, None))
    assert (ident.meta, ident.album, ident.artist) == (None, "Home Tape", "Me")
    assert prov["album_source"] == "user"


def test_a_blank_album_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from cdda2img.rip_abort import RipAborted

    _wire(monkeypatch, _folder(tagged=None), [0, 1, 2], [])
    with (
        patch("cdda2img.metadata_menu._prompt", side_effect=[""]),
        pytest.raises(RipAborted),
    ):
        _identify({}, interactive=True, evidence=_evidence(None, None))


def test_a_tagged_vinyl_release_is_not_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kgr: CD and digital media releases only."""
    tags = {"musicbrainz_albumid": [RID]}
    paths = _stub_tags(monkeypatch, [tags, tags])
    monkeypatch.setattr(
        "cdda2img.mb_lookup.release_media", lambda r: ([_release()], ('12" Vinyl',))
    )
    prov: dict[str, str] = {}
    assert cli._gather_tagged(paths, prov) == ([], {})
    assert prov["file_tag_release_check"] == "not_cd_or_digital"


def test_vinyl_never_enters_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    def rel(rid: str, fmt: str) -> dict:
        return {"id": rid, "medium-list": [{"track-count": "3", "format": fmt}]}

    monkeypatch.setattr(
        "cdda2img.acoustid_lookup.releases_for_recording",
        lambda r: [rel("lp", '12" Vinyl'), rel("cd", "CD"), rel("mc", "Cassette")],
    )
    assert cli._release_ids_by_recordings(_folder(tagged=None)) == ["cd"]


def test_a_bare_number_is_a_name_number_not_a_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata, "File", lambda name, easy=True: {})
    files, _ = metadata.read_file_tracks([Path("/u/01.flac")])
    assert (files[0].title, files[0].name_number, files[0].name_title) == (
        None,
        1,
        None,
    )


def _retitled(meta: DiscMeta) -> DiscMeta:
    return replace(
        meta,
        tracks=[replace(t, title=f"Different Song {t.number}") for t in meta.tracks],
    )


def test_every_fingerprint_failing_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for "partial"."""
    monkeypatch.setattr("cdda2img.acoustid_lookup.is_available", lambda: True)
    monkeypatch.setattr(
        "cdda2img.acoustid_lookup.fingerprint_recordings", lambda w: None
    )
    assert cli._fingerprint_files([Path("a"), Path("b")]) == ({}, "down")


def test_a_digital_release_s_barcode_never_becomes_the_mcn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kgr, 2026-11-28: digital barcodes are PROV-only (Toca wrote 5037300003931
    as the disc's MCN)."""
    digital = replace(_release(), barcode="5037300003931")
    _wire(monkeypatch, _folder(), [0, 1, 2], [digital], {RID: ("Digital Media",)})
    prov: dict[str, str] = {}
    ident = _identify(prov)
    assert prov["barcode_source"] == "digital"
    assert ident.meta is not None
    disc = cli.RBIDisc(album="A", artist="B", barcode=ident.meta.barcode)
    cli._finalize_identifiers(prov, disc)
    assert disc.catalog is None and "mcn_source" not in prov
    assert prov["barcode"] == "5037300003931"  # kept, in PROV


@pytest.mark.parametrize(
    ("prov", "mcn"),
    [
        ({}, "5037300003931"),  # the control: a CD release's barcode synthesises
        ({"media_override": "digital_media"}, None),  # rip --digital-media
    ],
)
def test_mcn_synthesis_and_digital_media(prov: dict, mcn: str | None) -> None:
    disc = cli.RBIDisc(album="A", artist="B", barcode="5037300003931")
    cli._finalize_identifiers(prov, disc)
    assert disc.catalog == mcn


def test_the_file_match_s_release_skips_r3_s_duration_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toca, live: the original-release lookup's own duration sum refused the
    release the file match had accepted (its runtime class had judged them)."""
    from cdda2img.original_release import _rg_path_reject_reason
    from cdda2img.rbi_format import RBITocEntry

    meta = replace(
        _release(), tracks=[replace(t, duration_ms=200_000) for t in _release().tracks]
    )
    disc = cli.RBIDisc(album="A", artist="B", mb_release_id=RID)
    disc.tracks = [
        RBITocEntry(
            track_number=k,
            title=t,
            performer="B",
            start_frame=0,
            duration_frames=18_000,
        )
        for k, t in enumerate(TITLES, start=1)
    ]
    assert _rg_path_reject_reason(disc, meta) == "durations"  # the control
    assert _rg_path_reject_reason(disc, meta, runtimes_judged=True) is None


def test_gathering_keeps_the_whole_set_over_compilations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release is a candidate only when ALL its tracks fit in the input: a
    20-track compilation holding a sampled song fits 57 files too, but the
    3-CD set (57) is the closest fit and comes first; a 4-CD box never fits."""

    def rel(rid: str, *counts: int) -> dict:
        return {
            "id": rid,
            "release-group": {"id": "g"},
            "medium-list": [{"track-count": str(c), "format": "CD"} for c in counts],
        }

    monkeypatch.setattr(
        "cdda2img.acoustid_lookup.releases_for_recording",
        lambda r: [rel("comp", 20), rel("set", 20, 18, 19), rel("box", 20, 20, 20, 20)],
    )
    files = tuple(
        fm.FileTrack(position=i, name=f"{i}.ogg", tag_recording=_rec(i + 1))
        for i in range(57)
    )
    assert cli._release_ids_by_recordings(fm.FolderEvidence(files)) == ["set", "comp"]


@pytest.mark.parametrize(
    ("album", "name"),
    [
        (
            "The Best of David Bowie 1969/1974",
            "The Best of David Bowie 1969-1974_disc1.rbi",
        ),
        ("AC\\DC", "AC-DC_disc1.rbi"),
        ("..", "untitled_disc1.rbi"),
        ("", "untitled_disc1.rbi"),
    ],
)
def test_an_album_title_names_one_file(
    album: str, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kgr, 2026-11-28: "1969/1974" made the output a file in a directory that did
    not exist, and the finished container could not be written."""
    monkeypatch.chdir(tmp_path)
    out = cli._resolve_output_path(None, album, "_disc1")
    assert out == Path(name) and out.parent == Path()
    assert cli._resolve_output_path(tmp_path, album, "_disc1") == tmp_path / name
