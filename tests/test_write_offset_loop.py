"""The interactive write-offset loop (``setup --write-offset``), driven by script.

Since AccuDisc 0.37.0 an eject can fail: a mounted filesystem or another process
holding the device keeps the door locked, and the engine now says so instead of
reporting success with the tray shut. Before this file, the loop discarded that
answer and printed "Disc ejected. Reinsert the burned disc" regardless.

Every device call is replaced, and both prompt helpers answer from a script that
raises on any prompt it was not told to expect, so a changed flow fails loudly
rather than hanging on a questionary prompt.

Since 2026-10-02 the loop also closes the tray itself before every burn and every
read, and a disc that fails to read gets one eject, reload and re-read before
another disc is offered. A CD-R burned that day read blank at its first load and
ripped 11/11 at a later one, so one failed reading says nothing about the burn.
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
        self.load_results: list[str | None] = []
        self.rip_errors: list[str | None] = []


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Rig:
    r = _Rig()

    def _eject(_device: str) -> str | None:
        r.calls.append("eject")
        return r.eject_results.pop(0) if r.eject_results else None

    def _load(_device: str) -> str | None:
        r.calls.append("load")
        return r.load_results.pop(0) if r.load_results else None

    def _rip(_device: str, _bin: Path, _toc: Path) -> None:
        r.calls.append("rip")
        error = r.rip_errors.pop(0) if r.rip_errors else None
        if error is not None:
            raise RuntimeError(error)

    def _save(_path: Path, data: dict) -> None:
        r.calls.append(("save", len(data["cycles"])))

    monkeypatch.setattr(wo, "probe_drive", lambda _device, _name: ("TEST DRIVE", 30))
    monkeypatch.setattr(wo, "work_dir", lambda: tmp_path)
    monkeypatch.setattr(wo, "results_path", lambda _slug: tmp_path / "results.toml")
    monkeypatch.setattr(wo, "generate_test_signal", lambda _w, _t: None)
    monkeypatch.setattr(wo, "burn_disc", lambda _t, _d, _s: r.calls.append("burn"))
    monkeypatch.setattr(wo, "rip_disc", _rip)
    monkeypatch.setattr(wo, "eject", _eject)
    monkeypatch.setattr(wo, "load", _load)
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
    # The eject that opens the tray for the blank succeeds; the one after the
    # burn does not.
    rig.eject_results = [None, "disc still present: device held open"]
    _run(
        monkeypatch,
        selects=[setup._CYCLE_BURN, setup._EJECT_RETRY],
        # insert blank, reload burned, save offset? no, another disc? no
        confirms=[True, True, False, False],
    )
    out = capsys.readouterr().out
    assert "WARNING: the disc did not eject: disc still present" in out
    # The rip only happens after the eject that succeeded, and after a load.
    assert rig.calls == [
        "eject",
        "load",
        "burn",
        "eject",
        "eject",
        "load",
        "rip",
        ("save", 1),
        "eject",
    ]


def test_quitting_a_failed_eject_after_a_burn_rips_nothing_and_says_how_to_resume(
    rig, monkeypatch, capsys
):
    rig.eject_results = [None, "busy"]
    _, confirm = _run(
        monkeypatch,
        selects=[setup._CYCLE_BURN, setup._EJECT_QUIT],
        confirms=[True],
    )
    assert rig.calls == ["eject", "load", "burn", "eject"]
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
    # Ejected and loaded first: a disc left in the drive since its burn has not
    # been loaded since it was written, so "leave it in" is no longer offered.
    assert rig.calls == ["eject", "load", "rip", ("save", 1), "eject"]
    assert "leave it in" not in confirm.asked[0][0]
    # "press Enter" must mean yes: questionary returns the default on Enter.
    assert confirm.asked[0][1] == (True,)


def test_a_measured_cycle_is_saved_before_the_eject_that_can_quit(rig, monkeypatch):
    # The eject before the read succeeds; the one after the measurement does not.
    rig.eject_results = [None, "busy"]
    _, confirm = _run(
        monkeypatch,
        selects=[setup._CYCLE_READ, setup._EJECT_QUIT],
        confirms=[True, False],
    )
    assert rig.calls == ["eject", "load", "rip", ("save", 1), "eject"]
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


# ── a failed read gets one reload before the disc is blamed ──────────────────

_ANOTHER = "Try again with another disc?"


def _asked(confirm: _Script, text: str) -> int:
    return sum(text in prompt for prompt, _ in confirm.asked)


def test_one_failed_read_reloads_and_rereads_the_same_disc(rig, monkeypatch, capsys):
    rig.rip_errors = ["no audio tracks on disc"]
    _, confirm = _run(
        monkeypatch,
        selects=[setup._CYCLE_BURN],
        # insert blank, reload burned, save offset? no, another disc? no
        confirms=[True, True, False, False],
    )
    # The second rip follows an eject and a load of its own.
    assert rig.calls == [
        "eject",
        "load",
        "burn",
        "eject",
        "load",
        "rip",
        "eject",
        "load",
        "rip",
        ("save", 1),
        "eject",
    ]
    # One failure never offers another disc. (An unscripted prompt would also
    # have raised, so this assertion cannot pass by the prompt going unanswered.)
    assert _asked(confirm, _ANOTHER) == 0
    assert "Rip failed: no audio tracks on disc" in capsys.readouterr().out


def test_two_failed_reads_offer_another_disc_once(rig, monkeypatch):
    rig.rip_errors = ["no audio tracks on disc", "no audio tracks on disc"]
    _, confirm = _run(
        monkeypatch,
        selects=[setup._CYCLE_BURN],
        # insert blank, reload burned, another disc? no
        confirms=[True, True, False],
    )
    assert rig.calls == [
        "eject",
        "load",
        "burn",
        "eject",
        "load",
        "rip",
        "eject",
        "load",
        "rip",
        "eject",
    ]
    assert _asked(confirm, _ANOTHER) == 1


def test_the_failure_count_belongs_to_the_disc_not_the_session(rig, monkeypatch):
    """Disc 1 fails twice. Disc 2 fails once and must still get its own re-read:
    a counter carried across discs would offer a third disc after one failure."""
    rig.rip_errors = ["unreadable", "unreadable", "unreadable", None]
    _, confirm = _run(
        monkeypatch,
        selects=[setup._CYCLE_BURN, setup._CYCLE_BURN],
        # disc 1: insert blank, reload, another disc? yes
        # disc 2: insert blank, reload, save offset? no, another disc? no
        confirms=[True, True, True, True, True, False, False],
    )
    assert rig.calls.count("rip") == 4
    assert rig.calls.count("burn") == 2
    assert rig.calls[-4:] == ["load", "rip", ("save", 1), "eject"]
    assert _asked(confirm, _ANOTHER) == 1


def test_a_failed_burn_still_offers_another_disc_at_once(rig, monkeypatch):
    """Control: the re-read rule is about reads. A failed burn has spent its
    blank, and the question about another disc comes straight away. The disc is
    ejected first, so the question is asked with the tray out."""

    def _burn(_toc: Path, _device: str, _speed: int) -> None:
        rig.calls.append("burn")
        msg = "accudisc write failed (exit 2)"
        raise RuntimeError(msg)

    monkeypatch.setattr(wo, "burn_disc", _burn)
    _, confirm = _run(monkeypatch, selects=[setup._CYCLE_BURN], confirms=[True, False])
    assert rig.calls == ["eject", "load", "burn", "eject"]
    assert _asked(confirm, _ANOTHER) == 1


def test_a_refused_burn_does_not_ask_for_another_disc(rig, monkeypatch, capsys):
    """AccuDisc 0.48.0 refuses a burn when the drive does not hold its write
    parameters. Nothing was written, so the blank is not spent and another one
    would meet the same refusal: no eject, no question, and the user is told to
    keep the disc."""

    def _burn(_toc: Path, _device: str, _speed: int) -> None:
        rig.calls.append("burn")
        msg = "the disc is still blank (test_write held=1)"
        raise wo.BurnRefused(msg)

    monkeypatch.setattr(wo, "burn_disc", _burn)
    _, confirm = _run(monkeypatch, selects=[setup._CYCLE_BURN], confirms=[True])
    assert rig.calls == ["eject", "load", "burn"]
    assert _asked(confirm, _ANOTHER) == 0
    out = capsys.readouterr().out
    assert "Burn refused: the disc is still blank (test_write held=1)" in out
    assert "Keep this disc" in out
    assert "Burn failed" not in out


def test_burn_disc_raises_burn_refused_on_the_write_params_token(tmp_path, monkeypatch):
    """The token has to survive ``write_offset.burn_disc`` or the loop above
    cannot tell a refusal from a spoiled disc."""
    import cdda2img.accudisc_reader as ar

    monkeypatch.setattr(
        ar, "write_disc", lambda *a, **k: (2, "bufe sent=1 held=0", "write_params")
    )
    with pytest.raises(wo.BurnRefused, match="bufe sent=1 held=0"):
        wo.burn_disc(tmp_path / "test.toc", "/dev/sr0", 8)


def test_a_failed_burn_whose_eject_is_quit_asks_nothing_more(rig, monkeypatch):
    def _burn(_toc: Path, _device: str, _speed: int) -> None:
        rig.calls.append("burn")
        msg = "accudisc write failed (exit 2)"
        raise RuntimeError(msg)

    monkeypatch.setattr(wo, "burn_disc", _burn)
    rig.eject_results = [None, "busy"]
    _, confirm = _run(
        monkeypatch, selects=[setup._CYCLE_BURN, setup._EJECT_QUIT], confirms=[True]
    )
    assert rig.calls == ["eject", "load", "burn", "eject"]
    assert _asked(confirm, _ANOTHER) == 0


def test_a_failed_load_warns_and_a_retry_carries_on(rig, monkeypatch, capsys):
    rig.load_results = ["tray did not close"]
    _run(
        monkeypatch,
        selects=[setup._CYCLE_READ, setup._LOAD_RETRY],
        confirms=[True, False, False],
    )
    assert "WARNING: the drive did not load the disc: tray did not close" in (
        capsys.readouterr().out
    )
    assert rig.calls == ["eject", "load", "load", "rip", ("save", 1), "eject"]


def test_quitting_a_failed_load_reads_nothing_and_says_how_to_resume(
    rig, monkeypatch, capsys
):
    rig.load_results = [None, "tray did not close"]
    _run(
        monkeypatch,
        selects=[setup._CYCLE_BURN, setup._LOAD_QUIT],
        confirms=[True, True],
    )
    assert rig.calls == ["eject", "load", "burn", "eject", "load"]
    assert setup._CYCLE_READ in capsys.readouterr().out


def test_quitting_the_eject_between_two_reads_says_how_to_resume(
    rig, monkeypatch, capsys
):
    rig.rip_errors = ["unreadable"]
    rig.eject_results = [None, None, "busy"]
    _, confirm = _run(
        monkeypatch,
        selects=[setup._CYCLE_BURN, setup._EJECT_QUIT],
        confirms=[True, True],
    )
    assert rig.calls == ["eject", "load", "burn", "eject", "load", "rip", "eject"]
    assert _asked(confirm, _ANOTHER) == 0
    assert setup._CYCLE_READ in capsys.readouterr().out


def test_the_tray_is_opened_before_a_blank_is_asked_for(rig, monkeypatch):
    """Quitting that first eject burns nothing and asks nothing."""
    rig.eject_results = ["busy"]
    _, confirm = _run(
        monkeypatch, selects=[setup._CYCLE_BURN, setup._EJECT_QUIT], confirms=[]
    )
    assert rig.calls == ["eject"]
    assert confirm.asked == []
