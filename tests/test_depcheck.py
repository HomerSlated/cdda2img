"""Tests for the dependency pre-flight.

Two of these guard properties that no ordinary test would catch, because the
test environment has every dependency installed and therefore exercises only
the path where nothing is wrong:

* :func:`test_depcheck_imports_only_the_standard_library` reads the module's own
  AST instead of trusting the import to fail. Adding ``from cdda2img.container
  import ...`` to ``depcheck.py`` would leave every other test green while
  making the checker unable to run on the machines it was written for.
* :func:`test_dependency_table_matches_pyproject` diffs the written table
  against ``[project.dependencies]``. The table exists because the
  distribution-to-import-name mapping is not derivable; this keeps it from
  becoming a second, drifting source of truth.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from cdda2img import depcheck

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _requirement_name(spec: str) -> str:
    """``"tomli>=2.0.0 ; python_version < '3.11'"`` -> ``"tomli"``."""
    name = spec.split(";", 1)[0]
    for sep in ("<", ">", "=", "!", "~", "["):
        name = name.split(sep, 1)[0]
    return name.strip()


# ---------------------------------------------------------------------------
# The two guards that only an out-of-band check can make
# ---------------------------------------------------------------------------


def test_depcheck_imports_only_the_standard_library() -> None:
    """`depcheck` must be reachable when the dependencies are absent.

    `cdda2img.cdda2img` imports av/mutagen/numpy/ortools/unidecode eagerly, so a
    checker that pulled in any `cdda2img` module risks inheriting that and dying
    with `ImportError` before it can report anything — diagnosing every
    dependency except the missing ones. Asserted against the source rather than
    by importing, because in this environment the import would succeed.
    """
    tree = ast.parse((_PROJECT_ROOT / "src/cdda2img/depcheck.py").read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])

    non_stdlib = {r for r in roots if r not in sys.stdlib_module_names}
    assert non_stdlib == set(), (
        f"depcheck.py imports non-stdlib modules {sorted(non_stdlib)}; it must be "
        "importable on a machine that has none of cdda2img's dependencies"
    )


def test_dependency_table_matches_pyproject() -> None:
    """`RUNTIME_PYTHON` + `_TOMLI` must be exactly `[project.dependencies]`."""
    raw = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text())
    declared = {_requirement_name(s) for s in raw["project"]["dependencies"]}
    tabled = {d.dist for d in depcheck.RUNTIME_PYTHON} | {depcheck._TOMLI.dist}
    assert tabled == declared


def test_every_tabled_module_is_importable_here() -> None:
    """The import names in the table are real, not guessed.

    `pyacoustid` imports as `acoustid` and `discogs-client` as `discogs_client`;
    a typo in either column would make the checker report a present dependency
    as missing and refuse to start.
    """
    for dep in depcheck.required_python_deps():
        assert depcheck._module_present(dep.module), dep


# ---------------------------------------------------------------------------
# Probe semantics
# ---------------------------------------------------------------------------


def test_namespace_package_does_not_count_as_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty directory on `sys.path` imports fine and must still read absent.

    This is the trap that `tools/accudisc/` — a directory holding a symlink to a
    binary — springs on `import accudisc`: it binds as an empty PEP 420
    namespace package and raises no `ImportError`. A presence check that only
    asked "did `find_spec` return something" would call that installed.
    """
    (tmp_path / "phantom_pkg").mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))

    import importlib.util

    assert importlib.util.find_spec("phantom_pkg") is not None, "premise: it resolves"
    assert depcheck._module_present("phantom_pkg") is False


def test_tomli_is_required_only_below_311() -> None:
    """3.11 absorbed it as `tomllib`; demanding it on 3.14 is a false finding."""
    names = {d.dist for d in depcheck.required_python_deps()}
    assert ("tomli" in names) == (sys.version_info < (3, 11))


# ---------------------------------------------------------------------------
# The runtime pre-flight
# ---------------------------------------------------------------------------


def test_preflight_is_silent_when_nothing_is_missing(
    capsys: pytest.CaptureFixture,
) -> None:
    depcheck.preflight_or_exit()
    assert capsys.readouterr().err == ""


