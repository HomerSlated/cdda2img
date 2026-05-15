"""Tests for catalogue.py — schema, parsing helpers, duplicate detection, registration."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from cdda2img.catalogue import (
    _find_duplicates,
    _parse_prov,
    _parse_year,
    catalogue_db_path,
    open_catalogue_db,
)

# ---------------------------------------------------------------------------
# catalogue_db_path
# ---------------------------------------------------------------------------


def test_catalogue_db_path_default(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    p = catalogue_db_path()
    assert p == tmp_path / ".local" / "share" / "cdda2img" / "cdda2img.db"


def test_catalogue_db_path_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    p = catalogue_db_path()
    assert p == tmp_path / "xdg" / "cdda2img" / "cdda2img.db"


# ---------------------------------------------------------------------------
# open_catalogue_db — schema creation
# ---------------------------------------------------------------------------


def test_open_catalogue_db_creates_schema(tmp_path):
    db_path = tmp_path / "test.db"
    conn = open_catalogue_db(db_path)
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"db_meta", "catalogue", "catalogue_tracks"} <= tables
    finally:
        conn.close()


def test_open_catalogue_db_sets_meta(tmp_path):
    db_path = tmp_path / "test.db"
    conn = open_catalogue_db(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM db_meta WHERE key='schema_version'"
        ).fetchone()
        assert row is not None
        assert row[0] == "1"
    finally:
        conn.close()


def test_open_catalogue_db_reopens_existing(tmp_path):
    """Re-opening an existing DB must not raise (schema_version check passes)."""
    db_path = tmp_path / "test.db"
    conn = open_catalogue_db(db_path)
    conn.close()
    conn2 = open_catalogue_db(db_path)
    conn2.close()


def test_open_catalogue_db_too_new_raises(tmp_path):
    db_path = tmp_path / "test.db"
    conn = open_catalogue_db(db_path)
    conn.execute("UPDATE db_meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="schema v99"):
        open_catalogue_db(db_path)


# ---------------------------------------------------------------------------
# _parse_prov
# ---------------------------------------------------------------------------


def test_parse_prov_basic():
    data = b"release_date=2005-09-13\nartist=Green Day\n"
    result = _parse_prov(data)
    assert result["release_date"] == "2005-09-13"
    assert result["artist"] == "Green Day"


def test_parse_prov_empty():
    assert _parse_prov(b"") == {}


def test_parse_prov_comments_ignored():
    data = b"# comment\nkey=value\n"
    result = _parse_prov(data)
    assert result == {"key": "value"}


def test_parse_prov_invalid_utf8():
    assert _parse_prov(b"\xff\xfe") == {}


def test_parse_prov_no_equals():
    assert _parse_prov(b"noequals\n") == {}


def test_parse_prov_value_contains_equals():
    data = b"url=http://example.com/path?a=b\n"
    result = _parse_prov(data)
    assert result["url"] == "http://example.com/path?a=b"


# ---------------------------------------------------------------------------
# _parse_year
# ---------------------------------------------------------------------------


def test_parse_year_full_date():
    assert _parse_year("2005-09-13") == 2005


def test_parse_year_year_only():
    assert _parse_year("1999") == 1999


def test_parse_year_none():
    assert _parse_year(None) is None


def test_parse_year_empty():
    assert _parse_year("") is None


def test_parse_year_no_leading_digits():
    assert _parse_year("unknown") is None


# ---------------------------------------------------------------------------
# _find_duplicates — direct SQL fixture setup
# ---------------------------------------------------------------------------


def _insert_disc(
    conn: sqlite3.Connection,
    *,
    album: str = "Test Album",
    artist: str = "Test Artist",
    year: int | None = 2000,
    disc_number: int = 1,
    disc_total: int = 1,
    track_count: int = 2,
    mcn: str | None = None,
    durations: tuple[int, ...] = (11025, 22050),
    ar_crcs: tuple[str | None, ...] = (None, None),
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO catalogue
           (album, artist, year, disc_number, disc_total, track_count, mcn,
            file_basename, file_path, file_size, registered_at,
            created_by, mode, remaster)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            album,
            artist,
            year,
            disc_number,
            disc_total,
            track_count,
            mcn,
            "test.rbi",
            "/tmp/test.rbi",  # noqa: S108
            0,
            now,
            "test",
            "?",
            "UNKNOWN",
        ),
    )
    assert cur.lastrowid is not None
    cat_id: int = cur.lastrowid
    for i, (dur, crc) in enumerate(zip(durations, ar_crcs), start=1):
        conn.execute(
            """INSERT INTO catalogue_tracks
               (catalogue_id, track_number, title, duration_frames, ar_v1_crc)
               VALUES (?,?,?,?,?)""",
            (cat_id, i, f"Track {i}", dur, crc),
        )
    conn.commit()
    return cat_id


def test_find_duplicates_exact_match(tmp_path):
    conn = open_catalogue_db(tmp_path / "c.db")
    cat_id = _insert_disc(conn)
    matches = _find_duplicates(
        conn,
        "Test Album",
        "Test Artist",
        2000,
        1,
        1,
        2,
        None,
        [11025, 22050],
        [None, None],
    )
    conn.close()
    assert matches == [cat_id]


def test_find_duplicates_different_album(tmp_path):
    conn = open_catalogue_db(tmp_path / "c.db")
    _insert_disc(conn)
    matches = _find_duplicates(
        conn,
        "Other Album",
        "Test Artist",
        2000,
        1,
        1,
        2,
        None,
        [11025, 22050],
        [None, None],
    )
    conn.close()
    assert matches == []


def test_find_duplicates_year_mismatch(tmp_path):
    conn = open_catalogue_db(tmp_path / "c.db")
    _insert_disc(conn, year=1999)
    matches = _find_duplicates(
        conn,
        "Test Album",
        "Test Artist",
        2005,
        1,
        1,
        2,
        None,
        [11025, 22050],
        [None, None],
    )
    conn.close()
    assert matches == []


def test_find_duplicates_null_year_matches_any(tmp_path):
    """A null year in the DB matches a non-null incoming year (and vice versa)."""
    conn = open_catalogue_db(tmp_path / "c.db")
    cat_id = _insert_disc(conn, year=None)
    matches = _find_duplicates(
        conn,
        "Test Album",
        "Test Artist",
        2005,
        1,
        1,
        2,
        None,
        [11025, 22050],
        [None, None],
    )
    conn.close()
    assert matches == [cat_id]


def test_find_duplicates_mcn_mismatch(tmp_path):
    conn = open_catalogue_db(tmp_path / "c.db")
    _insert_disc(conn, mcn="0000000000001")
    matches = _find_duplicates(
        conn,
        "Test Album",
        "Test Artist",
        2000,
        1,
        1,
        2,
        "9999999999999",
        [11025, 22050],
        [None, None],
    )
    conn.close()
    assert matches == []


def test_find_duplicates_duration_mismatch(tmp_path):
    conn = open_catalogue_db(tmp_path / "c.db")
    _insert_disc(conn, durations=(11025, 22050))
    matches = _find_duplicates(
        conn,
        "Test Album",
        "Test Artist",
        2000,
        1,
        1,
        2,
        None,
        [11025, 99999],
        [None, None],
    )
    conn.close()
    assert matches == []


def test_find_duplicates_ar_crc_mismatch(tmp_path):
    conn = open_catalogue_db(tmp_path / "c.db")
    _insert_disc(conn, ar_crcs=("aabbccdd", "11223344"))
    matches = _find_duplicates(
        conn,
        "Test Album",
        "Test Artist",
        2000,
        1,
        1,
        2,
        None,
        [11025, 22050],
        ["ffffffff", "11223344"],
    )
    conn.close()
    assert matches == []


def test_find_duplicates_ar_crc_one_side_null(tmp_path):
    """If only one side has AR CRCs, we can't compare — treat as match."""
    conn = open_catalogue_db(tmp_path / "c.db")
    cat_id = _insert_disc(conn, ar_crcs=(None, None))
    matches = _find_duplicates(
        conn,
        "Test Album",
        "Test Artist",
        2000,
        1,
        1,
        2,
        None,
        [11025, 22050],
        ["aabbccdd", "11223344"],
    )
    conn.close()
    assert matches == [cat_id]


