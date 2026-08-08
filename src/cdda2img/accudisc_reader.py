"""accudisc_reader.py — raw MMC audio + C2 + subchannel capture via AccuDisc.

The whole of this project's disc access — read, probe, burn — goes through here
and nowhere else.

READ CD returns s16le, so — unlike cdrdao's s16be BIN — the PCM needs no
byte-swap. Hard-unreadable sectors are zero-filled by AccuDisc (C2 bitmap
all-ones), so the PCM/C2/sub streams always stay length-consistent.

**Seam invariant: every AccuDisc call in the tree lives here.** Five modules once
made their own (``drive_speed``, ``rip_log``, ``write_offset``, ``disc_writer``),
which meant "change how we talk to the engine" was five scattered edits, each
with its own chance of being missed. The invariant paid for itself twice: once
when the CLI moved to a Python binding, and again when the CLI was removed
altogether — both were one-module changes.

**One transport: the API.** Every call here goes through the AccuDisc Python
binding (``import accudisc``, a cffi API-mode extension over ``libaccudisc``).
There is no subprocess path and no ``accudisc`` binary is spawned anywhere in
this project.

This module used to carry both, and the older text here argued the subprocess
"is not a hedge that will be retired" because it was the acceptance instrument
for the binding — same disc, both carriers, compare bytes. That argument was
sound while the question was open and it is retained here in summary because the
answer it produced is what closed it: Tracy, req=40, binding 112.69 s vs
subprocess 112.75 s, PCM and C2 byte-identical.

kgr's ruling on 2026-08-01 is what makes the second carrier not merely redundant
but counterproductive: **AccuDisc's CLI is built to the same API and cannot
perform an operation the API does not define, so there is nothing for an A/B to
measure — and if the two ever disagree, that is a bug in AccuDisc rather than a
delta to characterise.** Testing exclusively through the API is the fastest way
to surface such a bug, because a shortfall shows up as something we cannot do
rather than as a discrepancy we have to reconcile against a CLI that would, by
then, be broken.

Consequences worth knowing before they surprise someone:

* A missing or unimportable binding is now **fatal**, not a degrade. It was the
  one condition the fallback existed for, and there is no second carrier to fall
  back to.
* An **ABI mismatch** is likewise fatal. It used to degrade — the extension is
  broken while the binary is fine — and that reasoning still holds; what has
  changed is that "the binary is fine" no longer helps us.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

_T = TypeVar("_T")

log = logging.getLogger(__name__)


#: Red Book audio frame. Plain PCM only — a span read requests no C2 and no
#: subchannel, so this is the whole sector.
_SECTOR_BYTES = 2352


# ── the binding: import, identity, and error translation ──────────────────────

# The table that used to live here tracked which entry points had been flipped to
# the binding and which had not. It is gone because the answer is now "all of
# them" and a table whose every row says the same thing is a place for a wrong row
# to hide. One caveat from it survives and is NOT closed:
#
#   write_disc is not hardware-tested. Device.write() is exercised only against a
#   CDEmu virtual writer, which proves the return path, byte layout and TOC
#   grammar — not laser timing, DAO lead-in or media quality. Retire the caveat
#   with one simulated burn on a blank CD-R.
#
# One policy item likewise survives the carrier work and is deliberately separate
# from it: speed_ladder_rows returns the (req, page2a, measured) triple unchanged,
# and the binding also offers AccuDisc's own admission verdicts, which would fix
# the known gap in drive_speed.admitted_ladder. Adopting them changes what the
# recovery ladder does, so it is a policy change needing its own evidence.

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


#: Sibling of the binary symlink: ``tools/accudisc/pybinding`` points at AccuDisc's
#: ``bindings/python``. Git-ignored, machine-local, same arrangement and the same
#: reasons as the binary symlink that used to sit beside it — see
#: :func:`_binding_search_path`.
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


def _binding(what: str) -> Any:
    """The binding module, or raise. There is no second transport to fall back to.

    This used to return ``None`` to mean "use the subprocess", which made every
    call site a two-branch decision. Raising collapses those branches and, more
    usefully, makes "the binding is missing" arrive at the top of the operation
    that needed it, naming the operation — rather than as a rip that quietly ran
    on a different carrier.
    """
    module, why = _import_binding()
    if module is None:
        msg = (
            f"the AccuDisc Python binding is required for {what} but could not be "
            f"imported: {why}. Install AccuDisc's binding wheel "
            f"(cdda2img's install.sh does this via `pipx inject`)."
        )
        raise RuntimeError(msg)
    return module


def _call(module: Any, what: str, fn: Callable[[], _T]) -> _T:
    """Run *fn*, translating AccuDisc's exceptions into this module's contract.

    Callers document ``RuntimeError``, so both arms raise that rather than making
    every consumer of the seam learn AccuDisc's exception hierarchy.

    ``AbiMismatch`` is kept as a distinct arm even though both now raise, because
    the two mean genuinely different things and the remedy differs: a mismatch
    says the extension and ``libaccudisc`` were built from different headers and
    the fix is a rebuild, while an ``AccuDiscError`` is a real device or media
    failure and a rebuild would be wasted effort. It used to degrade to the
    subprocess — correct while a second carrier existed, since a skewed extension
    leaves the binary perfectly good. What changed is not that reasoning but the
    availability of the thing it fell back to.
    """
    try:
        return fn()
    except module.AbiMismatch as exc:
        msg = (
            f"accudisc {what} failed: the Python binding and libaccudisc were "
            f"built from different headers ({exc}). Rebuild the binding."
        )
        raise RuntimeError(msg) from exc
    except module.AccuDiscError as exc:
        msg = f"accudisc {what} failed: {exc}"
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


def read_lead_in(
    device: str, fulltoc_path: Path, cdtext_path: Path | None = None
) -> None:
    """Dump the raw full TOC (and optionally CD-Text) from the lead-in only.

    Answers from the lead-in without spinning the program area, which is what
    makes this cheap enough for the pre-rip banner.

    Best-effort by contract, and **this one swallows even a device failure** —
    unlike every other read here. Every caller is cosmetic and tests for the file
    rather than catching, so a banner that cannot be drawn must not stop a rip.
    Files are written only once their bytes are in hand: a half-written or empty
    ``fulltoc`` would read as a successful capture of a disc with no TOC, which
    is worse than no answer at all.

    **CD-Text absence is normal** — most discs have none — so
    ``read_cdtext_raw()`` returning ``None`` leaves no file and is not an error.

    Both reads share **one** ``Device``, as ``_read_disc_binding`` does: they come
    from the same lead-in, and two devices would be two spin-ups for data sitting
    in the same place, which is the entire reason this is cheap.
    """
    module = _binding("lead-in")
    try:
        with module.Device(device) as dev:
            fulltoc = dev.read_full_toc_raw()
            cdtext = dev.read_cdtext_raw() if cdtext_path is not None else None
    except (module.AccuDiscError, OSError) as exc:
        log.debug("accudisc lead-in read failed for %s: %s", device, exc)
        return
    if fulltoc:
        fulltoc_path.write_bytes(fulltoc)
    if cdtext and cdtext_path is not None:
        cdtext_path.write_bytes(cdtext)


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
    """Surface an untrusted or degraded TOC."""
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

    Raises RuntimeError when the read itself fails; a *degrade* is a success and
    is reported in the result, not raised — failing it would break exactly the
    discs the degrade path exists to serve.
    """
    module = _binding("toc")
    geom = _call(module, "toc", lambda: _toc_geometry_from_binding(module, device))
    _warn_about_geometry(geom)
    return geom


