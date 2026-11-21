"""A rip writes a complete, fully verified container, or nothing (kgr, 2026-11-21).

"Any outcome other than 100% AR verified tracks should not result in an RBI being
written, as the result is meaningless. The objective is perfect archival." The
user still has the disc. So every check that used to keep a container and exit 3,
4 or 5 now raises :class:`~cdda2img.rip_abort.RipAborted` before
``_finalize_import``, which ``main`` turns into a failure report and exit 2 or 3.

The Q-collapse thresholds below are the 2026-09-14 evidence (a LITE-ON rip
misframed by libata's PIO padding, verifying 0/11 and exiting 0 with "No errors
occurred" sealed in its log), carried over unchanged into the new gate.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from types import SimpleNamespace

import pytest

from cdda2img import cdda2img as app
from cdda2img import query_log
from cdda2img import rip_abort as ra


def _v(exc: pytest.ExceptionInfo) -> ra.RipAborted:
    """The raised abort, narrowed for the type checker."""
    assert isinstance(exc.value, ra.RipAborted)
    return exc.value


def _track(n: int, *, verified: bool, in_db: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        track=n,
        confidence_v1=5 if verified else None,
        confidence_v2=None,
        max_confidence=10 if in_db else None,
    )


# ---- the Q gate ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "aborts"),
    [
        ("0/162892", True),  # the LITE-ON rip: sub read from C2 zeros
        ("7082/162892", True),  # 4.35%: one record in 23 framed right
        ("10859/162892", True),  # 6.67%: one in 15, AccuDisc's test chunk size
        ("12216/162892", True),  # just under 7.5%
        ("12217/162892", False),  # at 7.5%
        # 13.43%: a read while another process used the drive, AR v2 passing
        # (RECOVERY.md §12.0). The lowest yield on record with good audio.
        ("21876/162892", False),
        ("164704/347208", False),  # 47.4%: PX-716A at 32x, uncontended
        ("0/0", True),  # asked for raw subchannel and got none
    ],
)
def test_q_collapse_threshold(value: str, aborts: bool) -> None:
    if not aborts:
        app._require_usable_q({"subq_q_valid": value})
        return
    with pytest.raises(ra.RipAborted) as exc:
        app._require_usable_q({"subq_q_valid": value})
    assert _v(exc).code == ra.EXIT_INVESTIGATE
    assert value in _v(exc).details[0]


def test_an_unanchored_q_stream_aborts() -> None:
    """Rule (b): pre-gaps, INDEX points, MCN and ISRCs come only from Q."""
    with pytest.raises(ra.RipAborted) as exc:
        app._require_usable_q({
            "subq_q_valid": "300000/347208",
            "subq_layout": "unanchored",
        })
    assert any("anchored" in d for d in _v(exc).details)


@pytest.mark.parametrize("value", [None, "", "12", "a/b", "5/0", "-1/10"])
def test_an_unusable_q_count_is_not_a_collapse(value: str | None) -> None:
    """Malformed counts are not evidence either way. A fresh rip always writes the
    key, so only a malformed producer would reach this."""
    prov = {} if value is None else {"subq_q_valid": value}
    app._require_usable_q(prov)


def test_the_q_count_subq_toc_writes_is_the_one_the_gate_reads() -> None:
    """Producer and consumer in one process: a format drift on either side would
    pass both hand-written tables above."""
    from cdda2img.subchannel import CD_SUBCODE_SIZE
    from cdda2img.subq_toc import build_rip_info
    from tests.test_subq_toc import _TOC

    info = build_rip_info(_TOC, b"\x00" * (CD_SUBCODE_SIZE * 4), track1_pregap=0)
    with pytest.raises(ra.RipAborted):
        app._require_usable_q(dict(info.prov or {}))


def test_the_q_gate_runs_straight_after_the_capture() -> None:
    """Before any AccurateRip query or repair read, so a doomed rip spends nothing
    more on the drive."""
    src = inspect.getsource(app.rip_image)
    assert src.index("_rip_disc_stage(") < src.index("_require_usable_q(info.prov")
    assert src.index("_require_usable_q(info.prov") < src.index("verify_rip(")


# ---- the AccurateRip gates ----------------------------------------------------


def test_accuraterip_unreachable_is_retry_later() -> None:
    with pytest.raises(ra.RipAborted) as exc:
        app._require_accuraterip_answer([_track(1, verified=False)], False, 0xAB)
    assert _v(exc).code == ra.EXIT_RETRY_LATER


def test_a_disc_accuraterip_does_not_hold_is_investigated_not_trusted() -> None:
    """Every past 'not in the database' was our own fault (kgr)."""
    tracks = [
        _track(1, verified=False, in_db=False),
        _track(2, verified=False, in_db=False),
    ]
    with pytest.raises(ra.RipAborted) as exc:
        app._require_accuraterip_answer(tracks, True, 0x0912_1513)
    assert _v(exc).code == ra.EXIT_INVESTIGATE
    assert _v(exc).context["cddb_id"] == "09121513"
    assert any("URL" in d for d in _v(exc).details)


def test_a_held_disc_passes_the_answer_gate() -> None:
    app._require_accuraterip_answer([_track(1, verified=False)], True, 1)


def test_the_answer_gate_runs_before_any_repair() -> None:
    src = inspect.getsource(app.rip_image)
    gate = src.index("_require_accuraterip_answer(")
    assert gate < src.index("repair_whole_disc(")
    assert gate < src.index("_recover_failed_tracks(")


# ---- every track verified -------------------------------------------------------


def test_one_unverified_track_aborts_and_names_its_recovery() -> None:
    tracks = [_track(1, verified=True), _track(2, verified=False)]
    with pytest.raises(ra.RipAborted) as exc:
        app._require_every_track_verified(
            tracks, {2: "unrecovered"}, {"recovery_attempts_track_2": "40>40:done"}
        )
    assert _v(exc).code == ra.EXIT_INVESTIGATE
    assert _v(exc).details == ["track 2: unrecovered"]
    assert _v(exc).context["recovery_attempts_track_2"] == "40>40:done"


def test_all_verified_passes() -> None:
    app._require_every_track_verified([_track(1, verified=True)], {}, {})


# ---- the total miss ---------------------------------------------------------------


@pytest.mark.parametrize("miss", ["no_offset_verifies", "offset_probe_failed"])
def test_a_total_miss_names_no_offset_without_a_confirmed_match(miss: str) -> None:
    reason, details = app._total_miss_message({"ar_total_miss": miss})
    assert "none of its tracks verified" in reason
    assert not any("read offset -" in d or "[[drives]]" in d for d in details)


def test_a_confirmed_offset_candidate_is_named_without_blaming_the_offset() -> None:
    _reason, details = app._total_miss_message({
        "ar_total_miss": "offset_mismatch",
        "ar_offset_candidates": "-669,0",
        "ar_offset_suggests": "-675",
    })
    (text,) = details
    assert "read offset -669" in text
    assert "-675 from the offset used" in text
    assert "[[drives]]" in text
    assert "pressing" in text


# ---- nothing is written -----------------------------------------------------------


def test_finalize_is_only_reached_past_both_end_gates() -> None:
    """The one call that creates the container must come after the total-miss
    abort and the every-track gate, in the same block."""
    src = inspect.getsource(app.rip_image)
    finalize = src.index("rbi_path = _finalize_import(")
    assert src.index("raise RipAborted(EXIT_INVESTIGATE, reason, details") < finalize
    assert src.index("_require_every_track_verified(") < finalize


def test_the_rip_finalize_is_archival() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.rip_image)))
    (call,) = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_finalize_import"
    ]
    kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert kw.get("archival") == "True"


_ANSWERED = {
    "mb_identified_via": "disc_id",
    "lookup_status_mb": "OK",
    "lookup_status_cddb": "OK",
    "lookup_status_discogs": "OK",
    "lookup_status_acoustid": "OK",
}


def test_musicbrainz_down_aborts_only_an_archival_run() -> None:
    prov = {**_ANSWERED, "lookup_status_mb": "down", "lookup_error_mb": "503"}
    del prov["mb_identified_via"]
    app._require_service_answers(prov, archival=False)  # import keeps its rules
    with pytest.raises(ra.RipAborted) as exc:
        app._require_service_answers(prov, archival=True)
    assert _v(exc).code == ra.EXIT_RETRY_LATER


def test_the_service_gate_runs_before_the_metadata_menu() -> None:
    src = inspect.getsource(app._finalize_import)
    assert src.index("_archival_gates(") < src.index("run_metadata_menu(")
    gates = inspect.getsource(app._archival_gates)
    assert "_require_service_answers(" in gates
    assert "_require_acoustid_identity(" in gates


# ---- the report and the exit codes ----------------------------------------------


def test_the_report_carries_reason_context_and_every_query(tmp_path) -> None:
    query_log.start()
    query_log.record(
        "AccurateRip",
        "https://www.accuraterip.com/accuraterip/a/b/c/dBAR-011-x.bin?key=SECRET",
        "HTTP 404",
        "Not Found",
    )
    abort = ra.RipAborted(
        ra.EXIT_INVESTIGATE, "AccurateRip has no record", ["detail"], {"cddb_id": "1"}
    )
    path = ra.write_report(abort, tmp_path)
    text = path.read_text()
    assert "Exit status 3" in text
    assert "AccurateRip has no record" in text
    assert "cddb_id=1" in text
    assert "dBAR-011-x.bin" in text
    assert "HTTP 404" in text
    assert "SECRET" not in text  # credentials never reach the report


def test_an_abort_goes_to_stderr_with_the_report_path(capsys, tmp_path) -> None:
    abort = ra.RipAborted(ra.EXIT_RETRY_LATER, "X did not answer", ["why"])
    rc = ra.report_to_stderr(abort, tmp_path / "r.txt")
    out, err = capsys.readouterr()
    assert rc == 2
    assert out == ""
    assert "Rip aborted, nothing written: X did not answer" in err
    assert "r.txt" in err


def test_an_abort_must_carry_an_abort_code() -> None:
    with pytest.raises(ValueError, match="not an abort exit code"):
        ra.RipAborted(0, "nothing")


def test_accuraterip_queries_are_recorded_with_their_exact_url(monkeypatch) -> None:
    """kgr: "this would not be the first time we searched for the wrong thing". The
    report must show the URL actually asked, and what came back."""
    import io
    import urllib.error
    from email.message import Message

    from cdda2img import accuraterip as arp

    def not_found(url, timeout):
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), io.BytesIO())

    monkeypatch.setattr(arp.urllib.request, "urlopen", not_found)
    query_log.start()
    assert arp._fetch_ar(11, "0015190b", "00b48124", 0x8A0BC50B) == (None, "https")
    (rec,) = query_log.records()
    assert rec.service == "AccurateRip"
    assert rec.query.endswith("dBAR-011-0015190b-00b48124-8a0bc50b.bin")
    assert rec.outcome == "HTTP 404"


def test_query_log_redacts_and_caps(monkeypatch) -> None:
    query_log.start()
    query_log.record(
        "AcoustID",
        "https://api.acoustid.org/v2/lookup?client=abc123&duration=200",
        "200",
        "x" * (query_log.MAX_DETAIL + 10),
    )
    (rec,) = query_log.records()
    assert "abc123" not in rec.query
    assert "client=<redacted>" in rec.query
    assert "truncated" in rec.detail
    assert len(rec.detail) < query_log.MAX_DETAIL + 200


# ---- every service must contribute (stage 2, kgr 2026-11-21) -----------------


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"lookup_status_cddb": "empty"}, ra.EXIT_INVESTIGATE),
        ({"lookup_status_discogs": "empty"}, ra.EXIT_INVESTIGATE),
        ({"lookup_status_acoustid": "empty"}, ra.EXIT_INVESTIGATE),
        ({"lookup_status_cddb": "down"}, ra.EXIT_RETRY_LATER),
        ({"lookup_status_discogs": "disabled"}, ra.EXIT_INVESTIGATE),
        ({"lookup_status_acoustid": None}, ra.EXIT_INVESTIGATE),
    ],
    ids=[
        "cddb-empty",
        "discogs-empty",
        "acoustid-empty",
        "cddb-down",
        "disabled",
        "never",
    ],
)
def test_a_service_that_contributed_nothing_aborts(change, code) -> None:
    prov = {**_ANSWERED, **{k: v for k, v in change.items() if v is not None}}
    for k, v in change.items():
        if v is None:
            del prov[k]
    with pytest.raises(ra.RipAborted) as exc:
        app._require_service_answers(prov, archival=True)
    assert _v(exc).code == code


def test_every_service_answered_passes() -> None:
    app._require_service_answers(dict(_ANSWERED), archival=True)


@pytest.mark.parametrize("via", ["disc_id", "isrc_tally", "duration_match"])
def test_musicbrainz_is_judged_per_service_not_per_query(via: str) -> None:
    """A disc-ID miss that a fallback resolved is MusicBrainz having answered."""
    prov = {**_ANSWERED, "lookup_status_mb": "empty", "mb_identified_via": via}
    app._require_service_answers(prov, archival=True)


def test_an_empty_answer_after_a_network_error_is_retry_later() -> None:
    """Discogs returns [] on a network error, so its status says empty; the query
    log is what shows it never really answered."""
    query_log.start()
    query_log.record("Discogs", "GET https://api.discogs.com/x", "network: timed out")
    prov = {**_ANSWERED, "lookup_status_discogs": "empty"}
    with pytest.raises(ra.RipAborted) as exc:
        app._require_service_answers(prov, archival=True)
    assert _v(exc).code == ra.EXIT_RETRY_LATER


def test_one_real_miss_beside_an_outage_is_investigated() -> None:
    """Re-running fixes the outage, not the miss; the miss decides the code."""
    query_log.start()
    query_log.record("Discogs", "GET https://api.discogs.com/x", "network: timed out")
    prov = {
        **_ANSWERED,
        "lookup_status_discogs": "empty",
        "lookup_status_acoustid": "empty",
    }
    with pytest.raises(ra.RipAborted) as exc:
        app._require_service_answers(prov, archival=True)
    assert _v(exc).code == ra.EXIT_INVESTIGATE


def test_missing_cover_art_aborts_an_archival_run() -> None:
    query_log.start()
    app._require_cover_art({"lookup_status_art": "OK"}, archival=True)
    app._require_cover_art({"lookup_status_art": "empty"}, archival=False)
    with pytest.raises(ra.RipAborted) as exc:
        app._require_cover_art({"lookup_status_art": "empty"}, archival=True)
    assert _v(exc).code == ra.EXIT_INVESTIGATE


def test_the_art_gate_runs_before_the_container_is_built() -> None:
    src = inspect.getsource(app._finalize_import)
    assert src.index("fetch_cover(") < src.index("_require_cover_art(")
    assert src.index("_require_cover_art(") < src.index("build_container(")


def test_mb_identified_via_names_the_path() -> None:
    prov: dict[str, str] = {}
    app._record_mb_identification(prov, SimpleNamespace(match_count=2, meta=None))
    assert prov == {"mb_identified_via": "disc_id"}
    prov = {}
    app._record_mb_identification(prov, SimpleNamespace(match_count=0, meta=object()))
    assert prov == {"mb_identified_via": "isrc_tally"}
    prov = {}
    app._record_mb_identification(prov, SimpleNamespace(match_count=0, meta=None))
    assert prov == {}


def test_a_rip_refuses_without_discogs_or_acoustid(monkeypatch) -> None:
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)
    monkeypatch.delenv("ACOUSTID_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Discogs and AcoustID") as exc:
        app._require_rip_services_configured()
    assert "DISCOGS_TOKEN" in str(exc.value)
    assert "ACOUSTID_API_KEY" in str(exc.value)


def test_the_configuration_check_runs_before_the_drive(monkeypatch) -> None:
    src = inspect.getsource(app._dispatch)
    rip = src[src.index('"rip"') :]
    assert rip.index("_require_rip_services_configured()") < rip.index("rip_image(")


def test_cddb_exchanges_are_recorded() -> None:
    """The CDDB query is the disc ID and offsets we sent; a wrong one is exactly
    what the report exists to expose."""
    import io

    from cdda2img.cddb import _CddbSession

    sess = _CddbSession.__new__(_CddbSession)
    sent: list[bytes] = []
    sess._sock = SimpleNamespace(sendall=sent.append)  # type: ignore[attr-defined]
    sess._file = io.StringIO("202 No match for disc ID 09121513.\n")  # type: ignore[attr-defined]
    query_log.start()
    assert sess.cmd("cddb query 09121513 19 150 0").startswith("202")
    (rec,) = query_log.records()
    assert rec.service == "CDDB"
    assert rec.query == "cddb query 09121513 19 150 0"
    assert rec.outcome.startswith("202 No match")