def test_preflight_names_every_missing_dependency(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """All of them in one run, not the first one.

    Python's own `ImportError` names a single module, so a user three
    dependencies short learns that three times over. Reporting one here would
    reproduce exactly the behaviour this replaces.
    """
    monkeypatch.setattr(
        depcheck,
        "RUNTIME_PYTHON",
        (
            depcheck.PyDep("absent-one", "absent_one_xyz", "first reason"),
            depcheck.PyDep("absent-two", "absent_two_xyz", "second reason"),
        ),
    )
    # Caught by hand rather than via `pytest.raises(...).value.code`: the exit
    # code is half the contract (the shell has to be able to tell), and `ty`
    # cannot resolve `.value` through `ExceptionInfo`.
    code: object = None
    try:
        depcheck.preflight_or_exit()
    except SystemExit as exc:
        code = exc.code
    else:
        pytest.fail("preflight_or_exit returned instead of exiting")

    assert code == 1
    err = capsys.readouterr().err
    assert "absent-one" in err
    assert "absent-two" in err
    assert "first reason" in err


def test_preflight_hint_targets_pipx_inside_a_pipx_venv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pip install` into a pipx venv is wrong advice that appears to work.

    It survives until the next `pipx upgrade` rebuilds the venv and silently
    drops the hand-installed package.
    """
    monkeypatch.setattr(sys, "prefix", "/home/u/.local/pipx/venvs/cdda2img")
    assert depcheck._install_hint(["blake3"]) == "pipx inject cdda2img blake3"

    monkeypatch.setattr(sys, "prefix", "/usr")
    assert "pip install blake3" in depcheck._install_hint(["blake3"])


# ---------------------------------------------------------------------------
# The doctor
# ---------------------------------------------------------------------------


def test_doctor_passes_on_this_machine(capsys: pytest.CaptureFixture) -> None:
    """The development environment must have everything required."""
    assert depcheck.run_doctor() == 0
    assert "required missing" in capsys.readouterr().out


def test_doctor_fails_when_a_required_dependency_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(
        depcheck,
        "RUNTIME_PYTHON",
        (depcheck.PyDep("absent-one", "absent_one_xyz", "first reason"),),
    )
    assert depcheck.run_doctor() == 1
    assert "absent-one" in capsys.readouterr().out


def test_doctor_rejects_arguments_instead_of_ignoring_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`cdda2img doctor --json` must fail, not quietly run the plain report.

    The dispatch is a bare argv test rather than an argparse subparser, because
    argparse lives behind the import `doctor` exists to survive — so the "takes
    no options" contract has to be enforced by hand or not at all. Ignoring the
    argument would make a `--json` that does not exist look like one that does.
    """
    from cdda2img import cli

    monkeypatch.setattr(sys, "argv", ["cdda2img", "doctor", "--json"])
    code: object = None
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    else:
        pytest.fail("cli.main() returned instead of exiting")

    assert code == 2
    assert "--json" in capsys.readouterr().err


def test_doctor_runs_with_no_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half: a bare `doctor` still reaches the report."""
    from cdda2img import cli

    monkeypatch.setattr(sys, "argv", ["cdda2img", "doctor"])
    monkeypatch.setattr(cli.depcheck, "run_doctor", lambda: 0)
    code: object = None
    try:
        cli.main()
    except SystemExit as exc:
        code = exc.code
    else:
        pytest.fail("cli.main() returned instead of exiting")

    assert code == 0


def test_doctor_does_not_report_the_dev_shim_as_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A binding reachable only through `tools/accudisc/pybinding` is not clean.

    It works here and nowhere else, which is the whole point of the report — and
    is the exact false pass that let the binding transport sit inert for two
    days while every check said fine. It warns rather than fails, because the
    machine does work; what it must never do is render as `ok`.
    """
    import importlib.util

    real = importlib.util.find_spec

    def fake(name: str, *a: object, **k: object) -> object | None:
        return None if name == "accudisc" else real(name, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    monkeypatch.setattr(depcheck, "_dev_shim_path", lambda: tmp_path / "pybinding")

    binding = next(
        r for r in depcheck._check_accudisc() if r.name == "accudisc (binding)"
    )
    assert binding.status == depcheck.WARN
    assert binding.status != depcheck.OK
    assert "shim" in binding.detail


def test_binding_library_asks_the_extension_not_the_python_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`spec.origin` names `__init__.py`, which `ldd` cannot answer about.

    Passing it straight through returned "not a dynamic executable" and an empty
    string, so the `libaccudisc ->` line was silently never emitted for an
    *installed* binding — the one configuration where it matters most, since a
    pipx-injected wheel is exactly the artefact whose linkage was wrong in
    §123.2 while every check reported success.
    """
    pkg = tmp_path / "accudisc"
    pkg.mkdir()
    (pkg / "__init__.py").touch()
    (pkg / "_accudisc.abi3.so").touch()

    asked: list[Path] = []

    def fake(origin: Path) -> str:
        asked.append(origin)
        return "/prefix/lib64/libaccudisc.so.0"

    monkeypatch.setattr(depcheck, "_linked_library", fake)

    assert depcheck._binding_library(pkg) == "/prefix/lib64/libaccudisc.so.0"
    assert [p.name for p in asked] == ["_accudisc.abi3.so"]


def test_library_skew_is_silent_unless_both_are_known_and_differ() -> None:
    """ "Unknown" must not render as "differs".

    An empty string means `ldd` declined to answer, which is not evidence of a
    mismatch — reporting one would manufacture a discrepancy out of a missing
    measurement.
    """
    assert "different" in depcheck._library_skew("/a/lib.so.0", "/b/lib.so.0")
    assert depcheck._library_skew("/a/lib.so.0", "/a/lib.so.0") == ""
    assert depcheck._library_skew("", "/b/lib.so.0") == ""
    assert depcheck._library_skew("/a/lib.so.0", "") == ""
    assert depcheck._library_skew("", "") == ""


def test_a_bare_shim_directory_is_not_evidence_of_a_binding(tmp_path: Path) -> None:
    """The shim must hold the package, not merely exist.

    `_dev_shim_path` is read as evidence *against* a total engine absence — the
    `disc engine` one-of does not fire while a shim is present. So a directory
    that exists but holds no `accudisc` package would suppress the single
    required failure this whole report exists to raise, and would do it on a
    machine with no working engine at all.
    """
    shim = tmp_path / "tools" / "accudisc" / "pybinding"
    shim.mkdir(parents=True)
    assert shim.is_dir(), "premise: the directory exists"
    assert depcheck._dev_shim_path(root=tmp_path) is None

    pkg = shim / "accudisc"
    pkg.mkdir()
    (pkg / "__init__.py").touch()
    assert depcheck._dev_shim_path(root=tmp_path) == shim


def test_engine_resolution_matches_the_reader() -> None:
    """`depcheck` must name the same `accudisc` the reader will actually run.

    The logic is duplicated rather than imported, because `accudisc_reader` sits
    behind the heavy imports `depcheck` exists to stay clear of. That makes drift
    the obvious failure and it is not one an ordinary test would catch: the two
    copies would simply disagree, and `doctor` would go back to confidently
    naming an artefact that never runs — the defect this pins, with a new cause.
    """
    from cdda2img import accudisc_reader

    assert depcheck._resolve_engine_binary() == accudisc_reader._resolve_accudisc()


def test_doctor_names_the_resolved_binary_and_the_install_it_shadows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`shutil.which` is the wrong answer when the symlink outranks `$PATH`.

    Measured on the development box: `which` said `/usr/local/bin/accudisc`
    while the reader ran `tools/accudisc/accudisc` — different sha256, different
    `RUNPATH`, different `libaccudisc.so.0`. Nothing failed, so only the report
    was wrong, which is the kind of wrong that survives.
    """
    runs = tmp_path / "build" / "accudisc"
    runs.parent.mkdir()
    runs.touch()

    monkeypatch.setattr(depcheck, "_resolve_engine_binary", lambda: str(runs))
    monkeypatch.setattr(depcheck.shutil, "which", lambda _n: "/usr/local/bin/accudisc")

    binary = next(
        r for r in depcheck._check_accudisc() if r.name == "accudisc (binary)"
    )
    assert binary.status == depcheck.OK
    assert str(runs) in binary.detail
    assert "shadows" in binary.detail
    assert "/usr/local/bin/accudisc" in binary.detail


def test_doctor_does_not_claim_shadowing_when_there_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: an installed-only machine gets a plain line."""
    monkeypatch.setattr(depcheck, "_resolve_engine_binary", lambda: "accudisc")
    monkeypatch.setattr(depcheck.shutil, "which", lambda _n: "/usr/local/bin/accudisc")

    binary = next(
        r for r in depcheck._check_accudisc() if r.name == "accudisc (binary)"
    )
    assert binary.status == depcheck.OK
    assert "shadows" not in binary.detail


def test_doctor_flags_a_total_absence_of_the_disc_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Binding or binary is a one-of; neither is a required failure.

    Marking each individually required would fail a working install that has
    only one of them, which is the normal state after the subprocess retires.
    """
    import importlib.util

    real = importlib.util.find_spec

    def fake(name: str, *a: object, **k: object) -> object | None:
        return None if name == "accudisc" else real(name, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", fake)
    monkeypatch.setattr(depcheck, "_dev_shim_path", lambda: None)
    monkeypatch.setattr(depcheck.shutil, "which", lambda _n: None)
    # Also the symlink: it outranks `$PATH`, so clearing `which` alone leaves
    # the development tree's own binary answering and no absence to detect.
    monkeypatch.setattr(depcheck, "_resolve_engine_binary", lambda: "accudisc")

    results = depcheck._check_accudisc()
    engine = [r for r in results if r.name == "disc engine"]
    assert len(engine) == 1
    assert engine[0].required is True
    assert engine[0].status == depcheck.MISSING
