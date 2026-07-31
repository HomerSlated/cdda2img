"""Dependency pre-flight — deliberately standard-library only.

Two callers share one table:

* :func:`preflight_or_exit` runs on **every** invocation, from ``cli.py``,
  before the application package is imported. It names every missing runtime
  dependency at once and exits non-zero.
* :func:`run_doctor` backs ``cdda2img doctor``: the same Python check plus the
  external binaries, the native libraries, and the AccuDisc engine, reported as
  a table. It never installs anything, and it never touches the network.

**Why this module imports nothing from ``cdda2img`` and nothing from PyPI.**
``import cdda2img.cdda2img`` eagerly pulls in ``av``, ``mutagen``, ``numpy``,
``ortools`` and ``unidecode`` (measured 2026-07-30). A checker reached *through*
that import dies with ``ImportError`` before it can report anything — able to
diagnose every dependency except the ones actually missing. So the check has to
be reachable without them, and this module's import list is therefore the
standard library and nothing else.

That constraint fails silently if broken: adding ``from cdda2img.container
import ...`` here would leave the whole test suite green, because the test
environment has every dependency installed. The guard is
``tests/test_depcheck.py::test_depcheck_imports_only_the_standard_library``,
which reads this file's own AST rather than trusting the import to fail.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import shutil
import subprocess
import sys
from ctypes.util import find_library
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

OK = "ok"
MISSING = "missing"
WARN = "warn"

# Mirrors `recovery_profile.BUILTIN_PROFILE`. Duplicated rather than imported for
# the stdlib-only reason documented on `_shipped_profiles_dir`; pinned by a test.
_BUILTIN_PROFILE = "track-ladder"


@dataclass(frozen=True)
class PyDep:
    """One entry of ``[project.dependencies]``.

    ``dist`` and ``module`` are both needed and neither derives from the other:
    ``pyacoustid`` imports as ``acoustid``, ``discogs-client`` as
    ``discogs_client``. ``importlib.metadata.packages_distributions()`` can
    recover the mapping, but only for packages that are *already installed* —
    which is exactly the case this module is not interested in. Hence a written
    table, kept honest by ``tests/test_depcheck.py``, which diffs ``dist``
    against ``pyproject.toml``.
    """

    dist: str
    module: str
    why: str


@dataclass(frozen=True)
class Binary:
    """An external executable looked up on ``$PATH``."""

    name: str
    why: str
    upstream: str
    """The project that provides it — a distro-neutral remedy. Naming a Void or
    Debian package here would be wrong on every other system."""


@dataclass(frozen=True)
class Result:
    """One line of the doctor's report."""

    name: str
    status: str
    detail: str
    remedy: str = ""
    required: bool = False


# --------------------------------------------------------------------------
# The tables
# --------------------------------------------------------------------------

RUNTIME_PYTHON: tuple[PyDep, ...] = (
    PyDep("av", "av", "transcode, track extraction, cover art (PyAV)"),
    PyDep("blake3", "blake3", "RBI block digests"),
    PyDep("discogs-client", "discogs_client", "Discogs label/catalogue lookup"),
    PyDep("mutagen", "mutagen", "reading tags from source audio files"),
    PyDep("musicbrainzngs", "musicbrainzngs", "MusicBrainz disc-ID lookup"),
    PyDep("numpy", "numpy", "PCM and checksum arithmetic"),
    PyDep("ortools", "ortools", "the `best` batching strategy (CP-SAT)"),
    PyDep("pyacoustid", "acoustid", "AcoustID fingerprint lookup"),
    PyDep("pyebur128", "pyebur128", "EBU R128 loudness analysis"),
    PyDep("questionary", "questionary", "the interactive metadata menu"),
    PyDep("rapidfuzz", "rapidfuzz", "fuzzy title matching"),
    PyDep("unidecode", "unidecode", "transliteration for filenames and TOC text"),
)

_TOMLI = PyDep("tomli", "tomli", "TOML config parsing before 3.11's tomllib")

