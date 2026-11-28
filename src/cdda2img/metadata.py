"""
metadata.py — Derive album and artist metadata from audio files or a tracklist.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from mutagen import File  # type: ignore[import-untyped]  # LINT-004

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
                metadata_artist = (
                    value[0].strip() if isinstance(value, list) else str(value).strip()
                )
                break

        album_value = audio.get("album") or audio.get("ALBUM")
        if album_value:
            metadata_album = (
                album_value[0].strip()
                if isinstance(album_value, list)
                else str(album_value).strip()
            )

        break  # only need the first readable track

    # Album fallback is the audio files' parent directory name (per the docstring),
    # not the process CWD — `create /music/Album` run from /home/user must fall back
    # to "Album", not "user".
    dir_album = tracks[0].parent.name if tracks else Path.cwd().name

    final_album = metadata_album or dir_album
    final_artist = metadata_artist or "Unknown Artist"

    if not autoaccept:
        final_album = _confirm("album title", final_album)
        final_artist = _confirm("album artist", final_artist)

    return {"album": final_album, "artist": final_artist}


def _confirm(prompt: str, default: str) -> str:
    """Interactively confirm or edit a derived metadata value."""
    while True:
        response = (
            input(f"Confirm {prompt} [{default}]: a=accept, e=edit > ").strip().lower()
        )
        if response == "a":
            return default
        if response == "e":
            return input(f"Enter {prompt}: ").strip()
        print("Please enter 'a' to accept or 'e' to edit.")


_MBID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class FileTagRelease(NamedTuple):
    """The MusicBrainz release the source files' tags name (rbi_spec.md
    ``file_tag_release``).

    *state* is ``None`` when no file carries the tag, ``"mixed"`` when the files
    disagree (or only some carry it), ``"invalid"`` when the one value is not a
    UUID, else the MBID itself, which is then also *release_id*. *disc_number* is
    the files' common ``DISCNUMBER``, or ``None`` when they do not share one.
    """

    release_id: str | None
    state: str | None
    disc_number: int | None


def _first_tag(audio, key: str) -> str | None:
    value = audio.get(key) if audio else None
    if not value:
        return None
    text = (value[0] if isinstance(value, list) else str(value)).strip()
    return text or None


def _disc_number(text: str | None) -> int | None:
    m = re.match(r"\s*(\d+)", text or "")
    return int(m.group(1)) if m else None


def read_file_tag_release(tracks: list[Path]) -> FileTagRelease:
    """Read the MusicBrainz release id (``musicbrainz_albumid``) from every file.

    Only the id is read: the release is fetched from MusicBrainz, so a tagger's
    copies of the barcode, label and the rest never reach the container. The id
    counts only when **every** file names the same one: a batch whose files name
    different releases, or where some name none, is not one release's tracks.
    mutagen's easy interface maps the name onto Vorbis comments, ID3 ``TXXX`` and
    MP4 freeform atoms alike.
    """
    ids: list[str | None] = []
    discs: list[int | None] = []
    for track in tracks:
        try:
            audio = File(str(track), easy=True)
        except Exception:
            audio = None
        mbid = _first_tag(audio, "musicbrainz_albumid")
        ids.append(mbid.lower() if mbid else None)
        discs.append(_disc_number(_first_tag(audio, "discnumber")))
    if not any(ids):
        return FileTagRelease(None, None, None)
    if len(set(ids)) != 1:
        return FileTagRelease(None, "mixed", None)
    mbid = ids[0] or ""  # never empty: any(ids) and one distinct value
    if not _MBID_RE.match(mbid):
        return FileTagRelease(None, "invalid", None)
    disc = discs[0] if len(set(discs)) == 1 else None
    return FileTagRelease(mbid, mbid, disc)


# ── create's file match (trust_model_design.md §14) ────────────────────────────

#: A leading track number in a file name ("01 Another World", "1-Track", "03_x").
#: The same shape ``create`` strips from its raw titles.
LEADING_NUMBER = re.compile(r"^(\d{1,3})[-. _]+")


@dataclass(frozen=True)
class FileTags:
    """One file as the file match sees it: its tags, and, apart, its name.

    kgr, 2026-11-28: file names are ignored until every other piece of evidence
    has been gathered, and then used only as corroboration that may support a
    match and is never required. So nothing here falls back to the name:
    ``title`` and ``tracknumber`` are tags only, and the name's number and text
    are kept separately for that last, optional check.
    """

    path: Path
    title: str | None  # the title tag
    tracknumber: int | None  # the track-number tag
    recording_id: str | None  # musicbrainz_trackid (the recording, despite the name)
    name_number: int | None = None  # the file name's leading number
    name_title: str | None = None  # the file name without its leading number
    discnumber: int | None = None  # the disc-number tag
    duration_ms: int | None = None  # the source file's length (before any trim)


@dataclass(frozen=True)
class AlbumTags:
    """Album-level tags every file that carries them agrees on (else ``None``)."""

    album: str | None = None
    artist: str | None = None
    date: str | None = None
    originaldate: str | None = None
    barcode: str | None = None
    catalognumber: str | None = None
    label: str | None = None
    country: str | None = None


_ALBUM_TAG_KEYS: dict[str, tuple[str, ...]] = {
    "album": ("album",),
    "artist": ("albumartist", "album artist", "album_artist", "artist"),
    "date": ("date", "year"),
    "originaldate": ("originaldate",),
    "barcode": ("barcode",),
    "catalognumber": ("catalognumber",),
    "label": ("label", "publisher", "organization"),
    "country": ("releasecountry",),
}


def _any_tag(audio, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _first_tag(audio, key)
        if value:
            return value
    return None


def read_file_tracks(tracks: list[Path]) -> tuple[list[FileTags], AlbumTags]:
    """Every file's own tags, and the album-level tags they agree on.

    An album-level value counts only when no two files that state it disagree:
    one file's stray date is not the album's.
    """
    files: list[FileTags] = []
    album_values: dict[str, set[str]] = {k: set() for k in _ALBUM_TAG_KEYS}
    for track in tracks:
        try:
            audio = File(str(track), easy=True)
        except Exception:
            audio = None
        stem = track.stem.strip()
        m = LEADING_NUMBER.match(stem) or re.fullmatch(r"(\d{1,3})", stem)
        name_number = int(m.group(1)) if m else None
        name_title = (stem[m.end() :] if m else stem).strip() or None
        rec = _first_tag(audio, "musicbrainz_trackid")
        rec = rec.lower() if rec and _MBID_RE.match(rec.lower()) else None
        length = getattr(getattr(audio, "info", None), "length", None)
        files.append(
            FileTags(
                path=track,
                title=_first_tag(audio, "title"),
                tracknumber=_disc_number(_first_tag(audio, "tracknumber")),
                recording_id=rec,
                name_number=name_number,
                name_title=name_title,
                discnumber=_disc_number(_first_tag(audio, "discnumber")),
                duration_ms=round(length * 1000) if length else None,
            )
        )
        for name, keys in _ALBUM_TAG_KEYS.items():
            value = _any_tag(audio, keys)
            if value:
                album_values[name].add(value)
    album = AlbumTags(**{
        k: next(iter(v)) if len(v) == 1 else None for k, v in album_values.items()
    })
    return files, album


def files_order(files: list[FileTags]) -> tuple[list[int], bool]:
    """The files' own order, as indices into *files*, and whether it is KNOWN
    (trust_model_design.md §14.4): their (disc-number, track-number) tags when
    every file has a track number and no two share the pair (track numbers
    usually restart on each disc of a set). Never their names (kgr, 2026-11-28:
    names are corroboration only, after everything else) and never the batch
    position. Without the tags the order is unknown: the indices are the order
    given, and the archive takes the chosen release's order."""
    keys = [(f.discnumber or 0, f.tracknumber) for f in files]
    if all(t is not None for _, t in keys) and len(set(keys)) == len(keys):
        return sorted(
            range(len(files)), key=lambda i: (keys[i][0], keys[i][1] or 0)
        ), True
    return list(range(len(files))), False
