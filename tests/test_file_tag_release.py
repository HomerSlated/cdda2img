"""`create`: the release the files' tags name, and the file match that settles
which release, which files and which order the archive takes (kgr, 2026-11-27;
trust_model_design.md §14; rbi_spec.md ``file_tag_release``, ``file_match*``)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import av
import pytest
from mutagen.flac import FLAC

from cdda2img import cdda2img as cli
from cdda2img import file_match as fm
from cdda2img import metadata
from cdda2img import release_match as rm
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


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    folder: fm.FolderEvidence,
    order: list[int],
    metas: list[DiscMeta],
    formats: dict | None = None,
) -> None:
    monkeypatch.setattr(cli, "_folder_evidence", lambda b, t: (folder, order))
    monkeypatch.setattr(cli, "_gather_tagged", lambda b, p: (metas, formats or {}))


def _identify(prov: dict[str, str], *, interactive: bool = False) -> cli.FileIdentity:
    return cli._identify_files([], [], prov, interactive=interactive, tui=False)


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


def test_create_builds_the_disc_from_the_kept_files_in_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The restructure: concat, durations and the TOC see only the kept files, in
    the chosen release's order."""
    names = ["01 a.flac", "02 b.flac", "03 c.flac"]
    for n in names:
        (tmp_path / n).write_bytes(b"x")
    paths = [tmp_path / n for n in names]
    seen: dict[str, list[str]] = {}

    class Stop(Exception):
        pass

    def concat(wavs, _out):
        seen["concat"] = [w.name for w in wavs]

    def toc_entries(batch, _durations, _disc):
        seen["toc"] = [p.name for p in batch]
        raise Stop

    monkeypatch.setattr(cli, "select_batches", lambda *a, **k: [paths])
    monkeypatch.setattr(cli, "_check_batch_limits", lambda *a, **k: None)
    monkeypatch.setattr(
        cli, "derive_album_info", lambda f: {"album": "A", "artist": "B"}
    )
    monkeypatch.setattr(cli, "transcode_audio", lambda src, dst: None)
    monkeypatch.setattr(
        cli,
        "_identify_files",
        lambda *a, **k: cli.FileIdentity(None, [2, 0]),
    )
    monkeypatch.setattr(cli, "concat_wav", concat)
    monkeypatch.setattr(cli, "wav_to_raw_pcm", lambda *a: None)
    monkeypatch.setattr(cli, "track_frame_durations", lambda w: ([1] * len(w), len(w)))
    monkeypatch.setattr(cli, "pad_pcm_to_declared_frames", lambda *a: None)
    monkeypatch.setattr(cli, "build_toc_entries", toc_entries)
    with pytest.raises(Stop):
        cli.create_image(tmp_path, silence_mode="notrim")
    assert seen["toc"] == ["03 c.flac", "01 a.flac"]
    assert seen["concat"] == ["temp_track_3_trans.wav", "temp_track_1_trans.wav"]


def test_leaving_a_screen_of_contradicted_releases_chooses_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is marked from tier C, so leaving accepts nothing (§14.4)."""
    tags = rm.ReleaseRecord("tags", "", barcodes=("082839490627",))
    contradicted = replace(_release(), barcode="0075021490628")
    folder = replace(_folder(), tags=tags)
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