EXTERNAL_BINARIES: tuple[Binary, ...] = (
    Binary("ctanalyse", "CTDB parity repair of damaged rips", "CUETools / ctdb"),
    Binary("ffplay", "audition playback and the rip's track-1 preview", "FFmpeg"),
    Binary("cdemu", "the `mount` subcommand", "cdemu-daemon"),
    Binary("fpcalc", "AcoustID fingerprinting", "Chromaprint"),
)

ART_VIEWERS: tuple[str, ...] = ("chafa", "timg", "kitten")
"""Album-art terminal preview: first one found wins, none means no preview."""

NATIVE_LIBS: tuple[tuple[str, str], ...] = (
    ("chromaprint", "AcoustID fingerprinting through pyacoustid"),
)


def required_python_deps() -> tuple[PyDep, ...]:
    """Every Python dependency this interpreter actually needs.

    ``tomli`` is conditional on the interpreter, not on configuration: 3.11
    absorbed it as ``tomllib``. Reporting it missing on 3.14 would be a false
    finding on every modern system.
    """
    if sys.version_info < (3, 11):
        return (*RUNTIME_PYTHON, _TOMLI)
    return RUNTIME_PYTHON


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


def _module_present(module: str) -> bool:
    """True when ``module`` resolves to a real, non-namespace module.

    ``find_spec`` rather than ``import_module``: locating answers "is it
    installed" without executing the package, which keeps the per-invocation
    pre-flight cheap.

    ``spec.origin is None`` is the PEP 420 namespace-package case and counts as
    absent. That is not hypothetical here — ``tools/accudisc/``, a directory
    holding a symlink to a binary, imports as an empty namespace package named
    ``accudisc`` and raises no ``ImportError``. None of our dependencies are
    namespace packages, so demanding a real origin costs nothing and closes the
    phantom.
    """
    try:
        spec = importlib.util.find_spec(module)
    except (ImportError, ValueError):
        return False
    return spec is not None and spec.origin is not None


def _version(dist: str) -> str:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        # Importable but with no distribution metadata: a source directory on
        # `sys.path`, or a vendored copy. It works; we just cannot name a version.
        return "unknown version"


def missing_python_deps() -> list[PyDep]:
    """Every required Python dependency that is not installed."""
    return [d for d in required_python_deps() if not _module_present(d.module)]


def _install_hint(dists: list[str]) -> str:
    """The command that would install ``dists`` — printed, never run.

    A pipx install is detected from the venv layout rather than assumed, because
    ``pip install`` into a pipx venv is the wrong advice and would appear to
    work right up until the next ``pipx upgrade``.
    """
    names = " ".join(dists)
    if f"{Path('pipx') / 'venvs'}" in sys.prefix:
        return f"pipx inject cdda2img {names}"
    return f"{Path(sys.executable).name} -m pip install {names}"


# --------------------------------------------------------------------------
# The runtime pre-flight
# --------------------------------------------------------------------------


def preflight_or_exit(stream: TextIO | None = None) -> None:
    """Report every missing runtime dependency, then exit 1.

    Every missing one, not the first: Python's own ``ImportError`` names one
    module per run, so a user three dependencies short learns that three times.

    Returns silently when nothing is missing, which is the overwhelmingly
    common path — so it must stay cheap. It is a ``find_spec`` per dependency
    and no imports.
    """
    missing = missing_python_deps()
    if not missing:
        return

    out = stream if stream is not None else sys.stderr
    print("cdda2img cannot start: missing Python dependencies\n", file=out)
    width = max(len(d.dist) for d in missing)
    for dep in missing:
        print(f"  {dep.dist:<{width}}  {dep.why}", file=out)
    print(
        f"\nInstall them with:\n  {_install_hint([d.dist for d in missing])}", file=out
    )
    print(
        "\nFor the full picture, including external tools:  cdda2img doctor", file=out
    )
    raise SystemExit(1)


