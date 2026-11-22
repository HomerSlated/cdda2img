"""``rip --profile-only`` (kgr, 2026-11-22): a temporary test switch that skips the
two zero-read repairs, CTDB parity repair and the displacement check, so a track
that fails AccurateRip reaches the named recovery profile itself."""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from cdda2img import cdda2img as app


def _rip_args(monkeypatch: pytest.MonkeyPatch, *argv: str):
    monkeypatch.setattr("sys.argv", ["cdda2img", "rip", *argv])
    return app.parse_args()


def test_the_switch_is_off_unless_asked_for(monkeypatch) -> None:
    assert _rip_args(monkeypatch).profile_only is False
    assert _rip_args(monkeypatch, "--profile-only").profile_only is True


def _guards_of(tree: ast.AST, callee: str) -> list[ast.expr]:
    """The test of every ``if`` whose body calls *callee*."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        calls = {
            n.func.id
            for stmt in node.body
            for n in ast.walk(stmt)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        if callee in calls:
            found.append(node.test)
    return found


def _skips_under_profile_only(test: ast.expr) -> bool:
    return any(
        isinstance(n, ast.UnaryOp)
        and isinstance(n.op, ast.Not)
        and isinstance(n.operand, ast.Name)
        and n.operand.id == "profile_only"
        for n in ast.walk(test)
    )


@pytest.mark.parametrize("callee", ["repair_whole_disc", "_displacement_stage"])
def test_both_zero_read_repairs_are_skipped_under_the_switch(callee) -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.rip_image)))
    guards = _guards_of(tree, callee)
    assert guards, f"rip_image no longer calls {callee} under an if"
    assert any(_skips_under_profile_only(g) for g in guards), callee


def test_the_recovery_stages_are_not_skipped() -> None:
    """Control: the switch must not reach the profile it exists to test."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.rip_image)))
    for callee in ("_recovery_chain", "_recover_failed_tracks"):
        for guard in _guards_of(tree, callee):
            assert not _skips_under_profile_only(guard), callee


def test_dispatch_hands_the_switch_to_the_rip() -> None:
    tree = ast.parse(textwrap.dedent(inspect.getsource(app._dispatch)))
    kwargs = {
        k.arg
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "rip_image"
        for k in n.keywords
    }
    assert "profile_only" in kwargs
