import warnings

# discogs_client/fetchers.py:102 uses '\w' in a non-raw string; the parser emits
# SyntaxWarning at bytecode-compile time. `module=` is matched against the
# *filename* (stripped of `.py`) via re.match, NOT against the dotted module
# name — so we need a path-style pattern anchored at the start, not
# `discogs_client\..*`. See `re.match`/`warnings.filterwarnings` docs.
warnings.filterwarnings(
    "ignore",
    category=SyntaxWarning,
    module=r".*/discogs_client/.*",
)

from cdda2img.cdda2img import main  # noqa: E402

main()
