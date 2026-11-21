"""CD-R detection, --cdr and --digital-media (kgr, 2026-11-21).

A recordable disc, or one whose type cannot be determined, needs the user's
word. ``--digital-media`` skips every disc-ID database, proves the audio by
byte-identical reads at different speeds, and identifies it by AcoustID on
every track. Measured on Toca (20th Anniversary Edition): AcoustID lists that
release among the candidates for its tracks, so the identity gate can pass.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from cdda2img import cdda2img as app
from cdda2img import rip_abort as ra
from cdda2img.network_preflight import services_for


def _v(exc: pytest.ExceptionInfo) -> ra.RipAborted:
    assert isinstance(exc.value, ra.RipAborted)
    return exc.value


# ---- the media decision -----------------------------------------------------------


@pytest.mark.parametrize("kind", ["recordable", "unknown"])
def test_a_disc_that_is_not_pressed_needs_the_users_word(kind: str) -> None:
    with pytest.raises(ra.RipAborted) as exc:
        app._require_media_decision(kind, "profile=0x0009,atip=present", None)
    err = _v(exc)
    assert err.code == ra.EXIT_FIX_INVOCATION
    assert any("--cdr" in d for d in err.details)
    assert any("--digital-media" in d for d in err.details)


@pytest.mark.parametrize("override", ["cdr", "digital_media"])
@pytest.mark.parametrize("kind", ["recordable", "unknown"])
def test_either_override_lets_it_through(kind: str, override: str) -> None:
    app._require_media_decision(kind, "e", override)


def test_a_pressed_disc_needs_nothing_and_refuses_digital_media() -> None:
    app._require_media_decision("pressed", "e", None)
    app._require_media_decision("pressed", "e", "cdr")
    with pytest.raises(ra.RipAborted) as exc:
        app._require_media_decision("pressed", "e", "digital_media")
    assert _v(exc).code == ra.EXIT_FIX_INVOCATION


def test_the_media_check_is_the_first_thing_the_rip_does() -> None:
    src = inspect.getsource(app.rip_image)
    gate = src.index("_require_media_decision(")
    assert gate < src.index("_start_track_preview(")
    assert gate < src.index("_rip_disc_stage(")


@pytest.mark.parametrize(
    ("argv", "override"),
    [([], None), (["--cdr"], "cdr"), (["--digital-media"], "digital_media")],
)
def test_the_flags(monkeypatch, argv, override) -> None:
    monkeypatch.setattr("sys.argv", ["cdda2img", "rip", *argv])
    assert app.parse_args().media_override == override


def test_the_flags_are_exclusive(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["cdda2img", "rip", "--cdr", "--digital-media"])
    with pytest.raises(SystemExit):
        app.parse_args()


# ---- what --digital-media asks ------------------------------------------------------


def test_digital_media_asks_no_disc_id_database(monkeypatch) -> None:
    monkeypatch.setenv("DISCOGS_TOKEN", "x")
    monkeypatch.setenv("ACOUSTID_API_KEY", "x")
    keys = {s.key for s in services_for("rip-digital", cddb_server=None)}
    assert not keys & {"accuraterip", "cddb", "ctdb"}
    assert {"musicbrainz", "coverart"} <= keys


def test_digital_media_does_not_require_cddb() -> None:
    prov = {
        "mb_identified_via": "duration_match",
        "lookup_status_discogs": "OK",
        "lookup_status_acoustid": "OK",
    }
    app._require_service_answers(prov, archival=True, digital_media=True)
    with pytest.raises(ra.RipAborted):
        app._require_service_answers(prov, archival=True, digital_media=False)


def test_the_digital_branch_skips_accuraterip() -> None:
    src = inspect.getsource(app.rip_image)
    branch = src[src.index("        if digital_media:\n") :]
    branch = branch[: branch.index("        else:\n")]
    assert "_digital_media_consensus(" in branch
    assert "verify_rip(" not in branch


# ---- the consensus reads ---------------------------------------------------------------


_SECTOR = 2352


def _capture(path: Path, sectors: int, flip: int | None = None) -> None:
    data = bytearray(b"".join(bytes([i % 251]) * _SECTOR for i in range(sectors)))
    if flip is not None:
        data[flip * _SECTOR] ^= 0xFF
    path.write_bytes(bytes(data))


def _consensus(
    monkeypatch, tmp_path, *, c2: bool, flip=None, damage=False, ladder=(40, 32, 24)
):
    first = tmp_path / "disc.pcm"
    _capture(first, 20)
    calls: list[int | None] = []

    def read(device, out, read_speed=None, map_cb=None, **_k):
        calls.append(read_speed)
        _capture(out, 20, flip)
        if map_cb is not None:
            map_cb(SimpleNamespace(damage=bytearray([1 if damage else 0] * 20)))
        return 0

    monkeypatch.setattr("cdda2img.accudisc_reader.read_disc_c2", read)
    monkeypatch.setattr("cdda2img.accudisc_reader.drive_supports_c2", lambda _d: c2)
    monkeypatch.setattr("cdda2img.drive_speed.admitted_ladder", lambda _d: list(ladder))
    prov = app._digital_media_consensus("/dev/sr0", first, bytes(20), None)
    return prov, calls


def test_agreeing_reads_at_a_different_rung_are_the_proof(
    monkeypatch, tmp_path
) -> None:
    prov, calls = _consensus(monkeypatch, tmp_path, c2=True)
    assert calls == [32]  # not the capture's fastest rung
    assert prov == {"digital_media_consensus": "2@capture,32X:clean"}
    assert not list(tmp_path.glob("consensus-*"))  # scratch removed


def test_without_c2_a_third_read_at_a_third_rung_is_required(
    monkeypatch, tmp_path
) -> None:
    prov, calls = _consensus(monkeypatch, tmp_path, c2=False)
    assert calls == [32, 24]
    assert prov["digital_media_consensus"] == "3@capture,32X,24X:unavailable"


def test_a_disagreeing_read_aborts_and_says_where(monkeypatch, tmp_path) -> None:
    with pytest.raises(ra.RipAborted) as exc:
        _consensus(monkeypatch, tmp_path, c2=True, flip=7)
    err = _v(exc)
    assert err.code == ra.EXIT_INVESTIGATE
    assert "first at LBA 7" in err.details[0]


def test_a_flagged_read_aborts(monkeypatch, tmp_path) -> None:
    with pytest.raises(ra.RipAborted):
        _consensus(monkeypatch, tmp_path, c2=True, damage=True)


def test_a_flagged_capture_aborts_before_any_read(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr("cdda2img.accudisc_reader.drive_supports_c2", lambda _d: True)
    monkeypatch.setattr(
        "cdda2img.accudisc_reader.read_disc_c2", lambda *a, **k: calls.append(a)
    )
    with pytest.raises(ra.RipAborted):
        app._digital_media_consensus("/dev/sr0", tmp_path / "x", bytes([0, 1, 0]), None)
    assert calls == []


def test_first_difference(tmp_path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _capture(a, 600)
    _capture(b, 600, flip=513)  # across the 512-sector chunk boundary
    assert app._first_difference(a, b) == (513, 1)
    _capture(b, 600)
    assert app._first_difference(a, b) is None


# ---- the AcoustID identity gate --------------------------------------------------------


def _disc(n: int, group: str | None = "rg-1"):
    tracks = [
        SimpleNamespace(track_number=i, pregap_frames=0, duration_frames=2)
        for i in range(1, n + 1)
    ]
    return SimpleNamespace(tracks=tracks, mb_release_group_id=group)


@pytest.mark.parametrize(
    ("groups", "matched", "aborts"),
    [
        ([["rg-1", "x"], ["rg-1"]], "2/2", False),
        ([["rg-1"], ["x"]], "1/2", True),
        ([[], ["rg-1"]], "1/2", True),  # a track with no candidates
    ],
)
def test_every_track_must_fingerprint_to_the_release(
    monkeypatch, tmp_path, groups, matched, aborts
) -> None:
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(2 * 2 * _SECTOR))
    answers = iter(groups)
    monkeypatch.setattr(
        "cdda2img.acoustid_lookup.fingerprint_and_lookup",
        lambda _w: [SimpleNamespace(mb_release_group_id=g) for g in next(answers)],
    )
    prov: dict[str, str] = {}
    if aborts:
        with pytest.raises(ra.RipAborted):
            app._require_acoustid_identity(_disc(2), pcm, prov, None)
    else:
        app._require_acoustid_identity(_disc(2), pcm, prov, None)
    assert prov["acoustid_tracks_matched"] == matched


def test_no_identified_release_matches_nothing(monkeypatch, tmp_path) -> None:
    pcm = tmp_path / "disc.pcm"
    pcm.write_bytes(bytes(2 * _SECTOR))
    monkeypatch.setattr(
        "cdda2img.acoustid_lookup.fingerprint_and_lookup",
        lambda _w: [SimpleNamespace(mb_release_group_id=None)],
    )
    with pytest.raises(ra.RipAborted):
        app._require_acoustid_identity(_disc(1, group=None), pcm, {}, None)


def test_the_consensus_avoids_the_captures_own_speed(monkeypatch, tmp_path) -> None:
    """With --ad-speed 32 the capture ran at 32; a re-read at 32 would be the
    same-speed agreement the proof exists to rule out."""
    first = tmp_path / "disc.pcm"
    _capture(first, 4)
    calls: list[int | None] = []

    def read(device, out, read_speed=None, map_cb=None, **_k):
        calls.append(read_speed)
        _capture(out, 4)
        if map_cb is not None:
            map_cb(SimpleNamespace(damage=bytearray(4)))
        return 0

    monkeypatch.setattr("cdda2img.accudisc_reader.read_disc_c2", read)
    monkeypatch.setattr("cdda2img.accudisc_reader.drive_supports_c2", lambda _d: True)
    monkeypatch.setattr("cdda2img.drive_speed.admitted_ladder", lambda _d: [40, 32, 24])
    app._digital_media_consensus("/dev/sr0", first, bytes(4), None, capture_speed=32)
    assert calls == [40]
