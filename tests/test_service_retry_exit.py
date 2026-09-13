"""Mid-run service retry, and exit status 3 for a container written without them.

The pre-flight asks before a run starts. A service can still fail mid-run, and
until 2026-09-13 that was only recorded: ``lookup_status_accuraterip=down`` or
``lookup_status_mb=down`` in PROV, and exit status 0. Now, on a TTY without
``--auto``, the user is offered a retry at the point the service failed; and a
container that still records a required service as ``down`` makes the run exit 3,
so a script can tell "written, but re-run later" from "done".
"""

from __future__ import annotations

import argparse
import ast
import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cdda2img import cdda2img as app
from cdda2img.lookup_result import DiscMeta
from cdda2img.mb_lookup import MBLookupError, MBPrepopResult
from cdda2img.rbi_format import RBIDisc, RBITocEntry


class _Answers:
    def __init__(self, *answers: str | type[BaseException]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        answer = self._answers.pop(0)
        if isinstance(answer, type):
            raise answer
        return answer


class _UI:
    def __init__(self) -> None:
        self.events: list[str] = []

    def pause(self) -> None:
        self.events.append("pause")

    def resume(self) -> None:
        self.events.append("resume")

    def clear_output(self) -> None:
        pass

    def set_status(self, text: str, _prog: float = 0.0) -> None:
        self.events.append(f"status:{text}")


# ── the prompt ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("answers", "retry"),
    [
        (("",), True),  # Enter retries
        (("r",), True),
        (("c",), False),
        (("what", "C"), False),  # unrecognised input asks again
        ((EOFError,), False),  # Ctrl-D continues rather than aborting the run
    ],
    ids=["enter", "r", "c", "reask", "eof"],
)
def test_the_prompt_answers(answers, retry: bool, capsys) -> None:
    ask = _Answers(*answers)
    ui = _UI()
    got = app._offer_service_retry(
        ui,  # type: ignore[arg-type]
        "MusicBrainz",
        "HTTP 503",
        interactive=True,
        input_fn=ask,
    )
    assert got is retry
    assert "MusicBrainz did not answer (HTTP 503)" in capsys.readouterr().out
    assert "exit status 3" in ask.prompts[0]
    # The TUI is handed back whatever the answer, including an EOF.
    assert ui.events == ["pause", "resume"]


def test_an_unattended_run_is_never_prompted() -> None:
    ask = _Answers()  # any call would pop from an empty list and fail
    assert (
        app._offer_service_retry(
            None, "AccurateRip", "x", interactive=False, input_fn=ask
        )
        is False
    )
    assert ask.prompts == []


@pytest.mark.parametrize(
    ("isatty", "auto", "expected"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_interactive_means_a_tty_without_auto(
    monkeypatch, isatty: bool, auto: bool, expected: bool
) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: isatty))
    assert app._interactive(auto) is expected


# ── AccurateRip ──────────────────────────────────────────────────────────────


def _ar(reachable: bool) -> SimpleNamespace:
    return SimpleNamespace(reachable=reachable)


def test_accuraterip_is_retried_until_it_answers(monkeypatch) -> None:
    results = [_ar(False), _ar(False), _ar(True)]
    verify = MagicMock(side_effect=results)
    offer = MagicMock(return_value=True)
    monkeypatch.setattr(app, "_offer_service_retry", offer)

    got = app._verify_ar_with_retry(verify, None, interactive=True)

    assert got is results[2]
    assert verify.call_count == 3
    assert offer.call_count == 2
    assert offer.call_args.args[1] == "AccurateRip"


@pytest.mark.parametrize(
    ("first", "retry", "calls"),
    [(True, True, 1), (False, False, 1)],
    ids=["answered-no-prompt", "user-continues"],
)
def test_accuraterip_is_verified_once_otherwise(
    monkeypatch, first: bool, retry: bool, calls: int
) -> None:
    verify = MagicMock(return_value=_ar(first))
    offer = MagicMock(return_value=retry)
    monkeypatch.setattr(app, "_offer_service_retry", offer)

    assert app._verify_ar_with_retry(verify, None, interactive=True).reachable is first
    assert verify.call_count == calls
    assert offer.call_count == (0 if first else 1)


