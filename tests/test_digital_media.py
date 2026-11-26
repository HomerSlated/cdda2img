"""CD-R detection, --cdr and --digital-media (kgr, 2026-11-21).

A recordable disc, or one whose type cannot be determined, needs the user's
word. ``--digital-media`` skips every disc-ID database, proves the audio by
byte-identical reads at different speeds, and identifies it by AcoustID on
every track. Measured on Toca (20th Anniversary Edition): AcoustID lists that
release among the candidates for its tracks, so the identity gate can pass.
"""

from __future__ import annotations

import inspect
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
# Since 2026-11-25 (kgr: every 0.53-0.59 feature on) each re-read is AccuDisc's
# compare_read under its read policy's REFERENCE_VERIFY, which picks the rung.
# These fakes stand in for the seam; what is OURS to get right is what we pass
# it (the reference and the excluded speed), what we check afterwards (the speed
# the drive used), and what aborts.

_SECTOR = 2352


def _verify(
    rungs: dict[tuple[int, int], int | None],
    *,
    honours=None,
    disagree_at=None,
    agree_c2=0,
):
    """A fake ``verify_against_capture``: *rungs* maps (ref, exclude) to the rung
    the policy would pick (``None`` = no rung left)."""
    from cdda2img.accudisc_reader import NoVerifyRung, VerifyRead

    calls: list[tuple[int, int]] = []

    def verify(device, capture, *, ref_speed_x, exclude_speed_x, on_speed=None, **_k):
        calls.append((ref_speed_x, exclude_speed_x))
        speed = rungs.get((ref_speed_x, exclude_speed_x))
        if speed is None:
            msg = "no admitted rung left"
            raise NoVerifyRung(msg)
        if on_speed is not None:
            on_speed(speed)
        return VerifyRead(
            speed_requested=speed,
            speed_honoured=(honours or {}).get(speed, speed),
            agree=20,
            agree_c2=agree_c2,
            differ=0 if disagree_at is None else 1,
            shifted=0,
            hard=0,
            not_compared=0,
            first_disagreement=disagree_at,
            policy="r2:engine_recovery_pending_ab",
        )

    return verify, calls


def _consensus(
    monkeypatch, tmp_path, *, c2: bool, ladder=(40, 32, 24), rungs=None, **kw
):
    verify, calls = _verify(
        {(40, 0): 32, (32, 40): 24} if rungs is None else rungs,
        **{k: kw.pop(k) for k in ("honours", "disagree_at", "agree_c2") if k in kw},
    )
    monkeypatch.setattr("cdda2img.accudisc_reader.verify_against_capture", verify)
    monkeypatch.setattr("cdda2img.accudisc_reader.drive_supports_c2", lambda _d: c2)
    monkeypatch.setattr("cdda2img.drive_speed.admitted_ladder", lambda _d: list(ladder))
    prov = app._digital_media_consensus(
        "/dev/sr0", tmp_path / "disc.pcm", bytes(20), None, **kw
    )
    return prov, calls


def test_agreeing_reads_at_a_different_rung_are_the_proof(
    monkeypatch, tmp_path
) -> None:
    prov, calls = _consensus(monkeypatch, tmp_path, c2=True)
    assert calls == [(40, 0)]  # the reference is the capture's fastest rung
    assert prov == {
        "digital_media_consensus": "2@capture,32X:clean",
        "digital_media_policy": "r2:engine_recovery_pending_ab",
    }


def test_without_c2_the_third_read_excludes_the_captures_speed(
    monkeypatch, tmp_path
) -> None:
    prov, calls = _consensus(monkeypatch, tmp_path, c2=False)
    assert calls == [(40, 0), (32, 40)]
    assert prov["digital_media_consensus"] == "3@capture,32X,24X:unavailable"


def test_a_disagreeing_read_aborts_and_says_where(monkeypatch, tmp_path) -> None:
    with pytest.raises(ra.RipAborted) as exc:
        _consensus(monkeypatch, tmp_path, c2=True, disagree_at=7)
    err = _v(exc)
    assert err.code == ra.EXIT_INVESTIGATE
    assert "the first at LBA 7" in err.details[0]


def test_a_flagged_read_aborts(monkeypatch, tmp_path) -> None:
    with pytest.raises(ra.RipAborted) as exc:
        _consensus(monkeypatch, tmp_path, c2=True, agree_c2=1)
    assert "flagged C2" in _v(exc).reason


