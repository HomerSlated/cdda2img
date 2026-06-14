"""albumart.py — retired standalone probe. See src/cdda2img/album_art.py."""

msg = (
    "tools/albumart.py is retired.\n"
    "Use the production module: src/cdda2img/album_art.py\n"
    "Album art is embedded via the rip/create/import pipeline, or extracted\n"
    "via 'cdda2img extract --embedart' for FLAC embedding."
)
raise SystemExit(msg)
