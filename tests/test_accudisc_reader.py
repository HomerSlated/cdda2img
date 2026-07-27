"""accudisc_reader: AccuDisc subprocess wrappers — args, combos, progress streaming.

Asserts the AccuDisc machine interface: subcommand form, ``--c2f`` (not ``--c2``),
whole-disc read via no ``--count``, inline ``--cdtext`` / ``--fulltoc`` lead-in
capture, ``--progress-fd 1`` machine tokens on stdout, and the exit contract
(0/3 = completed, 1/2 = fatal).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

import cdda2img.accudisc_reader as ar

_ACC = ar._ACCUDISC

_FEATURES_OUT = """\
cd_read_feature present current=1 dap=0 c2_flags=1 cd_text=1
combo c2 ok
combo sub_raw ok
combo sub_q ok
combo c2+sub_raw ok
combo c2+sub_q failed
verdict C2_SUPPORTED
accurate_stream yes
"""


class _Result:
    def __init__(
        self, stdout: str | bytes = "", stderr: bytes = b"", returncode: int = 0
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _patch_run(monkeypatch, result_for=None):
    """Record every subprocess.run argv; return a per-command _Result."""
    calls: list[list[str]] = []
    make = result_for or (lambda cmd: _Result(returncode=0))

    def _run(cmd, **k):
        calls.append(cmd)
        return make(cmd)

    monkeypatch.setattr(ar.subprocess, "run", _run)
    return calls


# ── drive_supports_c2 / probe_combos ─────────────────────────────────────────


def test_drive_supports_c2_gates_on_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess, "run", lambda *a, **k: _Result(stdout=_FEATURES_OUT)
    )
    assert ar.drive_supports_c2("/dev/sr0") is True
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: _Result(stdout=_FEATURES_OUT, returncode=1),
    )
    assert ar.drive_supports_c2("/dev/sr0") is False


def test_drive_supports_c2_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*a: object, **k: object) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(ar.subprocess, "run", _raise)
    assert ar.drive_supports_c2("/dev/sr0") is False


def test_probe_combos_parses_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess, "run", lambda *a, **k: _Result(stdout=_FEATURES_OUT)
    )
    combos = ar.probe_combos("/dev/sr0")
    assert combos == {
        "c2": True,
        "sub_raw": True,
        "sub_q": True,
        "c2+sub_raw": True,
        "c2+sub_q": False,
    }


# ── read_disc_c2 command construction ────────────────────────────────────────


def test_read_disc_c2_default_args(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_disc_c2("/dev/sr0", Path("a.pcm"), Path("a.c2"))
    (cmd,) = calls  # no cdtext/fulltoc requested → only the read
    assert cmd[:4] == [_ACC, "--device", "/dev/sr0", "read"]
    assert cmd[cmd.index("--c2f") + 1] == "a.c2"
    assert cmd[cmd.index("--pcm") + 1] == "a.pcm"
    assert "--full" not in cmd  # whole-disc read is no --count, not --full
    assert "--count" not in cmd
    assert "--c2" not in cmd  # bitmap flag is --c2f
    assert cmd[-1] == "-q"  # no progress_cb → quiet


def test_read_disc_c2_sub_and_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_disc_c2(
        "/dev/sr0", Path("a.pcm"), Path("a.c2"), output_sub=Path("a.sub"), read_speed=8
    )
    (cmd,) = calls
    i = cmd.index("--sub")
    assert cmd[i : i + 4] == ["--sub", "raw", "--subf", "a.sub"]
    assert cmd[cmd.index("--speed") + 1] == "8"


def test_read_disc_c2_inline_leadin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_disc_c2(
        "/dev/sr0",
        Path("a.pcm"),
        Path("a.c2"),
        output_cdtext=Path("a.cdtext"),
        output_fulltoc=Path("a.fulltoc"),
    )
    # Single read pass; lead-in dumps captured inline (one spin-up).
    (cmd,) = calls
    assert cmd[3] == "read"
    assert cmd[cmd.index("--fulltoc") + 1] == "a.fulltoc"
    assert cmd[cmd.index("--cdtext") + 1] == "a.cdtext"


def test_read_disc_c2_metadata_only_omits_pcm_and_c2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The parity gate wants sub + lead-in only: a whole-disc read with no audio
    # write. Omitting output_pcm/output_c2 must drop --pcm/--c2f entirely (no
    # ~600 MB PCM), while --sub/--fulltoc/--cdtext still appear.
    calls = _patch_run(monkeypatch)
    ar.read_disc_c2(
        "/dev/sr0",
        output_sub=Path("a.sub"),
        output_fulltoc=Path("a.fulltoc"),
        output_cdtext=Path("a.cdtext"),
    )
    (cmd,) = calls
    assert cmd[:4] == [_ACC, "--device", "/dev/sr0", "read"]
    assert "--pcm" not in cmd
    assert "--c2f" not in cmd
    assert cmd[cmd.index("--sub") : cmd.index("--sub") + 4] == [
        "--sub",
        "raw",
        "--subf",
        "a.sub",
    ]
    assert cmd[cmd.index("--fulltoc") + 1] == "a.fulltoc"
    assert cmd[cmd.index("--cdtext") + 1] == "a.cdtext"


def test_read_disc_c2_exit_3_is_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exit 3 = completed with caveats (hard/suspect/residual C2) — not a failure.
    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **k: _Result(returncode=3))
    ar.read_disc_c2("/dev/sr0", Path("a.pcm"), Path("a.c2"))  # no raise


def test_read_disc_c2_exit_1_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: _Result(stderr=b"boom", returncode=1),
    )
    with pytest.raises(RuntimeError, match=r"exit 1.*boom"):
        ar.read_disc_c2("/dev/sr0", Path("a.pcm"), Path("a.c2"))


# ── read_span (targeted recovery re-read) ────────────────────────────────────


def test_read_span_command_and_speed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_span("/dev/sr0", 111142, 9481, Path("w.pcm"), read_speed=8)
    (cmd,) = calls
    assert cmd[:4] == [_ACC, "--device", "/dev/sr0", "read"]
    assert cmd[cmd.index("--start") + 1] == "111142"
    assert cmd[cmd.index("--count") + 1] == "9481"
    assert cmd[cmd.index("--speed") + 1] == "8"
    assert cmd[cmd.index("--pcm") + 1] == "w.pcm"
    assert "--c2f" not in cmd  # span read captures PCM only


def test_read_span_no_speed_flag_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_run(monkeypatch)
    ar.read_span("/dev/sr0", 0, 10, Path("w.pcm"))
    (cmd,) = calls
    assert "--speed" not in cmd


def test_read_span_exit_1_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: _Result(stderr=b"boom", returncode=1),
    )
    with pytest.raises(RuntimeError, match=r"exit 1.*boom"):
        ar.read_span("/dev/sr0", 0, 10, Path("w.pcm"))


def test_read_span_exit_3_is_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **k: _Result(returncode=3))
    ar.read_span("/dev/sr0", 0, 10, Path("w.pcm"))  # no raise


# ── progress streaming (--progress-fd 1 machine tokens on stdout) ─────────────


class _FakeProc:
    """Popen stand-in: stdout yields the --progress-fd machine lines; returncode set.

    stderr is written to a real TemporaryFile by _run_with_progress (which the
    mock ignores), so only stdout + wait()/returncode need faking here.
    """

    def __init__(self, stdout_lines: str, returncode: int = 0) -> None:
        self.stdout = io.StringIO(stdout_lines)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


def _patch_popen(monkeypatch, stdout_lines: str, returncode: int = 0) -> dict:
    captured: dict = {}

    def _popen(cmd, **k):
        captured["cmd"] = cmd
        return _FakeProc(stdout_lines, returncode)

    monkeypatch.setattr(ar.subprocess, "Popen", _popen)
    return captured


def test_read_disc_c2_streams_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    # --progress-fd 1 machine tokens on stdout, then the summary line.
    stdout = (
        "progress 23 100\n"
        "progress 100 100\n"
        "summary hard=0 c2=0 recovered=0 suspect=0 rereads=0 slips=0\n"
    )
    captured = _patch_popen(monkeypatch, stdout, returncode=0)
    seen: list[tuple[int, int]] = []
    ar.read_disc_c2(
        "/dev/sr0",
        Path("a.pcm"),
        Path("a.c2"),
        progress_cb=lambda d, t: seen.append((d, t)),
    )
    assert seen == [(23, 100), (100, 100)]
    # The machine channel is requested on fd 1, with the human line muted.
    cmd = captured["cmd"]
    assert cmd[cmd.index("--progress-fd") + 1] == "1"
    assert "-q" in cmd


def test_read_disc_c2_progress_exit_3_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exit 3 via the progress path is completed-with-caveats, not a failure.
    _patch_popen(monkeypatch, "progress 100 100\n", returncode=3)
    ar.read_disc_c2(
        "/dev/sr0", Path("a.pcm"), Path("a.c2"), progress_cb=lambda d, t: None
    )  # no raise


def test_read_disc_c2_progress_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_popen(monkeypatch, "", returncode=1)
    with pytest.raises(RuntimeError, match="exit 1"):
        ar.read_disc_c2(
            "/dev/sr0", Path("a.pcm"), Path("a.c2"), progress_cb=lambda d, t: None
        )


# ── binary resolution ────────────────────────────────────────────────────────


def test_resolve_accudisc_prefers_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    # The snapshot is git-ignored (AccuDisc ships from its own repo), so whether it
    # exists is an environment fact — stub the probe and assert the branch instead.
    monkeypatch.setattr(Path, "is_file", lambda self: True)
    resolved = ar._resolve_accudisc()
    assert resolved.endswith("tools/accudisc/accudisc")
    assert Path(resolved).is_absolute()


def test_resolve_accudisc_falls_back_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # No snapshot in the checkout (the CI case): fall back to a bare PATH lookup.
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    assert ar._resolve_accudisc() == "accudisc"


# ---------------------------------------------------------------------------
# TOC geometry — the 0x02 -> 0x00 degrade and the session-safety rule
# ---------------------------------------------------------------------------

_HEALTHY = """track 1 lba 0 sectors 25705 audio
track 2 lba 25705 sectors 25110 audio
leadout lba 253937
source=fulltoc degrade=none pregaps=none sessions=1..1 disc_type=0x00
"""

_DEGRADED = """track 1 lba 0 sectors 18350 audio
track 2 lba 18350 sectors 22125 audio
leadout lba 236435
source=toc degrade=leadin_unreadable pregaps=none
"""


def test_parse_toc_healthy_full_toc() -> None:
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(_HEALTHY)
    assert geom.track_lsns == [0, 25705]
    assert geom.disc_last_lsn == 253936  # lead-out - 1
    assert geom.source == "fulltoc"
    assert not geom.degraded
    assert geom.sessions == "1..1"
    assert geom.data_tracks == []


def test_parse_toc_degraded_carries_the_reason() -> None:
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(_DEGRADED)
    assert geom.track_lsns == [0, 18350]
    assert geom.degraded
    assert geom.degrade == "leadin_unreadable"
    assert geom.sessions is None


def test_parse_toc_geometry_is_identical_across_both_paths() -> None:
    """AccuDisc cross-checked the two decodes byte-for-byte on real hardware;
    this pins that our parse does not introduce a difference either."""
    from cdda2img.accudisc_reader import parse_toc_output

    body = "track 1 lba 0 sectors 100 audio\nleadout lba 100\n"
    a = parse_toc_output(body + "source=fulltoc degrade=none sessions=1..1\n")
    b = parse_toc_output(body + "source=toc degrade=leadin_unreadable\n")
    assert (a.track_lsns, a.disc_last_lsn) == (b.track_lsns, b.disc_last_lsn)


def test_parse_toc_marks_data_tracks() -> None:
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(
        "track 1 lba 0 sectors 100 audio\n"
        "track 2 lba 100 sectors 200 data\n"
        "leadout lba 300\nsource=toc degrade=leadin_unreadable\n"
    )
    assert geom.data_tracks == [2]


def test_parse_toc_tolerates_a_missing_acquisition_line() -> None:
    """Pre-degrade AccuDisc builds emit no source=/degrade= line; they only ever
    answered from the lead-in, so that is the honest default."""
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output("track 1 lba 0 sectors 100 audio\nleadout lba 100\n")
    assert geom.source == "fulltoc"
    assert not geom.degraded


def test_parse_toc_rejects_unparseable_output() -> None:
    import pytest

    from cdda2img.accudisc_reader import parse_toc_output

    with pytest.raises(ValueError, match="could not parse"):
        parse_toc_output("accudisc: something went wrong\n")


def test_session_safe_full_toc_is_always_safe() -> None:
    from cdda2img.accudisc_reader import parse_toc_output

    safe, _why = parse_toc_output(_HEALTHY).session_safe
    assert safe


def test_session_safe_degraded_all_audio_is_inferred_not_measured() -> None:
    """Accepted, but the reason must say it is an inference — a multi-session
    all-audio disc would pass this test and still be unsafe."""
    from cdda2img.accudisc_reader import parse_toc_output

    safe, why = parse_toc_output(_DEGRADED).session_safe
    assert safe
    assert "NOT measured" in why


def test_session_safe_degraded_with_a_data_track_refuses() -> None:
    """Cannot distinguish mixed-mode (refuse) from Enhanced CD (exclude) without
    session structure, and the two demand opposite handling."""
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(
        "track 1 lba 0 sectors 100 audio\n"
        "track 2 lba 100 sectors 200 data\n"
        "leadout lba 300\nsource=toc degrade=leadin_unreadable\n"
    )
    safe, why = geom.session_safe
    assert not safe
    assert "mixed-mode" in why


def test_session_safe_measured_single_session_beats_the_inference() -> None:
    """session_count=1 on the degrade path (READ DISC INFORMATION, a separate
    opcode that does not re-read the lead-in) settles it as a measured fact —
    even against a data track that the inference alone would refuse on."""
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(
        "track 1 lba 0 sectors 100 audio\n"
        "track 2 lba 100 sectors 200 data\n"
        "leadout lba 300\nsource=toc degrade=leadin_unreadable session_count=1\n"
    )
    assert geom.session_count == 1
    safe, why = geom.session_safe
    assert safe  # measured single session outranks the data-track inference
    assert "measured" in why


def test_session_safe_measured_multisession_refuses_even_if_all_audio() -> None:
    """The hole AccuDisc found: an audio CD-R written in two TAO sessions is all
    audio, so the data-track inference would wrongly call it safe. A measured
    count catches it."""
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(
        "track 1 lba 0 sectors 100 audio\n"
        "track 2 lba 100 sectors 200 audio\n"
        "leadout lba 300\nsource=toc degrade=leadin_unreadable session_count=2\n"
    )
    safe, why = geom.session_safe
    assert not safe
    assert "2 sessions" in why


def test_session_count_is_ignored_when_the_toc_is_healthy() -> None:
    """session_count is always emitted now, including on a full TOC. It must not
    override the full-TOC path — degrade=none is already the strongest evidence."""
    from cdda2img.accudisc_reader import parse_toc_output

    body = (
        "track 1 lba 0 sectors 100 audio\nleadout lba 100\n"
        "source=fulltoc degrade=none sessions=1..1 session_count=2\n"
    )
    geom = parse_toc_output(body)
    assert geom.session_count == 2
    safe, why = geom.session_safe
    assert safe  # a full TOC carries real session structure regardless of the count
    assert why == "full TOC"


def test_session_count_zero_on_degrade_falls_back_to_the_inference() -> None:
    """session_count=0 means READ DISC INFORMATION could not say — indistinguishable
    from a pre-count build, so the conservative data-track inference governs."""
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(
        "track 1 lba 0 sectors 100 audio\n"
        "track 2 lba 100 sectors 200 data\n"
        "leadout lba 300\nsource=toc degrade=leadin_unreadable session_count=0\n"
    )
    assert geom.session_count == 0
    safe, why = geom.session_safe
    assert not safe  # falls through to the data-track refusal
    assert "mixed-mode" in why


def test_untrusted_toc_geometry_refuses_regardless_of_sessions() -> None:
    """toc_trusted=0 (a self-contradicting lead-in, usually copy protection) makes
    the track map unbelievable — refuse even though the disc claims one session."""
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(
        "track 1 lba 0 sectors 100 audio\n"
        "track 2 lba 100 sectors 200 audio\n"
        "leadout lba 300\n"
        "source=fulltoc degrade=none sessions=1..1 session_count=1 "
        "anomalies=lba_order,overlap toc_trusted=0\n"
    )
    assert geom.anomalies == ["lba_order", "overlap"]
    assert not geom.toc_trusted
    safe, why = geom.session_safe
    assert not safe
    assert "untrusted" in why
    assert "lba_order" in why


def test_clean_disc_reports_trusted_geometry_and_no_anomalies() -> None:
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(_HEALTHY)
    assert geom.toc_trusted
    assert geom.anomalies == []


def test_report_only_anomalies_do_not_make_the_toc_untrusted() -> None:
    """The six report-only slugs (e.g. empty_track) are recorded but the disc still
    rips — only lba_order/overlap/leadout_before set toc_trusted=0, and AccuDisc
    signals that separately with the token. We key on the token, not the slugs."""
    from cdda2img.accudisc_reader import parse_toc_output

    geom = parse_toc_output(
        "track 1 lba 0 sectors 100 audio\nleadout lba 100\n"
        "source=fulltoc degrade=none sessions=1..1 session_count=1 "
        "anomalies=empty_track\n"
    )
    assert geom.anomalies == ["empty_track"]
    assert geom.toc_trusted  # no toc_trusted=0 token → still trusted
    safe, _why = geom.session_safe
    assert safe


# ── speed probes (moved here with the parsing, from test_drive_speed.py) ──────

# Real `accudisc --device /dev/sr0 speed` output (PLEXTOR PX-716A), throttled to 8x.
_SPEED_OUT = """\
page2A     max 40x (7056 kB/s)  current 8x (1411 kB/s)
rotation   CAV (constant angular velocity)
  curve[0] lba 0..359999  17.0x..40.0x (nominal)
