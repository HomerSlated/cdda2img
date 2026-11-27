"""`create` reads the MusicBrainz release id from the source files' tags (kgr,
2026-11-27; rbi_spec.md ``file_tag_release`` / ``file_tag_release_check``)."""

from __future__ import annotations

from pathlib import Path

import av
import pytest
from mutagen.flac import FLAC

from cdda2img import metadata
from cdda2img.cdda2img import _apply_file_tag_release
from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.metadata import FileTagRelease, read_file_tag_release
from cdda2img.rbi_format import RBIDisc, RBITocEntry

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


# ── _apply_file_tag_release ─────────────────────────────────────────────────


def _disc(n: int = 3, frames: int = 18000) -> RBIDisc:
    disc = RBIDisc(album="Night and Day", artist="Joe Jackson")
    disc.tracks = [
        RBITocEntry(
            track_number=i,
            title=f"0{i} Song {i}",
            performer="Joe Jackson",
            start_frame=(i - 1) * frames,
            duration_frames=frames,
        )
        for i in range(1, n + 1)
    ]
    return disc


def _release(n: int = 3, ms: int = 240_000, *, title: str = "Song") -> DiscMeta:
    return DiscMeta(
        album="Night and Day",
        artist="Joe Jackson",
        mb_release_id=RID,
        mb_release_group_id="0c55d762-c505-33fc-865b-5ac66cb90391",
        barcode="082839490627",
        catalog_number="394 906-2",
        label="A&M Records",
        country="DE",
        tracks=[
            TrackMeta(number=i, title=f"{title} {i}", duration_ms=ms)
            for i in range(1, n + 1)
        ],
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    tagged: tuple,
    fetched: tuple[DiscMeta, tuple[str, ...]] | None,
) -> list[tuple]:
    calls: list[tuple] = []

    def fetch(rid: str, disc_number: int | None = None):
        calls.append((rid, disc_number))
        return fetched

    monkeypatch.setattr(
        metadata, "read_file_tag_release", lambda _b: FileTagRelease(*tagged)
    )
    monkeypatch.setattr("cdda2img.mb_lookup.lookup_release_with_formats", fetch)
    return calls


def test_a_release_that_fits_is_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire(monkeypatch, (RID, RID, 1), (_release(), ("CD",)))
    disc = _disc()
    prov: dict[str, str] = {}

    out, meta, fmt = _apply_file_tag_release(disc, [], prov, check_durations=True)

    assert calls == [(RID, 1)]
    assert meta is not None and fmt == "CD"
    assert prov == {
        "file_tag_release": RID,
        "file_tag_release_check": "applied",
        "mb_identified_via": "file_tags",
    }
    assert out.mb_release_id == RID
    assert (out.label, out.catalog_number, out.country) == (
        "A&M Records",
        "394 906-2",
        "DE",
    )
    # Overwrite, not fill-blank: the filename stems give way to MusicBrainz's titles.
    assert [t.title for t in out.tracks] == ["Song 1", "Song 2", "Song 3"]
    # The geometry is the files', never the release's.
    assert [(t.start_frame, t.duration_frames) for t in out.tracks] == [
        (t.start_frame, t.duration_frames) for t in disc.tracks
    ]


def test_a_digital_release_lists_file_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, (RID, RID, None), (_release(), ("Digital Media",)))
    _, _, fmt = _apply_file_tag_release(_disc(), [], {}, check_durations=True)
    assert fmt == "File"


def test_a_mixed_release_lists_cd_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, (RID, RID, None), (_release(), ("Digital Media", "CD")))
    _, _, fmt = _apply_file_tag_release(_disc(), [], {}, check_durations=True)
    assert fmt == "CD"


def test_a_wrong_track_count_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, (RID, RID, None), (_release(n=4), ("CD",)))
    disc = _disc()
    prov: dict[str, str] = {}

    out, meta, _ = _apply_file_tag_release(disc, [], prov, check_durations=True)

    assert out is disc and meta is None
    assert prov == {
        "file_tag_release": RID,
        "file_tag_release_check": "rejected:track_count",
    }


def test_trim_skips_only_the_duration_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under --silence trim the durations are ours, so the gate is skipped; the
    control is the same release refused on durations when the gate runs."""
    far = _release(ms=200_000)  # 3 x 40 s short of the disc
    _wire(monkeypatch, (RID, RID, None), (far, ("CD",)))
    prov: dict[str, str] = {}
    _apply_file_tag_release(_disc(), [], prov, check_durations=True)
    assert prov["file_tag_release_check"] == "rejected:durations"

    prov = {}
    out, _, _ = _apply_file_tag_release(_disc(), [], prov, check_durations=False)
    assert prov["file_tag_release_check"] == "applied:no_durations"
    assert out.mb_release_id == RID


def test_no_answer_applies_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, (RID, RID, None), None)
    disc = _disc()
    prov: dict[str, str] = {}
    out, meta, fmt = _apply_file_tag_release(disc, [], prov, check_durations=True)
    assert (out, meta, fmt) == (disc, None, "CD")
    assert prov == {"file_tag_release": RID, "file_tag_release_check": "unanswered"}


@pytest.mark.parametrize("state", ["mixed", "invalid"])
def test_an_unusable_tag_is_recorded_and_not_fetched(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    calls = _wire(monkeypatch, (None, state, None), (_release(), ("CD",)))
    prov: dict[str, str] = {}
    _apply_file_tag_release(_disc(), [], prov, check_durations=True)
    assert calls == []
    assert prov == {"file_tag_release": state}


def test_untagged_files_leave_no_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire(monkeypatch, (None, None, None), (_release(), ("CD",)))
    prov: dict[str, str] = {}
    disc = _disc()
    out, meta, fmt = _apply_file_tag_release(disc, [], prov, check_durations=True)
    assert (out, meta, fmt, prov, calls) == (disc, None, "CD", {}, [])