#: The binding's ``Features.combos`` keys spelled the way this module publishes
#: them. They differ by one character on two keys (``c2_sub_raw`` vs
#: ``c2+sub_raw``). Kept as an explicit map rather than adopting the library's
#: spelling because ``c2+sub_raw`` is this seam's published contract and the key
#: that gates the single-pass capture: renaming it here would move a rename into
#: every caller, which is the opposite of what a seam is for.
_COMBO_KEY_ALIASES = {"c2_sub_raw": "c2+sub_raw", "c2_sub_q": "c2+sub_q"}


def _probe_features(device: str) -> tuple[bool, dict[str, bool]]:
    """``(c2_supported, combos)`` from ``Device.probe_features``.

    Best-effort: a device that will not open or will not answer yields
    ``(False, {})``, and the caller degrades to a read without C2 rather than
    failing. That is the honest reading — we could not establish C2 support, and
    asking a drive for pointers it never demonstrated is the worse error.

    ``C2Verdict.SUPPORTED`` is deliberately the only true: it means claimed **and**
    functional. ``UNVERIFIED`` is "could not tell", not a weaker yes.
    """
    module = _binding("features")

    def _run() -> tuple[bool, dict[str, bool]]:
        try:
            with module.Device(device) as dev:
                feats = dev.probe_features()
        except (module.AccuDiscError, OSError) as exc:
            log.debug("accudisc features failed for %s: %s", device, exc)
            return (False, {})
        combos = {_COMBO_KEY_ALIASES.get(k, k): v for k, v in feats.combos.items()}
        return (feats.c2_verdict == module.C2Verdict.SUPPORTED, combos)

    return _call(module, "features", _run)