"""


class _TextResult:
    """subprocess.run(text=True) result — str stderr, unlike the bytes _Result."""

    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_read_speed_parses_current_and_max(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess, "run", lambda *a, **k: _TextResult(stdout=_SPEED_OUT)
    )
    assert ar.read_speed("/dev/sr0") == (1411, 7056)


def test_read_speed_calls_the_speed_subcommand_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One command, and it is `speed`.

    The name matters: this used to call `speed-report`, which AccuDisc removed —
    so it failed on every invocation and silently fell through to cdrdao. There
    is no fallback now, which is exactly why the subcommand name is asserted.
    """
    calls: list[list[str]] = []

    def _run(cmd: list[str], **k: object) -> _TextResult:
        calls.append(cmd)
        return _TextResult(stdout=_SPEED_OUT)

    monkeypatch.setattr(ar.subprocess, "run", _run)
    assert ar.read_speed("/dev/sr0") == (1411, 7056)
    assert len(calls) == 1
    assert calls[0][0].endswith("accudisc")
    assert calls[0][-1] == "speed"


def test_read_speed_scans_stderr_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess, "run", lambda *a, **k: _TextResult(stderr=_SPEED_OUT)
    )
    assert ar.read_speed("/dev/sr0") == (1411, 7056)


def test_read_speed_max_without_current(monkeypatch: pytest.MonkeyPatch) -> None:
    """A max with no current line still yields the max — callers need only that."""
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: _TextResult(stdout="page2A     max 40x (7056 kB/s)\n"),
    )
    assert ar.read_speed("/dev/sr0") == (None, 7056)


