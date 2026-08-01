"""accudisc_reader.py — raw MMC audio + C2 + subchannel capture via AccuDisc.

Drop-in AccuDisc replacement for :mod:`cdda2img.c2_reader` (which drove the
``c2read`` prototype). The public surface is identical — ``read_disc_c2`` /
``read_span`` / ``drive_supports_c2`` / ``probe_combos`` / ``park_spindle`` — so
the rip pipeline swaps modules with no call-site logic change. AccuDisc is the
successor to c2read; the invocation moved from flags to subcommands and the
progress interface changed, both handled here.

Differences from c2read, and how they are absorbed:

* **Subcommand form** — ``accudisc --device DEV <command>`` (``--device`` is a
  global option *before* the command), vs c2read's flat flag list.
* **C2 bitmap file** is ``--c2f`` (was ``--c2``); C2 pointers are requested by
  default (``--no-c2`` opts out).
* **Whole-disc read** is ``read`` with no ``--count`` (defaults through the
  lead-out), replacing c2read's ``--full``.
* **CD-Text and full TOC** are captured inline on the read pass
  (``read --fulltoc F --cdtext F``) — one spin-up, like c2read's single pass.
  The ``fulltoc`` dump is byte-identical to c2read's, so
  ``subq_toc.parse_fulltoc`` is unaffected. Absence of CD-Text writes no file and
  does not change the read's exit code, so the caller just checks for the file.
* **Progress** uses ``read --progress-fd 1``: newline-delimited machine tokens on
  stdout — ``progress <done> <total>`` lines plus a final
  ``summary hard=… c2=… recovered=… suspect=… rereads=… slips=…`` — unaffected by
  ``-q`` (which mutes only the human ``\\r`` stderr line). Parsed in
  :func:`_run_with_progress`. The frozen contract is the AccuDisc repo's
  ``docs/cli-machine-interface.md`` — parse against that, never the human stderr.
* **Exit contract** — ``0`` = completed clean; ``3`` = completed with caveats
  (``hard``/``suspect``/residual-C2 after recovery). Both mean the image was
  delivered, so the acceptance check is ``in (0, 3)``. ``1`` = usage/argument/
  local-file; ``2`` = fatal device/transport. Exit 0 is **not** verification — it
  means no *relative* signal fired; AccurateRip/CTDB gating stays with the caller.

READ CD returns s16le, so — unlike cdrdao's s16be BIN — the PCM needs no
byte-swap. Hard-unreadable sectors are zero-filled by AccuDisc (C2 bitmap
all-ones), so the PCM/C2/sub streams always stay length-consistent.

**Seam invariant: every AccuDisc invocation in the tree lives here.** No other
module builds an argv or imports ``_ACCUDISC`` — five once did (``drive_speed``,
``rip_log``, ``write_offset``, ``disc_writer``), which meant "swap the transport"
was five scattered edits instead of one. That matters now because AccuDisc's
API_PLAN phase 4 is a Python binding over ``libaccudisc``, and this module is
what it replaces. Stdout parsing (six regexes, below) is the part the binding
deletes; keep it here so there is exactly one place to delete it from.

**Two transports, one seam.** The AccuDisc Python binding (``import accudisc``, a
cffi API-mode extension over ``libaccudisc``) is preferred where it is bound, and
the subprocess path is the fallback. Both call the same ``accudisc_read()`` in the
same C library, so this is a change of *carrier*, not of behaviour — the CLI is a
thin argv layer over the calls the binding makes directly. See the transport
section below for what is flipped and what deliberately is not.

The subprocess path is not a hedge that will be retired. It is the acceptance
instrument: parity is "same disc, both transports, compare bytes"
(AccuDisc API_PLAN §7.3 names us as the only consumer who can run that test —
``tools/binding_ab.py`` is it), and it is also what serves a machine that has the
binary but no importable library.

That last clause used to read "which is currently *every* machine running this
project's 3.10 venv", and it stopped being true on 2026-07-29: AccuDisc's
extension is now built ``py_limited_api``, so one ``abi3`` artefact imports on
3.10 through 3.14 and the binding serves this venv. It is corrected rather than
deleted because the sentence was accurate when written and the situation it
described lasted two days longer than anyone noticed — the flip to ``binding``
landed on 2026-07-27 and was inert until the day it was fixed.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")

log = logging.getLogger(__name__)


def _resolve_accudisc() -> str:
    """Locate the AccuDisc binary: ``tools/accudisc/`` first, else ``$PATH``.

    ``tools/accudisc/accudisc`` is a **symlink into the AccuDisc build tree**, not
    a copy — it was a snapshot until their API was complete, and maintaining a
    separate one stopped paying once the binding linked ``libaccudisc`` from that
    same tree. ``is_file()`` follows the link, so a dangling one falls through.

    That makes the ``$PATH`` fallback a **silent version change** rather than a
    convenience. Previously it meant "pinned copy, or a system install if you
    have no checkout"; now it means "their live build, or something else of
    unknown vintage if their tree is momentarily absent" — mid-``make``, or a
    checkout that has not relinked yet. Both are readable and neither announces
    itself. :func:`engine_version` is what records which one actually ran, and is
    the only reason a rip log can settle the question afterwards.

    Path is dev-tree relative (mirrors the conf-example resolution); replace with
    ``importlib.resources`` when packaging lands.
    """
    local = Path(__file__).parent.parent.parent / "tools" / "accudisc" / "accudisc"
    return str(local) if local.is_file() else "accudisc"


_ACCUDISC = _resolve_accudisc()

#: Red Book audio frame. Plain PCM only — a span read requests no C2 and no
#: subchannel, so this is the whole sector on both transports.
_SECTOR_BYTES = 2352


# ── transport selection (library binding, falling back to the subprocess) ─────

#: ``auto`` (default) prefers the binding and falls back silently-once to the
#: subprocess; ``binding`` refuses to fall back; ``subprocess`` never imports it.
TRANSPORT_ENV = "CDDA2IMG_ACCUDISC_TRANSPORT"
_TRANSPORT_MODES = ("auto", "binding", "subprocess")

# What is flipped, and what is not. Each entry names the evidence that would
# retire it, because the previous version of this block did not and rotted twice:
#   flipped   read_toc          — Device.read_toc_src(), structs instead of regexes
#   flipped   read_span_bytes   — the call a subprocess cannot express (no temp file)
#   flipped   read_disc_c2      — a sequence on one Device: read_full_toc_raw() →
#                                 read_cdtext_raw() → read_to_file(). The old reason
#                                 recorded here ("the binding would add a
#                                 Python-level copy of ~800 MB") was never true:
#                                 read_to_file passes copy=False and the sink gets
#                                 a memoryview over library memory. It was a cost
#                                 model nobody measured until tools/sink_prescreen.py
#                                 put 0.55 s of CPU per disc against it (2026-07-29).
#   flipped   speed_ladder_rows — probe_speed_ladder(points=3), span left to the
#                                 library. AccuDisc hardware-validated `speeds
#                                 --sweep` and bound the probe (2026-07-29). The
#                                 (req, page2a, measured) triple is UNCHANGED: the
#                                 binding also offers verdicts and min/max, and
#                                 adopting its admission rule would fix the known
#                                 gap in drive_speed.admitted_ladder — but that is
#                                 policy, not carrier, and gets its own change.
#   flipped   write_disc        — Device.write(), after accudisc_write_opts gained
#                                 its `size` field (2026-07-29), which was the
#                                 stated blocker: without the ABI guard a future
#                                 field addition cannot raise AbiMismatch and
#                                 becomes a well-formed call about the wrong bytes
#                                 on the one operation here that is not idempotent.
#                                 NOT hardware-tested — burning needs blank media
#                                 and --simulate needs it too. Retire that caveat
#                                 with one simulated burn on a blank CD-R.
#                                 (lba = leadout/4, count clamped to leadout/2) or
#                                 the rows stop comparing with past measurements.
#   NOT       read_lead_in / engine_version / eject / park_spindle — bound or
#                                 trivial; no measured reason to move them.

# The names this module actually calls on the binding. Used as an identity proof,
# not a version check — see _import_binding for the namespace-package trap it
# closes. Keep in step with the call sites, or a real binding gets rejected.
_BINDING_SURFACE = (
    "Device",
    "AccuDiscError",
    "AbiMismatch",
    "anomaly_token",
    "C2",
    "Sub",
    "Unsupported",
    # AccuDisc 0.4.0+. Listed so an older binding is refused HERE, by name, rather
    # than mismapping a not-blank disc at the burn. Under 0.3.x "not blank" raised
    # `Unsupported`; from 0.4.0 it raises `NotBlank`, a **sibling** of it, and
    # AccuDisc declined to subclass on purpose (§ck.3) — subclassing would keep
    # `except Unsupported` catching a not-blank disc, which is the exact ambiguity
    # the new code exists to end. So there is no compatible catch to write: a
    # tuple of both would misreport a genuine `Unsupported` as "not blank" under
    # 0.4.0, buying compatibility by preserving the bug. Break loudly instead.
    "NotBlank",
    "WriteResult",
    "Anomaly",
    # Added with the CLI retirement (TODO item 6). `C2Verdict` decides whether the
    # rip asks for C2 at all and `version_string` is what a rip log records itself
    # as; both are read on paths that degrade quietly, so a binding missing either
    # would answer "no C2" and "version unknown" rather than fail — which is the
    # kind of wrong that survives a release. Named here, it is refused at import.
    "C2Verdict",
    "version_string",
)

_binding_warned = False
_abi_warned = False


#: Sibling of the binary symlink: ``tools/accudisc/pybinding`` points at AccuDisc's
#: ``bindings/python``. Git-ignored, machine-local, same arrangement and the same
#: reasons as :func:`_resolve_accudisc` — see :func:`_binding_search_path`.
_PYBINDING_LINK = (
    Path(__file__).parent.parent.parent / "tools" / "accudisc" / "pybinding"
)


def _binding_search_path() -> str | None:
    """``tools/accudisc/pybinding`` if it holds the real package, else ``None``.

    AccuDisc's Python binding is not on PyPI and its build sets an RPATH into
    their **build tree**, so the artefact is correct in exactly one directory and
    non-relocatable by construction (their ``build_accudisc.py`` names this as an
    open TODO). It therefore cannot be a declared dependency here — the only
    honest expression of "external project, resolved locally" is the one this
    repo already uses for their *binary*: a git-ignored symlink under ``tools/``.

    Installing it into the virtualenv instead was tried and rejected: ``uv sync``
    prunes anything absent from the lockfile, so ``make check`` uninstalled it
    every run. A hand-written ``.pth`` survives that, but leaves the arrangement
    invisible to the repo — which is the failure that cost us two days (the
    transport default was flipped to ``binding`` on 2026-07-27 and was inert
    until 2026-07-29, because nothing anywhere stated what the environment
    needed). A symlink that the code names out loud is discoverable; a ``.pth``
    is not.

    Note ``cffi`` is still required and *is* declared, in the dev group: it is a
    runtime dependency of an API-mode cffi extension, not a build-time one, so a
    module that builds cleanly still fails at ``from ._accudisc import ffi, lib``.
    """
    return (
        str(_PYBINDING_LINK)
        if (_PYBINDING_LINK / "accudisc" / "__init__.py").is_file()
        else None
    )


@functools.cache
def _import_binding() -> tuple[Any | None, str]:
    """Import the binding once, returning ``(module, why_not)``.

    Typed ``Any`` deliberately. ``tools/`` is on ty's ``extra-paths``, so a static
    check resolves ``accudisc`` to ``tools/accudisc/`` — the git-ignored *binary*
    snapshot directory — as a PEP 420 namespace portion, and reports every real
    attribute as missing. At runtime the scan records a directory without
    ``__init__.py`` as a namespace portion and keeps searching, so the real
    package further along ``sys.path`` wins. The alternative was seven
    suppressions on lines that are correct.

    Cached because the answer cannot change within a process and the import is
    the expensive half; the *policy* below is re-read on every call.

    **A successful import is not evidence the right thing was imported.** With
    ``tools/`` on ``sys.path`` — which is how every tool and its tests import
    each other — ``import accudisc`` *succeeds* and binds ``tools/accudisc/``,
    the git-ignored **binary snapshot directory**, as an empty PEP 420 namespace
    package. No ``ImportError`` is raised, because nothing failed: a module was
    found. It simply has no ``Device``, and the first attribute access dies
    somewhere far from here. So the module has to prove it is the binding by
    carrying the names we actually call, and a namespace portion (``__file__``
    is None) is named as such in the reason rather than reported as a vague
    missing attribute.

    The local symlink is **appended** to ``sys.path``, never prepended. A
    properly installed ``accudisc`` must win over our machine-local shim, so that
    the day AccuDisc close their "install properly" TODO this resolution retires
    itself silently instead of shadowing the thing it was standing in for.
    Appending is safe against the namespace trap above: a portion without
    ``__init__.py`` does not end the scan, so a real package later on the path
    still wins.
    """
    extra = _binding_search_path()
    if extra is not None and extra not in sys.path:
        sys.path.append(extra)
    try:
        # ty cannot resolve this and must not try: `accudisc` is an out-of-tree
        # optional dependency that exists only where AccuDisc is installed, which
        # is why the whole function is written around it being absent. The ignore
        # is inert where it *does* resolve (measured — ty does not report an
        # unused ignore here), so one spelling is correct in both environments.
        import accudisc  # ty: ignore[unresolved-import]
    except ImportError as exc:
        return None, str(exc)

    missing = [name for name in _BINDING_SURFACE if not hasattr(accudisc, name)]
    if missing:
        if getattr(accudisc, "__file__", None) is None:
            where = getattr(accudisc, "__path__", ["?"])
            return None, (
                f"'accudisc' resolved to the namespace directory {list(where)}, "
                f"not the Python binding — that is the binary snapshot, and it "
                f"shadows nothing because no real package is installed"
            )
        return None, f"'accudisc' is missing {', '.join(missing)} — not the binding"
    return accudisc, ""


def _transport_mode() -> str:
    """Read the transport policy fresh from the environment.

    Not frozen at import: a test that pins the transport must be able to pin it
    per case, or it asserts against whichever path the machine happened to take —
    and passing would then tell you nothing about the path you meant to exercise.
    """
    mode = os.environ.get(TRANSPORT_ENV, "auto").strip().lower() or "auto"
    if mode not in _TRANSPORT_MODES:
        log.warning(
            "%s=%r is not one of %s — using auto",
            TRANSPORT_ENV,
            mode,
            ", ".join(_TRANSPORT_MODES),
        )
        return "auto"
    return mode


def active_transport() -> str:
    """Which transport a flipped call would use right now: ``binding``/``subprocess``.

    Recorded in the rip log. A silent fallback would make the whole flip
    untestable — a later A/B would pass and nobody could say which transport
    passed it. Deliberately does not warn: this is the reporting path, and a
    report that changes the log it describes is its own bug.
    """
    if _transport_mode() == "subprocess":
        return "subprocess"
    return "binding" if _import_binding()[0] is not None else "subprocess"


def _binding(what: str) -> Any | None:
    """The binding module if it should serve *what*, else None (use subprocess)."""
    global _binding_warned

    mode = _transport_mode()
    if mode == "subprocess":
        return None
    module, why = _import_binding()
    if module is not None:
        return module
    if mode == "binding":
        # Explicitly demanded, so falling back would answer a question nobody
        # asked. The whole point of pinning it is to be sure which one ran.
        msg = f"{TRANSPORT_ENV}=binding requested but it is not importable: {why}"
        raise RuntimeError(msg)
    if not _binding_warned:
        _binding_warned = True
        log.warning(
            "AccuDisc Python binding unavailable (%s) — using the subprocess "
            "transport for %s and everything after it. Set %s=subprocess to "
            "silence this.",
            why,
            what,
            TRANSPORT_ENV,
        )
    return None


def _try_binding(module: Any, what: str, fn: Callable[[], _T]) -> _T | None:
    """Run *fn* on the binding path. ``None`` means "fall back to the subprocess".

    Only an **ABI mismatch** degrades: it means the extension and
    ``libaccudisc`` were built from different headers, which breaks the binding
    while leaving the CLI binary perfectly good — precisely the "binary but no
    working library" case the fallback exists for. It surfaces on ``Device()``
    rather than on import (the binding runs its skew check in ``__init__``), so
    it cannot be probed for in advance.

    Every other ``AccuDiscError`` is a real device or media failure. Retrying it
    through the subprocess would re-run the same failing operation against the
    same drive and report the second failure as if it were the first, so it is
    raised — as ``RuntimeError``, the exception the subprocess path already
    documents, so no caller needs to learn a new type.
    """
    global _abi_warned

    try:
        return fn()
    except module.AbiMismatch as exc:
        if not _abi_warned:
            _abi_warned = True
            log.warning(
                "AccuDisc binding/library ABI mismatch (%s) — falling back to the "
                "subprocess transport. Rebuild the binding to use it.",
                exc,
            )
        return None
    except module.AccuDiscError as exc:
        msg = f"accudisc {what} failed (binding transport): {exc}"
        raise RuntimeError(msg) from exc


# ── TOC geometry (READ TOC, with the 0x02 → 0x00 degrade) ─────────────────────


@dataclass
class TocGeometry:
    """Track boundaries from ``accudisc toc``, plus how they were obtained.

    ``accudisc toc`` prefers READ TOC format 0x02 (the lead-in) and falls back to
    format 0x00 (the drive's cooked track list) when the lead-in cannot be read,
    reporting which served the answer. The ``track``/``leadout`` lines are
    identical either way — AccuDisc cross-checked both decodes of the same disc
    byte-for-byte — so *geometry does not depend on the path*. What the fallback
    loses is **session structure**, which is why :attr:`session_safe` exists.
    """

    track_lsns: list[int]  # audio track start LBAs, in order
    disc_last_lsn: int  # last audio sector (lead-out - 1)
    source: str  # "fulltoc" (0x02) | "toc" (0x00)
    degrade: str  # "none" | leadin_unreadable | leadin_absent | leadin_malformed
    sessions: str | None = None  # "1..1" range — fulltoc only; the lead-in's numbering
    data_tracks: list[int] = field(default_factory=list)  # 1-based, CTRL bit 2
    # READ DISC INFORMATION session count. A separate token from `sessions`, and a
    # different opcode — answered from the drive's disc model, not the groove — so
    # it survives a degrade where the `sessions` range does not. 0 = unknown; None
    # = a pre-count AccuDisc build that never emitted it.
    session_count: int | None = None
    # A malformed lead-in that contradicts itself (copy protection, usually). When
    # `toc_trusted` is False the track map cannot be believed; `anomalies` names why.
    anomalies: list[str] = field(default_factory=list)
    toc_trusted: bool = True

    @property
    def degraded(self) -> bool:
        return self.degrade != "none"

    @property
    def session_safe(self) -> tuple[bool, str]:
        """Whether the session-1-only policy can be applied to this geometry.

        The policy (``subchannel.session1_audio_tracks``) needs per-track session
        membership and session 1's lead-out. Format 0x02 carries both; format
        0x00 carries neither — it returns a flat track list and the **last**
        session's lead-out. On a multi-session disc that lead-out is not session
        1's, so building geometry from it yields a wrong disc ID silently.

        The decision follows AccuDisc's §2026-07-22e/f table verbatim, strongest
        evidence first:

        0. ``toc_trusted`` is False — a self-contradicting lead-in (copy
           protection). The map is unbelievable whatever the sessions say; refuse
           and surface it as needing a human.
        1. ``session_count`` from READ DISC INFORMATION. This is a *separate*
           opcode that does not re-read the lead-in, so it is present on a
           degrade. ``1`` is a measured fact and settles it whatever the tracks
           are; ``>1`` is the multi-session hole and is refused.
        2. No count (a pre-count build, or ``0`` = unknown): fall back to "no data
           track anywhere". This rules out an Enhanced CD (whose session 2 is
           always data) but **not** a multi-session all-audio disc (an audio CD-R
           written in two TAO sessions) — AccuDisc caught that hole in our first
           formulation. So it is an inference, not a measurement, reported as such.
        """
        if not self.toc_trusted:
            detail = ", ".join(self.anomalies) or "self-contradicting lead-in"
            return False, f"untrusted TOC geometry ({detail}) — needs a human"
        if not self.degraded:
            return True, "full TOC"
        if self.session_count == 1:
            return True, "single session (measured)"
        if self.session_count is not None and self.session_count > 1:
            return False, f"{self.session_count} sessions and no session structure"
        if self.data_tracks:
            return False, (
                f"data track(s) {self.data_tracks} and no session structure — "
                "cannot distinguish mixed-mode (refuse) from Enhanced CD (exclude)"
            )
        return True, "all-audio, single session inferred (NOT measured)"


_TRACK_RE = re.compile(r"^track\s+(\d+)\s+lba\s+(-?\d+)\s+sectors\s+(\d+)\s+(\w+)")
_LEADOUT_RE = re.compile(r"^leadout\s+lba\s+(\d+)")


def parse_toc_output(stdout: str) -> TocGeometry:
    """Parse ``accudisc toc`` output. Frozen in AccuDisc's cli-machine-interface.md.

    The acquisition line is ``key=value`` tokens and may gain keys, so it is
    parsed as tokens, never by position.
    """
    lsns: list[int] = []
    data_tracks: list[int] = []
    leadout: int | None = None
    tokens: dict[str, str] = {}

    for line in stdout.splitlines():
        track = _TRACK_RE.match(line)
        if track:
            number, lba, _sectors, kind = track.groups()
            lsns.append(int(lba))
            if kind != "audio":
                data_tracks.append(int(number))
            continue
        tail = _LEADOUT_RE.match(line)
        if tail:
            leadout = int(tail.group(1))
            continue
        if "=" in line:
            for item in line.split():
                key, _, value = item.partition("=")
                if value:
                    tokens[key] = value

    if not lsns or leadout is None:
        msg = f"could not parse accudisc toc output:\n{stdout}"
        raise ValueError(msg)

    session_count: int | None = None
    if "session_count" in tokens:
        with contextlib.suppress(ValueError):
            session_count = int(tokens["session_count"])

    return TocGeometry(
        track_lsns=lsns,
        disc_last_lsn=leadout - 1,
        # Pre-degrade AccuDisc builds emit no acquisition line; they only ever
        # answered from the lead-in, so "fulltoc"/"none" is the honest default.
        source=tokens.get("source", "fulltoc"),
        degrade=tokens.get("degrade", "none"),
        sessions=tokens.get("sessions"),
        data_tracks=data_tracks,
        session_count=session_count,
        # `anomalies=` is absent when clean; `toc_trusted=0` appears only when the
        # geometry is untrusted (its absence means trusted).
        anomalies=tokens["anomalies"].split(",") if tokens.get("anomalies") else [],
        toc_trusted=tokens.get("toc_trusted") != "0",
    )


def _lead_in_via_binding(
    device: str, fulltoc_path: Path, cdtext_path: Path | None
) -> bool:
    """Lead-in dump through the binding. False means "fall back to the subprocess".

    Writes each file only once its bytes are in hand. The subprocess contract is
    that a failed dump leaves **no file**, and every caller tests for the file
    rather than catching — so a half-written or empty ``fulltoc`` would read as a
    successful capture of a disc with no TOC, which is worse than no answer.

    ``read_cdtext_raw`` returning ``None`` is the ordinary case, not an error:
    most discs carry no CD-Text. It leaves no file, exactly as the CLI does.
    """
    module = _binding("lead-in")
    if module is None:
        return False

    def _run() -> bool:
        try:
            with module.Device(device) as dev:
                fulltoc = dev.read_full_toc_raw()
                cdtext = dev.read_cdtext_raw() if cdtext_path is not None else None
        except module.AbiMismatch:
            raise
        except (module.AccuDiscError, OSError) as exc:
            log.debug("accudisc lead-in read failed for %s: %s", device, exc)
            return True
        if fulltoc:
            fulltoc_path.write_bytes(fulltoc)
        if cdtext and cdtext_path is not None:
            cdtext_path.write_bytes(cdtext)
        return True

    return _try_binding(module, "lead-in", _run) is not None


def read_lead_in(
    device: str, fulltoc_path: Path, cdtext_path: Path | None = None
) -> None:
    """Dump the raw full TOC (and optionally CD-Text) from the lead-in only.

    The two standalone subcommands answer from the lead-in without spinning the
    program area, which is what makes this cheap enough for the pre-rip banner
    (M3, replacing ``cdrdao read-toc --fast-toc``).

    Best-effort by contract: **CD-Text absence is normal** — most discs have
    none — so a missing or failed ``cdtext`` leaves no file and is not an error.
    A failed ``fulltoc`` likewise leaves no file; the caller checks for it rather
    than catching, because every caller of this is cosmetic.

    Binding path takes **one** ``Device`` for both reads, as the CLI's inline
    capture does and as ``_read_disc_binding`` already does. Two devices would be
    two spin-ups for data that lives in the same lead-in, which is the entire
    reason this is cheap enough to sit in front of a rip.
    """
    if _lead_in_via_binding(device, fulltoc_path, cdtext_path):
        return
    for sub, out in (("fulltoc", fulltoc_path), ("cdtext", cdtext_path)):
        if out is None:
            continue
        try:
            subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
                [_ACCUDISC, "--device", device, sub, str(out)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.debug("accudisc %s failed for %s: %s", sub, device, exc)


def _toc_geometry_from_binding(module: Any, device: str) -> TocGeometry:
    """Build a :class:`TocGeometry` from the binding's ``Toc``/``TocInfo`` structs.

    The same eight fields ``tools/binding_ab.py`` compared live against the CLI
    text on 2026-07-27 (all agreed), assembled here instead of asserted. Two
    fields need a word:

    ``anomalies`` is sorted, because the flag set has no inherent order to
    preserve — the CLI emits them in its own; the A/B compared both sorted, so
    that is the shape known to agree. Nothing downstream is order-sensitive
    (they are joined into a warning string).

    ``sessions`` is the one field the A/B never compared, because the CLI's
    ``"1..1"`` has no direct counterpart. It is derived from the real
    ``Session.number`` values rather than synthesised from a count — writing
    ``f"1..{sessions_total}"`` would assume contiguous 1-based numbering and
    produce a well-formed string that answers a different question. On the
    format-0 degrade ``toc.sessions`` is empty and this is ``None``, which is
    exactly when the CLI omits the token too.
    """
    with module.Device(device) as dev:
        toc, info = dev.read_toc_src()

    audio = toc.audio_tracks
    if not audio:
        msg = f"accudisc toc (binding) found no audio tracks on {device}"
        raise RuntimeError(msg)

    numbers = [s.number for s in toc.sessions]
    return TocGeometry(
        track_lsns=[t.lba for t in audio],
        disc_last_lsn=toc.leadout_lba - 1,
        source=info.source.token,
        degrade=info.degrade.token,
        sessions=f"{min(numbers)}..{max(numbers)}" if numbers else None,
        data_tracks=[t.number for t in toc.data_tracks],
        session_count=info.session_count,
        # Iterate the flag CLASS and mask, never the flag VALUE. `Anomaly` is an
        # enum.IntFlag, and iterating an IntFlag *instance* only became legal in
        # Python 3.11 — on 3.10, this project's floor, it raises "'Anomaly'
        # object is not iterable" and takes every binding-path read_toc with it.
        # This survived review and 1423 tests because the test fake supplied a
        # tuple for `anomalies`, which iterates happily; the bug was unreachable
        # until the binding actually became importable on 3.10 (2026-07-29) and
        # then failed on the first real call.
        anomalies=sorted(
            module.anomaly_token(bit) for bit in module.Anomaly if bit & toc.anomalies
        ),
        toc_trusted=toc.trusted,
    )


def _warn_about_geometry(geom: TocGeometry) -> None:
    """Surface an untrusted or degraded TOC. Shared, so both transports say it."""
    if not geom.toc_trusted:
        log.warning(
            "TOC geometry is untrusted (anomalies: %s) — the track map contradicts "
            "itself; this usually means copy protection, not damage",
            ", ".join(geom.anomalies) or "unspecified",
        )
    elif geom.degraded:
        log.warning(
            "TOC lead-in unavailable (%s) — geometry from the cooked track list; "
            "session count is %s",
            geom.degrade,
            geom.session_count if geom.session_count else "unknown",
        )


def read_toc(device: str) -> TocGeometry:
    """Track geometry via AccuDisc, tolerating an unreadable lead-in.

    Raises RuntimeError when the command itself fails; a *degrade* is a success
    (exit 0) and is reported in the result, not raised — failing it would break
    exactly the discs the fallback exists to serve.

    Served by the binding where available (structs, no regexes), else by
    ``accudisc toc`` and :func:`parse_toc_output`.
    """
    module = _binding("toc")
    if module is not None:
        geom = _try_binding(
            module, "toc", lambda: _toc_geometry_from_binding(module, device)
        )
        if geom is not None:
            _warn_about_geometry(geom)
            return geom

    try:
        proc = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "toc"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        msg = f"accudisc toc failed to run: {exc}"
        raise RuntimeError(msg) from exc
    if proc.returncode not in (0, 3):
        msg = f"accudisc toc exited {proc.returncode}: {proc.stderr.strip()}"
        raise RuntimeError(msg)
    geom = parse_toc_output(proc.stdout)
    _warn_about_geometry(geom)
    return geom


def _run_probe(
    args: list[str], what: str, timeout: float | None = None
) -> tuple[int, str, str] | None:
    """Run a read-only ``accudisc`` probe; return ``(rc, stdout, stderr)`` or None.

    Every probe on this module's surface is best-effort — ``features``, ``speed``,
    ``speeds``, ``--version`` — so a missing binary or a transport error yields
    None and the caller degrades. None means *"could not ask"*; a non-zero ``rc``
    means *"asked and was refused"*, which is a different thing and stays visible.
    """
    try:
        result = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("accudisc %s unavailable: %s", what, exc)
        return None
    return result.returncode, result.stdout, result.stderr


def _run_features(device: str) -> tuple[int, str] | None:
    """Run ``accudisc features``; return (returncode, stdout) or None if unavailable."""
    probe = _run_probe(["--device", device, "features"], "features")
    return None if probe is None else (probe[0], probe[1])


#: The binding's ``Features.combos`` keys spelled the way the CLI prints them —
#: and the way this module has always published them. The two differ by one
#: character on two keys (``c2_sub_raw`` vs ``c2+sub_raw``), which is enough for
#: a caller testing ``combos["c2+sub_raw"]`` to get a ``KeyError`` on one carrier
#: and a working single-pass capture on the other. Absorbing that is what a seam
#: is for; leaving it to callers is how a transport swap becomes a behaviour swap.
_COMBO_KEY_ALIASES = {"c2_sub_raw": "c2+sub_raw", "c2_sub_q": "c2+sub_q"}


def _features_via_binding(device: str) -> tuple[bool, dict[str, bool]] | None:
    """``(c2_supported, combos)`` from ``Device.probe_features``, or None to fall back.

    ``C2Verdict.SUPPORTED`` is the binding's spelling of the CLI's exit 0, and it
    means the same conservative thing: claimed **and** functional. ``UNVERIFIED``
    is not a weaker yes — it is "we could not tell" — so it maps to False, which
    is the answer the subprocess gives for the same drive.
    """
    module = _binding("features")
    if module is None:
        return None

    def _run() -> tuple[bool, dict[str, bool]]:
        try:
            with module.Device(device) as dev:
                feats = dev.probe_features()
        except module.AbiMismatch:
            raise
        except (module.AccuDiscError, OSError) as exc:
            log.debug("accudisc features failed for %s: %s", device, exc)
            return (False, {})
        combos = {_COMBO_KEY_ALIASES.get(k, k): v for k, v in feats.combos.items()}
        return (feats.c2_verdict == module.C2Verdict.SUPPORTED, combos)

    return _try_binding(module, "features", _run)


def drive_supports_c2(device: str) -> bool:
    """True iff the drive both advertises AND functionally supports C2.

    (``C2Verdict.SUPPORTED`` on the binding; exit 0 from ``accudisc features`` on
    the subprocess — the same conservative test either way.) Best-effort: any
    failure → False, and the caller degrades to a read without C2.
    """
    served = _features_via_binding(device)
    if served is not None:
        return served[0]
    probe = _run_features(device)
    return probe is not None and probe[0] == 0


def probe_combos(device: str) -> dict[str, bool]:
    """Per-combination READ CD support from the ``features`` smoke probe.

    Returns e.g. ``{"c2": True, "sub_raw": True, "c2+sub_raw": True, ...}`` — the
    ``c2+sub_raw`` key gates the single-pass audio+C2+subchannel capture. Empty
    dict when the probe is unavailable. The key spelling is this module's
    contract, not the carrier's: see ``_COMBO_KEY_ALIASES``.
    """
    served = _features_via_binding(device)
    if served is not None:
        return served[1]
    probe = _run_features(device)
    if probe is None:
        return {}
    combos: dict[str, bool] = {}
    for line in probe[1].splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "combo":
            combos[parts[1]] = parts[2] == "ok"
    return combos


# ── speed probes (page 2A, and the timed ladder) ──────────────────────────────

# `accudisc speed` page-2A line, e.g.
#   page2A     max 40x (7056 kB/s)  current 8x (1411 kB/s)
# The Nx figure is the drive's own rounding; we take the kB/s, which is what every
# caller compares against.
_AD_MAX_RE = re.compile(r"\bmax\s+\d+x\s+\((\d+)\s*kB/s\)")
_AD_CUR_RE = re.compile(r"\bcurrent\s+\d+x\s+\((\d+)\s*kB/s\)")

# `accudisc speeds` row, e.g. `speed req=32 page2a=32 measured=19.20`.
_SPEED_ROW_RE = re.compile(
    r"^speed\s+req=(\d+)\s+page2a=(\d+)\s+measured=([\d.]+)"
    r"(?:.*?\bverdict=(\S+))?",
    re.MULTILINE,
)


class SpeedRow(NamedTuple):
    """One rung of a ``speeds`` probe, on either transport.

    ``verdict`` is AccuDisc's own judgement of the rung and is the field that
    matters: ``admitted`` / ``duplicate:<n>`` / ``quantized:<n>`` / ``unknown``.
    It is ``None`` only when the engine did not supply one — an older build, or a
    ``points=1`` probe where nothing was judged.

    Kept a NamedTuple rather than a dataclass so existing positional reads still
    work, but every new call site should use the names: the fourth field is
    exactly the axis that ``(req, page2a, measured)`` could not express, and
    unpacking a fixed arity is how a consumer silently keeps ignoring it.
    """

    requested: int
    page2a: int
    measured: float
    verdict: str | None = None


def read_speed(device: str) -> tuple[int | None, int | None]:
    """``(current_kbps, max_kbps)`` from ``accudisc speed``, or ``(None, None)``.

    MODE SENSE page 2A read at the correct offsets (max = page[8:10], current =
    page[14:16] — the fields ``cdrdao drive-info`` reports; the "page 2A lies"
    folklore is naive readers using the wrong ones). Instant: no disc spin-up.

    Never raises — every failure is ``(None, None)``, which callers read as
    "unknown". Note that ``max`` is the *advertised* ceiling; the drive's governor
    enforces a lower one on CD-DA and does not expose it (§9.3).

    **The binding returns the pair the other way round.** ``Device.get_speed()``
    is documented ``(max_kbps, current_kbps)``; this function has always returned
    ``(current, max)``. Both are two ints in the same units, so a straight
    hand-over would type-check, run, and silently swap every caller's reading of
    the drive — `drive_speed` would take the advertised ceiling for the current
    rate and the current rate for the ceiling. The swap is written out below
    rather than hidden in a comprehension for that reason.
    """
    module = _binding("speed")
    if module is not None:

        def _read() -> tuple[int | None, int | None] | None:
            try:
                with module.Device(device) as dev:
                    max_kbps, cur_kbps = dev.get_speed()
            except module.AbiMismatch:
                raise
            except (module.AccuDiscError, OSError) as exc:
                log.debug("accudisc speed failed for %s: %s", device, exc)
                return (None, None)
            return (cur_kbps or None, max_kbps or None)

        pair = _try_binding(module, "speed", _read)
        if pair is not None:
            return pair

    probe = _run_probe(["--device", device, "speed"], "speed")
    if probe is None:
        return None, None
    rc, stdout, stderr = probe
    if rc != 0:
        log.debug("accudisc speed exited %d for %s", rc, device)
        return None, None
    text = stdout + "\n" + stderr
    max_m = _AD_MAX_RE.search(text)
    if max_m is None:
        log.debug("accudisc speed output unparseable for %s", device)
        return None, None
    cur_m = _AD_CUR_RE.search(text)
    return (int(cur_m.group(1)) if cur_m else None), int(max_m.group(1))


def _speed_ladder_binding(module: Any, device: str) -> list[SpeedRow]:
    """``speed_ladder_rows`` over ``Device.probe_speed_ladder``.

    ``points=3`` — the default, and load-bearing rather than incidental. It cuts
    the span into three bands and measures every rung in each, and **only at
    ``points=3`` does any rung get a verdict other than UNKNOWN**; a ladder
    derived from point samples is a confident wrong answer.

    The span is left to the library on purpose. Our migration plan had recorded
    the CLI's span as ``lba = leadout/4, count clamped to leadout/2`` — true of
    the *non-sweep* probe, and wrong here: at ``points=3`` the CLI opens out to
    the whole disc, because three bands of the middle half are three samples of
    much the same neighbourhood and nothing in the output would say so. AccuDisc
    caught that before it cost a run (§ce.3). Passing no span gets their
    computation instead of our copy of it — and a correction we copy is a
    correction we can copy wrong.

    ``measured_x`` is the middle band, matching the CLI's ``measured=`` token, so
    the rows this returns are identical on both transports — including the
    verdict class, once :func:`_verdict_class` has stripped the ``:<rung>`` suffix
    that only the CLI carries.
    """
    with module.Device(device) as dev:
        rungs = dev.probe_speed_ladder(points=3)
    return [
        SpeedRow(r.requested_x, r.reported_x, r.measured_x, _verdict_class(r.verdict))
        for r in rungs
    ]


def _verdict_class(raw: Any) -> str | None:
    """The verdict *class* — ``admitted``/``duplicate``/``quantized``/``unknown``.

    Normalised so the two transports produce the **same string**, not merely
    equivalent information. That took a correction: the CLI prints
    ``verdict=duplicate:40`` while the binding's enum yields ``duplicate`` and
    carries the collapsed-onto rung in a separate ``equiv_x`` field. So the raw
    tokens differ, and the suffix is stripped here rather than at the call sites.

    The divergence was invisible in testing because ``admitted`` — the only
    verdict the ladder policy compares against — has no suffix on either side.
    A future branch on ``duplicate`` would have matched on one carrier and not
    the other, which is the shape of a bug that survives every test that does not
    happen to use the affected value.

    The collapsed-onto rung is dropped rather than parsed: nothing consumes it,
    and a field that is populated on one transport only is worse than absent.
    """
    if raw is None:
        return None
    token = getattr(raw, "token", None) or getattr(raw, "name", None) or raw
    return str(token).split(":", 1)[0].strip().lower() or None


def speed_ladder_rows(device: str) -> list[SpeedRow]:
    """Timed streaming reads per rung: ``(req, page2a, measured, verdict)``.

    *req* is what was asked for, *page2a* what the drive settled on (0 = the page
    did not report), *measured* the achieved throughput in X, *verdict* AccuDisc's
    own judgement of the rung. The probe performs real reads, so it warms the disc
    and **leaves the drive at its last rung** — restoring it is the caller's job
    (``drive_speed.admitted_ladder``).

    Empty list on any failure.

    The verdict is the whole point of the fourth field: ``req == page2a`` cannot
    detect a rung that is real-but-redundant, because both of its operands derive
    from the same advertised ceiling, so the equality cross-checks the drive's
    *quantiser* and never its ceiling. With the Plextor uncap set, page 2A
    advertises the 48x **data** ceiling while CD-DA is governed to 40x, and
    ``req=48 page2a=48 measured=22.96`` sits above ``req=40 page2a=40
    measured=23.68`` — one speed wearing two labels, the top one slower than the
    rung below it. Only a rate comparison sees that, and AccuDisc's verdict
    (``duplicate:40``) is a rate comparison made against three radii.

    Both transports produce the same token, but only after normalisation: the
    CLI prints ``verdict=duplicate:40`` where the binding's enum gives
    ``duplicate`` and puts the collapsed-onto rung in a separate field. The suffix
    is stripped by :func:`_verdict_class`, so what reaches a caller is the verdict
    *class* on both.

    ``min_x``/``max_x`` are deliberately **not** surfaced: they are ``None`` (not
    ``0.0``) when no gradient was measured, and this row type has no way to carry
    that distinction without inviting a caller to flatten it. The verdict already
    encodes what the gradient was measured *for*.
    """
    module = _binding("speed ladder")
    if module is not None:
        rows = _try_binding(
            module, "speed ladder", lambda: _speed_ladder_binding(module, device)
        )
        if rows is not None:
            return rows

    probe = _run_probe(["--device", device, "speeds"], "speeds")
    if probe is None:
        return []
    rc, stdout, stderr = probe
    if rc != 0:
        log.debug("accudisc speeds exited %d for %s", rc, device)
        return []
    return [
        SpeedRow(
            int(m.group(1)),
            int(m.group(2)),
            float(m.group(3)),
            _verdict_class(m.group(4)),
        )
        for m in _SPEED_ROW_RE.finditer(stdout + "\n" + stderr)
    ]


def engine_version() -> str:
    """AccuDisc's version banner, for the RLOG block. Device-free; never raises.

    Recorded verbatim so a rip can be traced to the build that produced it. Falls
    back to a placeholder rather than raising — a missing version must not fail a
    rip that has already succeeded.

    The transport is appended because the fallback is silent after its one
    warning, and a rip log that does not say which carrier read the disc cannot
    settle a later question about a discrepancy between them. The version still
    comes from the binary either way: it is the same library underneath, and this
    call is device-free on the subprocess path where the binding is not.

    Binding path is ``version_string()`` — device-free on both carriers here, so
    unlike every other flip this one has no drive to disagree about. Note it is a
    module function, not a ``Device`` method: asking for a device would make the
    version of a rip that has already finished depend on the drive still being
    openable, which is the opposite of "must not fail a rip that succeeded".
    """
    module = _binding("version")
    if module is not None:
        banner = _try_binding(module, "version", lambda: module.version_string())
        if banner is not None:
            return f"accudisc {banner} [transport: {active_transport()}]"

    probe = _run_probe(["--version"], "--version", timeout=5)
    if probe is None:
        return f"accudisc (version unknown) [transport: {active_transport()}]"
    lines = (probe[1] + probe[2]).splitlines()
    first = next((ln for ln in lines if ln.strip()), None)
    banner = first.strip() if first else "accudisc (version unknown)"
    return f"{banner} [transport: {active_transport()}]"


def _run_read(
    cmd: list[str],
    progress_cb: Callable[[int, int], None] | None,
    what: str,
) -> None:
    """Run an ``accudisc read`` command, raising RuntimeError on a fatal exit.

    Shared by :func:`read_disc_c2` and :func:`read_span`. Exit 0 (clean) and 3
    (completed with caveats — hard/suspect/residual-C2, all zero-filled and
    reported) both mean the image was delivered, so neither raises; 1 (usage) and
    2 (fatal device/transport) do. Exit 0 is not verification — AR/CTDB gating is
    the caller's job.
    """
    try:
        if progress_cb is None:
            # -q silences the human stderr line; capture the rest for logging.
            result = subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
                [*cmd, "-q"], capture_output=True, check=False
            )
            returncode = result.returncode
            stderr_text = result.stderr.decode(errors="replace")
        else:
            returncode, stderr_text = _run_with_progress(cmd, progress_cb)
    except FileNotFoundError:
        msg = "accudisc not found — symlink it into tools/accudisc/ or put it on $PATH"
        raise RuntimeError(msg) from None
    if returncode not in (0, 3):
        msg = f"accudisc {what} failed (exit {returncode}): {stderr_text.strip()}"
        raise RuntimeError(msg)
    log.debug("accudisc %s (exit %d): %s", what, returncode, stderr_text.strip())


def _log_read_caveats(stats: Any, what: str) -> None:
    """Reconstruct the CLI's exit-3 verdict from ``ReadStats`` and log it.

    Exit 3 is **not** a library return. ``cli/main.c`` computes it after the read
    from three counters — ``(hard_errors || sectors_suspect || sectors_flagged)
    ? 3 : 0`` — and ``Device.read`` raises on genuine failure and discards ``rc``
    otherwise. So the caveat signal exists only if this side rebuilds it; without
    this the binding transport would report "clean" on exactly the discs where
    the subprocess said "delivered, but gate it".

    Neither value fails a read on either transport, which is why this only logs.
    "Delivered with caveats" means the image is complete (AccuDisc zero-fills
    hard-unreadable sectors and flags them); whether it is *trustworthy* is
    AccurateRip's and CTDB's question, not the engine's.

    The subchannel counters have no CLI equivalent at all — the exit code cannot
    carry them — so they are reported here as the extra the binding buys. Q-frame
    yield falls off a cliff at high speed (98% at 24x, 47% at 32x on the PX-716A)
    and takes pre-gaps and INDEX points with it while the audio stays clean, so a
    rip can pass every audio gate and still have lost the disc's structure.
    """
    if stats.subq_total:
        log.debug(
            "accudisc %s subchannel: %d/%d Q frames good (%d bad)",
            what,
            stats.subq_ok,
            stats.subq_total,
            stats.subq_bad,
        )
    if not (stats.hard_errors or stats.sectors_suspect or stats.sectors_flagged):
        log.debug("accudisc %s: clean (%d sectors)", what, stats.sectors_read)
        return
    log.debug(
        "accudisc %s: completed with caveats — %d hard, %d suspect, %d flagged "
        "(the subprocess transport would have exited 3 here)",
        what,
        stats.hard_errors,
        stats.sectors_suspect,
        stats.sectors_flagged,
    )


def _split_streams(chunk: Any, files: dict[str, Any]) -> None:
    """De-interleave one chunk into its per-stream files.

    ``chunk.data`` is valid **only** for the duration of the sink call: with
    ``copy=False`` it is a ``memoryview`` over library memory, and a view that
    escapes the call raises ``RetainedBufferError`` rather than quietly reading
    freed bytes. Every slice here is consumed before returning.

    The lengths come from the chunk rather than from constants because the
    request decides them: ``c2_len``/``sub_len`` are zero when those streams were
    not requested, and hard-coding 294/96 would mis-slice a pcm-only read.
    """
    for i in range(chunk.nsec):
        base = i * chunk.sector_len
        if "pcm" in files:
            files["pcm"].write(chunk.data[base : base + chunk.audio_len])
        if "c2" in files and chunk.c2_len:
            off = base + chunk.audio_len
            files["c2"].write(chunk.data[off : off + chunk.c2_len])
        if "sub" in files and chunk.sub_len:
            off = base + chunk.audio_len + chunk.c2_len
            files["sub"].write(chunk.data[off : off + chunk.sub_len])


def _read_disc_binding(
    module: Any,
    device: str,
    output_pcm: Path | None,
    output_c2: Path | None,
    output_sub: Path | None,
    output_cdtext: Path | None,
    output_fulltoc: Path | None,
    read_speed: int | None,
    progress_cb: Callable[[int, int], None] | None,
) -> bool:
    """:func:`read_disc_c2` over the binding: one ``Device``, one spin-up.

    Returns ``True`` — a truthy success sentinel, not information. ``_try_binding``
    signals "declined, fall back" with ``None``, so a function whose natural
    return is also ``None`` cannot be distinguished from one that never ran.

    The call order — full TOC, then CD-Text, then the audio pass — is the CLI's
    and is deliberate rather than incidental. Both lead-in reads happen while the
    disc is already spinning for the audio read that follows, which is the whole
    point of the inline capture; and reordering them re-opens the ground the
    stale-cached-lead-in incident was fought on.

    Two request fields are set to match ``cli/main.c`` rather than to match this
    function's arguments, because an A/B between transports is only meaningful if
    the drive is asked for the same thing both times:

    * **C2 is always requested** (``req.c2 = ACCUDISC_C2_PTRS`` at main.c:1176,
      independent of ``--c2f``). Requesting it only when writing it would change
      the sector length from 2646 to 2352 and silently make the binding arm a
      different measurement, not a different carrier.
    * Everything else is left at its default, matching ``ACCUDISC_READ_REQ_INIT``
      — a designated initialiser, so retries/chunk/overlap are all zero on the
      CLI path too.

    ``read_to_file`` is not used despite doing the same de-interleave: it has no
    progress callback, and a whole-disc rip with no progress is not an option.
    """
    result = None
    with module.Device(device) as dev:
        if output_fulltoc is not None:
            output_fulltoc.write_bytes(dev.read_full_toc_raw())
        if output_cdtext is not None:
            packs = dev.read_cdtext_raw()
            # None is absence, not failure — a disc without CD-Text writes no
            # file, exactly as the subprocess path leaves none behind.
            if packs:
                output_cdtext.write_bytes(packs)

        count = dev.read_toc().leadout_lba
        if count <= 0:
            msg = f"accudisc read: lead-out reported at LBA {count} — nothing to read"
            raise RuntimeError(msg)

        with contextlib.ExitStack() as stack:
            files: dict[str, Any] = {
                key: stack.enter_context(path.open("wb"))
                for key, path in (
                    ("pcm", output_pcm),
                    ("c2", output_c2),
                    ("sub", output_sub),
                )
                if path is not None
            }
            done = 0

            def split(chunk: Any) -> None:
                nonlocal done
                _split_streams(chunk, files)
                done += chunk.nsec
                if progress_cb is not None:
                    progress_cb(done, count)

            result = dev.read(
                0,
                count,
                sink=split,
                copy=False,
                c2=module.C2.PTRS,
                sub=module.Sub.RAW if output_sub is not None else module.Sub.NONE,
                speed_x=read_speed or 0,
            )
    _log_read_caveats(result.stats, "read")
    return True


def read_disc_c2(
    device: str,
    output_pcm: Path | None = None,
    output_c2: Path | None = None,
    output_sub: Path | None = None,
    output_cdtext: Path | None = None,
    output_fulltoc: Path | None = None,
    read_speed: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Full-disc raw audio (s16le, no byte-swap) + C2 bitmap via ``accudisc read``.

    Every output is opt-in — ``accudisc read`` reads the disc and writes only the
    streams asked for. *output_pcm* / *output_c2* write the audio and C2 bitmap;
    *output_sub* captures the raw P-W subchannel (96 B/sector); *output_cdtext* /
    *output_fulltoc* are captured **inline** on the same read (``--cdtext`` /
    ``--fulltoc``, single spin-up). Omitting *output_pcm* still reads the whole
    disc (the sub stream needs it) but skips the ~600 MB PCM write — the
    metadata-only pass the parity gate uses. A disc without CD-Text simply
    produces no cdtext file (absence does not affect the read's exit code) — check
    for existence. *progress_cb(done, total)* receives sector counts from the
    ``--progress-fd`` machine channel.

    Raises RuntimeError only on a genuine read failure (exit 1/2); exit 3
    (completed with caveats) is not a failure — on the binding transport there is
    no exit code to inspect, so :func:`_log_read_caveats` rebuilds that verdict
    from ``ReadStats``."""
    module = _binding("disc read")
    if module is not None:
        served = _try_binding(
            module,
            "disc read",
            lambda: _read_disc_binding(
                module,
                device,
                output_pcm,
                output_c2,
                output_sub,
                output_cdtext,
                output_fulltoc,
                read_speed,
                progress_cb,
            ),
        )
        if served:
            return

    # Whole-disc read (no --count → through the lead-out); each output is opt-in.
    cmd = [_ACCUDISC, "--device", device, "read"]
    if output_pcm is not None:
        cmd += ["--pcm", str(output_pcm)]
    if output_c2 is not None:
        cmd += ["--c2f", str(output_c2)]
    if output_sub is not None:
        cmd += ["--sub", "raw", "--subf", str(output_sub)]
    if output_fulltoc is not None:
        cmd += ["--fulltoc", str(output_fulltoc)]
    if output_cdtext is not None:
        cmd += ["--cdtext", str(output_cdtext)]
    if read_speed:
        cmd += ["--speed", str(read_speed)]
    _run_read(cmd, progress_cb, "read")


