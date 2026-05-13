"""
test_metadata_menu.py — Tests for metadata_menu pure-logic functions and discogs_lookup.

Interactive menu functions require TTY input and are not unit-tested here.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from cdda2img.lookup_result import DiscMeta, TrackMeta
from cdda2img.metadata_menu import _show_diff, _trunc, run_metadata_menu
from cdda2img.rbi_format import RBIDisc, RBITocEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disc(album: str = "Album", artist: str = "Artist", tracks: int = 2) -> RBIDisc:
    entries = [
        RBITocEntry(
            i, f"Track {i}", artist, start_frame=(i - 1) * 10000, duration_frames=10000
        )
        for i in range(1, tracks + 1)
    ]
    return RBIDisc(album=album, artist=artist, tracks=entries)


def _meta(**kw) -> DiscMeta:
    return DiscMeta(**kw)


# ---------------------------------------------------------------------------
# _trunc
# ---------------------------------------------------------------------------


def test_trunc_short():
    assert _trunc("hello", 10) == "hello"


def test_trunc_exact():
    assert _trunc("hello", 5) == "hello"


def test_trunc_long():
    result = _trunc("hello world", 8)
    assert len(result) == 8
    assert result.endswith("…")


def test_trunc_none():
    assert _trunc(None, 10) == ""


# ---------------------------------------------------------------------------
# _show_diff (captured via output)
# ---------------------------------------------------------------------------


def test_show_diff_no_changes(capsys):
    disc = _disc("Same Album", "Same Artist")
    meta = _meta(album="Same Album", artist="Same Artist")
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    assert "no fields" in out


def test_show_diff_album_change(capsys):
    disc = _disc(album="")
    meta = _meta(album="New Album", artist="Artist")
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    assert "Album" in out
    assert "New Album" in out


def test_show_diff_unknown_artist_replaced(capsys):
    disc = _disc(artist="Unknown Artist")
    meta = _meta(artist="Real Artist")
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    assert "Real Artist" in out


def test_show_diff_isrc_added(capsys):
    disc = _disc()
    meta = _meta(
        tracks=[
            TrackMeta(number=1, isrc="BEXX89300001"),
            TrackMeta(number=2, isrc="BEXX89300002"),
        ]
    )
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    assert "BEXX89300001" in out
    assert "BEXX89300002" in out


def test_show_diff_existing_isrc_not_shown(capsys):
    disc = _disc()
    disc.tracks[0].isrc = "EXISTING0001"
    meta = _meta(tracks=[TrackMeta(number=1, isrc="NEWISRC0001")])
    _show_diff(meta, disc)
    out = capsys.readouterr().out
    # existing ISRC is not None, so no "+" line for it
    assert "NEWISRC0001" not in out


# ---------------------------------------------------------------------------
# run_metadata_menu — non-TTY path
# ---------------------------------------------------------------------------


def test_run_metadata_menu_non_tty_returns_disc_unchanged():
    disc = _disc()
    with patch("sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        result = run_metadata_menu(disc)
    assert result is disc


# ---------------------------------------------------------------------------
# _clear_disc
# ---------------------------------------------------------------------------


def test_clear_disc_wipes_metadata():
    from cdda2img.metadata_menu import _clear_disc

    disc = _disc("My Album", "My Artist")
    disc.tracks[0].isrc = "USTEST000001"
    cleared = _clear_disc(disc)

    assert cleared.album == ""
    assert cleared.artist == ""
    assert cleared.catalog is None
    assert cleared.disc_number == disc.disc_number
    assert cleared.disc_total == disc.disc_total
    assert len(cleared.tracks) == len(disc.tracks)
    for t in cleared.tracks:
        assert t.title == ""
        assert t.performer == ""
        assert t.isrc is None


def test_clear_disc_preserves_timing():
    from cdda2img.metadata_menu import _clear_disc

    disc = _disc()
    cleared = _clear_disc(disc)
    for orig, new in zip(disc.tracks, cleared.tracks):
        assert new.track_number == orig.track_number
        assert new.start_frame == orig.start_frame
        assert new.duration_frames == orig.duration_frames
        assert new.pregap_frames == orig.pregap_frames


# ---------------------------------------------------------------------------
# discogs_lookup — unit tests
# ---------------------------------------------------------------------------


def test_discogs_is_available_false_without_token():
    from cdda2img import discogs_lookup

    with patch.dict("os.environ", {}, clear=True):
        assert not discogs_lookup.is_available()


def test_discogs_is_available_true_with_token():
    from cdda2img import discogs_lookup

    with patch.dict("os.environ", {"DISCOGS_TOKEN": "fake_token"}):
        assert discogs_lookup.is_available()


def test_discogs_search_returns_empty_without_token():
    from cdda2img import discogs_lookup

    with patch.dict("os.environ", {}, clear=True):
        results = discogs_lookup.search_releases("Technotronic")
    assert results == []


def test_discogs_parse_result_artist_album_split():
    from cdda2img.discogs_lookup import _parse_result

    r = SimpleNamespace(
        data={
            "title": "Technotronic - Pump Up the Jam",
            "year": 1989,
            "country": "Belgium",
            "label": ["Epic"],
            "catno": "466247 2",
            "barcode": ["5099747023521"],
        }
    )
    meta = _parse_result(r)
    assert meta.artist == "Technotronic"
    assert meta.album == "Pump Up the Jam"
    assert meta.release_date == "1989"
    assert meta.country == "Belgium"
    assert meta.label == "Epic"
    assert meta.catalog == "5099747023521"
    assert meta.source == "discogs"


def test_discogs_parse_result_no_separator():
    from cdda2img.discogs_lookup import _parse_result

    r = SimpleNamespace(data={"title": "Just An Album", "year": None})
    meta = _parse_result(r)
    assert meta.artist is None
    assert meta.album == "Just An Album"


def test_discogs_parse_result_remaster_classification():
    from cdda2img.discogs_lookup import _parse_result
    from cdda2img.lookup_result import REMASTERED_POSSIBLE

    r = SimpleNamespace(data={"title": "Artist - Album", "year": 2005})
    meta = _parse_result(r)
    assert meta.remastered_source == REMASTERED_POSSIBLE


# ---------------------------------------------------------------------------
# acoustid_lookup — availability
# ---------------------------------------------------------------------------


def test_acoustid_not_available_without_key():
    from cdda2img import acoustid_lookup

    with patch.dict("os.environ", {}, clear=True):
        assert not acoustid_lookup.is_available()


def test_acoustid_reason_no_key():
    from cdda2img import acoustid_lookup

    with patch.dict("os.environ", {}, clear=True):
        reason = acoustid_lookup.unavailability_reason()
    assert "ACOUSTID_API_KEY" in reason


def test_acoustid_fingerprint_returns_empty_when_unavailable(tmp_path):
    from cdda2img import acoustid_lookup

    fake_wav = tmp_path / "test.wav"
    fake_wav.write_bytes(b"\x00" * 44)  # minimal non-empty file
    with patch.dict("os.environ", {}, clear=True):
        results = acoustid_lookup.fingerprint_and_lookup(fake_wav)
    assert results == []


# ---------------------------------------------------------------------------
# acoustid_lookup — fingerprint chain (mocked network)
# ---------------------------------------------------------------------------


def _mb_recording_response(
    recording_id, title, artist_name, release_id, album, date, country="US"
):
    """Build a minimal musicbrainzngs get_recording_by_id response dict."""
    return {
        "recording": {
            "id": recording_id,
            "title": title,
            "artist-credit": [{"artist": {"name": artist_name}, "joinphrase": ""}],
            "isrc-list": ["USTEST000001"],
            "release-list": [
                {
                    "id": release_id,
                    "title": album,
                    "date": date,
                    "country": country,
                    "release-group": {
                        "id": f"rg-{release_id}",
                        "first-release-date": date,
                    },
                }
            ],
        }
    }


def test_acoustid_fingerprint_chains_to_mb():
    """High-score match chains to MB and returns full release metadata."""
    from pathlib import Path

    from cdda2img import acoustid_lookup

    match_data = [(0.9, "rec-uuid-1", "Pump Up the Jam", "Technotronic")]
    mb_resp = _mb_recording_response(
        "rec-uuid-1",
        "Pump Up the Jam",
        "Technotronic",
        "rel-1",
        "Pump Up the Jam",
        "1989",
        "BE",
    )

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
        patch("musicbrainzngs.get_recording_by_id", return_value=mb_resp),
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    assert len(results) == 1
    r = results[0]
    assert r.album == "Pump Up the Jam"
    assert r.artist == "Technotronic"
    assert r.mb_release_id == "rel-1"
    assert r.country == "BE"
    assert r.release_date == "1989"
    assert r.tracks[0].isrc == "USTEST000001"
    assert r.source == "acoustid"


def test_acoustid_fingerprint_falls_back_on_mb_failure():
    """When the MB follow-up fails, a basic DiscMeta from AcoustID data is returned."""
    from pathlib import Path

    import musicbrainzngs

    from cdda2img import acoustid_lookup

    match_data = [(0.8, "rec-uuid-2", "Some Track", "Some Artist")]

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
        patch(
            "musicbrainzngs.get_recording_by_id",
            side_effect=musicbrainzngs.NetworkError("timeout"),
        ),
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    assert len(results) == 1
    assert results[0].artist == "Some Artist"
    assert results[0].album is None
    assert results[0].tracks[0].title == "Some Track"
    assert results[0].source == "acoustid"


def test_acoustid_fingerprint_filters_low_score():
    """Matches below the 0.5 confidence threshold are discarded before any MB call."""
    from pathlib import Path

    from cdda2img import acoustid_lookup

    match_data = [(0.3, "rec-uuid-low", "Weak Match", "Artist")]

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    assert results == []


def test_acoustid_fingerprint_deduplicates_releases():
    """The same release appearing under two recordings is returned only once."""
    from pathlib import Path

    from cdda2img import acoustid_lookup

    shared_release = {
        "id": "shared-rel",
        "title": "Shared Album",
        "date": "1989",
        "country": "US",
        "release-group": {"id": "rg-shared", "first-release-date": "1989"},
    }
    match_data = [
        (0.9, "rec-1", "Title A", "Artist"),
        (0.8, "rec-2", "Title B", "Artist"),
    ]
    resp_1 = {
        "recording": {
            "title": "Title A",
            "artist-credit": [],
            "isrc-list": [],
            "release-list": [shared_release],
        }
    }
    resp_2 = {
        "recording": {
            "title": "Title B",
            "artist-credit": [],
            "isrc-list": [],
            "release-list": [shared_release],
        }
    }

    def mb_side_effect(recording_id, **_kwargs):
        return resp_1 if recording_id == "rec-1" else resp_2

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch("acoustid.match", return_value=iter(match_data)),
        patch("musicbrainzngs.get_recording_by_id", side_effect=mb_side_effect),
    ):
        results = acoustid_lookup.fingerprint_and_lookup(Path("/fake/track.wav"))

    release_ids = [r.mb_release_id for r in results if r.mb_release_id]
    assert release_ids.count("shared-rel") == 1


# acoustid — track_number assignment and merge


def _mystery_disc() -> RBIDisc:
    """A disc with no metadata — empty titles, unknown artist — simulating a raw import."""
    entries = [
        RBITocEntry(i, "", "", start_frame=(i - 1) * 10000, duration_frames=10000)
        for i in range(1, 3)
    ]
    return RBIDisc(album="", artist="", tracks=entries)


def test_acoustid_run_one_assigns_track_number_to_single_track_result():
    """When track_number=1 is passed, the single TrackMeta gets number=1 so title merges."""
    from pathlib import Path

    from cdda2img.lookup_result import DiscMeta, TrackMeta
    from cdda2img.metadata_menu import _acoustid_run_one

    result_no_number = DiscMeta(
        artist="Technotronic",
        album="Pump Up the Jam",
        source="acoustid",
        tracks=[TrackMeta(title="Pump Up the Jam", performer="Technotronic")],
    )
    disc = _mystery_disc()

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch(
            "cdda2img.acoustid_lookup.fingerprint_and_lookup",
            return_value=[result_no_number],
        ),
        patch("cdda2img.metadata_menu._prompt", side_effect=["1", "u"]),
        patch("cdda2img.acoustid_lookup.is_available", return_value=True),
    ):
        updated = _acoustid_run_one(disc, Path("/fake/t.wav"), track_number=1)

    assert updated.artist == "Technotronic"
    assert updated.tracks[0].title == "Pump Up the Jam"  # title merged into track 1
    assert updated.tracks[1].title == ""  # track 2 untouched


def test_acoustid_run_one_without_track_number_does_not_apply_title():
    """Without track_number, track title is not merged (number=None means no match)."""
    from pathlib import Path

    from cdda2img.lookup_result import DiscMeta, TrackMeta
    from cdda2img.metadata_menu import _acoustid_run_one

    result_no_number = DiscMeta(
        artist="Technotronic",
        album="Pump Up the Jam",
        source="acoustid",
        tracks=[TrackMeta(title="Pump Up the Jam", performer="Technotronic")],
    )
    disc = _mystery_disc()

    with (
        patch.dict("os.environ", {"ACOUSTID_API_KEY": "fake"}),
        patch(
            "cdda2img.acoustid_lookup.fingerprint_and_lookup",
            return_value=[result_no_number],
        ),
        patch("cdda2img.metadata_menu._prompt", side_effect=["1", "u"]),
        patch("cdda2img.acoustid_lookup.is_available", return_value=True),
    ):
        updated = _acoustid_run_one(disc, Path("/fake/t.wav"))

    # Disc-level fields applied; track title stays empty — number=None means no match
    assert updated.artist == "Technotronic"
    assert updated.tracks[0].title == ""


# _pcm_extract_track_wav


def test_pcm_extract_track_wav_writes_correct_frames(tmp_path):
    """Extraction writes the correct number of PCM frames for the target track."""
    import wave

    from cdda2img.metadata_menu import _BYTES_PER_FRAME, _pcm_extract_track_wav
    from cdda2img.rbi_format import RBIDisc, RBITocEntry

    track_frames = 1000  # short track
    disc = RBIDisc(
        album="",
        artist="",
        tracks=[
            RBITocEntry(1, "", "", start_frame=0, duration_frames=track_frames),
            RBITocEntry(
                2, "", "", start_frame=track_frames, duration_frames=track_frames
            ),
        ],
    )
    # Write two tracks of silence as raw PCM
    pcm_path = tmp_path / "disc.pcm"
    pcm_path.write_bytes(bytes(2 * track_frames * _BYTES_PER_FRAME))

    out_path = tmp_path / "track01.wav"
    result = _pcm_extract_track_wav(disc, pcm_path, 1, out_path)

    assert result == out_path
    with wave.open(str(out_path)) as w:
        # getnframes() = audio sample frames; 1000 CD frames x 588 samples/CD frame
        from cdda2img.rbi_format import CD_FRAMES_PER_SECOND, PCM_SAMPLE_RATE

        samples_per_cd_frame = PCM_SAMPLE_RATE // CD_FRAMES_PER_SECOND
        assert w.getnframes() == track_frames * samples_per_cd_frame


def test_pcm_extract_track_wav_returns_none_for_missing_track(tmp_path):
    """Returns None when the requested track number is not in the disc."""
    from cdda2img.metadata_menu import _pcm_extract_track_wav

    disc = RBIDisc(album="", artist="", tracks=[])
    pcm_path = tmp_path / "disc.pcm"
    pcm_path.write_bytes(b"\x00" * 100)

    result = _pcm_extract_track_wav(disc, pcm_path, 99, tmp_path / "t.wav")
    assert result is None