def test_read_speed_missing_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess, "run", lambda *a, **k: _TextResult(stdout="garbage")
    )
    assert ar.read_speed("/dev/sr0") == (None, None)


def test_read_speed_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: _TextResult(stdout=_SPEED_OUT, returncode=1),
    )
    assert ar.read_speed("/dev/sr0") == (None, None)


def test_read_speed_binary_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(ar.subprocess, "run", boom)
    assert ar.read_speed("/dev/sr0") == (None, None)


def test_speed_ladder_rows_parses_the_accudisc_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = "speed req=40 page2a=8 measured=8.01\nspeed req=4 page2a=4 measured=4.01\n"
    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **k: _TextResult(stdout=out))
    assert ar.speed_ladder_rows("/dev/sr0") == [(40, 8, 8.01), (4, 4, 4.01)]


def test_speed_ladder_rows_empty_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ar.subprocess, "run", lambda *a, **k: _TextResult(returncode=2, stderr="boom")
    )
    assert ar.speed_ladder_rows("/dev/sr0") == []


# ── engine_version ───────────────────────────────────────────────────────────


def test_engine_version_takes_the_first_non_blank_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ar.subprocess,
        "run",
        lambda *a, **k: _TextResult(stdout="\naccudisc 0.2.0\nbuilt with foo\n"),
    )
    monkeypatch.setenv(ar.TRANSPORT_ENV, "subprocess")
    assert ar.engine_version() == "accudisc 0.2.0 [transport: subprocess]"


