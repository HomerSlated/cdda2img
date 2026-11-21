"""Exit status 4: a rip written, but failing a verification its provenance records.

On 2026-09-14 a LITE-ON LH-20A1S rip had its audio misframed on 22 sectors in 23:
libata sent the reads by PIO, and the drive padded each record by 10 bytes. It
verified 0/11 against a disc AccurateRip holds at confidence 200, its subchannel Q
passed its CRC on no sector at all, and it exited 0, catalogued, with "No errors
occurred" sealed into its log.

A collapsed Q count does not refuse the rip: a drive returning subchannel before
C2 at a correct stride zeroes Q exactly as misframing does, with good audio. So the
container is kept and the exit status says to check it.

The AccurateRip half changed on 2026-11-21 (kgr: "An archive with no provenance is
pointless"). A disc AccurateRip holds with no track verified now aborts the rip:
nothing is written, the diagnosis goes to stderr, and the exit status is 5.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from cdda2img import cdda2img as app


def test_a_clean_rip_fails_nothing() -> None:
    assert app._failed_checks({"subq_q_valid": "341021/347208"}) == []


@pytest.mark.parametrize("miss", ["no_offset_verifies", "offset_probe_failed"])
def test_a_total_accuraterip_miss_is_an_abort_not_a_failed_check(miss: str) -> None:
    """A total miss no longer reaches exit 4: it aborts, so `_failed_checks` must
    not double-report it, and the abort text names no offset without a confirmed
    match."""
    assert app._failed_checks({"ar_total_miss": miss}) == []
    text = app._total_miss_message({"ar_total_miss": miss})
    assert text.startswith("Rip aborted, nothing written")
    assert "none of its tracks verified" in text
    assert "read offset -" not in text
    assert "[[drives]]" not in text


def test_a_confirmed_offset_candidate_is_named_without_blaming_the_offset() -> None:
    """With no container, the abort text is the only place the offset search's
    result survives: a confirmed match is reported, with both of its explanations
    and the delta to act on."""
    text = app._total_miss_message({
        "ar_total_miss": "offset_mismatch",
        "ar_offset_candidates": "-669,0",
        "ar_offset_suggests": "-675",
    })
    assert "read offset -669" in text
    assert "-675 from the offset used" in text
    assert "[[drives]]" in text
    assert "pressing" in text


@pytest.mark.parametrize(
    ("value", "fails"),
    [
        ("0/162892", True),  # the LITE-ON rip: sub read from C2 zeros
        ("7082/162892", True),  # 4.35%: one record in 23 framed right
        ("10859/162892", True),  # 6.67%: one in 15, AccuDisc's test chunk size
        ("12216/162892", True),  # just under 7.5%
        ("12217/162892", False),  # at 7.5%
        # 13.43%: a read while another process used the drive, AR v2 passing
        # (RECOVERY.md §12.0). The lowest yield on record with good audio, and
        # the case this threshold must not condemn.
        ("21876/162892", False),
        ("164704/347208", False),  # 47.4%: PX-716A at 32x, uncontended
    ],
)
def test_q_collapse_threshold(value: str, fails: bool) -> None:
    failed = app._failed_checks({"subq_q_valid": value})
    assert bool(failed) is fails
    if fails:
        assert value in failed[0]


def test_an_empty_subchannel_capture_is_a_collapse() -> None:
    """``0/0``: the rip asked for raw subchannel and got none. That is the limiting
    case of "Q almost never lines up", and exempting it would let the worst form of
    the failure exit 0."""
    (reason,) = app._failed_checks({"subq_q_valid": "0/0"})
    assert "0/0" in reason


@pytest.mark.parametrize("value", [None, "", "12", "a/b", "5/0", "-1/10"])
def test_an_unusable_q_count_is_not_a_failure(value: str | None) -> None:
    """Absent is not collapsed: a container ripped before the key existed has no
    witness and must not be condemned for it. Malformed counts are not evidence
    either way."""
    prov = {} if value is None else {"subq_q_valid": value}
    assert app._failed_checks(prov) == []


def test_the_q_count_subq_toc_writes_is_the_one_failed_checks_reads() -> None:
    """Producer and consumer in one process. Each half is tested alone above with
    hand-written strings, so a format drift on either side would pass both."""
    from cdda2img.subchannel import CD_SUBCODE_SIZE
    from cdda2img.subq_toc import build_rip_info
    from tests.test_subq_toc import _TOC

    info = build_rip_info(_TOC, b"\x00" * (CD_SUBCODE_SIZE * 4), track1_pregap=None)
    assert info.prov is not None
    failed = app._failed_checks(dict(info.prov))
    assert any("subchannel Q" in reason for reason in failed)


def test_a_collapsed_q_count_is_carried_into_the_abort_text() -> None:
    """A misframed read is the likelier cause of a 0/N than the offset, so an abort
    must still say the Q count collapsed: before 2026-11-21 that was the second
    of two exit-4 reasons, and the abort must not drop it."""
    prov = {"ar_total_miss": "no_offset_verifies", "subq_q_valid": "0/100"}
    text = app._total_miss_message(prov)
    assert "Also: subchannel Q passed its CRC on 0/100" in text


def test_report_exit_codes_and_precedence(capsys) -> None:
    assert app._report_rip_outcome(app.RipOutcome([], [])) == 0
    assert capsys.readouterr().out == ""

    assert app._report_rip_outcome(app.RipOutcome(["MusicBrainz"], [])) == 3
    capsys.readouterr()

    rc = app._report_rip_outcome(app.RipOutcome(["MusicBrainz"], ["reason A"]))
    out = capsys.readouterr().out
    assert rc == app.EXIT_WRITTEN_UNVERIFIED == 4
    assert "Written without an answer from: MusicBrainz (re-running" in out
    assert "Not verified: reason A." in out
    # One exit status is announced, not two contradictory ones.
    assert "Exit status 4" in out
    assert "Exit status 3" not in out


def test_an_aborted_rip_exits_5_on_stderr_only(capsys) -> None:
    """kgr, 2026-11-21: the notice goes to stderr. Exit 5, not 4: 4's contract
    is that a container was written."""
    outcome = app.RipOutcome([], [], "Rip aborted, nothing written: X.")
    rc = app._report_rip_outcome(outcome)
    captured = capsys.readouterr()
    assert rc == app.EXIT_NOT_WRITTEN == 5
    assert captured.out == ""
    assert "Rip aborted, nothing written: X." in captured.err
    assert "Exit status 5" in captured.err


def test_a_total_miss_writes_no_container() -> None:
    """The one call that creates the container and touches the catalogue sits in
    the ELSE arm of the total-miss gate, and an aborted rip returns before the
    exit-3/exit-4 bookkeeping. Reading the source rather than driving a whole rip,
    as the neighbouring tests do."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.rip_image)))
    finalize = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_finalize_import"
    ]
    assert len(finalize) == 1
    gates = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "total_miss"
    ]
    guarded = [
        g
        for g in gates
        if any(finalize[0] in list(ast.walk(stmt)) for stmt in g.orelse)
        and any("_total_miss_message" in ast.unparse(stmt) for stmt in g.body)
    ]
    assert len(guarded) == 1, "_finalize_import is not gated on the total miss"
    early = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and ast.unparse(n.test) == "aborted is not None"
        and any(isinstance(stmt, ast.Return) for stmt in n.body)
    ]
    assert len(early) == 1


