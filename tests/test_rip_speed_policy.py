"""The rip path must not blast the drive to max before reading the disc.

Settled as D1 in `docs/reference/accudisc-migration-plan.md` §6. Read speed is
drive state that persists across handles and processes, so restoring to maximum
on the way in governs the rip that follows.

**The rule survived its original justification.** D1 was argued from the
`subq_speed_cliff` model — raw-Q yield collapsing at the top of the range while
the audio gates stayed clean. That model is superseded (RECOVERY.md §12.3/§12.8:
one run read clean at 40/32/8x and dirty at 24/4x, non-monotone, so not a
cliff). The rule now rests on something plainer: since 2026-08-09 the initial
pass has a *requested* speed (`--ad-speed`), and a restore sited before the read
would silently overwrite it between the request and the read.

A test that merely asserts "restore_drive_speed is not called" would pass if
`rip_image` were deleted, so these read the source instead and pin WHERE the
call sits relative to the read.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from cdda2img import cdda2img


def _rip_image_tree() -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(cdda2img.rip_image)))


def _call_lines(tree: ast.AST, name: str) -> list[int]:
    """Lines calling *name*, whether as `mod.name(...)` or bare `name(...)`.

    Matching only `Attribute` was the first version, and it silently found zero
    `_rip_disc_stage` calls because that one is a bare name — the guard would
    have reported "no early restore" while measuring nothing. The vacuity
    assertion below is what caught it.
    """
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        called = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        if called == name:
            out.append(node.lineno)
    return out


def test_the_rip_still_restores_the_drive_at_the_end() -> None:
    """The other half of D1: one restore at session end.

    Asserted first, and deliberately, because it is what makes the next test
    meaningful — without it, "no restore before the read" would also pass on a
    tree where the restore had been deleted outright rather than moved.
    """
    assert _call_lines(_rip_image_tree(), "restore_drive_speed"), (
        "the end-of-rip restore is gone; an inherited ceiling now outlives the rip"
    )


def test_no_restore_to_max_before_the_disc_is_read() -> None:
    """The regression itself: every restore must come AFTER the read.

    The deleted call sat immediately before `_rip_disc_stage`, so its speed
    change governed the whole capture.
    """
    tree = _rip_image_tree()
    reads = _call_lines(tree, "_rip_disc_stage")
    assert reads, "cannot locate the read; this guard would pass vacuously"
    first_read = min(reads)
    early = [ln for ln in _call_lines(tree, "restore_drive_speed") if ln < first_read]
    assert not early, (
        f"restore_drive_speed called at line(s) {early}, before the disc read at "
        f"line {first_read} — this is the subq_speed_cliff regression (D1)"
    )