def test_engine_version_is_device_free(monkeypatch: pytest.MonkeyPatch) -> None:
    """--version must not open a device: it is called while building the RLOG,
    long after the drive work is done."""
    calls: list[list[str]] = []

    def _run(cmd: list[str], **k: object) -> _TextResult:
        calls.append(cmd)
        return _TextResult(stdout="accudisc 0.2.0\n")

    monkeypatch.setattr(ar.subprocess, "run", _run)
    ar.engine_version()
    assert calls[0][1:] == ["--version"]


def test_engine_version_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing version must not fail a rip that has already succeeded."""

    def boom(*a, **k):
        raise OSError

    monkeypatch.setattr(ar.subprocess, "run", boom)
    monkeypatch.setenv(ar.TRANSPORT_ENV, "subprocess")
    assert ar.engine_version() == "accudisc (version unknown) [transport: subprocess]"


# ── write_disc / eject ───────────────────────────────────────────────────────


class _FakeProc:
    def __init__(self, stdout: str, returncode: int) -> None:
        self.stdout = io.StringIO(stdout)
        self.returncode = returncode

    def wait(self) -> int:
        return self.returncode


def _patch_write_popen(monkeypatch, stdout: str = "", returncode: int = 0) -> dict:
    captured: dict = {}

    def _popen(cmd, **k):
        captured["cmd"] = cmd
        return _FakeProc(stdout, returncode)

    monkeypatch.setattr(ar.subprocess, "Popen", _popen)
    return captured


def test_write_disc_builds_the_write_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _patch_write_popen(monkeypatch, "summary result=ok\n", 0)
    rc, _err, outcome = ar.write_disc(
        "/dev/sr0", Path("/burn/a.toc"), Path("/burn/a.pcm"), 8
    )
    cmd = cap["cmd"]
    assert cmd[1:4] == ["--device", "/dev/sr0", "write"]
    assert cmd[cmd.index("--toc") + 1] == "/burn/a.toc"
    assert cmd[cmd.index("--bin") + 1] == "/burn/a.pcm"
    assert cmd[cmd.index("--speed") + 1] == "8"
    assert cmd[cmd.index("--progress-fd") + 1] == "1"
    assert "--simulate" not in cmd
    assert (rc, outcome) == (0, "ok")


def test_write_disc_simulate_appends_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _patch_write_popen(monkeypatch, "", 0)
    ar.write_disc("/dev/sr0", Path("a.toc"), Path("a.pcm"), 8, simulate=True)
    assert "--simulate" in cap["cmd"]


def test_write_disc_returns_the_code_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 3 means the disc WAS written. Raising here would let a caller report a
    successful burn as a failure, so the decision stays with the caller."""
    _patch_write_popen(monkeypatch, "summary result=ok\n", 3)
    rc, _err, outcome = ar.write_disc("/dev/sr0", Path("a.toc"), Path("a.pcm"), 8)
    assert (rc, outcome) == (3, "ok")