# --------------------------------------------------------------------------
# The doctor
# --------------------------------------------------------------------------


def _check_python() -> list[Result]:
    """One line per dependency, with **no** per-line remedy.

    A bare install has twelve of these missing, and twelve near-identical
    ``pip install`` lines bury the twelve names they are attached to. The
    remedy for this group is one aggregated command, emitted as a group footer
    by :func:`run_doctor` — which is also the command the user should actually
    run, since installing them one at a time re-resolves the whole set twelve
    times.
    """
    results = []
    for dep in required_python_deps():
        if _module_present(dep.module):
            results.append(Result(dep.dist, OK, _version(dep.dist), required=True))
        else:
            results.append(Result(dep.dist, MISSING, dep.why, required=True))
    return results


def _dev_shim_path(root: Path | None = None) -> Path | None:
    """``tools/accudisc/pybinding`` relative to this checkout, if it exists.

    ``root`` overrides the checkout location, so the "holds a package" predicate
    can be tested against a built tree rather than by patching this function out
    — patching it out is what every other test here does, which means none of
    them exercise the check itself.

    Only meaningful when running from a source tree; an installed cdda2img has
    no ``tools/`` above it and this returns ``None``.

    The package is looked for, not just the directory, because this result is
    also read as evidence *against* a total engine absence — the ``disc engine``
    one-of below does not fire while a shim is present. A bare or wrongly-aimed
    directory answering "shim present" would therefore suppress the one required
    failure this report exists to raise. Checking for ``accudisc/__init__.py`` is
    what this docstring always claimed and what the code did not do.
    """
    base = Path(__file__).resolve().parents[2] if root is None else root
    shim = base / "tools" / "accudisc" / "pybinding"
    return shim if (shim / "accudisc" / "__init__.py").is_file() else None


def _resolve_engine_binary() -> str:
    """The ``accudisc`` the application will actually run.

    A deliberate duplicate of ``accudisc_reader._resolve_accudisc``, which this
    module cannot call: that one is on the far side of the heavy imports this
    whole file exists to stay clear of. The copy is pinned to the original by
    ``tests/test_depcheck.py::test_engine_resolution_matches_the_reader``,
    because a report that silently stopped agreeing with the reader would be
    the very defect this function was added to fix, wearing a new cause.

    Reporting ``shutil.which`` instead was wrong, and measurably so on the
    development box: ``which`` answered ``/usr/local/bin/accudisc`` while the
    reader ran ``tools/accudisc/accudisc`` — different sha256, different
    ``RUNPATH``, resolving different ``libaccudisc.so.0`` files. Both work, so
    nothing failed; ``doctor`` simply named an artefact that never runs.
    """
    local = Path(__file__).parent.parent.parent / "tools" / "accudisc" / "accudisc"
    return str(local) if local.is_file() else "accudisc"