def test_rip_returns_its_failed_checks_only_for_a_written_container() -> None:
    """Read the source rather than drive a whole rip: the checks must be computed
    from the rip's provenance and gated on a container having been written, the
    same way the unanswered services are."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.rip_image)))
    gated = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.IfExp)
        and isinstance(n.body, ast.Call)
        and isinstance(n.body.func, ast.Name)
        and n.body.func.id == "_failed_checks"
    ]
    assert len(gated) == 1
    assert "rbi_path" in ast.unparse(gated[0].test)


def test_no_keep_rbi_never_deletes_a_container_that_failed_a_check() -> None:
    """Exit 4 says "check it", and the reasons live only in the container's PROV.
    `rip --extract --no-keep-rbi` must not delete the one thing left to check."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.rip_image)))
    # The If that DIRECTLY holds the unlink: an enclosing `if extract and ...`
    # also contains it, nested, and says nothing about the check.
    guarding = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If)
        and any(
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and ast.unparse(stmt.value.func) == "rbi_path.unlink"
            for stmt in n.body
        )
    ]
    assert guarding, "no conditional guards rbi_path.unlink()"
    assert all("failed_checks" in ast.unparse(n.test) for n in guarding)


def test_the_q_count_is_merged_before_the_checks_read_it() -> None:
    """``subq_q_valid`` reaches the rip's provenance only through
    ``provenance.update(info.prov)``. A reorder that moved ``_failed_checks`` above
    that merge would silence the Q arm while every table test above still passed."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(app.rip_image)))
    merges = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and ast.unparse(n.func) == "provenance.update"
        and ast.unparse(n.args[0]) == "info.prov"
    ]
    checks = [
        n.lineno
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_failed_checks"
    ]
    assert merges
    assert checks
    assert max(merges) < min(checks)