def test_write_disc_extracts_the_result_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decisions key on result=, never on stderr wording."""
    _patch_write_popen(monkeypatch, "summary written=0 result=not_blank\n", 2)
    _rc, _err, outcome = ar.write_disc("/dev/sr0", Path("a.toc"), Path("a.pcm"), 8)
    assert outcome == "not_blank"


def test_write_disc_token_is_none_without_a_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_write_popen(monkeypatch, "progress 1 2\n", 2)
    _rc, _err, outcome = ar.write_disc("/dev/sr0", Path("a.toc"), Path("a.pcm"), 8)
    assert outcome is None


def test_write_disc_forwards_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_write_popen(monkeypatch, "progress 10 300\nprogress 300 300\n", 0)
    seen: list[tuple[int, int]] = []
    ar.write_disc(
        "/dev/sr0",
        Path("a.toc"),
        Path("a.pcm"),
        8,
        progress_cb=lambda d, t: seen.append((d, t)),
    )
    assert seen == [(10, 300), (300, 300)]


def test_eject_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(ar.subprocess, "run", boom)
    ar.eject("/dev/sr0")  # no exception


# ── the seam invariant ───────────────────────────────────────────────────────


def test_no_module_outside_the_seam_invokes_accudisc() -> None:
    """Every AccuDisc invocation in ``src/`` lives in this module.

    Not style — this is what makes the AccuDisc Python binding (their API_PLAN
    phase 4) a one-module swap. Five modules once imported ``_ACCUDISC`` and built
    their own argv (``drive_speed``, ``rip_log``, ``write_offset``, ``disc_writer``),
    which would have made "change the transport" five scattered edits, each with
    its own chance of being missed.

    Scope is ``src/`` only. ``tools/recovery_bench.py`` deliberately resolves its
    own binary and drives flags the seam does not expose (``features --stream``,
    ``speed <n>``, engine hashing) — a bench harness is allowed closer to the
    metal than the library is.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "cdda2img"
    seam = src / "accudisc_reader.py"
    offenders: list[str] = []
    for path in sorted(src.glob("*.py")):
        if path == seam:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "_ACCUDISC" in code:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == [], "AccuDisc invoked outside the seam:\n" + "\n".join(
        offenders
    )