def read_span(
    device: str,
    start_lba: int,
    count: int,
    output_pcm: Path,
    read_speed: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Targeted raw read of ``[start_lba, start_lba + count)`` sectors (s16le PCM only,
    no C2/sub capture) — the AR-recovery re-read primitive. Speed is set per invocation
    (``--speed``) and NOT restored by AccuDisc, so a recovery sweep can step the ladder
    without re-spinning between attempts; the caller restores once after.

    Same exit contract as :func:`read_disc_c2` (0/3 = completed; hard-unreadable
    sectors arrive zero-filled, so the output is always exactly ``count`` sectors
    long).

    The binding path reuses ``_read_span_binding`` — the same function
    :func:`read_span_bytes` has been served by since 2026-07-27 — and writes the
    bytes out. Deliberately not ``Device.read_span``: that helper supplies its own
    sink and so has nowhere to hang ``progress_cb``, which drives the TUI through
    every recovery re-read, and a silent multi-minute stream reads as a hang.
    (``read_to_file`` is ruled out for the same reason, and it is the API this
    function most obviously resembles — which is exactly why it is named here.)
    """
    module = _binding("span read")
    if module is not None:
        data = _try_binding(
            module,
            "span read",
            lambda: _read_span_binding(
                module, device, start_lba, count, read_speed, progress_cb
            ),
        )
        if data is not None:
            output_pcm.write_bytes(data)
            return

    cmd = [
        _ACCUDISC,
        "--device",
        device,
        "read",
        "--start",
        str(start_lba),
        "--count",
        str(count),
        "--pcm",
        str(output_pcm),
    ]
    if read_speed:
        cmd += ["--speed", str(read_speed)]
    _run_read(cmd, progress_cb, "span read")


def _read_span_binding(
    module: Any,
    device: str,
    start_lba: int,
    count: int,
    read_speed: int | None,
    progress_cb: Callable[[int, int], None] | None,
) -> bytes:
    """Span read straight into memory — no temp file, no filesystem round-trip.

    Uses ``Device.read()`` with our own sink rather than the binding's
    ``read_span()`` helper, for one reason: ``read_span()`` supplies its own sink
    and so has nowhere to hang progress, and this call carries a ``progress_cb``
    that drives the TUI through every recovery re-read. Streaming silently for
    minutes reads as a hang.

    The ``sector_len`` check is kept from that helper because it is the right
    check: 2352 is our *prediction* of a number the library *reports*, and slice
    assignment into a ``bytearray`` silently resizes it — so a wrong prediction
    would yield a plausible buffer of the wrong length rather than an error.

    ``speed_x`` is set and **not** restored, matching the subprocess contract —
    now stated outright on ``accudisc_read_req.speed_x`` in their public header
    (AccuDisc §bw.4); it used to be inferable only from the silence next to
    ``pregap_scan_opts.speed_x``, which *does* document a restore. Cited by field
    name, not by line: the header is the contract, the line number is not part of
    it, and their §bw.4 edit moved everything below that field already. The
    mechanism is stronger than "nothing restores": ``ladder_restore`` fires only
    when a ladder rung moved the speed, and returns to ``req->speed_x``. The
    caller's prior speed is never sampled, so there is nothing that *could* be
    restored. The recovery ladder depends on exactly that — it steps rungs
    without re-spinning and the caller restores once after the loop. Both
    transports enter the same ``accudisc_read()``, so this is structural rather
    than a coincidence to re-verify per release.
    """
    buf = bytearray(count * _SECTOR_BYTES)
    pos = 0

    def collect(chunk: Any) -> None:
        nonlocal pos
        if chunk.sector_len != _SECTOR_BYTES:
            msg = (
                f"span read returned {chunk.sector_len}-byte sectors, expected "
                f"{_SECTOR_BYTES} — the C2/sub layout assumption is wrong, "
                f"refusing to reassemble"
            )
            raise RuntimeError(msg)
        size = chunk.nsec * chunk.sector_len
        buf[pos : pos + size] = chunk.data
        pos += size
        if progress_cb is not None:
            progress_cb(pos // _SECTOR_BYTES, count)

    with module.Device(device) as dev:
        # copy=False is safe: `collect` consumes the view synchronously into
        # `buf` and never retains it past the call.
        dev.read(
            start_lba,
            count,
            sink=collect,
            copy=False,
            speed_x=read_speed or 0,
        )

    # Length is a guarantee on the subprocess path — AccuDisc zero-fills
    # hard-unreadable sectors, so the output file is always exactly `count`
    # sectors and `read_bytes()` inherits that. Here the length is whatever the
    # sink happened to accumulate, so the guarantee has to be re-established or
    # it is quietly lost in the swap.
    #
    # A short return would not look like an error anywhere downstream: the AR
    # recovery ladder splices this at `track_start*2352 + read_offset*4`, a
    # sample-exact offset, so a short buffer is silent audio corruption rather
    # than a failure. The sector_len guard above covers the wrong *width*; this
    # covers the wrong *count*, which is the same defect one axis over.
    if pos != len(buf):
        msg = (
            f"span read delivered {pos // _SECTOR_BYTES} of {count} sectors from "
            f"lba {start_lba} — refusing a short span, it would splice silently"
        )
        raise RuntimeError(msg)
    return bytes(buf)


def read_span_bytes(
    device: str,
    start_lba: int,
    count: int,
    read_speed: int | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> bytes:
    """:func:`read_span` returning the PCM, for callers that never wanted a file.

    Every caller of ``read_span`` bar one immediately did ``read_bytes()`` and
    unlinked — a full filesystem round-trip for bytes that only ever wanted to be
    in memory, repeated ``recovery_passes * ladder_rungs`` times per failed track.
    The temp file here is an implementation detail of the *subprocess* transport
    and is exactly what AccuDisc's library binding removes (their API_PLAN §7.3:
    the sink is the binding's reason to exist, and a bounded span is the case where
    that pays off). Expressing the call as "give me the bytes" is what let the
    binding swap in underneath without touching a call site — which it now has:
    :func:`_read_span_binding` serves this where the binding is importable, and
    the temp-file body below is the fallback.

    Bounded reads only — one track is ~50 MB at worst. Use :func:`read_span` when
    the destination genuinely is a file, and :func:`read_disc_c2` for a whole disc.

    The scratch file goes through ``container.resolve_temp_dir`` rather than bare
    ``tempfile``: this project's ``/tmp`` is RAM-backed, and that resolver is where
    the "prefer disk-backed ``/var/tmp``, and check free space first" decision
    already lives. Bypassing it would silently put disc-recovery scratch in RAM.
    """
    module = _binding("span read")
    if module is not None:
        data = _try_binding(
            module,
            "span read",
            lambda: _read_span_binding(
                module, device, start_lba, count, read_speed, progress_cb
            ),
        )
        if data is not None:
            return data

    # Local import: this module is the drive seam and everything else in the tree
    # imports *it*, so it stays free of package-level dependencies.
    from cdda2img.container import resolve_temp_dir

    need = count * _SECTOR_BYTES
    with tempfile.TemporaryDirectory(
        prefix="accudisc-span-", dir=resolve_temp_dir(need)
    ) as td:
        out = Path(td) / "span.pcm"
        read_span(device, start_lba, count, out, read_speed, progress_cb)
        return out.read_bytes()


def _run_with_progress(
    cmd: list[str], progress_cb: Callable[[int, int], None]
) -> tuple[int, str]:
    """Run ``accudisc read`` with the ``--progress-fd`` machine channel on stdout.

    We add ``-q --progress-fd 1``: ``-q`` mutes the human ``\\r`` stderr line, and
    ``--progress-fd 1`` emits newline-delimited ``progress <done> <total>`` tokens
    (plus a final ``summary …`` line) on stdout, which we iterate line by line and
    forward to *progress_cb*. stderr goes to a temp file (not a pipe) so a heavily
    damaged disc printing many log lines can never deadlock the single-threaded
    stdout reader; it is read back afterwards for error detail.
    """
    with tempfile.TemporaryFile() as err_fp:
        proc = subprocess.Popen(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [*cmd, "-q", "--progress-fd", "1"],
            stdout=subprocess.PIPE,
            stderr=err_fp,
            text=True,
        )
        assert proc.stdout is not None  # noqa: S101 — guaranteed by stdout=PIPE
        for line in proc.stdout:
            parts = line.split()
            if len(parts) == 3 and parts[0] == "progress":
                try:
                    progress_cb(int(parts[1]), int(parts[2]))
                except ValueError:  # pragma: no cover — malformed token
                    log.debug("accudisc: unparseable progress line %r", line)
            elif parts and parts[0] == "summary":
                log.debug("accudisc read %s", line.strip())
        proc.wait()
        err_fp.seek(0)
        stderr_text = err_fp.read().decode(errors="replace")
    return proc.returncode, stderr_text


# ── write path (the one destructive subcommand) ───────────────────────────────


def _write_disc_binding(
    module: Any,
    device: str,
    toc_path: Path,
    bin_path: Path,
    speed: int,
    simulate: bool,
    progress_cb: Callable[[int, int], None] | None,
    cdtext_path: Path | None,
) -> tuple[int, str, str | None]:
    """:func:`write_disc` over ``Device.write``, reproducing the CLI's triple.

    **The contract is a line, not a value: a return means the disc WAS written,
    an exception means it was not.** ``WriteResult`` has no failure member on
    purpose — AccuDisc removed the possibility rather than documenting it,
    because "report a written disc as blank" is the one mistake this call exists
    to prevent, and a failure member makes it a one-line bug. The mapping:

    ==================  ==========================  ====  ==============
    CLI ``result=``     binding                     rc    written?
    ==================  ==========================  ====  ==============
    ``ok``              ``WriteResult.OK``          0     yes
    ``caveats``         ``WriteResult.CAVEATS``     3     **yes**
    ``not_blank``       raises ``NotBlank``         2     no
    ``error``           raises ``AccuDiscError``    2     no
    ==================  ==========================  ====  ==============

    ``NotBlank`` is AccuDisc 0.4.0's ``ACCUDISC_ERR_NOT_BLANK = -13``, and it is a
    **sibling** of ``Unsupported``, not a subclass. Before 0.4.0 the two were one
    exception and "not blank" was exact *by census, not by construction*: it was
    the only place ``ERR_UNSUPPORTED`` was reachable under the write path, so any
    future unsupported operation would have silently joined it and told the user
    to insert a blank disc they had already inserted. The CLI's ``result=not_blank``
    token was our insurance against that; retiring the subprocess cashed it in,
    which is why the code was asked for and authorised. A genuine ``Unsupported``
    now falls to the ``AccuDiscError`` arm and is reported as ``error``, which is
    the whole point of the split.

    The exit codes are **ours**, synthesised to keep this function's signature
    and every caller's branch unchanged. AccuDisc deliberately do not expose them
    (a process convention belongs to the process, API_PLAN §3), so this is the
    seam absorbing a difference rather than the library leaking one.

    A ``set_log`` sink is installed before the burn because ``CAVEATS`` is
    otherwise a boolean with no cause — the detail (today, a CD-Text SIZE_INFO
    pack whose track range disagrees with the ``.toc``) arrives only through the
    log. Those lines become this function's ``stderr`` return, which is where the
    subprocess path put the same information.

    ``cdtext_path`` is a raw READ TOC format-0x05 blob, byte-for-byte as
    ``read_disc_c2``'s ``output_cdtext`` writes it, laid into the lead-in verbatim.
    Supported on both transports (``Device.write(cdtext_path=…)`` and the CLI's
    ``write --cdtext``).

    **No caller supplies it yet, and that is a container limitation rather than an
    oversight**: the RBI stores CD-Text only as decoded strings inside the TOC
    text, and the raw pack blob is discarded after ``subq_toc`` has read it. Round-
    tripping CD-Text through a burn therefore needs a new RBI block, which is
    spec-before-code work. Wired here so the capability exists, is testable, and
    does not have to be rediscovered — and because its absence is the only reason
    ``WriteResult.CAVEATS`` (a SIZE_INFO/TOC track-range disagreement) is
    unreachable in testing today.

    ``rdwr=True``: burning needs a writable handle, and the failure without it
    surfaces at the burn rather than at open.
    """
    lines: list[str] = []
    try:
        with module.Device(device, rdwr=True) as dev:
            dev.set_log(lines.append)
            result = dev.write(
                str(toc_path),
                str(bin_path),
                simulate=simulate,
                speed=speed,
                cdtext_path=str(cdtext_path) if cdtext_path else None,
                progress=progress_cb,
            )
    except module.AbiMismatch:
        # MUST outrank the AccuDiscError arm below, which it subclasses. An ABI
        # mismatch surfaces on Device() — before any laser fires — and means the
        # extension is broken while the binary is fine, so it is the one error
        # here that should degrade to the subprocess rather than be reported as
        # a failed burn. Swallowing it into `result=error` would turn a working
        # subprocess burn into a refusal, on the one operation a user cannot
        # simply retry.
        raise
    except module.NotBlank as exc:
        # Nothing was written. The subprocess reports this as exit 2 with
        # result=not_blank, and the caller distinguishes it from a transport
        # failure by the token, never by the code. Caught by its own type since
        # 0.4.0 — `Unsupported` no longer implies it and must NOT be caught here.
        return 2, str(exc), "not_blank"
    except module.AccuDiscError as exc:
        # Deliberately returned, not raised: a raise would reach _try_binding,
        # which falls back to the subprocess — and the subprocess would attempt
        # a SECOND BURN of a disc whose state we no longer know.
        return 2, str(exc), "error"
    return (
        (3 if result is module.WriteResult.CAVEATS else 0),
        "\n".join(lines),
        result.token,
    )


def write_disc(
    device: str,
    toc_path: Path,
    bin_path: Path,
    speed: int,
    simulate: bool = False,
    progress_cb: Callable[[int, int], None] | None = None,
    cdtext_path: Path | None = None,
) -> tuple[int, str, str | None]:
    """Burn *toc_path* + *bin_path* via ``accudisc write``. Returns (rc, stderr, result).

    Deliberately returns the exit code rather than raising: exit **3** means
    *completed with caveats* — the disc **was** written — and a caller that treats
    that as a failure has told the user their disc is blank when it is not. Only
    the caller knows how to say that, so the decision stays there.

    *result* is the ``summary … result=<token>`` machine token. Decisions key on
    it, never on stderr wording — AccuDisc reserve the right to reword stderr and
    exit 2 covers both "not blank" and transport failure, so the code alone cannot
    disambiguate. None when no summary line arrived.

    stderr goes to a temp file, never a pipe, so a chatty burn cannot deadlock the
    single-threaded stdout reader.

    On the binding, ``stderr`` carries the ``set_log`` lines instead and the exit
    code is synthesised — see :func:`_write_disc_binding`. **Not hardware-tested
    on either transport as of 2026-07-29**: burning needs blank media, and
    ``--simulate`` needs it too, so no burn has exercised the binding path.
    """
    module = _binding("write")
    if module is not None:
        served = _try_binding(
            module,
            "write",
            lambda: _write_disc_binding(
                module,
                device,
                toc_path,
                bin_path,
                speed,
                simulate,
                progress_cb,
                cdtext_path,
            ),
        )
        if served is not None:
            return served

    cmd = [
        _ACCUDISC,
        "--device",
        device,
        "write",
        "--toc",
        str(toc_path),
        "--bin",
        str(bin_path),
        "--speed",
        str(speed),
    ]
    if simulate:
        cmd.append("--simulate")
    if cdtext_path is not None:
        cmd += ["--cdtext", str(cdtext_path)]

    return _run_write_subprocess(cmd, progress_cb)


def _run_write_subprocess(
    cmd: list[str], progress_cb: Callable[[int, int], None] | None
) -> tuple[int, str, str | None]:
    """Run a built ``accudisc write`` argv, returning ``(rc, stderr, result_token)``.

    stderr goes to a temp file, never a pipe, so a chatty burn cannot deadlock the
    single-threaded stdout reader — the machine channel is what this loop is
    reading, and blocking on the human one would stall the burn's progress.
    """
    result_token: str | None = None
    with tempfile.TemporaryFile() as err_fp:
        proc = subprocess.Popen(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [*cmd, "--progress-fd", "1"],
            stdout=subprocess.PIPE,
            stderr=err_fp,
            text=True,
        )
        assert proc.stdout is not None  # noqa: S101 — guaranteed by stdout=PIPE
        for line in proc.stdout:
            parts = line.split()
            if len(parts) == 3 and parts[0] == "progress" and progress_cb is not None:
                try:
                    progress_cb(int(parts[1]), int(parts[2]))
                except ValueError:  # pragma: no cover — malformed token
                    log.debug("accudisc write: unparseable progress line %r", line)
            elif parts and parts[0] == "summary":
                for tok in parts[1:]:
                    if tok.startswith("result="):
                        result_token = tok.partition("=")[2]
        proc.wait()
        err_fp.seek(0)
        stderr_text = err_fp.read().decode(errors="replace")
    return proc.returncode, stderr_text, result_token


def _best_effort_device_op(device: str, what: str, method: str) -> bool:
    """Run ``Device.<method>()`` on the binding. True if it ran, False to fall back.

    Shared by the two tray/spindle operations, which are the only calls in the
    seam whose contract is "never raises" *including* the device failing to open.
    Everywhere else a failure to open is the caller's problem; here the whole
    operation is a courtesy — a drive that will not eject has not broken a rip
    that already finished.

    Note what is *not* caught: ``AbiMismatch`` is left to ``_try_binding``, which
    is the only condition under which falling back to the subprocess is right.
    A device that refuses to open will refuse for the subprocess too, so
    swallowing that here and returning True stops us running the same failing
    operation twice to report the second failure as if it were the first.
    """
    module = _binding(what)
    if module is None:
        return False

    def _run() -> bool:
        try:
            with module.Device(device) as dev:
                getattr(dev, method)()
        except module.AbiMismatch:
            raise
        except module.AccuDiscError as exc:
            log.debug("accudisc %s failed for %s: %s", what, device, exc)
        except OSError as exc:
            log.debug("accudisc %s could not open %s: %s", what, device, exc)
        return True

    return _try_binding(module, what, _run) is not None


def eject(device: str) -> None:
    """Best-effort tray eject (``Device.eject``). Never raises."""
    if _best_effort_device_op(device, "eject", "eject"):
        return
    try:
        subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "eject"],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("accudisc eject failed for %s: %s", device, exc)


def park_spindle(device: str) -> None:
    """Best-effort spindle stop (SCSI START STOP UNIT) once done reading, so a
    finished pass doesn't leave the drive spinning. Never raises.

    ``Device.park_spindle`` already treats ``ERR_UNSUPPORTED`` as success, which
    matches what this call means: a drive with no stop command has nothing to
    stop, and that is not a failure to report.
    """
    if _best_effort_device_op(device, "stop", "park_spindle"):
        return
    try:
        subprocess.run(  # noqa: S603 — snapshot/PATH binary, fixed argv
            [_ACCUDISC, "--device", device, "stop"],
            capture_output=True,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        log.debug("accudisc stop failed for %s: %s", device, exc)
