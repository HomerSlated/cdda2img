"""cd-paranoia recovery controls: -z retries, -e callback-stream progress,
and exact-frame (sector-range) re-ripping."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

import cdda2img.container as container
import cdda2img.disc_reader as dr


class _FakeProc:
    returncode = 0


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Stub query_disc + subprocess.run + wav_to_raw_pcm; capture the argv."""
    calls: dict = {}

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(dr, "query_disc", lambda d: (0, 999, [(1, 0, 1000)]))
    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    monkeypatch.setattr(container, "wav_to_raw_pcm", lambda *a, **k: None)
    return calls


# ── -z retry budget (attempts per frame) ───────────────────────────────────


def test_retry_flags_default_empty() -> None:
    assert dr._retry_flags(None, False) == []


def test_retry_flags_count_is_attached() -> None:
    # -z takes an optional argument, so the count must be attached (-z40, not -z 40).
    assert dr._retry_flags(40, False) == ["-z40"]


def test_retry_flags_never_skip_is_bare() -> None:
    assert dr._retry_flags(20, True) == ["-z"]  # never_skip wins over a count


def test_rip_disc_max_retries_adds_attached_z(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_run(monkeypatch)
    dr.rip_disc("/dev/sr0", tmp_path / "o.pcm", read_offset=30, max_retries=40)
    cmd = calls["cmd"]
    assert "-z40" in cmd
    assert cmd.index("-z40") < cmd.index("--")


def test_rip_single_track_never_skip_adds_bare_z(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_run(monkeypatch)
    dr.rip_single_track("/dev/sr0", 1, tmp_path / "o.pcm", never_skip=True)
    cmd = calls["cmd"]
    assert "-z" in cmd
    assert cmd.index("-z") < cmd.index("--")


# ── -e callback-stream progress ─────────────────────────────────────────────


class _FakePopen:
    """Fake cd-paranoia: yields canned -e callback lines on stderr, then exits."""

    lines: ClassVar[list[str]] = []
    rc: int = 0
    last_cmd: ClassVar[list[str]] = []

    def __init__(self, cmd, **kw):
        _FakePopen.last_cmd = cmd
        self.stderr = iter(_FakePopen.lines)

    def wait(self) -> int:
        return _FakePopen.rc


def test_progress_parses_wrote_frontier(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakePopen.lines = [
        "Sending all callback output to stderr for wrapper script\n",
        "##: 0 [read] @ 1176\n",  # read event — ignored (not WROTE)
        "##: 14 [wrote] @ 0\n",  # sector 0 — not > elapsed(0), no emit
        "##: 14 [wrote] @ 1176\n",  # sector 1
        "##: 14 [wrote] @ 588000\n",  # sector 500
        "##: 14 [wrote] @ 588\n",  # backwards — must not emit
        "##: 15 [finished] @ 1175424\n",
    ]
    _FakePopen.rc = 0
    monkeypatch.setattr(dr.subprocess, "Popen", _FakePopen)

    from cdda2img.cdrdao_progress import ProgressUpdate

    seen: list[ProgressUpdate] = []
    wav = tmp_path / "x.wav"
    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "1-", str(wav)]
    rc = dr._run_paranoia_with_progress(cmd, wav, 1000, [(1, 0, 1000)], 0, seen.append)

    assert rc == 0
    # -e injected right after the binary name
    assert _FakePopen.last_cmd[1] == "-e"
    # monotonic WROTE frontier (1, 500), backwards dropped, then 100% close (1000)
    assert [u.elapsed_frames for u in seen] == [1, 500, 1000]
    assert seen[-1].fraction == 1.0


def test_single_track_close_stays_on_its_track(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recovery close-out at 100% must not roll over to the next track's number.

    A single-track rip passes disc_first == track's first LSN and
    total_sectors == track length. The final ``emit(total_sectors)`` addresses the
    sector one past the track's end — the first LSN of the *next* track — so before
    the clamp it reported "track 9" the instant track 8 finished. The bar must close
    at 100% while the track number stays 8.
    """
    # Track 8 spans LSN 700..799; track 9 begins at 800.
    tracks = [(8, 700, 100), (9, 800, 100)]
    last_sector_words = 799 * dr._CD_FRAMEWORDS  # last real sector of track 8
    _FakePopen.lines = [
        "Sending all callback output to stderr for wrapper script\n",
        f"##: 14 [wrote] @ {last_sector_words}\n",
        "##: 15 [finished] @ 0\n",
    ]
    _FakePopen.rc = 0
    monkeypatch.setattr(dr.subprocess, "Popen", _FakePopen)

    from cdda2img.cdrdao_progress import ProgressUpdate

    seen: list[ProgressUpdate] = []
    wav = tmp_path / "x.wav"
    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "8", str(wav)]
    rc = dr._run_paranoia_with_progress(cmd, wav, 100, tracks, 700, seen.append)

    assert rc == 0
    # Every update — streaming and the 100% close — stays on track 8.
    assert [u.track for u in seen] == [8, 8]
    assert seen[-1].elapsed_frames == 100  # count == length: bar still hits 100%
    assert seen[-1].fraction == 1.0


def test_capture_env_tees_raw_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # CDDA2IMG_PARANOIA_CAPTURE tees every raw -e line for offline replay.
    _FakePopen.lines = [
        "##: 0 [read] @ 1176\n",
        "##: 3 [correction] @ 117600\n",
        "##: 14 [wrote] @ 1176\n",
    ]
    _FakePopen.rc = 0
    monkeypatch.setattr(dr.subprocess, "Popen", _FakePopen)
    cap = tmp_path / "live.cs"
    monkeypatch.setenv("CDDA2IMG_PARANOIA_CAPTURE", str(cap))

    wav = tmp_path / "x.wav"
    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "1-", str(wav)]
    dr._run_paranoia_with_progress(cmd, wav, 1000, [(1, 0, 1000)], 0, lambda u: None)

    assert cap.read_text() == "".join(_FakePopen.lines)


def test_progress_failure_no_final_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _FakePopen.lines = ["##: 14 [wrote] @ 1176\n"]
    _FakePopen.rc = 1  # non-zero exit → no 100% close emitted
    monkeypatch.setattr(dr.subprocess, "Popen", _FakePopen)

    from cdda2img.cdrdao_progress import ProgressUpdate

    seen: list[ProgressUpdate] = []
    wav = tmp_path / "x.wav"
    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "1-", str(wav)]
    rc = dr._run_paranoia_with_progress(cmd, wav, 1000, [(1, 0, 1000)], 0, seen.append)
    assert rc == 1
    assert [u.elapsed_frames for u in seen] == [1]  # only the WROTE line, no close


# ── exact-frame (sector-range) re-rip ───────────────────────────────────────


def test_rip_sector_range_builds_track_relative_span(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    out = tmp_path / "o.pcm"
    calls: dict = {}

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        out.write_bytes(b"\x00" * (dr._CD_FRAME_BYTES * 50))  # 50 frames produced
        return _FakeProc()

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    monkeypatch.setattr(container, "wav_to_raw_pcm", lambda *a, **k: None)

    produced = dr.rip_sector_range("/dev/sr0", 8, 100, 150, out, read_offset=30)

    cmd = calls["cmd"]
    assert "8:[.100]-8:[.150]" in cmd
    assert cmd.index("8:[.100]-8:[.150]") > cmd.index("--")
    # returns the ACTUAL frames produced, not the requested width
    assert produced == 50


def test_rip_sector_range_rejects_bad_range(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid frame range"):
        dr.rip_sector_range("/dev/sr0", 8, 150, 100, tmp_path / "o.pcm")


# ── stall-status readout (cumulative recovery tally on the read-head line) ───
#
# Design note: corrections arrive in fast in-memory bursts the TUI can't catch, so we
# DON'T flash a per-event label. Instead the per-bucket recovery counts accumulate over
# the track (cumulative, never reset) and ride on the read-head line — the only phase the
# TUI reliably renders — as "recovering @ sector N — K jitter, …". Validated against the
# real-disc capture /var/tmp/t8.cs: 2513 jitter, visible on ~80% of 10 Hz refresh frames.


def test_recovery_summary_empty_is_blank() -> None:
    from collections import Counter

    assert dr._recovery_summary(Counter()) == ""


def test_recovery_summary_orders_by_severity() -> None:
    from collections import Counter

    # jitter is least severe (last in _RECOVERY_ORDER); read err outranks it.
    summary = dr._recovery_summary(Counter({"jitter": 5, "read err": 2}))
    assert summary == "2 read err, 5 jitter"


def test_recovery_summary_omits_zero_buckets() -> None:
    from collections import Counter

    assert dr._recovery_summary(Counter({"jitter": 3, "scratch": 0})) == "3 jitter"


def test_correction_codes_map_to_buckets() -> None:
    assert dr._RECOVERY_BUCKETS[3] == "jitter"  # FIXUP_ATOM
    assert dr._RECOVERY_BUCKETS[12] == "read err"
    assert dr._RECOVERY_BUCKETS[6] == "skipped"
    assert 0 not in dr._RECOVERY_BUCKETS  # plain read is not recovery
    assert 1 not in dr._RECOVERY_BUCKETS  # verify is not recovery
    assert 9 not in dr._RECOVERY_BUCKETS  # overlap is normal flow, not recovery


def test_stall_line_reads_before_any_recovery() -> None:
    # A pure stall with no corrections shows the read head, no tally.
    p = dr._ParanoiaProgress(disc_first=0)
    for s in range(100, 100 + dr._STALL_EVENTS):
        emitted = p.feed(0, s)  # code 0 = read
    assert emitted is True  # the _STALL_EVENTS-th read trips the stall line
    assert p.note == "reading @ sector 107"
    assert p.recovery == {}


def test_stall_line_shows_cumulative_recovery_tally() -> None:
    # Corrections accumulate; once stalled, the read line carries the running total.
    p = dr._ParanoiaProgress(disc_first=0)
    for s in range(100, 100 + dr._STALL_EVENTS):
        p.feed(3, s)  # code 3 = jitter (FIXUP_ATOM)
    assert p.recovery["jitter"] == dr._STALL_EVENTS
    assert p.note == "recovering @ sector 107 — 8 jitter"


def test_recovery_tally_survives_wrote_bursts() -> None:
    # The cumulative tally must NOT reset when the WROTE frontier advances — otherwise it
    # would be zeroed inside the same commit burst that hides it (the core fix).
    p = dr._ParanoiaProgress(disc_first=0)
    for s in range(100, 100 + dr._STALL_EVENTS):
        p.feed(3, s)  # 8 jitter → recovering line
    assert p.recovery["jitter"] == 8

    # a commit advances the bar and reverts the note to the plain count
    assert p.feed(14, 50) is True  # WROTE @ sector 50 > elapsed 0
    assert p.note == ""
    assert p.elapsed == 50
    assert p.recovery["jitter"] == 8  # tally survived the commit

    # the next read-hold shows the *cumulative* total again, not a fresh zero
    for s in range(200, 200 + dr._STALL_EVENTS):
        p.feed(0, s)  # reads
    assert p.note == "recovering @ sector 207 — 8 jitter"


def test_wrote_burst_does_not_emit_recovery_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # End-to-end through _run_paranoia_with_progress: a correction then a stall of reads
    # surfaces the tally on the read line; the bar only moves on WROTE.
    _FakePopen.lines = [
        "##: 14 [wrote] @ 1176\n",  # sector 1 — bar to 1, count
        "##: 3 [correction] @ 117600\n",  # jitter @ sector 100 (tally=1, not yet stalled)
        *["##: 0 [read] @ 117600\n"]
        * dr._STALL_EVENTS,  # reads → stall, recovering line
        "##: 14 [wrote] @ 235200\n",  # sector 200 — bar advances, note back to count
    ]
    _FakePopen.rc = 0
    monkeypatch.setattr(dr.subprocess, "Popen", _FakePopen)

    from cdda2img.cdrdao_progress import ProgressUpdate

    seen: list[ProgressUpdate] = []
    wav = tmp_path / "x.wav"
    cmd = ["cd-paranoia", "-d", "/dev/sr0", "--", "1-", str(wav)]
    rc = dr._run_paranoia_with_progress(cmd, wav, 1000, [(1, 0, 1000)], 0, seen.append)

    assert rc == 0
    pairs = [(u.elapsed_frames, u.note) for u in seen]
    assert pairs == [
        (1, ""),  # first WROTE — count
        (1, "recovering @ sector 100 — 1 jitter"),  # stall, tally on the read line
        (200, ""),  # bar advances — back to count
        (1000, ""),  # 100% close
    ]