# ── read_span_bytes ──────────────────────────────────────────────────────────


def test_read_span_bytes_returns_the_pcm_and_leaves_no_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The temp file is an implementation detail of the subprocess transport."""
    seen: list[Path] = []

    def _run(cmd, **k):
        out = Path(cmd[cmd.index("--pcm") + 1])
        out.write_bytes(b"\x01\x02" * 8)
        seen.append(out)
        return _Result(returncode=0)

    monkeypatch.setattr(ar.subprocess, "run", _run)
    monkeypatch.setattr("cdda2img.container.resolve_temp_dir", lambda need=0: tmp_path)
    data = ar.read_span_bytes("/dev/sr0", 100, 4)
    assert data == b"\x01\x02" * 8
    assert seen and not seen[0].exists()  # scratch dir torn down
    assert list(tmp_path.iterdir()) == []


def test_read_span_bytes_passes_start_count_and_speed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def _run(cmd, **k):
        calls.append(cmd)
        Path(cmd[cmd.index("--pcm") + 1]).write_bytes(b"")
        return _Result(returncode=0)

    monkeypatch.setattr(ar.subprocess, "run", _run)
    monkeypatch.setattr("cdda2img.container.resolve_temp_dir", lambda need=0: tmp_path)
    ar.read_span_bytes("/dev/sr0", 4500, 300, read_speed=8)
    cmd = calls[0]
    assert cmd[cmd.index("--start") + 1] == "4500"
    assert cmd[cmd.index("--count") + 1] == "300"
    assert cmd[cmd.index("--speed") + 1] == "8"


def test_read_span_bytes_asks_for_room_for_the_span(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The free-space check must be sized to the read, not left at the default —
    /tmp here is RAM-backed and the resolver is what steers scratch off it."""
    asked: list[int] = []

    def _run(cmd, **k):
        Path(cmd[cmd.index("--pcm") + 1]).write_bytes(b"")
        return _Result(returncode=0)

    monkeypatch.setattr(ar.subprocess, "run", _run)
    monkeypatch.setattr(
        "cdda2img.container.resolve_temp_dir",
        lambda need=0: (asked.append(need), tmp_path)[1],
    )
    ar.read_span_bytes("/dev/sr0", 0, 1000)
    assert asked == [1000 * 2352]


def test_read_span_bytes_propagates_a_fatal_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ar.subprocess, "run", lambda *a, **k: _Result(returncode=2))
    monkeypatch.setattr("cdda2img.container.resolve_temp_dir", lambda need=0: tmp_path)
    with pytest.raises(RuntimeError, match="span read"):
        ar.read_span_bytes("/dev/sr0", 0, 10)
    assert list(tmp_path.iterdir()) == []  # cleaned up even on failure


# ── transport selection (binding ⇄ subprocess) ───────────────────────────────
#
# Every test here clears the _import_binding cache: it is a functools.cache on a
# module global, so one test's fake binding would otherwise be the next test's
# environment.


class _FakeBindingError(Exception):
    pass


class _FakeAbiMismatch(_FakeBindingError):
    pass


class _FakeBinding:
    """The minimum surface accudisc_reader calls, so _BINDING_SURFACE is satisfied."""

    AccuDiscError = _FakeBindingError
    AbiMismatch = _FakeAbiMismatch

    def __init__(self) -> None:
        self.opened: list[str] = []

    @staticmethod
    def anomaly_token(bit: object) -> str:
        return str(bit)

    def Device(self, path: str) -> object:
        raise NotImplementedError


def _install(
    monkeypatch: pytest.MonkeyPatch, module: object | None, why: str = "x"
) -> None:
    ar._import_binding.cache_clear()
    monkeypatch.setattr(ar, "_import_binding", lambda: (module, "" if module else why))


@pytest.fixture(autouse=True)
def _reset_transport_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The warn-once flags are module globals; leaking them hides later warnings."""
    monkeypatch.setattr(ar, "_binding_warned", False)
    monkeypatch.setattr(ar, "_abi_warned", False)
    ar._import_binding.cache_clear()


def test_a_namespace_package_is_not_the_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The trap this project actually fell into, kept as a regression.

    With tools/ on sys.path, ``import accudisc`` SUCCEEDS and binds
    tools/accudisc/ — the git-ignored *binary snapshot* directory — as an empty
    PEP 420 namespace package. No ImportError is raised, because nothing failed:
    a module was found. It just has no ``Device``, and the failure surfaces far
    from the import.

    The condition is built here rather than relied on: whether the real tools/
    directory is on sys.path depends on which test files ran first, so a test
    that waited for it would pass vacuously in isolation — which is how it
    escaped notice in ``tools/binding_ab.py`` in the first place.
    """
    import importlib
    import sys

    (tmp_path / "accudisc").mkdir()  # a directory, no __init__.py — the snapshot shape
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "accudisc", raising=False)
    importlib.invalidate_caches()

    # The trap itself: the import succeeds and yields an attribute-less module.
    # Both accesses go through getattr, matching what _import_binding does — a
    # plain `phantom.__file__` makes ty resolve the name to tools/accudisc/ and
    # reject the attribute, which is ty being right about the thing under test.
    phantom = importlib.import_module("accudisc")

    assert getattr(phantom, "__file__", None) is None
    assert not hasattr(phantom, "Device")

    ar._import_binding.cache_clear()
    module, why = ar._import_binding()
    assert module is None, (
        "an attribute-less namespace package was accepted as the binding"
    )
    assert "namespace directory" in why


