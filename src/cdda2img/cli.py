"""Console-script entry point.

Exists so ``[project.scripts]`` has a callable to name. ``__main__.py`` cannot
serve that role: it calls ``main()`` at *import* time, which is right for
``python -m cdda2img`` and wrong for an entry point (the loader imports the
module and then calls the named attribute, so the command would run twice).

The warning filter has to be installed before ``discogs_client`` is imported,
which is why the real import sits inside the function rather than at module
level — an entry point that imported the package first would install the filter
too late and leak the warning it exists to suppress.

The same deferral carries the dependency pre-flight, for a stronger reason.
``cdda2img.cdda2img`` imports ``av``, ``mutagen``, ``numpy``, ``ortools`` and
``unidecode`` at module level, so any check placed behind that import — an
argparse subcommand, for instance — raises ``ImportError`` before it can report
a thing. This module and :mod:`cdda2img.depcheck` are therefore standard-library
only, and both the ``doctor`` dispatch and the pre-flight run *above* the
application import rather than inside it.
"""

from __future__ import annotations

import sys
import warnings

from cdda2img import depcheck


def main() -> None:
    """Check dependencies, install the warning filters, then run the CLI."""
    # `doctor` is dispatched here, not in the argparse tree, because argparse
    # lives on the far side of the import this command exists to survive. A
    # bare argv test is enough: `doctor` takes no options and the application
    # parser never sees it.
    if sys.argv[1:2] == ["doctor"]:
        raise SystemExit(depcheck.run_doctor())

    depcheck.preflight_or_exit()

    # discogs_client/fetchers.py:102 uses '\w' in a non-raw string; the parser
    # emits SyntaxWarning at bytecode-compile time. `module=` is matched against
    # the *filename* (stripped of `.py`) via re.match, NOT against the dotted
    # module name — so we need a path-style pattern anchored at the start, not
    # `discogs_client\..*`. See `re.match`/`warnings.filterwarnings` docs.
    warnings.filterwarnings(
        "ignore",
        category=SyntaxWarning,
        module=r".*/discogs_client/.*",
    )

    from cdda2img.cdda2img import main as _run

    _run()
