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
    monkeypatch,
    tmp_path,
    *,
    c2: bool,
    flip=None,
    damage=False,
    ladder=(40, 32, 24),
    honours=None,
    **speeds,
):
    """Run the consensus on fakes. *honours* maps a requested speed to the one
    the fake drive reports using, through ``stats_cb`` as the engine does."""
    from cdda2img.accudisc_reader import CaptureCounters

    first = tmp_path / "disc.pcm"
    _capture(first, 20)
    calls: list[int | None] = []

    def read(device, out, read_speed=None, map_cb=None, stats_cb=None, **_k):
        calls.append(read_speed)
        _capture(out, 20, flip)
        if map_cb is not None:
            map_cb(SimpleNamespace(damage=bytearray([1 if damage else 0] * 20)))
        if stats_cb is not None:
            got = (honours or {}).get(read_speed, read_speed)
            stats_cb(CaptureCounters(None, None, None, speed_honoured_x=got))
        return 0

    monkeypatch.setattr("cdda2img.accudisc_reader.read_disc_c2", read)
    monkeypatch.setattr("cdda2img.accudisc_reader.drive_supports_c2", lambda _d: c2)
    monkeypatch.setattr("cdda2img.drive_speed.admitted_ladder", lambda _d: list(ladder))
    prov = app._digital_media_consensus("/dev/sr0", first, bytes(20), None, **speeds)
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
    # The nearest rung BELOW the capture's (AccuDisc's REFERENCE_VERIFY rule,
    # adopted 2026-11-25); before then the fastest other rung, 40.
    assert calls == [24]


# ---- no two reads at one speed (kgr, 2026-11-25: "Fix the digital-media consensus
# now"). Until then a one-rung drive re-read at the capture's own speed and a
# third read reused the second's rung, both silently.


def test_a_one_rung_drive_is_refused_not_read_twice_at_one_speed(
    monkeypatch, tmp_path
) -> None:
    with pytest.raises(ra.RipAborted) as exc:
        _consensus(monkeypatch, tmp_path, c2=True, ladder=(40,))
    err = _v(exc)
    assert err.code == ra.EXIT_INVESTIGATE
    assert "2 distinct speeds" in " ".join(err.details)


def test_two_rungs_without_c2_cannot_give_a_third_speed(monkeypatch, tmp_path) -> None:
    with pytest.raises(ra.RipAborted) as exc:
        _consensus(monkeypatch, tmp_path, c2=False, ladder=(8, 4), capture_speed=4)
    assert "3 distinct speeds" in " ".join(_v(exc).details)


def test_a_capture_at_the_lowest_rung_goes_up_and_never_back(
    monkeypatch, tmp_path
) -> None:
    """AccuDisc's own case (outbound 282): 4x, then 8x, then NOT 4x again."""
    prov, calls = _consensus(
        monkeypatch, tmp_path, c2=False, ladder=(40, 16, 8, 4), capture_speed=4
    )
    assert calls == [8, 16]
    assert prov["digital_media_consensus"] == "3@capture,8X,16X:unavailable"


def test_the_capture_is_keyed_on_the_speed_the_drive_used(
    monkeypatch, tmp_path
) -> None:
    """Asked for 40, the drive read at 32: the re-read must avoid 32, not 40."""
    _prov, calls = _consensus(
        monkeypatch, tmp_path, c2=True, capture_speed=40, capture_honoured=32
    )
    assert calls == [24]


def test_a_reread_the_drive_ran_at_a_used_speed_is_refused(
    monkeypatch, tmp_path
) -> None:
    """The drive quantizes 32x up to 40x, the capture's speed: two reads at one
    speed prove nothing, whatever they agree on."""
    with pytest.raises(ra.RipAborted) as exc:
        _consensus(monkeypatch, tmp_path, c2=True, honours={32: 40})
    assert "the drive read at 40X" in _v(exc).reason


def test_the_record_names_the_speed_each_reread_ran_at(monkeypatch, tmp_path) -> None:
    prov, _calls = _consensus(monkeypatch, tmp_path, c2=True, honours={32: 24})
    assert prov["digital_media_consensus"] == "2@capture,24X:clean"