# ---------------------------------------------------------------------------
# register_rbi — disabled flag test
# ---------------------------------------------------------------------------


def test_register_rbi_disabled(tmp_path):
    """When enable_catalogue=False, register_rbi must not touch the DB."""
    from cdda2img.catalogue import register_rbi

    fake_rbi = tmp_path / "fake.rbi"
    fake_rbi.write_bytes(b"")

    with (
        patch(
            "cdda2img.catalogue._get_catalogue_config", return_value=(False, None)
        ) as mock_cfg,
        patch("cdda2img.catalogue._register_impl") as mock_impl,
    ):
        register_rbi(fake_rbi)
        mock_cfg.assert_called_once()
        mock_impl.assert_not_called()


# ---------------------------------------------------------------------------
# register_rbi — integration tests (build a fresh RBI from example audio)
# ---------------------------------------------------------------------------

_EXAMPLE_TRACKS = [
    Path("example/Koiduuni.mp3"),
    Path("example/Action Strike.mp3"),
]

_have_examples = pytest.mark.skipif(
    not all(p.exists() for p in _EXAMPLE_TRACKS),
    reason="example audio files not present",
)


@pytest.fixture(scope="module")
def built_rbi(tmp_path_factory):
    """Build a minimal v4 RBI from example MP3s once per module."""
    from cdda2img.concat import concat_wav
    from cdda2img.container import build_container, wav_to_raw_pcm
    from cdda2img.rbi_format import RBIDisc
    from cdda2img.toc import build_toc_entries, generate_toc, get_track_durations
    from cdda2img.transcode import transcode_audio

    tmp = tmp_path_factory.mktemp("rbi_fixture")
    wavs = []
    for src in _EXAMPLE_TRACKS:
        out = tmp / f"{src.stem}.wav"
        transcode_audio(src, out)
        wavs.append(out)

    disc = RBIDisc(
        album="Test Album", artist="Test Artist", disc_number=1, disc_total=1
    )
    disc.tracks = build_toc_entries(_EXAMPLE_TRACKS, get_track_durations(wavs), disc)
    toc_data = generate_toc(disc)

    concat = tmp / "all.wav"
    pcm = tmp / "all.pcm"
    concat_wav(wavs, concat)
    wav_to_raw_pcm(concat, pcm)

    rbi = tmp / "test.rbi"
    build_container(pcm, toc_data, disc, rbi, rg_block=None, prov_data={"mode": "c"})
    return rbi


