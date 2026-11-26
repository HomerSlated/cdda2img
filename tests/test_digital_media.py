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
        mb_release_group_id=None,
        country=None,
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
    monkeypatch.setattr("cdda2img.discogs_lookup.search_by_barcode", lambda _c: [])
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
# kgr, 2026-11-26 (trust_model_design.md §12). MusicBrainz release 0a19729e (GB
# Virgin) has no Discogs link; its release group links master 98041. Its barcode
# 5012981234522 finds only two barcoded reissues on Discogs; the disc was 5222631,
# "No barcode". The provisional pick was a German WEA pressing (20 barcode hits).

_NJR_GB = "0a19729e"
_NJR_DE = "d73bd2e7"
_NJR_RG = "86b4651b"
_GB_BARCODE = "5012981234522"
_DE_BARCODE = "0022925169921"
_MASTER = 98041


def _njr_disc(discogs=None, mbid=_NJR_GB):
    return SimpleNamespace(
        album="No Jacket Required",
        mb_release_id=mbid,
        mb_release_group_id=_NJR_RG,
        country="GB",
        discogs_release_id=discogs,
        discogs_master_id=None,
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


def _group_master(monkeypatch, master=_MASTER):
    asked: list[str] = []

    def link(group):
        asked.append(group)
        return master

    monkeypatch.setattr("cdda2img.mb_lookup.discogs_master_of_release_group", link)
    return asked


def _reopen(pick=None, prov=None):
    """A fake post-menu Discogs screen: records what it was handed and, given
    *pick*, takes that release the way the real one records it."""
    seen: list[int | None] = []

    def reopen(disc, barcode_hit):
        seen.append(barcode_hit)
        if pick is not None and prov is not None:
            disc.discogs_release_id = pick
            disc.discogs_master_id = _MASTER
            prov["discogs_release"] = str(pick)
            prov["discogs_release_via"] = "master_versions"
        return disc

    return reopen, seen


@pytest.mark.usefixtures("_discogs_on")
def test_a_new_pressing_without_a_link_reopens_the_discogs_screen(monkeypatch) -> None:
    _links(monkeypatch, (None, _GB_BARCODE))
    asked = _discogs_search(monkeypatch, [5221810, 19343422])
    disc, prov = _njr_disc(), _njr_prov()
    reopen, seen = _reopen(5222631, prov)
    disc = app._discogs_after_menu(disc, prov, _NJR_DE, reopen=reopen)
    assert asked == [_GB_BARCODE]  # the chosen pressing's barcode, not the stale one
    assert seen == [None]  # two hits: nothing to mark
    assert disc.discogs_release_id == 5222631
    assert prov["discogs_release_via"] == "master_versions"
    app._require_discogs_release(prov, archival=True)


@pytest.mark.usefixtures("_discogs_on")
def test_a_single_barcode_hit_is_handed_to_the_screen_to_mark(monkeypatch) -> None:
    """§12 D6: interactive, one hit is a marked row, not a decision."""
    _links(monkeypatch, (None, _GB_BARCODE))
    _discogs_search(monkeypatch, [5221810])
    disc, prov = _njr_disc(), _njr_prov()
    reopen, seen = _reopen(None, prov)
    disc = app._discogs_after_menu(disc, prov, _NJR_DE, reopen=reopen)
    assert seen == [5221810]
    assert disc.discogs_release_id is None
    assert prov["discogs_release"] == "not_chosen"
    with pytest.raises(ra.RipAborted) as exc:
        app._require_discogs_release(prov, archival=True)
    assert "not_chosen" in " ".join(_v(exc).details)


@pytest.mark.usefixtures("_discogs_on")
def test_unattended_a_single_barcode_hit_settles_it(monkeypatch) -> None:
    _links(monkeypatch, (None, _GB_BARCODE))
    _discogs_search(monkeypatch, [5221810])
    groups = _group_master(monkeypatch)
    disc, prov = _njr_disc(), _njr_prov()
    disc = app._discogs_after_menu(disc, prov, _NJR_DE)
    assert disc.discogs_release_id == 5221810
    assert prov["discogs_release_via"] == "barcode_after_menu"
    assert groups == []


@pytest.mark.usefixtures("_discogs_on")
def test_unattended_with_no_single_release_the_master_is_recorded(monkeypatch) -> None:
    """§12 D7: the master, never in the release field."""
    _links(monkeypatch, (None, _GB_BARCODE))
    _discogs_search(monkeypatch, [5221810, 19343422])
    groups = _group_master(monkeypatch)
    disc, prov = _njr_disc(), _njr_prov()
    disc = app._discogs_after_menu(disc, prov, _NJR_DE)
    assert groups == [_NJR_RG]
    assert disc.discogs_release_id is None
    assert disc.discogs_master_id == _MASTER
    assert prov["discogs_release"] == f"master_only:{_MASTER}"
    app._require_discogs_release(prov, archival=True)  # accepted unattended


@pytest.mark.usefixtures("_discogs_on")
def test_unattended_with_no_master_the_rip_aborts(monkeypatch) -> None:
    _links(monkeypatch, (None, None))
    _group_master(monkeypatch, master=None)
    disc, prov = _njr_disc(), _njr_prov()
    disc = app._discogs_after_menu(disc, prov, _NJR_DE)
    assert disc.discogs_master_id is None
    assert prov["discogs_release"] == "no_master"
    with pytest.raises(ra.RipAborted):
        app._require_discogs_release(prov, archival=True)


@pytest.mark.usefixtures("_discogs_on")
def test_an_unchanged_unresolved_pressing_gets_the_master_unattended(
    monkeypatch,
) -> None:
    _links(monkeypatch, None)  # never asked: the pressing did not change
    asked = _discogs_search(monkeypatch, [1])
    _group_master(monkeypatch)
    disc, prov = _njr_disc(mbid=_NJR_DE), _njr_prov()
    disc = app._discogs_after_menu(disc, prov, _NJR_DE)
    assert asked == []  # the pre-menu barcode search already answered
    assert prov["discogs_release"] == f"master_only:{_MASTER}"


@pytest.mark.usefixtures("_discogs_on")
def test_an_unchanged_unresolved_pressing_was_already_offered_interactively(
    monkeypatch,
) -> None:
    """The master screen opened in the menu (§12 D8); nothing was taken, so the
    rip is not asked twice and aborts."""
    _links(monkeypatch, None)
    groups = _group_master(monkeypatch)
    disc, prov = _njr_disc(mbid=_NJR_DE), _njr_prov()
    reopen, seen = _reopen(None, prov)
    app._discogs_after_menu(disc, prov, _NJR_DE, reopen=reopen)
    assert seen == []
    assert groups == []
    assert prov["discogs_release"] == "not_chosen"


def test_a_pick_on_the_master_screen_stands(monkeypatch) -> None:
    _links(monkeypatch, None)
    disc = _njr_disc(discogs=5222631)
    prov = {"discogs_release": "5222631", "discogs_release_via": "master_versions"}
    app._discogs_after_menu(disc, prov, _NJR_DE)
    assert prov["discogs_release_via"] == "master_versions"
    assert disc.discogs_release_id == 5222631


def test_the_master_only_gate_is_not_a_pass_for_other_reasons() -> None:
    for reason in ("not_chosen", "no_master", "ambiguous_20+no_link"):
        with pytest.raises(ra.RipAborted):
            app._require_discogs_release({"discogs_release": reason}, archival=True)


@pytest.mark.parametrize(
    ("archival", "auto", "tty", "offered"),
    [
        (True, False, True, True),  # an interactive rip
        (False, False, True, False),  # import: needs no Discogs release
        (True, True, True, False),  # --auto
        (True, False, False, False),  # no terminal
    ],
)
def test_only_an_interactive_rip_gets_the_reopened_screen(
    monkeypatch, archival, auto, tty, offered
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: tty)
    got = app._discogs_reopener(archival=archival, auto=auto, tui=False, provenance={})
    assert (got is not None) is offered


def test_the_master_is_filled_from_the_settled_release(monkeypatch) -> None:
    from cdda2img.lookup_result import DiscMeta

    fetched: list[int] = []

    def fetch(rid):
        fetched.append(rid)
        return DiscMeta(discogs_release_id=rid, discogs_master_id=_MASTER)

    monkeypatch.setattr("cdda2img.discogs_lookup.fetch_release", fetch)
    disc = _njr_disc(discogs=5222631)
    app._settle_discogs_master(disc)
    assert disc.discogs_master_id == _MASTER
    app._settle_discogs_master(disc)  # known: not fetched again
    assert fetched == [5222631]


def test_the_master_id_is_written_to_provenance() -> None:
    from cdda2img.rbi_format import RBIDisc

    disc = RBIDisc(album="No Jacket Required", artist="Phil Collins")
    disc.discogs_master_id = _MASTER
    prov: dict[str, str] = {}
    app._add_release_provenance(prov, disc)
    assert prov["discogs_master_id"] == str(_MASTER)
    assert "discogs_release_id" not in prov


def test_a_release_set_another_way_drops_a_screen_picks_master(monkeypatch) -> None:
    """Fetch -> Discogs applied a release after a screen pick: the master the pick
    left behind is cleared, to be refilled from the release itself."""
    _links(monkeypatch, None)
    disc = _njr_disc(discogs=5221810)
    disc.discogs_master_id = 12345
    prov = {"discogs_release": "ambiguous_20+no_link"}
    app._discogs_after_menu(disc, prov, _NJR_GB)
    assert prov["discogs_release_via"] == "menu"
    assert disc.discogs_master_id is None


@pytest.mark.usefixtures("_discogs_on")
def test_digital_media_unattended_records_the_master(monkeypatch) -> None:
    """kgr 2026-11-26: digital media uses the master too, through its File
    versions; unattended, the master is recorded as for a pressed disc."""
    groups = _group_master(monkeypatch, master=51867)
    disc = _njr_disc(mbid=_NJR_DE)
    prov = {"discogs_release": "no_match+no_link"}
    app._discogs_after_menu(disc, prov, _NJR_DE)
    assert groups == [_NJR_RG]
    assert prov["discogs_release"] == "master_only:51867"


def test_digital_media_lists_file_versions_on_both_screens() -> None:
    src = inspect.getsource(app._finalize_import)
    assert 'discogs_fmt = "File" if digital_media else "CD"' in src
    assert "discogs_open=archival and via != " in src  # digital no longer excluded
    assert "discogs_fmt=discogs_fmt" in src  # the in-menu screen
    assert "fmt=discogs_fmt" in src  # the post-menu reopener


def test_the_reopener_carries_the_format(monkeypatch) -> None:
    from cdda2img import metadata_menu

    seen: list[str] = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        metadata_menu, "run_discogs_menu", lambda d, **kw: seen.append(kw["fmt"]) or d
    )
    reopen = app._discogs_reopener(
        archival=True, auto=False, tui=False, provenance={}, fmt="File"
    )
    assert reopen is not None
    reopen(_njr_disc(), None)
    assert seen == ["File"]


