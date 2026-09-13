"""The interactive write-offset loop (``setup --write-offset``), driven by script.

Since AccuDisc 0.37.0 an eject can fail: a mounted filesystem or another process
holding the device keeps the door locked, and the engine now says so instead of
reporting success with the tray shut. Before this file, the loop discarded that
answer and printed "Disc ejected. Reinsert the burned disc" regardless.

Every device call is replaced, and both prompt helpers answer from a script that
raises on any prompt it was not told to expect, so a changed flow fails loudly
rather than hanging on a questionary prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import cdda2img.config as config
from cdda2img import setup
from cdda2img import write_offset as wo


class _Script:
    """Answers prompts in order; records what was asked and with which default."""

    def __init__(self, answers: list[Any]) -> None:
        self._answers = list(answers)
        self.asked: list[tuple[str, tuple[Any, ...]]] = []

    def __call__(self, prompt: str, *args: Any) -> Any:
        self.asked.append((prompt, args))
        if not self._answers:
            msg = f"unscripted prompt: {prompt!r}"
            raise AssertionError(msg)
        return self._answers.pop(0)


class _Rig:
    def __init__(self) -> None:
        self.calls: list[Any] = []
        self.eject_results: list[str | None] = []


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    r = _Rig()

    def _eject(_device: str) -> str | None:
        r.calls.append("eject")
        return r.eject_results.pop(0) if r.eject_results else None

    def _save(_path: Path, data: dict) -> None:
        r.calls.append(("save", len(data["cycles"])))

    monkeypatch.setattr(wo, "probe_drive", lambda _device, _name: ("TEST DRIVE", 30))
    monkeypatch.setattr(wo, "work_dir", lambda: tmp_path)
    monkeypatch.setattr(wo, "results_path", lambda _slug: tmp_path / "results.toml")
    monkeypatch.setattr(wo, "generate_test_signal", lambda _w, _t: None)
    monkeypatch.setattr(wo, "burn_disc", lambda _t, _d, _s: r.calls.append("burn"))
    monkeypatch.setattr(wo, "rip_disc", lambda _d, _b, _t: r.calls.append("rip"))
    monkeypatch.setattr(wo, "eject", _eject)
    monkeypatch.setattr(wo, "analyse_cycle", lambda _b, _r: {"measured_offset": -30})
    monkeypatch.setattr(wo, "save_results", _save)
    monkeypatch.setattr(setup, "_load_wotcd", list)
    monkeypatch.setattr(
        config, "save_drive_write_offset", lambda *_a: r.calls.append("save_offset")
    )
    return r


def _run(
    monkeypatch: pytest.MonkeyPatch, selects: list[Any], confirms: list[Any]
) -> tuple[_Script, _Script]:
    select, confirm = _Script(selects), _Script(confirms)
    monkeypatch.setattr(setup, "_select", select)
    monkeypatch.setattr(setup, "_confirm", confirm)
    setup._section_write_offset("/dev/sr0", 8)
    return select, confirm


def test_a_failed_eject_warns_and_a_retry_carries_on(rig, monkeypatch, capsys):
    rig.eject_results = ["disc still present: device held open"]
    _run(
        monkeypatch,
        selects=[setup._CYCLE_BURN, setup._EJECT_RETRY],
        # insert blank, reinsert burned, save offset? no, another disc? no
        confirms=[True, True, False, False],
    )
    out = capsys.readouterr().out
    assert "WARNING: the disc did not eject: disc still present" in out
    # The rip only happens after the eject that succeeded.
    assert rig.calls == ["burn", "eject", "eject", "rip", ("save", 1), "eject"]


def test_quitting_a_failed_eject_after_a_burn_rips_nothing_and_says_how_to_resume(
    rig, monkeypatch, capsys
):
    rig.eject_results = ["busy"]
    _, confirm = _run(
        monkeypatch,
        selects=[setup._CYCLE_BURN, setup._EJECT_QUIT],
        confirms=[True],
    )
    assert rig.calls == ["burn", "eject"]
    # Never told the user the disc was ejected.
    assert not any("Disc ejected" in prompt for prompt, _ in confirm.asked)
    assert setup._CYCLE_READ in capsys.readouterr().out


def test_an_already_burned_disc_can_be_measured_without_burning(rig, monkeypatch):
    _, confirm = _run(
        monkeypatch,
        selects=[setup._CYCLE_READ],
        # insert burned disc, save offset? no, another disc? no
        confirms=[True, False, False],
    )
    assert rig.calls == ["rip", ("save", 1), "eject"]
    # "press Enter" must mean yes: questionary returns the default on Enter.
    assert confirm.asked[0][1] == (True,)


def test_a_measured_cycle_is_saved_before_the_eject_that_can_quit(rig, monkeypatch):
    rig.eject_results = ["busy"]
    _, confirm = _run(
        monkeypatch,
        selects=[setup._CYCLE_READ, setup._EJECT_QUIT],
        confirms=[True, False],
    )
    assert rig.calls == ["rip", ("save", 1), "eject"]
    assert not any("Another disc" in prompt for prompt, _ in confirm.asked)


def test_pressing_enter_at_the_burn_prompt_burns(rig, monkeypatch):
    """Control on the defaults: the burn prompt says "press Enter to burn", and
    ``_confirm`` defaults to No, so without an explicit default Enter quit."""
    _, confirm = _run(
        monkeypatch, selects=[setup._CYCLE_BURN], confirms=[True, True, False, False]
    )
    burn_prompt = confirm.asked[0]
    assert "press Enter to burn" in burn_prompt[0]
    assert burn_prompt[1] == (True,)


def test_quit_at_the_first_menu_touches_nothing(rig, monkeypatch):
    _run(monkeypatch, selects=[setup._CYCLE_QUIT], confirms=[])
    assert rig.calls == []