def drive_supports_c2(device: str) -> bool:
    """True iff the drive both advertises AND functionally supports C2.

    Best-effort: any failure → False, and the caller degrades to a read with no
    C2 rather than aborting.
    """
    return _probe_features(device)[0]


def probe_combos(device: str) -> dict[str, bool]:
    """Per-combination READ CD support from the feature probe.

    Returns e.g. ``{"c2": True, "sub_raw": True, "c2+sub_raw": True, ...}`` — the
    ``c2+sub_raw`` key gates the single-pass audio+C2+subchannel capture. Empty
    dict when the probe could not answer. The key spelling is this module's
    contract, not the library's: see ``_COMBO_KEY_ALIASES``.
    """
    return _probe_features(device)[1]


# ── speed probes (page 2A, and the timed ladder) ──────────────────────────────


class SpeedRow(NamedTuple):
    """One rung of a ``probe_speed_ladder`` sweep.

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
    """``(current_kbps, max_kbps)``, or ``(None, None)`` when it cannot be read.

    MODE SENSE page 2A read at the correct offsets (max = page[8:10], current =
    page[14:16] — the fields ``cdrdao drive-info`` reports; the "page 2A lies"
    folklore is naive readers using the wrong ones). Instant: no disc spin-up.

    Never raises — every failure is ``(None, None)``, which callers read as
    "unknown". Note that ``max`` is the *advertised* ceiling; the drive's governor
    enforces a lower one on CD-DA and does not expose it (§9.3).

    **The library returns the pair the other way round.** ``Device.get_speed()``
    is documented ``(max_kbps, current_kbps)``; this function returns
    ``(current, max)`` and always has. Both are two ints in the same units, so a
    straight hand-over would type-check, run, and silently swap every caller's
    reading of the drive — `drive_speed` would take the advertised ceiling for
    the current rate and the current rate for the ceiling. The swap is written
    out below rather than hidden in a comprehension for exactly that reason, and
    it was verified against the drive with the two fields deliberately made to
    differ (8x: 1411 vs 7056), since at a drive's default they are equal and the
    order is unobservable.

    ``0`` maps to ``None``: an unsigned zero from the library means "did not
    report", and a caller told 0 kB/s has been handed a measurement nobody made.
    """
    module = _binding("speed")

    def _read() -> tuple[int | None, int | None]:
        try:
            with module.Device(device) as dev:
                max_kbps, cur_kbps = dev.get_speed()
        except (module.AccuDiscError, OSError) as exc:
            log.debug("accudisc speed failed for %s: %s", device, exc)
            return (None, None)
        return (cur_kbps or None, max_kbps or None)

    return _call(module, "speed", _read)


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

    The enum gives ``duplicate`` with the collapsed-onto rung in a separate
    field; :func:`_verdict_class` normalises to the verdict *class*, which is
    what a caller acts on.

    ``min_x``/``max_x`` are deliberately **not** surfaced: they are ``None`` (not
    ``0.0``) when no gradient was measured, and this row type has no way to carry
    that distinction without inviting a caller to flatten it. The verdict already
    encodes what the gradient was measured *for*.
    """
    module = _binding("speed ladder")
    return _call(module, "speed ladder", lambda: _speed_ladder_binding(module, device))


