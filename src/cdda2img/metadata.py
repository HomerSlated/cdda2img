"""
metadata.py — Derive album and artist metadata from audio files or a tracklist.
"""

from pathlib import Path

from mutagen import File  # type: ignore[import-untyped]

_ARTIST_TAGS: list[str] = [
    "ALBUM ARTIST",
    "ALBUM_ARTIST",
    "ALBUMARTIST_CREDIT",
    "ALBUMARTISTSORT",
    "ALBUMARTISTS",
    "ALBUM_ARTISTS",
    "ALBUMARTISTS_CREDIT",
    "ALBUMARTISTS_SORT",
    "ARTIST",
    "ARTIST_CREDIT",
    "ARTISTSORT",
    "ARTISTS",
    "ARTISTS_CREDIT",
    "ARTISTS_SORT",
]


def derive_album_info(tracks: list[Path], autoaccept: bool = False) -> dict[str, str]:
    """
    Derive album title and artist from a list of audio file paths.

    Priority order:
      1. Embedded audio tags (mutagen) from the first readable track
      2. Parent directory name (for album title)
      3. Fallback string "Unknown Artist"

    If autoaccept is False, the user is prompted to confirm or edit the derived values.

    Returns a dict with keys "album" and "artist".
    """
    metadata_album: str | None = None
    metadata_artist: str | None = None

    for track in tracks:
        audio = File(str(track), easy=True)
        if not audio:
            continue

        for tag in _ARTIST_TAGS:
            value = audio.get(tag)
            if value:
                metadata_artist = value[0].strip() if isinstance(value, list) else str(value).strip()
                break

        album_value = audio.get("album") or audio.get("ALBUM")
        if album_value:
            metadata_album = album_value[0].strip() if isinstance(album_value, list) else str(album_value).strip()

        break  # only need the first readable track

    cwd_album = Path.cwd().name

    final_album = metadata_album or cwd_album
    final_artist = metadata_artist or "Unknown Artist"

    if not autoaccept:
        final_album = _confirm("album title", final_album)
        final_artist = _confirm("album artist", final_artist)

    return {"album": final_album, "artist": final_artist}


def _confirm(prompt: str, default: str) -> str:
    """Interactively confirm or edit a derived metadata value."""
    while True:
        response = input(f"Confirm {prompt} [{default}]: a=accept, e=edit > ").strip().lower()
        if response == "a":
            return default
        if response == "e":
            return input(f"Enter {prompt}: ").strip()
        print("Please enter 'a' to accept or 'e' to edit.")