def test_import_rejects_a_module_missing_part_of_the_surface() -> None:
    class Partial:
        Device = object  # has Device, lacks the error types

    ar._import_binding.cache_clear()
    assert [n for n in ar._BINDING_SURFACE if not hasattr(Partial, n)]


def test_subprocess_mode_never_imports_the_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> tuple[object | None, str]:
        msg = "the binding was imported under transport=subprocess"
        raise AssertionError(msg)

    monkeypatch.setattr(ar, "_import_binding", _boom)
    monkeypatch.setenv(ar.TRANSPORT_ENV, "subprocess")
    assert ar._binding("toc") is None
    assert ar.active_transport() == "subprocess"


def test_auto_falls_back_and_warns_exactly_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _install(monkeypatch, None, why="no module named accudisc")
    monkeypatch.setenv(ar.TRANSPORT_ENV, "auto")
    with caplog.at_level("WARNING"):
        assert ar._binding("toc") is None
        assert ar._binding("span read") is None
    warnings = [r for r in caplog.records if "binding unavailable" in r.message]
    assert len(warnings) == 1, (
        "the fallback must announce itself once, not never and not always"
    )


def test_binding_mode_refuses_to_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pinning the transport is how you know which one ran; falling back would
    answer a question nobody asked."""
    _install(monkeypatch, None, why="not built")
    monkeypatch.setenv(ar.TRANSPORT_ENV, "binding")
    with pytest.raises(RuntimeError, match="not importable"):
        ar._binding("toc")


def test_an_unknown_mode_degrades_to_auto(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(ar.TRANSPORT_ENV, "libary")  # typo, deliberately
    with caplog.at_level("WARNING"):
        assert ar._transport_mode() == "auto"
    assert any("not one of" in r.message for r in caplog.records)


def test_active_transport_reports_binding_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _FakeBinding())
    monkeypatch.setenv(ar.TRANSPORT_ENV, "auto")
    assert ar.active_transport() == "binding"


def test_active_transport_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """It is the reporting path: a report that changes the log it describes is
    its own bug."""
    _install(monkeypatch, None)
    monkeypatch.setenv(ar.TRANSPORT_ENV, "auto")
    with caplog.at_level("WARNING"):
        assert ar.active_transport() == "subprocess"
    assert not caplog.records


def test_abi_mismatch_degrades_to_the_subprocess(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A skewed build breaks the binding but leaves the CLI binary good — exactly
    the case the fallback exists for."""
    fake = _FakeBinding()

    def _skew() -> None:
        msg = "compiled against 0.2 but loaded 0.3"
        raise _FakeAbiMismatch(msg)

    with caplog.at_level("WARNING"):
        assert ar._try_binding(fake, "toc", _skew) is None
    assert any("ABI mismatch" in r.message for r in caplog.records)


