"""Exit status 4: a rip written, but failing a verification its provenance records.

On 2026-09-14 a LITE-ON LH-20A1S rip had its audio misframed on 22 sectors in 23:
libata sent the reads by PIO, and the drive padded each record by 10 bytes. It
verified 0/11 against a disc AccurateRip holds at confidence 200, its subchannel Q
passed its CRC on no sector at all, and it exited 0, catalogued, with "No errors
occurred" sealed into its log.

Neither signal refuses the rip. Both occur with good audio too: a pressing
AccurateRip does not hold, or a drive returning subchannel before C2 at a correct
stride, which zeroes Q exactly as misframing does. So the container is kept and
the exit status says to check it.
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
def test_a_total_accuraterip_miss_is_a_failed_check(miss: str) -> None:
    (reason,) = app._failed_checks({"ar_total_miss": miss})
    assert "matched none of its tracks" in reason
    assert "offset" not in reason  # no offset claim without a confirmed match


def test_a_confirmed_offset_candidate_is_named_without_blaming_the_offset() -> None:
    """The evidence-backed replacement for the removed "add a [[drives]] entry"
    hint: a confirmed match is reported, with both of its explanations."""
    (reason,) = app._failed_checks({
        "ar_total_miss": "offset_mismatch",
        "ar_offset_candidates": "-669,0",
        "ar_offset_suggests": "-675",
    })
    assert "read offset -669" in reason
    assert "pressing" in reason


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

    info = build_rip_info(_TOC, b"\x00" * (CD_SUBCODE_SIZE * 4))
    assert info.prov is not None
    failed = app._failed_checks(dict(info.prov))
    assert any("subchannel Q" in reason for reason in failed)


def test_both_checks_are_reported() -> None:
    failed = app._failed_checks({
        "ar_total_miss": "no_offset_verifies",
        "subq_q_valid": "0/100",
    })
    assert len(failed) == 2


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