@pytest.mark.usefixtures("_discogs_on")
def test_unattended_master_reaches_the_written_provenance(monkeypatch) -> None:
    """The post-menu sequence as _finalize_import runs it, on a real RBIDisc:
    nothing between the fallback and the PROV write may lose the master."""
    from cdda2img.rbi_format import RBIDisc

    _links(monkeypatch, None)
    _group_master(monkeypatch)
    monkeypatch.setattr(
        "cdda2img.discogs_lookup.fetch_release",
        lambda _r: pytest.fail("no release to fetch a master from"),
    )
    disc = RBIDisc(album="No Jacket Required", artist="Phil Collins")
    disc.mb_release_id = _NJR_DE
    disc.mb_release_group_id = _NJR_RG
    prov = _njr_prov()
    disc = app._discogs_after_menu(disc, prov, _NJR_DE)
    app._settle_discogs_master(disc)
    app._require_discogs_release(prov, archival=True)
    app._add_release_provenance(prov, disc)
    assert prov["discogs_master_id"] == str(_MASTER)
    assert prov["discogs_release"] == f"master_only:{_MASTER}"
    assert "discogs_release_id" not in prov


def test_the_post_menu_steps_run_in_order_before_the_provenance_write() -> None:
    src = inspect.getsource(app._finalize_import)
    at = [
        src.find("disc = _settle_identity_after_menu("),
        src.find("_add_release_provenance(provenance, disc)"),
    ]
    assert -1 not in at
    assert at == sorted(at)
    inner = inspect.getsource(app._settle_identity_after_menu)
    order = [
        "disc = _discogs_after_menu(",
        "_pair_unattended(",
        "_pair_after_pick(",
        "_settle_discogs_master(disc)",
        "_require_discogs_release(",
        "_settle_release_year(",
        "_require_identity_complete(",
    ]
    at = [inner.find(x) for x in order]
    assert -1 not in at, dict(zip(order, at, strict=True))
    assert at == sorted(at)


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
        "_settle_identity_after_menu(",
        "build_container(",
    ]
    at = [src.find(s) for s in order]
    assert -1 not in at, dict(zip(order, at, strict=True))
    assert at == sorted(at)
