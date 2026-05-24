import warnings

warnings.filterwarnings(
    "ignore",
    category=SyntaxWarning,
    module=r"discogs_client\..*",
)

from cdda2img.cdda2img import main  # noqa: E402

main()