def test_the_nearest_rung_rule_matches_accudiscs() -> None:
    """REFERENCE_VERIFY revision 2: nearest below, else nearest above, never an
    avoided rung, None when nothing is left."""
    ladder = [40, 32, 24, 16, 8, 4]
    assert app._verify_rung(ladder, 40, [40]) == 32
    assert app._verify_rung(ladder, 4, [4]) == 8
    assert app._verify_rung(ladder, 8, [4, 8]) == 16
    assert app._verify_rung([8, 4], 8, [4, 8]) is None
    assert app._verify_rung(ladder, 48, [48]) == 40  # a speed off the ladder


# ---- the digital release's own records (kgr, 2026-11-21) ----------------------------
# "We need to fetch the data corresponding to the media type. If the data
# corresponding to the media type is not available, then abort." For a digital
# release that is its MusicBrainz release (format Digital Media) and its Discogs
# record; the disc-ID databases are the wrong lookups and are not asked.

_TOCA = "6bc751d2-79ca-4c93-9ea5-f1456a252897"
_OTHER = "9f8006ea-d0a0-4222-86a6-38572b93b037"


def _mb(tally=None):
    return SimpleNamespace(tally_release_id=tally)


def _identity(monkeypatch, formats=("Digital Media",), discogs=22970945, error=None):
    from cdda2img import mb_lookup

    def fetch(_rid):
        if error is not None:
            raise mb_lookup.MBLookupError(error, "x")
        return mb_lookup.ReleaseIdentity(tuple(formats), discogs)

    monkeypatch.setattr(mb_lookup, "release_identity", fetch)


@pytest.mark.parametrize(
    ("tally", "duration", "kwargs", "expect"),
    [
        (_TOCA, None, {}, _TOCA),
        (None, _TOCA, {}, _TOCA),
        (_TOCA, _TOCA, {}, _TOCA),
        (None, None, {}, "none"),
        (_TOCA, _OTHER, {}, f"conflict:{_TOCA}|{_OTHER}"),
        (_TOCA, None, {"formats": ("CD",)}, f"not_digital:{_TOCA}"),
        (_TOCA, None, {"formats": ("Digital Media", "CD")}, f"not_digital:{_TOCA}"),
        (_TOCA, None, {"formats": ()}, f"not_digital:{_TOCA}"),
        (_TOCA, None, {"error": "503"}, f"down:{_TOCA}"),
    ],
)
def test_the_digital_release_is_one_agreed_digital_media_release(
    monkeypatch, tally, duration, kwargs, expect
) -> None:
    _identity(monkeypatch, **kwargs)
    prov = {"duration_match_release": duration} if duration else {}
    got = app._identify_digital_release(prov, _mb(tally))
    assert prov["digital_release"] == expect
    assert (got.release_id if got else None) == (expect if expect == _TOCA else None)


def _album(name="Toca (20th Anniversary Edition)"):
    return SimpleNamespace(album=name, barcode=None)


@pytest.fixture
def _discogs_on(monkeypatch):
    monkeypatch.setattr("cdda2img.discogs_lookup.is_available", lambda: True)


@pytest.mark.usefixtures("_discogs_on")
def test_discogs_is_reached_through_the_musicbrainz_link(monkeypatch) -> None:
    """Toca: the barcode search finds nothing (the Discogs record has no barcode),
    MusicBrainz links 22970945. Only the id is taken."""
    from cdda2img.lookup_result import DiscMeta

    record = DiscMeta(album="Toca (20th Anniversary Edition)", country="GB")
    monkeypatch.setattr("cdda2img.discogs_lookup.fetch_release", lambda _i: record)
    prov: dict[str, str] = {"discogs_barcode_outcome": "no_match"}
    got = app._discogs_release(_album(), prov, None, linked_id=22970945)
    assert got == 22970945
    assert prov["discogs_release"] == "22970945"
    assert prov["discogs_release_via"] == "mb_link"


@pytest.mark.usefixtures("_discogs_on")
def test_a_pressed_disc_reads_the_link_from_its_disc_id_release(monkeypatch) -> None:
    from cdda2img import mb_lookup
    from cdda2img.lookup_result import DiscMeta

    asked: list[str] = []

    def link(rid):
        asked.append(rid)
        return 1303585, "0075596077422"

    monkeypatch.setattr(mb_lookup, "discogs_link_and_barcode", link)
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.fetch_release",
        lambda _i: DiscMeta(album="Toca (20th Anniversary Edition)"),
    )
    prov = {"discogs_barcode_outcome": "ambiguous_72"}
    got = app._discogs_release(_album(), prov, None, link_release="e9b905e6")
    assert (got, asked) == (1303585, ["e9b905e6"])