def _linked_library(origin: Path) -> str:
    """Which ``libaccudisc`` ``origin`` actually resolves to.

    Called for both engine artefacts — the compiled binding extension and the
    ``accudisc`` executable. They can resolve *different* libraries on one
    machine, which is the whole reason this is reported per-artefact rather than
    once.

    ``ldd`` is asked rather than the extension's ``RUNPATH`` read, because
    ``RUNPATH`` is one input to the search and not the answer — an
    ``LD_LIBRARY_PATH`` or an installed copy outranks it. This is the figure
    that told us a supposedly clean pipx artefact was still bound to AccuDisc's
    build tree.
    """
    ldd = shutil.which("ldd")
    if ldd is None:
        return ""
    try:
        proc = subprocess.run(  # noqa: S603
            [ldd, str(origin)], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in proc.stdout.splitlines():
        if "libaccudisc" in line and "=>" in line:
            return line.split("=>", 1)[1].strip().split(" (")[0]
    return ""


def _binding_library(package_dir: Path) -> str:
    """Which ``libaccudisc`` the binding's compiled extension resolves to.

    The extension has to be found first. Asking ``ldd`` about the package's
    ``__init__.py`` — which is what ``spec.origin`` names — yields "not a dynamic
    executable" and an empty answer, so the library line was silently never
    emitted for an *installed* binding. That is the one configuration where it
    matters most: a pipx-injected wheel is exactly the artefact whose linkage
    §123.2 turned out to be wrong while every check said fine.

    The name is globbed rather than assumed: it carries an ABI tag
    (``_accudisc.abi3.so`` here, but ``_accudisc.cpython-314-x86_64-linux-gnu.so``
    for a non-abi3 build).

    The *location* is assumed, and the assumption was checked rather than
    inferred: a cffi API-mode extension can install as a top-level module beside
    the package instead of inside it, which would make this return "" again.
    ``unzip -l`` on AccuDisc's 0.4.0 wheel (2026-07-30) shows
    ``accudisc/_accudisc.abi3.so`` — package-internal, so one glob suffices. If a
    later wheel moves it, this silently stops reporting; that is the failure to
    look for, and searching ``package_dir.parent`` is the fix.
    """
    for so in sorted(package_dir.glob("_accudisc*.so")):
        lib = _linked_library(so)
        if lib:
            return lib
    return ""


def _library_skew(binding_lib: str, cli_lib: str) -> str:
    """A note when the binding and the CLI resolve different ``libaccudisc``es.

    Deliberately not a ``WARN``. On a development box the two *should* differ the
    moment a split install exists — an injected wheel is pinned to the install
    prefix at build time, the symlinked CLI tracks a build tree that moves on
    every compile — so warning would cry wolf at the normal state. What is not
    acceptable is the report implying there is one library when there are two.

    Silent when either side is unknown: an empty string means ``ldd`` could not
    answer, and "unknown" must not be rendered as "differs".
    """
    if not binding_lib or not cli_lib or binding_lib == cli_lib:
        return ""
    return (
        f"\n      NOTE: the CLI resolves a different libaccudisc ({cli_lib})."
        "\n      Expected after a split install; `pipx inject --force` realigns them."
    )


def _check_accudisc() -> list[Result]:
    """The AccuDisc engine: the binding, its cffi runtime, and the binary.

    Reported as a group because the requirement is a *one-of* — a read needs
    either the Python binding or the ``accudisc`` executable, and a report that
    marked each individually required would fail a perfectly working install.
    """
    results: list[Result] = []

    have_cffi = _module_present("cffi") and _module_present("_cffi_backend")
    results.append(
        Result("cffi", OK, _version("cffi"))
        if have_cffi
        else Result(
            "cffi",
            MISSING,
            "runtime requirement of the AccuDisc binding (an API-mode cffi extension)",
            remedy=_install_hint(["cffi"]),
        )
    )

    # Resolved first, because the binding's line compares against it. The two
    # routes to the engine can resolve two *different* libraries, and after a
    # split install (AccuDisc §cp.4) they normally do: an injected wheel's
    # RUNPATH is the install prefix, while the development symlink stays on the
    # build tree — which moves on every compile, with no event marking the drift.
    # Both are individually correct, which is why only reporting one of them is
    # the failure mode rather than either being wrong.
    resolved = _resolve_engine_binary()
    on_path = shutil.which("accudisc")
    binary = resolved if resolved != "accudisc" else on_path
    cli_lib = _linked_library(Path(binary)) if binary is not None else ""

    spec = None
    try:
        spec = importlib.util.find_spec("accudisc")
    except (ImportError, ValueError):
        spec = None
    installed = spec is not None and spec.origin is not None

    if installed and spec is not None and spec.origin is not None:
        origin = Path(spec.origin)
        binding_lib = _binding_library(origin.parent)
        detail = f"{_version('accudisc')} at {origin.parent}"
        if binding_lib:
            detail += f"\n      libaccudisc -> {binding_lib}"
        detail += _library_skew(binding_lib, cli_lib)
        results.append(Result("accudisc (binding)", OK, detail))
    else:
        shim = _dev_shim_path()
        if shim is not None:
            # Not clean. The binding works here only because accudisc_reader
            # appends this path at import time; on any other machine it is
            # simply absent. Rendering this as OK is the exact false pass that
            # left the binding transport inert for two days.
            binding_lib = _binding_library(shim / "accudisc")
            detail = (
                "not installed — resolved at runtime only via the development"
                f"\n      shim {shim}. Not portable to another system."
            )
            if binding_lib:
                detail += f"\n      libaccudisc -> {binding_lib}"
            detail += _library_skew(binding_lib, cli_lib)
            results.append(
                Result(
                    "accudisc (binding)",
                    WARN,
                    detail,
                    remedy="install AccuDisc's Python binding (see its `make install`)",
                )
            )
        else:
            results.append(
                Result(
                    "accudisc (binding)",
                    MISSING,
                    "the preferred transport to the disc engine",
                    remedy="install AccuDisc's Python binding (see its `make install`)",
                )
            )

    # Which one *runs*, not merely whether one exists. The reader prefers the
    # `tools/accudisc/` symlink over `$PATH`, so `shutil.which` can name a
    # perfectly good system install that is being bypassed — and did, here.
    if binary is None:
        results.append(
            Result(
                "accudisc (binary)",
                MISSING,
                "fallback transport, used when the binding is unavailable",
                remedy="install AccuDisc (https://github.com/HomerSlated/accudisc)",
            )
        )
    else:
        detail = binary
        lib = cli_lib
        if lib:
            detail += f"\n      libaccudisc -> {lib}"
        if on_path is not None and on_path != binary:
            # Not an error — the symlink is the documented development
            # arrangement — but invisible otherwise, and "an install exists and
            # is not the one being used" is precisely the fact a dependency
            # report is for.
            detail += f"\n      shadows the $PATH install at {on_path}"
        results.append(Result("accudisc (binary)", OK, detail))

    if not installed and binary is None and _dev_shim_path() is None:
        results.append(
            Result(
                "disc engine",
                MISSING,
                "neither the binding nor the binary is present — no disc can be read",
                required=True,
            )
        )
    return results


def _shipped_profiles_dir() -> Path:
    """Where the shipped recovery profiles live.

    A deliberate duplicate of :func:`cdda2img.recovery_profile.shipped_profiles_dir`,
    for the same reason :func:`_resolve_engine_binary` duplicates the reader's
    resolver: importing that module would pull in ``tomli`` on 3.10 and breach the
    stdlib-only rule this file exists to honour. `tests/test_depcheck.py` pins the
    two implementations to the same answer, so the copy cannot drift silently.
    """
    import contextlib
    import importlib.resources

    with contextlib.suppress(Exception):
        ref = importlib.resources.files("cdda2img").joinpath("profiles")
        p = Path(str(ref))
        if p.is_dir():
            return p
    return Path(__file__).parent / "profiles"


def _check_package_data() -> list[Result]:
    """Check the data files the package must carry, not just its dependencies.

    This group exists because of a real hole. Until 2026-07-31 the shipped recovery
    profiles lived in a top-level ``conf/`` that the wheel did not package, so an
    installed cdda2img had none of them — and `rip` aborts on that, because rung 4
    of the resolver loads ``track-ladder`` whether or not the user named a profile.
    `doctor` reported **21 ok, 0 warnings** on exactly that machine: every
    dependency was genuinely present, and it had never been asked whether the
    package's own files came along. A checker that only inspects dependencies
    certifies an application that cannot start.

    Required, matching the ``disc engine`` entry: both are needed by `rip` and by
    nothing else, and both make it fail outright rather than degrade.
    """
    directory = _shipped_profiles_dir()
    found = (
        sorted(p.stem for p in directory.glob("*.toml")) if directory.is_dir() else []
    )
    if _BUILTIN_PROFILE in found:
        return [
            Result(
                "recovery profiles",
                OK,
                f"{len(found)} in {directory}\n      {', '.join(found)}",
            )
        ]
    # Distinguish the two ways this fails: an empty/absent directory is a packaging
    # fault, a populated one missing the built-in is a tampered install. The remedy
    # differs, so the report must not merge them.
    why = (
        f"the built-in profile {_BUILTIN_PROFILE!r} is absent from {directory}"
        if found
        else f"no profiles found at {directory}"
    )
    return [
        Result(
            "recovery profiles",
            MISSING,
            f"{why} — `rip` cannot start without them",
            remedy="reinstall cdda2img (the profiles ship inside the package)",
            required=True,
        )
    ]


def _check_binaries() -> list[Result]:
    results = []
    for b in EXTERNAL_BINARIES:
        found = shutil.which(b.name)
        results.append(
            Result(b.name, OK, found)
            if found
            else Result(b.name, MISSING, b.why, remedy=f"install {b.upstream}")
        )
    viewer = next((v for v in ART_VIEWERS if shutil.which(v)), None)
    results.append(
        Result("album-art viewer", OK, f"{viewer} ({shutil.which(viewer)})")
        if viewer
        else Result(
            "album-art viewer",
            MISSING,
            "terminal cover-art preview",
            remedy=f"install any one of: {', '.join(ART_VIEWERS)}",
        )
    )
    return results


def _check_native() -> list[Result]:
    results = []
    for lib, why in NATIVE_LIBS:
        found = find_library(lib)
        results.append(
            Result(lib, OK, found)
            if found
            else Result(lib, MISSING, why, remedy=f"install lib{lib}")
        )
    return results


_MARK = {OK: "ok  ", MISSING: "MISS", WARN: "WARN"}


def _emit(title: str, results: list[Result], out: TextIO, footer: str = "") -> None:
    print(f"\n{title}", file=out)
    width = max((len(r.name) for r in results), default=0)
    for r in results:
        req = " (required)" if r.required and r.status != OK else ""
        print(f"  [{_MARK[r.status]}] {r.name:<{width}}  {r.detail}{req}", file=out)
        if r.remedy:
            print(f"      -> {r.remedy}", file=out)
    if footer:
        print(f"  -> {footer}", file=out)


def run_doctor(stream: TextIO | None = None) -> int:
    """Report every dependency and return the exit code.

    Exit 1 iff something **required** is missing. A missing optional dependency
    is reported and does not fail: ``ffplay``'s absence costs the audition
    preview, not the rip. The development-shim warning does not fail either —
    it describes a working machine — but it must never render as clean, or the
    report would certify a system that cannot be reproduced anywhere else.

    Checks only. Nothing here installs, downloads, or modifies anything.
    """
    out = stream if stream is not None else sys.stdout
    print(f"cdda2img dependency check — Python {sys.version.split()[0]}", file=out)
    print(f"interpreter: {sys.executable}", file=out)

    python = _check_python()
    absent_dists = [r.name for r in python if r.status != OK]
    groups = [
        (
            "Python packages (required)",
            python,
            _install_hint(absent_dists) if absent_dists else "",
        ),
        ("Disc engine — AccuDisc", _check_accudisc(), ""),
        ("Package data", _check_package_data(), ""),
        ("External tools (optional; each enables one feature)", _check_binaries(), ""),
        ("Native libraries (optional)", _check_native(), ""),
    ]
    for title, results, footer in groups:
        _emit(title, results, out, footer)

    every = [r for _, rs, _ in groups for r in rs]
    failed = [r for r in every if r.required and r.status != OK]
    warned = [r for r in every if r.status == WARN]
    absent = [r for r in every if r.status == MISSING and not r.required]

    print(
        f"\n{len(every) - len(failed) - len(warned) - len(absent)} ok, "
        f"{len(absent)} optional missing, {len(warned)} warning(s), "
        f"{len(failed)} required missing",
        file=out,
    )
    if failed:
        print(
            "\ncdda2img will not run until the required items above are installed.",
            file=out,
        )
        return 1
    return 0