def engine_version() -> str:
    """AccuDisc's version banner, for the RLOG block. Device-free; never raises.

    Recorded verbatim so a rip can be traced to the build that produced it. A
    missing version degrades to a placeholder rather than raising — it must not
    fail a rip that has already succeeded, which is also why it uses the module
    function and not a ``Device`` method: routing it through the drive would let
    a tray opened one second early turn good provenance into "version unknown".

    The ``[transport: …]`` suffix this used to carry is gone. It existed because
    a silent fallback between two carriers would otherwise leave a rip log unable
    to say which one read the disc; with one carrier it was a constant, and a
    constant in a provenance field is noise that reads like information.
    """
    try:
        module = _binding("version")
    except RuntimeError:
        return "accudisc (version unknown)"
    try:
        return f"accudisc {module.version_string()}"
    except Exception:
        log.debug("accudisc version_string() failed", exc_info=True)
        return "accudisc (version unknown)"


def _log_read_caveats(stats: Any, what: str) -> None:
    """Reconstruct the retired CLI's exit-3 verdict from ``ReadStats`` and log it.

    Exit 3 was **not** a library return. ``cli/main.c`` computed it after the read
    from three counters — ``(hard_errors || sectors_suspect || sectors_flagged)
    ? 3 : 0`` — and ``Device.read`` raises on genuine failure and discards ``rc``
    otherwise. So the caveat signal exists only because this side rebuilds it.
    That was written to stop the binding reporting "clean" on exactly the discs
    where the CLI said "delivered, but gate it"; with the CLI gone it is no longer
    a parity measure but the **only** source of the signal, which makes it more
    load-bearing than when it was added, not less.

    Neither value fails a read, which is why this only logs.
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


def _census_c2(chunk: Any, damage: bytearray, first: int) -> None:
    """Mark, per sector, whether C2 fired anywhere in it.

    Written **from the sink**, not from ``ReadResult.status_map``, and that is a
    deliberate correction to an earlier plan. The C API takes a caller-supplied
    ``uint8_t *status_map`` (``accudisc.h:1444``), but the Python binding
    allocates the buffer itself and surfaces it only through the returned
    ``ReadResult`` — which does not exist until the read *finishes*. So the map
    the binding documents as "read it live from another thread" is, through the
    binding, unreachable while the read is running. The C2 lane is the one thing
    we can honestly compute ourselves, so it is computed here.

    Nothing is lost by doing so. ``status_map``'s extra states are ``RECOVERED``
    and ``SUSPECT``, which only the reread machinery produces, and this path
    leaves ``retries``/``c2_retries``/``verify_passes``/``overlap_sectors`` at
    their zero defaults — a single streaming pass. Those states are structurally
    unreachable here. Raw C2 bits are also *finer* than ``MapState.C2``: the map
    is one enum per sector, these are 2352 bits.

    ``bytes(...).count(0)`` is one C-level pass; ``any(memoryview)`` is a
    per-byte interpreter loop, ~173,000 steps per chunk. The copy is 294 B per
    sector against the 2352 B already being written for the same sector.
    """
    if not chunk.c2_len:
        return
    for i in range(chunk.nsec):
        off = i * chunk.sector_len + chunk.audio_len
        c2 = bytes(chunk.data[off : off + chunk.c2_len])
        if c2.count(0) != chunk.c2_len:
            damage[first + i] = 1


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
    map_cb: Callable[[bytearray], None] | None = None,
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
            damage: bytearray | None = None
            if map_cb is not None:
                damage = bytearray(count)
                map_cb(damage)

            def split(chunk: Any) -> None:
                nonlocal done
                _split_streams(chunk, files)
                if damage is not None:
                    _census_c2(chunk, damage, done)
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
    map_cb: Callable[[bytearray], None] | None = None,
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

    *map_cb(damage)* is called **once**, with the per-sector C2 damage map, before
    the first sector is read — the buffer is handed out at allocation rather than
    returned at the end, because a map that only exists once the read is over
    cannot drive a live display. (That is precisely the shape the AccuDisc binding
    is missing for ``status_map``; see :func:`_census_c2`.) The caller keeps the
    reference and polls it from its render thread: one writer, one byte per
    sector, monotonic, so a stale frame is merely stale and never wrong, and no
    lock is needed. Requesting it does not change what is read — C2 pointers are
    on the wire unconditionally — so the map is available even with
    ``c2_recovery = "off"``, which suppresses only the bitmap *file*.

    Raises RuntimeError only on a genuine read failure; "completed with caveats"
    is not a failure, and since the library returns no verdict for it,
    :func:`_log_read_caveats` rebuilds one from ``ReadStats``."""
    module = _binding("disc read")
    _call(
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
            map_cb,
        ),
    )


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
    and NOT restored, so a recovery sweep can step the ladder without re-spinning
    between attempts; the caller restores once after.

    Hard-unreadable sectors arrive zero-filled, so the output is always exactly
    ``count`` sectors long.

    Routed through ``_read_span_binding`` — the same function
    :func:`read_span_bytes` uses — and the bytes are written out here.
    Deliberately not ``Device.read_span`` or ``read_to_file``: both supply their
    own sink and so have nowhere to hang ``progress_cb``, which drives the TUI
    through every recovery re-read, and a silent multi-minute stream reads as a
    hang. ``read_to_file`` is the API this function most obviously resembles,
    which is exactly why it is named here rather than left to be rediscovered.
    """
    module = _binding("span read")
    data = _call(
        module,
        "span read",
        lambda: _read_span_binding(
            module, device, start_lba, count, read_speed, progress_cb
        ),
    )
    output_pcm.write_bytes(data)


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
    That temp file was an implementation detail of the subprocess, which could not
    express "give me the bytes"; naming the call for what it wanted rather than
    for how it was then implemented is what let the library swap in underneath
    without touching a single call site.

    Bounded reads only — one track is ~50 MB at worst. Use :func:`read_span` when
    the destination genuinely is a file, and :func:`read_disc_c2` for a whole disc.
    """
    module = _binding("span read")
    return _call(
        module,
        "span read",
        lambda: _read_span_binding(
            module, device, start_lba, count, read_speed, progress_cb
        ),
    )