@pytest.mark.usefixtures("_discogs_on")
def test_a_barcode_hit_is_used_without_following_the_link(monkeypatch) -> None:
    from cdda2img.lookup_result import DiscMeta

    def no_fetch(_i):
        pytest.fail("the link must not be followed")

    monkeypatch.setattr("cdda2img.discogs_lookup.fetch_release", no_fetch)
    prov: dict[str, str] = {}
    from cdda2img.rbi_format import RBIDisc

    disc = RBIDisc(album="Toca", artist="Fragma", barcode="5037300003931")
    got = app._discogs_release(disc, prov, DiscMeta(discogs_release_id=7), linked_id=1)
    assert got == 7
    assert prov["discogs_release_via"] == "barcode"
    assert prov["discogs_release_barcode"] == "5037300003931"


@pytest.mark.usefixtures("_discogs_on")
@pytest.mark.parametrize(
    ("linked", "record", "expect"),
    [
        (None, None, "ambiguous_121+no_link"),
        (22970945, None, "unfetched:22970945"),
        (22970945, "Ibiza Anthems", "album_mismatch:22970945"),
    ],
)
def test_no_single_discogs_release(monkeypatch, linked, record, expect) -> None:
    """121 releases share ABBA Gold's barcode: that is an answer, not THE release."""
    from cdda2img.lookup_result import DiscMeta

    answer = DiscMeta(album=record) if record else None
    monkeypatch.setattr("cdda2img.discogs_lookup.fetch_release", lambda _i: answer)
    prov = {"discogs_barcode_outcome": "ambiguous_121"}
    assert app._discogs_release(_album(), prov, None, linked_id=linked) is None
    assert prov["discogs_release"] == expect


def test_no_link_is_followed_from_a_fallback_on_a_pressed_disc(monkeypatch) -> None:
    """C2: a fallback's release is not proven to be this pressing, so its link
    is not either. Only the disc-ID release (or the digital release) is passed."""
    seen: dict[str, object] = {}

    def resolve(_disc, _prov, _hit, *, link_release=None, linked_id=None):
        seen.update(link_release=link_release, linked_id=linked_id)

    from cdda2img.rbi_format import RBIDisc

    disc = RBIDisc(album="Toca", artist="Fragma")
    monkeypatch.setattr(app, "_discogs_release", resolve)
    app._apply_release_identity(disc, {}, None, None, None)
    assert seen == {"link_release": None, "linked_id": None}
    app._apply_release_identity(disc, {}, None, None, "disc-id-release")
    assert seen == {"link_release": "disc-id-release", "linked_id": None}
    app._apply_release_identity(disc, {}, app._DigitalRelease(_TOCA, 5), None, "x")
    assert seen == {"link_release": None, "linked_id": 5}


def test_the_digital_gate_passes_with_its_release() -> None:
    app._require_digital_identity({"digital_release": _TOCA})


@pytest.mark.parametrize(
    ("release", "code"),
    [
        (None, ra.EXIT_INVESTIGATE),
        (f"not_digital:{_TOCA}", ra.EXIT_INVESTIGATE),
        (f"conflict:{_TOCA}|{_OTHER}", ra.EXIT_INVESTIGATE),
        ("none", ra.EXIT_INVESTIGATE),
        (f"down:{_TOCA}", ra.EXIT_RETRY_LATER),
    ],
)
def test_the_digital_gate_aborts_without_its_release(release, code) -> None:
    prov = {"digital_release": release} if release else {}
    with pytest.raises(ra.RipAborted) as exc:
        app._require_digital_identity(prov)
    assert _v(exc).code == code


# ---- the Discogs release follows the pressing the menu settled on ----------------


def _menu_disc(mbid, discogs, barcode="0731451700729"):
    return SimpleNamespace(
        album="Gold: Greatest Hits",
        mb_release_id=mbid,
        discogs_release_id=discogs,
        barcode=barcode,
    )


def _links(monkeypatch, answer):
    from cdda2img import mb_lookup

    monkeypatch.setattr(mb_lookup, "discogs_link_and_barcode", lambda _r: answer)