def test_a_flagged_capture_aborts_before_any_read(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr("cdda2img.accudisc_reader.drive_supports_c2", lambda _d: True)
    monkeypatch.setattr(
        "cdda2img.accudisc_reader.verify_against_capture",
        lambda *a, **k: calls.append(a),
    )
    with pytest.raises(ra.RipAborted):
        app._digital_media_consensus("/dev/sr0", tmp_path / "x", bytes([0, 1, 0]), None)
    assert calls == []


def test_no_rung_left_is_refused_not_read_twice_at_one_speed(
    monkeypatch, tmp_path
) -> None:
    """kgr, 2026-11-25: "Fix the digital-media consensus now". A one-rung drive
    used to re-read at the capture's own speed; now the policy finds no rung and
    the rip stops."""
    with pytest.raises(ra.RipAborted) as exc:
        _consensus(monkeypatch, tmp_path, c2=True, ladder=(40,), rungs={})
    err = _v(exc)
    assert err.code == ra.EXIT_INVESTIGATE
    assert "2 distinct speeds" in " ".join(err.details)


def test_the_capture_is_keyed_on_the_speed_the_drive_used(
    monkeypatch, tmp_path
) -> None:
    """Asked for 40, the drive read at 32: the reference is 32, not 40."""
    _prov, calls = _consensus(
        monkeypatch,
        tmp_path,
        c2=True,
        rungs={(32, 0): 24},
        capture_speed=40,
        capture_honoured=32,
    )
    assert calls == [(32, 0)]


def test_the_asked_for_speed_stands_in_when_the_drive_reports_none(
    monkeypatch, tmp_path
) -> None:
    _prov, calls = _consensus(
        monkeypatch, tmp_path, c2=True, rungs={(24, 0): 32}, capture_speed=24
    )
    assert calls == [(24, 0)]


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
    assert "readable=readable, )" in src


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


# ---- No Jacket Required: a chosen pressing with no MusicBrainz -> Discogs link ----
#
# kgr, 2026-11-26: the GB Virgin pressing (MB 0a19729e, barcode 5012981234522)
# has no Discogs link, and Discogs holds two releases for its barcode (5221810,
# 19343422) that differ only in label print. The provisional pick was a German
# WEA pressing whose barcode found 20.

_NJR_GB = "0a19729e"
_NJR_DE = "d73bd2e7"
_GB_BARCODE = "5012981234522"
_DE_BARCODE = "0022925169921"


def _njr_disc(discogs=None, mbid=_NJR_GB):
    return SimpleNamespace(
        album="No Jacket Required",
        mb_release_id=mbid,
        discogs_release_id=discogs,
        barcode=_DE_BARCODE,  # stale: what phase A wrote for the provisional pick
    )


def _njr_prov():
    return {
        "discogs_barcode_outcome": "ambiguous_20",
        "discogs_release": "ambiguous_20+no_link",
    }


def _discogs_search(monkeypatch, hits):
    from cdda2img.lookup_result import DiscMeta

    asked: list[str] = []

    def search(code):
        asked.append(code)
        return [
            DiscMeta(album="No Jacket Required", discogs_release_id=i, country="GB")
            for i in hits
        ]

    monkeypatch.setattr("cdda2img.discogs_lookup.search_by_barcode", search)
    return asked


@pytest.mark.usefixtures("_discogs_on")
def test_a_new_pressing_without_a_link_is_searched_by_its_own_barcode(
    monkeypatch,
) -> None:
    _links(monkeypatch, (None, _GB_BARCODE))
    asked = _discogs_search(monkeypatch, [5221810])
    disc, prov = _njr_disc(), _njr_prov()
    app._discogs_after_menu(disc, prov, _NJR_DE)
    assert asked == [_GB_BARCODE]  # not the provisional pressing's barcode
    assert disc.discogs_release_id == 5221810
    assert prov["discogs_release"] == "5221810"
    assert prov["discogs_release_via"] == "barcode_after_menu"
    app._require_discogs_release(prov, archival=True)


@pytest.mark.usefixtures("_discogs_on")
def test_two_discogs_releases_are_offered_to_the_user(monkeypatch) -> None:
    _links(monkeypatch, (None, _GB_BARCODE))
    _discogs_search(monkeypatch, [19343422, 5221810])
    offered: list[list[int]] = []

    def choose(hits):
        offered.append([h.discogs_release_id for h in hits])
        return hits[1]

    disc, prov = _njr_disc(), _njr_prov()
    app._discogs_after_menu(disc, prov, _NJR_DE, choose=choose)
    assert offered == [[19343422, 5221810]]
    assert disc.discogs_release_id == 5221810
    assert prov["discogs_release_via"] == "chosen"
    assert "discogs_release_candidates" not in prov


@pytest.mark.usefixtures("_discogs_on")
@pytest.mark.parametrize("chooser", [None, lambda _h: None])
def test_unresolved_discogs_candidates_abort_and_are_named(
    monkeypatch, chooser
) -> None:
    """No chooser (headless, --auto) or none picked: nothing guesses between them."""
    _links(monkeypatch, (None, _GB_BARCODE))
    _discogs_search(monkeypatch, [19343422, 5221810])
    disc, prov = _njr_disc(), _njr_prov()
    app._discogs_after_menu(disc, prov, _NJR_DE, choose=chooser)
    assert disc.discogs_release_id is None
    assert prov["discogs_release"] == f"ambiguous_2:{_GB_BARCODE}+no_link"
    assert prov["discogs_release_candidates"] == "19343422,5221810"
    with pytest.raises(ra.RipAborted) as exc:
        app._require_discogs_release(prov, archival=True)
    assert f"ambiguous_2:{_GB_BARCODE}" in " ".join(_v(exc).details)


@pytest.mark.usefixtures("_discogs_on")
def test_a_chosen_pressing_with_no_barcode_is_not_searched_by_the_stale_one(
    monkeypatch,
) -> None:
    _links(monkeypatch, (None, None))
    asked = _discogs_search(monkeypatch, [1])
    disc, prov = _njr_disc(), _njr_prov()
    app._discogs_after_menu(disc, prov, _NJR_DE)
    assert asked == []
    assert prov["discogs_release"] == "no_barcode+no_link"


@pytest.mark.usefixtures("_discogs_on")
def test_an_unchanged_pressing_without_a_release_can_be_chosen(monkeypatch) -> None:
    _links(monkeypatch, None)  # never asked: the pressing did not change
    asked = _discogs_search(monkeypatch, [11, 22, 33])
    disc = _njr_disc(mbid=_NJR_DE)  # unchanged: its barcode is its own
    prov = _njr_prov()
    app._discogs_after_menu(disc, prov, _NJR_DE, choose=lambda h: h[2])
    assert asked == [_DE_BARCODE]
    assert disc.discogs_release_id == 33
    assert prov["discogs_release_via"] == "chosen"


@pytest.mark.usefixtures("_discogs_on")
def test_an_unchanged_pressing_is_not_searched_again_without_a_chooser(
    monkeypatch,
) -> None:
    """Control for the test above: headless, the pre-menu reason stands."""
    asked = _discogs_search(monkeypatch, [11, 22, 33])
    disc, prov = _njr_disc(mbid=_NJR_DE), _njr_prov()
    app._discogs_after_menu(disc, prov, _NJR_DE)
    assert asked == []
    assert prov["discogs_release"] == "ambiguous_20+no_link"


@pytest.mark.parametrize(
    ("archival", "auto", "tty", "offered"),
    [
        (True, False, True, True),  # an interactive rip
        (False, False, True, False),  # import: needs no single Discogs release
        (True, True, True, False),  # --auto
        (True, False, False, False),  # no terminal
    ],
)
def test_only_an_interactive_rip_gets_the_discogs_chooser(
    monkeypatch, archival, auto, tty, offered
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    got = app._discogs_chooser(archival=archival, auto=auto)
    assert (got is app._choose_discogs_release) is offered
    assert (got is None) is not offered


def test_the_discogs_chooser_returns_the_picked_row() -> None:
    from cdda2img.lookup_result import DiscMeta

    hits = [
        DiscMeta(album="X", discogs_release_id=19343422),
        DiscMeta(album="X", discogs_release_id=5221810),
    ]
    notes = {5221810: "Reissue of [r=5222631]. '80s reissue adds a barcode."}
    answers = iter(["9", "x", "2"])  # out of range and junk are asked again
    got = app._choose_discogs_release(
        hits,
        input_fn=lambda _p: next(answers),
        fetch=lambda i: DiscMeta(
            album="X", discogs_release_id=i, annotation=notes.get(i)
        ),
    )
    assert got is not None
    assert got.discogs_release_id == 5221810


@pytest.mark.parametrize("answer", ["", EOFError])
def test_the_discogs_chooser_declines_on_enter_or_eof(answer) -> None:
    from cdda2img.lookup_result import DiscMeta

    def ask(_p):
        if answer is EOFError:
            raise EOFError
        return answer

    hits = [DiscMeta(album="X", discogs_release_id=1)]
    assert (
        app._choose_discogs_release(hits, input_fn=ask, fetch=lambda _i: None) is None
    )


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
