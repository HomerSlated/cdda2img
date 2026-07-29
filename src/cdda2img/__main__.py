"""``python -m cdda2img`` — delegates to the same entry point as the console script.

Both paths must install the ``discogs_client`` SyntaxWarning filter before the
package is imported, so the filter lives in one place (``cli.main``) rather than
being duplicated here and drifting.
"""

from cdda2img.cli import main

main()