def test_an_unchanged_pressing_keeps_its_discogs_release(monkeypatch) -> None:
    _links(monkeypatch, None)  # would raise on unpacking if asked
    disc = _menu_disc("09cc", 338764)
    prov = {"discogs_release": "338764", "discogs_release_via": "mb_link"}
    app._discogs_after_menu(disc, prov, "09cc")
    assert disc.discogs_release_id == 338764


@pytest.mark.usefixtures("_discogs_on")
def test_a_new_pressing_follows_its_own_link(monkeypatch) -> None:
    from cdda2img.lookup_result import DiscMeta

    _links(monkeypatch, (999, "0731451700729"))
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.fetch_release",
        lambda _i: DiscMeta(album="Gold: Greatest Hits"),
    )
    disc = _menu_disc("159d", 338764)
    prov = {"discogs_release": "338764", "discogs_release_via": "mb_link"}
    app._discogs_after_menu(disc, prov, "09cc")
    assert disc.discogs_release_id == 999
    assert prov["discogs_release"] == "999"
    assert prov["discogs_release_via"] == "mb_link_after_menu"


@pytest.mark.usefixtures("_discogs_on")
@pytest.mark.parametrize(
    ("chosen_barcode", "kept"),
    [("0731451700729", True), ("0731453308329", False)],
)
def test_a_barcode_release_stands_only_for_the_same_barcode(
    monkeypatch, chosen_barcode, kept
) -> None:
    _links(monkeypatch, (None, chosen_barcode))
    disc = _menu_disc("159d", 7)
    prov = {
        "discogs_release": "7",
        "discogs_release_via": "barcode",
        "discogs_release_barcode": "0731451700729",
        "discogs_barcode_outcome": "matched",
    }
    app._discogs_after_menu(disc, prov, "09cc")
    assert (disc.discogs_release_id == 7) is kept
    if not kept:
        with pytest.raises(ra.RipAborted):
            app._require_discogs_release(prov, archival=True)


def test_a_discogs_release_set_in_the_menu_is_the_users(monkeypatch) -> None:
    _links(monkeypatch, None)
    disc = _menu_disc("159d", 42)
    prov = {"discogs_release": "338764", "discogs_release_via": "mb_link"}
    app._discogs_after_menu(disc, prov, "09cc")
    assert disc.discogs_release_id == 42
    assert prov["discogs_release_via"] == "menu"


def test_the_post_menu_gate_is_rip_only() -> None:
    app._require_discogs_release({}, archival=False)
    with pytest.raises(ra.RipAborted):
        app._require_discogs_release({}, archival=True)


def test_the_gate_runs_only_for_digital_media(monkeypatch, tmp_path) -> None:
    """A pressed disc is not asked for a digital release (the negative control)."""
    calls: list[str] = []
    monkeypatch.setattr(app, "_require_service_answers", lambda *_a, **_k: None)
    monkeypatch.setattr(
        app, "_require_digital_identity", lambda _p: calls.append("identity")
    )
    monkeypatch.setattr(
        app, "_require_acoustid_identity", lambda *_a: calls.append("acoustid")
    )
    from cdda2img.rbi_format import RBIDisc

    disc = RBIDisc(album="", artist="")
    app._archival_gates(disc, tmp_path, {}, None, archival=True, digital_media=False)
    assert calls == []
    app._archival_gates(disc, tmp_path, {}, None, archival=True, digital_media=True)
    assert calls == ["identity", "acoustid"]


def test_the_resolver_keeps_the_digital_release_id() -> None:
    """The release id reaches the committed disc through its own source; the same
    id from a recording-level source is still refused (C2, the control)."""
    from cdda2img.field_resolver import Field, FieldProposal, Source, Trust
    from cdda2img.lookup_result import DiscMeta
    from cdda2img.rbi_format import RBIDisc

    base = RBIDisc(album="Toca", artist="Fragma")
    props = app._collect_metadata_proposals(
        base,
        None,
        None,
        None,
        None,
        None,
        DiscMeta(mb_release_id=_TOCA, discogs_release_id=22970945),
    )
    got = {(p.field, p.value) for p in props}
    assert (Field.MB_RELEASE_ID, _TOCA) in got
    assert (Field.DISCOGS_RELEASE_ID, 22970945) in got
    with pytest.raises(ValueError, match="C2"):
        FieldProposal(Field.MB_RELEASE_ID, _TOCA, Trust.DURATION, Source.DURATION)