@_have_examples
def test_register_rbi_creates_record(tmp_path, built_rbi):
    from cdda2img.catalogue import register_rbi

    db_path = tmp_path / "c.db"
    register_rbi(built_rbi, catalogue_path=db_path)

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT album, artist, track_count FROM catalogue"
        ).fetchone()
        assert row is not None
        album, artist, track_count = row
        assert album == "Test Album"
        assert artist == "Test Artist"
        assert track_count == len(_EXAMPLE_TRACKS)
        track_rows = conn.execute("SELECT COUNT(*) FROM catalogue_tracks").fetchone()
        assert track_rows[0] == len(_EXAMPLE_TRACKS)
    finally:
        conn.close()


@_have_examples
def test_register_rbi_explicit_path_bypasses_enable_flag(tmp_path, built_rbi):
    """Passing an explicit catalogue_path bypasses the enable_catalogue flag."""
    from cdda2img.catalogue import register_rbi

    db_path = tmp_path / "c.db"
    with patch("cdda2img.catalogue._get_catalogue_config", return_value=(False, None)):
        register_rbi(built_rbi, catalogue_path=db_path)

    conn = sqlite3.connect(db_path)
    try:
        count = conn.execute("SELECT COUNT(*) FROM catalogue").fetchone()[0]
        assert count == 1
    finally:
        conn.close()