def test_the_rip_verifies_accuraterip_through_the_retry() -> None:
    """The helper is only worth having if the rip calls it. Read the source rather
    than drive a whole rip; and assert there is exactly one, since the later
    re-verifications run only after this one reached AccurateRip."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.rip_image)))
    names = [
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert names.count("_verify_ar_with_retry") == 1


# ── MusicBrainz ──────────────────────────────────────────────────────────────


def _disc() -> RBIDisc:
    return RBIDisc(
        album="",
        artist="",
        tracks=[
            RBITocEntry(
                track_number=1,
                title="",
                performer="",
                start_frame=0,
                duration_frames=18000,
            )
        ],
    )


def test_musicbrainz_is_retried_until_it_answers() -> None:
    disc = _disc()
    failed = MBPrepopResult(disc, [], 0, lookup_error="503")
    answered = MBPrepopResult(disc, [], 1)
    retry = MagicMock(return_value=True)
    with patch(
        "cdda2img.mb_lookup.prepopulate_from_mb", side_effect=[failed, answered]
    ) as prepop:
        got = app._retry_mb_lookup(
            failed, disc, retry, None, verbose=False, preferred_country=[]
        )
    assert got is answered
    assert prepop.call_count == 2
    assert retry.call_args_list[0].args == ("MusicBrainz", "HTTP 503")
    assert retry.call_args_list[1].args == ("MusicBrainz", "HTTP 503")


@pytest.mark.parametrize(
    ("error", "retry", "asked"),
    [(None, MagicMock(return_value=True), False), ("network", None, False)],
    ids=["answered-no-prompt", "no-retry-callback"],
)
def test_musicbrainz_is_not_repeated_otherwise(error, retry, asked: bool) -> None:
    disc = _disc()
    result = MBPrepopResult(disc, [], 0, lookup_error=error)
    with patch("cdda2img.mb_lookup.prepopulate_from_mb") as prepop:
        got = app._retry_mb_lookup(
            result, disc, retry, None, verbose=False, preferred_country=[]
        )
    assert got is result
    prepop.assert_not_called()
    if retry is not None:
        assert retry.called is asked


@pytest.mark.parametrize("retry", [True, False], ids=["retried", "continued"])
def test_a_retried_musicbrainz_that_answers_records_ok(retry: bool) -> None:
    """End to end through the real metadata lookups: the PROV status follows the
    LAST attempt, and a retry that succeeds leaves no ``lookup_error_mb`` behind."""
    mb_meta = DiscMeta(
        album="From MB", artist="A", mb_release_id="rid", source="musicbrainz"
    )
    prov: dict[str, str] = {}
    with (
        patch("cdda2img.cddb.query_cddb", return_value=[]),
        patch(
            "cdda2img.mb_lookup.lookup_disc_id",
            side_effect=[MBLookupError("network", "no route"), [mb_meta]],
        ),
        patch("cdda2img.mb_lookup.duration_match_lookup", return_value=None),
        patch("cdda2img.cdda2img._discogs_barcode_corroborate", lambda *a, **k: None),
        patch(
            "cdda2img.cdda2img._prepopulate_from_discogs",
            side_effect=lambda d, *a, **k: (d, None, None),
        ),
        patch(
            "cdda2img.cdda2img._r6_acoustid_corroborate",
            side_effect=lambda d, *a, **k: d,
        ),
    ):
        app._run_metadata_lookups(
            _disc(),
            Path("/nonexistent.pcm"),
            prov,
            do_cddb=True,
            cddb_track_lsns=[0],
            cddb_disc_last_lsn=18000,
            cddb_server=None,
            cddb_verbose=False,
            mb_verbose=False,
            preferred_country=[],
            ui=None,
            retry_service=lambda _s, _d: retry,
        )
    if retry:
        assert prov["lookup_status_mb"] == "OK"
        assert "lookup_error_mb" not in prov
    else:
        assert prov["lookup_status_mb"] == "down"
        assert prov["lookup_error_mb"] == "network"


def test_finalize_offers_the_musicbrainz_retry() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(app._finalize_import)))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_run_metadata_lookups"
    ]
    assert len(calls) == 1
    assert "retry_service" in {k.arg for k in calls[0].keywords}


# ── exit status 3 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("prov", "expected"),
    [
        (
            {"lookup_status_accuraterip": "down", "lookup_status_mb": "down"},
            ["AccurateRip", "MusicBrainz"],
        ),
        ({"lookup_status_mb": "down"}, ["MusicBrainz"]),
        # Controls: an empty answer is an answer, and advisory services do not count.
        ({"lookup_status_mb": "empty", "lookup_status_accuraterip": "OK"}, []),
        ({"lookup_status_discogs": "down", "lookup_status_cddb": "down"}, []),
    ],
    ids=["both", "mb", "answered", "advisory-only"],
)
def test_unanswered_services(prov, expected) -> None:
    assert app._unanswered_services(prov) == expected


def test_report_unanswered(capsys) -> None:
    assert app._report_unanswered([]) == 0
    assert capsys.readouterr().out == ""
    assert app._report_unanswered(["MusicBrainz"]) == 3
    out = capsys.readouterr().out
    assert "Written without an answer from: MusicBrainz." in out
    assert "Exit status 3" in out


def _run_main(rc: int) -> int:
    args = argparse.Namespace(verbose=False, cmd="setup")
    with (
        patch.object(app, "parse_args", return_value=args),
        patch.object(app, "_install_log_handler"),
        patch("cdda2img.net.install_default_socket_timeout"),
        patch.object(app, "_dispatch", return_value=rc),
    ):
        try:
            app.main()
        except SystemExit as exc:
            return int(exc.code or 0)
    return 0


def test_main_exits_with_the_dispatched_code() -> None:
    assert _run_main(3) == 3
    assert _run_main(0) == 0


def _dispatch(monkeypatch, argv: list[str], pipeline: str, unanswered: list[str]):
    monkeypatch.setattr("sys.argv", ["cdda2img", *argv])
    args = app.parse_args()
    cfg = MagicMock(auto=False, cddb_server=None, default_profile=None)
    with (
        patch("cdda2img.config.load_config", return_value=cfg),
        patch.object(app, "_network_preflight", return_value="ok"),
        patch("cdda2img.recovery_profile.resolve_recovery", return_value=None),
        patch.object(app, pipeline, return_value=unanswered),
    ):
        return app._dispatch(args)


@pytest.mark.parametrize("unanswered", [["AccurateRip"], []], ids=["down", "clean"])
def test_rip_and_import_return_their_exit_code(
    monkeypatch, tmp_path, unanswered: list[str]
) -> None:
    expected = 3 if unanswered else 0
    assert _dispatch(monkeypatch, ["rip"], "rip_image", unanswered) == expected
    src = tmp_path / "x.toc"
    src.write_text("")
    assert (
        _dispatch(monkeypatch, ["import", str(src)], "import_image", unanswered)
        == expected
    )