def test_the_tally_still_strips_for_the_merge_but_reports_its_release(
    monkeypatch,
) -> None:
    """Pressed discs keep C2: the merged meta carries no pressing id. Only the
    separate ``tally_release_id`` names it, for the digital identity."""
    from cdda2img import mb_lookup
    from cdda2img.lookup_result import DiscMeta
    from cdda2img.rbi_format import RBIDisc

    winner = DiscMeta(album="Toca", mb_release_id=_TOCA, mb_release_group_id="rg")
    monkeypatch.setattr(mb_lookup, "_isrc_tally_winner", lambda _d: winner)
    monkeypatch.setattr(mb_lookup, "_is_consistent", lambda *_a: True)
    r = mb_lookup.prepopulate_without_disc_id(
        RBIDisc(album="", artist=""), verbose=False
    )
    assert r.meta is not None
    assert r.meta.mb_release_id is None
    assert r.meta.mb_release_group_id == "rg"
    assert r.disc.mb_release_id is None
    assert r.tally_release_id == _TOCA


def test_release_identity_reads_formats_and_the_discogs_link(monkeypatch) -> None:
    from cdda2img import mb_lookup

    release = {
        "medium-list": [{"format": "Digital Media"}],
        "url-relation-list": [
            {"type": "free streaming", "target": "https://open.spotify.com/a"},
            {"type": "discogs", "target": "https://www.discogs.com/master/9"},
            {"type": "discogs", "target": "https://www.discogs.com/release/22970945"},
        ],
    }
    monkeypatch.setattr(mb_lookup, "_setup_useragent", lambda: None)
    monkeypatch.setattr(
        mb_lookup.musicbrainzngs,
        "get_release_by_id",
        lambda *_a, **_k: {"release": release},
    )
    got = mb_lookup.release_identity(_TOCA)
    assert got == mb_lookup.ReleaseIdentity(("Digital Media",), 22970945)


def test_release_identity_raises_on_an_outage(monkeypatch) -> None:
    from cdda2img import mb_lookup

    def down(*_a, **_k):
        raise mb_lookup.musicbrainzngs.NetworkError("down")

    monkeypatch.setattr(mb_lookup, "_setup_useragent", lambda: None)
    monkeypatch.setattr(mb_lookup.musicbrainzngs, "get_release_by_id", down)
    with pytest.raises(mb_lookup.MBLookupError):
        mb_lookup.release_identity(_TOCA)


@pytest.mark.parametrize(
    ("outcome", "errored"),
    [
        ("HTTP 429", True),
        ("HTTP 503", True),
        ("network: refused", True),
        ("error: boom", True),
        ("HTTP 404", False),  # a genuine "not found"
        ("HTTP 200", False),
        ("200", False),
    ],
)
def test_rate_limited_or_server_error_is_not_an_answer(outcome, errored) -> None:
    """Measured 2026-11-21: Discogs answered a burst with 429 and its client returned
    [], which the archival gate took for a real miss (exit 3, not 2)."""
    from cdda2img import query_log

    query_log.start()
    query_log.record("Discogs", "q", outcome)
    assert query_log.errored("Discogs") is errored


def test_the_discogs_release_is_settled_after_the_menu_and_before_writing() -> None:
    src = inspect.getsource(app._finalize_import)
    order = [
        "run_metadata_menu(",
        "_discogs_after_menu(",
        "_require_discogs_release(",
        "build_container(",
    ]
    at = [src.find(s) for s in order]
    assert -1 not in at, dict(zip(order, at, strict=True))
    assert at == sorted(at)


def test_the_captures_honoured_speed_is_recorded_and_handed_on() -> None:
    from cdda2img.accudisc_reader import CaptureCounters

    prov = app._capture_prov(CaptureCounters(1, 0, 0, speed_honoured_x=32))
    assert prov["capture_speed_honoured"] == "32"
    assert "capture_speed_honoured" not in app._capture_prov(
        CaptureCounters(1, 0, 0)  # not reported: absent, never 0
    )
    assert app._prov_int(prov, "capture_speed_honoured") == 32
    assert app._prov_int({}, "capture_speed_honoured") is None
    src = " ".join(inspect.getsource(app.rip_image).split())
    assert 'capture_honoured=_prov_int(info.prov, "capture_speed_honoured")' in src