def test_a_device_error_is_raised_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retrying a real device failure through the other transport would re-run the
    same failing operation and report the second failure as if it were the first."""
    fake = _FakeBinding()

    def _sense() -> None:
        msg = "sense 3/11/00 unrecovered read error"
        raise _FakeBindingError(msg)

    with pytest.raises(RuntimeError, match="binding transport"):
        ar._try_binding(fake, "span read", _sense)


# ── the flipped paths, exercised device-free ─────────────────────────────────
#
# tools/binding_ab.py is the acceptance test and needs a drive. These are the
# cheap half: that the struct→dataclass assembly and the sink reassembly are
# wired correctly at all, so a typo fails here rather than on the shelf.


class _FakeTrack:
    def __init__(self, number: int, lba: int, *, audio: bool = True) -> None:
        self.number = number
        self.lba = lba
        self.is_audio = audio
        self.is_data = not audio


class _FakeSession:
    def __init__(self, number: int) -> None:
        self.number = number


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeToc:
    def __init__(self, tracks, sessions, leadout, anomalies=(), trusted=True) -> None:
        self.tracks = tracks
        self.sessions = sessions
        self.leadout_lba = leadout
        self.anomalies = anomalies
        self.trusted = trusted

    @property
    def audio_tracks(self):
        return tuple(t for t in self.tracks if t.is_audio)

    @property
    def data_tracks(self):
        return tuple(t for t in self.tracks if t.is_data)


class _FakeInfo:
    def __init__(self, source: str, degrade: str, session_count: int) -> None:
        self.source = _FakeToken(source)
        self.degrade = _FakeToken(degrade)
        self.session_count = session_count


class _FakeDevice:
    """Context-manager device returning canned structs; records the reads it served."""

    def __init__(self, toc_src=None, chunks=()) -> None:
        self._toc_src = toc_src
        self._chunks = chunks
        self.read_kwargs: dict[str, object] = {}

    def __enter__(self) -> _FakeDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read_toc_src(self):
        return self._toc_src

    def read(self, lba: int, count: int, **kwargs: Any):
        self.read_kwargs = {"lba": lba, "count": count, **kwargs}
        sink = kwargs["sink"]
        for chunk in self._chunks:
            sink(chunk)


class _FakeChunk:
    def __init__(self, nsec: int, data: bytes, sector_len: int = 2352) -> None:
        self.nsec = nsec
        self.data = data
        self.sector_len = sector_len


def _binding_with(device: _FakeDevice) -> _FakeBinding:
    module = _FakeBinding()
    module.Device = lambda _path: device  # type: ignore[assignment]
    return module


def test_toc_geometry_from_binding_maps_every_field() -> None:
    device = _FakeDevice(
        toc_src=(
            _FakeToc(
                tracks=[
                    _FakeTrack(1, 0),
                    _FakeTrack(2, 15000),
                    _FakeTrack(3, 30000, audio=False),
                ],
                sessions=[_FakeSession(1)],
                leadout=162892,
                anomalies=("b", "a"),
            ),
            _FakeInfo("fulltoc", "none", 1),
        )
    )
    geom = ar._toc_geometry_from_binding(_binding_with(device), "/dev/sr0")

    assert geom.track_lsns == [0, 15000]  # audio only
    assert geom.disc_last_lsn == 162891  # leadout - 1
    assert geom.data_tracks == [3]
    assert geom.source == "fulltoc"
    assert geom.session_count == 1
    assert geom.sessions == "1..1"
    assert geom.anomalies == ["a", "b"]  # sorted: the shape the A/B found agreeing
    assert geom.toc_trusted is True


def test_toc_geometry_sessions_is_none_on_the_format_0_degrade() -> None:
    """No session structure means no range to report — not "1..1" invented from a
    count. The CLI omits the token here too."""
    device = _FakeDevice(
        toc_src=(
            _FakeToc(tracks=[_FakeTrack(1, 0)], sessions=[], leadout=1000),
            _FakeInfo("toc", "leadin_unreadable", 0),
        )
    )
    geom = ar._toc_geometry_from_binding(_binding_with(device), "/dev/sr0")
    assert geom.sessions is None
    assert geom.degraded is True


def test_read_span_binding_reassembles_chunks_in_order() -> None:
    chunks = [_FakeChunk(2, b"\xaa" * 4704), _FakeChunk(1, b"\xbb" * 2352)]
    device = _FakeDevice(chunks=chunks)
    seen: list[tuple[int, int]] = []

    data = ar._read_span_binding(
        _binding_with(device),
        "/dev/sr0",
        100,
        3,
        8,
        lambda done, total: seen.append((done, total)),
    )

    assert data == b"\xaa" * 4704 + b"\xbb" * 2352
    assert device.read_kwargs["lba"] == 100
    assert device.read_kwargs["count"] == 3
    assert device.read_kwargs["speed_x"] == 8
    assert seen == [(2, 3), (3, 3)]  # progress is cumulative sectors, not per chunk


def test_read_span_binding_leaves_speed_unset_when_not_asked() -> None:
    device = _FakeDevice(chunks=[_FakeChunk(1, b"\x00" * 2352)])
    ar._read_span_binding(_binding_with(device), "/dev/sr0", 0, 1, None, None)
    assert device.read_kwargs["speed_x"] == 0  # 0 = leave the drive alone


def test_read_span_binding_refuses_an_unexpected_sector_length() -> None:
    """2352 is our PREDICTION of a number the library REPORTS. Slice assignment
    into a bytearray silently resizes it, so an unchecked wrong prediction yields
    a plausible buffer of the wrong length instead of an error."""
    device = _FakeDevice(chunks=[_FakeChunk(1, b"\x00" * 2646, sector_len=2646)])
    with pytest.raises(RuntimeError, match="2646-byte sectors"):
        ar._read_span_binding(_binding_with(device), "/dev/sr0", 0, 1, None, None)


def test_read_toc_prefers_the_binding_and_never_shells_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _FakeDevice(
        toc_src=(
            _FakeToc(
                tracks=[_FakeTrack(1, 0)], sessions=[_FakeSession(1)], leadout=500
            ),
            _FakeInfo("fulltoc", "none", 1),
        )
    )
    _install(monkeypatch, _binding_with(device))
    monkeypatch.setenv(ar.TRANSPORT_ENV, "auto")

    def _boom(*a: object, **k: object) -> None:
        msg = "read_toc shelled out while the binding was available"
        raise AssertionError(msg)

    monkeypatch.setattr(ar.subprocess, "run", _boom)
    assert ar.read_toc("/dev/sr0").track_lsns == [0]


def test_read_span_bytes_falls_back_to_the_subprocess_on_abi_skew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The end-to-end degrade: a skewed binding must not fail the read when a
    perfectly good CLI binary is sitting right there."""
    module = _FakeBinding()

    def _skewed(_path: str) -> None:
        msg = "compiled against 0.2 but loaded 0.3"
        raise _FakeAbiMismatch(msg)

    module.Device = _skewed  # type: ignore[assignment]
    _install(monkeypatch, module)
    monkeypatch.setenv(ar.TRANSPORT_ENV, "auto")

    def _run(cmd: list[str], **k: object) -> _Result:
        Path(cmd[cmd.index("--pcm") + 1]).write_bytes(b"\x01" * 2352)
        return _Result(returncode=0)

    monkeypatch.setattr(ar.subprocess, "run", _run)
    monkeypatch.setattr("cdda2img.container.resolve_temp_dir", lambda need=0: tmp_path)
    assert ar.read_span_bytes("/dev/sr0", 0, 1) == b"\x01" * 2352