# ── write path (the one destructive operation) ────────────────────────────────


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
    log. Those lines become this function's ``stderr`` return.

    ``cdtext_path`` is a raw READ TOC format-0x05 blob, byte-for-byte as
    ``read_disc_c2``'s ``output_cdtext`` writes it, laid into the lead-in verbatim.

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
        # MUST outrank the AccuDiscError arm below, which it subclasses. A
        # mismatch surfaces on Device() — before any laser fires — so it is not a
        # failed burn and must not be reported as one. Re-raised for `_call` to
        # turn into a RuntimeError naming the rebuild, rather than swallowed into
        # `result=error`, which would report a disc as spoiled that was never
        # touched. (It used to degrade to the subprocess here; with one carrier
        # the distinction it protects is "nothing was written", not "try again".)
        raise
    except module.NotBlank as exc:
        # Nothing was written. Callers distinguish this from a transport failure
        # by the token, never by the code. Caught by its own type since 0.4.0 —
        # `Unsupported` no longer implies it and must NOT be caught here.
        return 2, str(exc), "not_blank"
    except module.AccuDiscError as exc:
        # Deliberately returned, not raised. The code shape is inherited from
        # when a raise would have triggered a second burn of a disc whose state
        # was unknown; it survives because callers of write_disc branch on the
        # (code, token) pair and a raise here would bypass every one of them.
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
    """Burn *toc_path* + *bin_path*. Returns ``(rc, stderr, result)``.

    Deliberately returns a code rather than raising: **3** means *completed with
    caveats* — the disc **was** written — and a caller that treats that as a
    failure has told the user their disc is blank when it is not. Only the caller
    knows how to say that, so the decision stays there.

    The codes and the *result* token are this seam's invention, synthesised in
    :func:`_write_disc_binding` from ``WriteResult`` and the exception type.
    AccuDisc deliberately do not expose process conventions through the library,
    so keeping the shape here is the seam absorbing a difference rather than the
    library leaking one — and it keeps every caller's branch untouched. Decisions
    key on the token, never on stderr wording.

    **Not hardware-tested**: burning needs blank media and ``--simulate`` needs it
    too, so no real burn has exercised this. Only a CDEmu virtual writer has.
    """
    module = _binding("write")
    return _call(
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


# ── CTDB parity repair (the one device-free operation in the seam) ────────────


@dataclass(frozen=True)
class CtdbRepairReport:
    """What AccuDisc's Reed-Solomon decode did to a rip, and which claim it supports.

    **Two audio buffers, not one, and the split is the whole point.** ``audio`` is
    populated only when every repaired column *re-verified*; ``audio_unverified``
    only when some column was ``determined`` instead. Both are ``None`` on a
    refusal. Collapsing them to one field — ``audio or audio_unverified`` — would
    type-check, run, and quietly destroy the distinction AccuDisc built the second
    return code to carry, so this mirrors their shape rather than simplifying it.

    A column carrying exactly ``npar`` erasures consumes every check equation the
    parity has: its errata are then exactly determined, and re-deriving the
    syndromes from them is an *identity* that cannot disagree. The re-verification
    that makes every other column trustworthy is therefore vacuous at exactly full
    erasure capacity, and such a column is right iff its erasure list was complete
    — which a C2-derived list is not guaranteed to be, since C2 under-flags as
    well as over-flags. This is a property of correct errata decoding rather than
    a defect in anyone's implementation, which is also why no A/B against a second
    decoder can surface it (AccuDisc §m.1/§n.3, 2026-08-02).

    ``erasure_columns`` is documented by AccuDisc as dirty columns carrying at
    least one erasure. ``ctanalyse``'s field of the same name counted columns
    where erasures were used *and changed the outcome*. Whether those are the
    same number is **open**: they agree exactly on both fixture arms we can run,
    including the misaligned control, and no arm we have separates them. Nothing
    here consumes it, so the ambiguity is recorded rather than resolved.

    Nothing here is an absolute gate. CTDB publishes per-track CRCs, so there is
    no whole-image value for the library to check against and ``crc32_after`` is
    one AccuDisc computed itself. The caller gates.
    """

    audio: bytes | bytearray | None
    audio_unverified: bytes | bytearray | None
    offset_pairs: int
    dirty_columns: int
    repaired_columns: int
    refused_columns: int
    erasure_columns: int
    unverified_columns: int
    corrections: int
    crc32_before: int
    crc32_after: int

    @property
    def refused(self) -> bool:
        """Beyond the parity's capacity. Nothing was written — a normal outcome."""
        return self.audio is None and self.audio_unverified is None


def ctdb_repair(
    *,
    pcm: bytes | bytearray,
    parity: bytes,
    npar: int,
    wire_stride: int,
    image_first_frame: int,
    image_frames: int,
    offset_pairs: int,
    erasures: bytes | None = None,
) -> CtdbRepairReport:
    """Reed-Solomon repair of *pcm* against a CTDB parity blob.

    The only operation in this seam that touches no device: arithmetic on buffers
    the caller already holds. It replaced a subprocess to the ``ctanalyse`` binary
    on 2026-08-02, after an eight-arm element-wise A/B against that binary agreed
    correction-for-correction on 1.6 GB of fixtures (AccuDisc §k).

    **The offset is an input.** AccuDisc does not search; it reconciles at the
    alignment given or declines. Our own sweep in ``ctdb_repair.select_entry``
    has always been the source of that number — we never consumed the binary's
    reported offset — so this is the same arrangement with the dead field gone.
    One behavioural change comes with it: ``ctanalyse``'s ``offset_found`` meant
    *some* offset within ±``stride/2 - 1`` reconciled, which is not the same
    number as the one erasures were then bucketed at. Those two have always
    agreed in practice; here only the offset actually passed is tested, so a
    wrong sweep result now refuses where the binary would have said "found".

    *erasures* is a C2 bitmap, one bit per 16-bit word, **absolute over pcm** —
    the ``[0, lead-out)`` domain, never CTDB's image window. AccuDisc performs the
    domain shift itself; pre-shifting applies it twice. ``None`` is error-only
    decoding, a normal mode rather than a degraded one.

    *pcm* is never mutated: the repaired audio comes back as a fresh buffer. (The
    binding's ``out=`` can alias the input to halve peak memory, deliberately not
    used here — a failed attempt must leave the buffer clean for the next one.)

    Raises ``RuntimeError`` on bad geometry, a mismatched buffer size, or an ABI
    skew. A **refusal is not an error** — it comes back as a report with both
    audio buffers ``None``.
    """
    module = _binding("CTDB parity repair")

    def _run() -> CtdbRepairReport:
        r = module.ctdb_repair(
            pcm=pcm,
            parity=parity,
            npar=npar,
            wire_stride=wire_stride,
            image_first_frame=image_first_frame,
            image_frames=image_frames,
            offset_pairs=offset_pairs,
            erasures=erasures,
        )
        return CtdbRepairReport(
            audio=r.audio,
            audio_unverified=r.audio_unverified,
            offset_pairs=r.offset_pairs,
            dirty_columns=r.dirty_columns,
            repaired_columns=r.repaired_columns,
            refused_columns=r.refused_columns,
            erasure_columns=r.erasure_columns,
            unverified_columns=r.unverified_columns,
            corrections=r.corrections,
            crc32_before=r.crc32_before,
            crc32_after=r.crc32_after,
        )

    return _call(module, "CTDB parity repair", _run)


def _best_effort_device_op(device: str, what: str, method: str) -> None:
    """Run ``Device.<method>()``, swallowing a device that will not cooperate.

    Shared by the two tray/spindle operations, which are the only calls in the
    seam whose contract is "never raises" *including* the device failing to open.
    Everywhere else a failure to open is the caller's problem; here the whole
    operation is a courtesy — a drive that will not eject has not broken a rip
    that already finished.

    ``AbiMismatch`` is deliberately **not** swallowed here — it reaches ``_call``
    and raises. A skewed extension is a build fault the user must be told about,
    and letting the one courtesy call in the seam hide it would mean discovering
    it on the next operation that matters instead.
    """
    module = _binding(what)

    def _run() -> None:
        try:
            with module.Device(device) as dev:
                getattr(dev, method)()
        except module.AbiMismatch:
            raise
        except module.AccuDiscError as exc:
            log.debug("accudisc %s failed for %s: %s", what, device, exc)
        except OSError as exc:
            log.debug("accudisc %s could not open %s: %s", what, device, exc)

    _call(module, what, _run)


def eject(device: str) -> None:
    """Best-effort tray eject (``Device.eject``)."""
    _best_effort_device_op(device, "eject", "eject")


def park_spindle(device: str) -> None:
    """Best-effort spindle stop (SCSI START STOP UNIT) once done reading, so a
    finished pass doesn't leave the drive spinning.

    ``Device.park_spindle`` already treats ``ERR_UNSUPPORTED`` as success, which
    matches what this call means: a drive with no stop command has nothing to
    stop, and that is not a failure to report.
    """
    _best_effort_device_op(device, "stop", "park_spindle")
