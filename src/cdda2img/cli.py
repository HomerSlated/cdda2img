"""Console-script entry point.

Exists so ``[project.scripts]`` has a callable to name. ``__main__.py`` cannot
serve that role: it calls ``main()`` at *import* time, which is right for
``python -m cdda2img`` and wrong for an entry point (the loader imports the
module and then calls the named attribute, so the command would run twice).

The warning filter has to be installed before ``discogs_client`` is imported,
which is why the real import sits inside the function rather than at module
level — an entry point that imported the package first would install the filter
too late and leak the warning it exists to suppress.
"""

from __future__ import annotations

import warnings


def main() -> None:
    """Install the import-time warning filters, then run the CLI."""
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
