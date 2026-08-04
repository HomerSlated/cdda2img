"""album_art.py — fetch, transcode, and display album-cover art.

Public API
----------
CoverArt              dataclass: raw image bytes + provenance
fetch_cover(disc)     → CoverArt | None   CAA rg → CAA release → Discogs
cover_from_file_tags  → CoverArt | None   mutagen; for the create pipeline
transcode_to_jpeg     → bytes             PyAV; no-op when already JPEG
downscale_jpeg        → bytes             PyAV; for FLAC embed (~600 px)
to_album_art          → RBIAlbumArt       ready for build_container
render_cover          → bool              terminal display (best-effort)

Every public function is best-effort: network errors, decode failures, and
missing renderers all degrade silently (logged at WARNING/DEBUG; never raise).
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import av
from av.video.codeccontext import VideoCodecContext
from av.video.frame import VideoFrame
from mutagen import File as _MutagenFile  # type: ignore[import-untyped]  # LINT-004

from cdda2img.rbi_format import (
    ART_BLOCK_VERSION,
    ART_IMAGE_FORMAT_JPEG,
    RBIAlbumArt,
    RBIDisc,
)

log = logging.getLogger(__name__)

_USER_AGENT = (
    f"cdda2img/{importlib.metadata.version('cdda2img')}"
    " +https://github.com/HomerSlated/cdda2img"
)
_CAA_BASE = "https://coverartarchive.org"
_DISCOGS_API = "https://api.discogs.com"
_MAX_BYTES = 30 * 1024 * 1024  # 30 MiB — generous cap for original CAA uploads
_DEFAULT_SIZE = "24x12"  # terminal cell geometry (columns x rows)


@dataclass
class CoverArt:
    """Raw image bytes and associated metadata from a single fetch."""

    data: bytes
    fmt: str  # "jpeg" | "png" | "gif" | "webp" | "unknown"
    width: int | None  # pixels; None when not readable from magic bytes
    height: int | None
    source: str  # PROV art_source value, e.g. "caa:release-group:<uuid>"


# ---------------------------------------------------------------------------
# Format sniffing (magic bytes only — no decode)
# ---------------------------------------------------------------------------


def sniff(data: bytes) -> tuple[str, int | None, int | None]:
    """Return (format, width, height) from magic bytes. Dimensions may be None."""
    if data[:3] == b"\xff\xd8\xff":
        w, h = _jpeg_dims(data)
        return ("jpeg", w, h)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if data[12:16] == b"IHDR":
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            return ("png", w, h)
        return ("png", None, None)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w = int.from_bytes(data[6:8], "little")
        h = int.from_bytes(data[8:10], "little")
        return ("gif", w, h)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp", None, None)
    return ("unknown", None, None)


def _jpeg_dims(data: bytes) -> tuple[int | None, int | None]:
    """Scan JPEG segment markers for an SOF frame header."""
    i, n = 2, len(data)
    try:
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                w = int.from_bytes(data[i + 7 : i + 9], "big")
                h = int.from_bytes(data[i + 5 : i + 7], "big")
                return (w, h)
            seg_len = int.from_bytes(data[i + 2 : i + 4], "big")
            i += 2 + seg_len
    except (IndexError, ValueError):
        pass
    return (None, None)


def _ext_for(fmt: str) -> str:
    return {"jpeg": ".jpg", "png": ".png", "gif": ".gif", "webp": ".webp"}.get(
        fmt, ".img"
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

# Per-request socket timeout for all cover fetches. Any caller that waits on a
# fetch (e.g. the pre-rip banner's worker join) must allow at least this long,
# or a slow-but-successful fetch is abandoned before it can be displayed.
HTTP_TIMEOUT = 30


def _http_get(url: str, max_bytes: int, headers: dict[str, str] | None = None) -> bytes:
    req = urllib.request.Request(  # noqa: S310
        url, headers={"User-Agent": _USER_AGENT, **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        msg = f"response exceeds {max_bytes} bytes"
        raise ValueError(msg)
    return data


# ---------------------------------------------------------------------------
# CAA / Discogs fetch
# ---------------------------------------------------------------------------


def _try_caa(entity: str, mbid: str) -> CoverArt | None:
    """Try CAA original, then 1200 on size-cap; return None on any real failure."""
    source = f"caa:{entity}:{mbid}"
    for size in ("original", "1200"):
        suffix = "front" if size == "original" else "front-1200"
        url = f"{_CAA_BASE}/{entity}/{mbid}/{suffix}"
        try:
            data = _http_get(url, _MAX_BYTES)
            fmt, w, h = sniff(data)
            return CoverArt(data=data, fmt=fmt, width=w, height=h, source=source)
        except ValueError:
            if size == "original":
                log.debug("CAA %s %s: original exceeds cap, trying 1200", entity, mbid)
                continue
            log.warning(
                "CAA %s %s: 1200 derivative also exceeds cap — skipping", entity, mbid
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                log.debug("CAA %s %s: no front cover (404)", entity, mbid)
            else:
                log.warning("CAA %s %s: HTTP %s", entity, mbid, exc.code)
            break
        except (urllib.error.URLError, OSError) as exc:
            log.warning("CAA %s %s: %s", entity, mbid, exc)
            break
    return None


def _try_discogs(release_id: int) -> CoverArt | None:
    """Fetch the primary image from a Discogs release. Token must be set."""
    token = os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        return None
    auth = {"Authorization": f"Discogs token={token}"}
    try:
        meta_raw = _http_get(
            f"{_DISCOGS_API}/releases/{release_id}", 4_000_000, headers=auth
        )
        meta = json.loads(meta_raw)
        images = meta.get("images") or []
        if not images:
            log.debug("Discogs release %s: no images", release_id)
            return None
        primary = next((im for im in images if im.get("type") == "primary"), images[0])
        uri = primary.get("uri")
        if not uri:
            log.debug("Discogs release %s: image entry has no uri", release_id)
            return None
        data = _http_get(uri, _MAX_BYTES, headers=auth)
        fmt, w, h = sniff(data)
        return CoverArt(
            data=data, fmt=fmt, width=w, height=h, source=f"discogs:{release_id}"
        )
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        log.warning("Discogs release %s: %s", release_id, exc)
    except Exception as exc:
        log.warning("Discogs release %s: unexpected error: %s", release_id, exc)
    return None


# ---------------------------------------------------------------------------
# Public fetch entry points
# ---------------------------------------------------------------------------

# OPT-2: process-lifetime cache for successful cover fetches, keyed on the
# CoverArt.source string (caa:{entity}:{mbid} / discogs:{id}). The pre-rip banner
# (_preview_worker) and finalization (_finalize_import) both call fetch_cover; when
# the pre- and post-menu IDs coincide (the common path) this avoids re-downloading
# the same image bytes. Only **successful** fetches are cached — a miss (None) is a
# cheap 404, and not caching it means a transient failure is retried, never frozen.
# No TTL; discarded on process exit. Clearable via _COVER_CACHE.clear().
_COVER_CACHE: dict[str, CoverArt] = {}


def _caa_cached(entity: str, mbid: str) -> CoverArt | None:
    key = f"caa:{entity}:{mbid}"
    hit = _COVER_CACHE.get(key)
    if hit is not None:
        return hit
    art = _try_caa(entity, mbid)
    if art is not None:
        _COVER_CACHE[key] = art
    return art


def _discogs_cached(release_id: int) -> CoverArt | None:
    key = f"discogs:{release_id}"
    hit = _COVER_CACHE.get(key)
    if hit is not None:
        return hit
    art = _try_discogs(release_id)
    if art is not None:
        _COVER_CACHE[key] = art
    return art


def fetch_cover(disc: RBIDisc) -> CoverArt | None:
    """Fetch the best available front cover for disc.

    Chain: CAA release → CAA release-group → Discogs. Best-effort — returns
    None on network failure. Successful fetches are memoised per source (OPT-2).

    **The release rung comes first, and the order is the point.** CAA's
    release-group endpoint serves the front cover of *one* release in the group,
    chosen by CAA, and it need not be the release we identified: measured on
    Tracy Chapman, release-group ``a738bdf1`` serves release ``b0760dd1``'s art
    while the disc in the drive is ``65e67d39`` (2026-08-04). Group-first
    therefore embedded another pressing's cover on every disc whose group had
    any art, discarding the pressing the §10.3 selection ladder, the barcode
    corroboration and ``preferred_country`` had all just worked to pin.

    The release-group rung is kept, second, because a disc can legitimately
    arrive here with a group and no release: ``field_resolver`` enforces C2 —
    recording-level sources (AcoustID) may not propose an ``mb_release_id`` —
    so an AcoustID-only identification has nothing else to fetch against.
    """
    if disc.mb_release_id:
        art = _caa_cached("release", disc.mb_release_id)
        if art is not None:
            return art

    if disc.mb_release_group_id:
        art = _caa_cached("release-group", disc.mb_release_group_id)
        if art is not None:
            return art

    if disc.discogs_release_id and os.environ.get("DISCOGS_TOKEN"):
        art = _discogs_cached(disc.discogs_release_id)
        if art is not None:
            return art

    return None


def cover_from_file_tags(path: Path) -> CoverArt | None:
    """Extract front-cover art from embedded audio tags (mutagen).

    Prefers COVER_FRONT (type 3); falls back to the first embedded picture.
    Used by the create pipeline as a zero-network art source.
    """
    try:
        tags = _MutagenFile(str(path))
        if tags is None:
            return None
        # ID3 (MP3, AIFF …) — tags object has getall("APIC")
        id3 = getattr(tags, "tags", None)
        if id3 is not None and hasattr(id3, "getall"):
            apics = id3.getall("APIC")
            if apics:
                pic = next(
                    (a for a in apics if getattr(a, "type", None) == 3), apics[0]
                )
                fmt, w, h = sniff(pic.data)
                return CoverArt(
                    data=pic.data, fmt=fmt, width=w, height=h, source="file:embedded"
                )
        # FLAC / Ogg Vorbis — tags object has .pictures list
        if hasattr(tags, "pictures"):
            pics = tags.pictures
            if pics:
                pic = next((p for p in pics if getattr(p, "type", None) == 3), pics[0])
                fmt, w, h = sniff(pic.data)
                return CoverArt(
                    data=pic.data, fmt=fmt, width=w, height=h, source="file:embedded"
                )
    except Exception as exc:
        log.debug("cover_from_file_tags %s: %s", path.name, exc)
    return None


# ---------------------------------------------------------------------------
# PyAV image transcoding
# ---------------------------------------------------------------------------

# Maps sniff() format strings to PyAV raw-codec decoder names.
# gif and webp are included so common Discogs/embedded-tag formats transcode cleanly,
# but the JPEG guard in to_album_art() is the load-bearing correctness check — not this map.
_CODEC_FOR: dict[str, str] = {
    "jpeg": "mjpeg",
    "png": "png",
    "gif": "gif",
    "webp": "webp",
}


def _decode_frame(data: bytes, fmt: str) -> VideoFrame | None:
    codec_name = _CODEC_FOR.get(fmt)
    if not codec_name:
        return None
    try:
        # cast: "mjpeg"/"png" are video codecs but not in PyAV's _VideoCodecName stub literal
        dec = cast(VideoCodecContext, av.CodecContext.create(codec_name, "r"))
        frames = list(dec.decode(av.Packet(data)))
        return frames[0] if frames else None
    except Exception as exc:
        log.debug("_decode_frame(%s): %s", fmt, exc)
        return None


def _encode_jpeg_from_frame(frame: VideoFrame) -> bytes:
    frame = frame.reformat(format="yuvj420p")
    # cast: "mjpeg" is a video codec but not in PyAV's _VideoCodecName stub literal
    enc = cast(VideoCodecContext, av.CodecContext.create("mjpeg", "w"))
    enc.width = frame.width
    enc.height = frame.height
    enc.pix_fmt = "yuvj420p"
    packets = enc.encode(frame) + enc.encode(None)
    return b"".join(bytes(p) for p in packets)


def transcode_to_jpeg(data: bytes, fmt: str) -> bytes:
    """Convert image to JPEG via PyAV. Identity when already JPEG (no re-encode).

    Falls back to returning the original bytes when the format is unsupported
    or decoding fails — the caller can still store the image.
    """
    if fmt == "jpeg":
        return data
    frame = _decode_frame(data, fmt)
    if frame is None:
        log.debug("transcode_to_jpeg: cannot decode %s — storing as-is", fmt)
        return data
    try:
        return _encode_jpeg_from_frame(frame)
    except Exception as exc:
        log.warning("transcode_to_jpeg: encode failed (%s) — storing as-is", exc)
        return data


def downscale_jpeg(jpeg: bytes, max_edge: int = 600) -> bytes:
    """Downscale a JPEG to max_edge px on the longest side. No-op if already fits.

    Produces the ~600 px copy embedded in FLAC PICTURE blocks (--embedart).
    """
    frame = _decode_frame(jpeg, "jpeg")
    if frame is None:
        return jpeg
    w, h = frame.width, frame.height
    if max(w, h) <= max_edge:
        return jpeg
    if w >= h:
        new_w = max_edge
        new_h = max(1, round(h * max_edge / w))
    else:
        new_h = max_edge
        new_w = max(1, round(w * max_edge / h))
    try:
        frame = frame.reformat(width=new_w, height=new_h, format="yuvj420p")
        return _encode_jpeg_from_frame(frame)
    except Exception as exc:
        log.warning("downscale_jpeg: reformat failed (%s) — returning original", exc)
        return jpeg


# ---------------------------------------------------------------------------
# RBI container packaging
# ---------------------------------------------------------------------------


def to_album_art(art: CoverArt) -> RBIAlbumArt | None:
    """Convert CoverArt to RBIAlbumArt, transcoding to JPEG if needed.

    Returns None when transcoding fails to produce a JPEG (e.g. unknown format).
    """
    jpeg = transcode_to_jpeg(art.data, art.fmt)
    fmt, w, h = sniff(jpeg)
    if fmt != "jpeg":
        log.warning("to_album_art: transcoded result is %s, not jpeg — skipping", fmt)
        return None
    return RBIAlbumArt(
        art_version=ART_BLOCK_VERSION,
        image_format=ART_IMAGE_FORMAT_JPEG,
        width=w or 0,
        height=h or 0,
        image_data=jpeg,
    )


# ---------------------------------------------------------------------------
# Terminal rendering
# ---------------------------------------------------------------------------


def _pick_renderer() -> str | None:
    for binary in ("chafa", "timg", "kitten"):
        if shutil.which(binary):
            return binary
    return None


def _render_argv(binary: str, path: Path, size: str) -> list[str]:
    if binary == "chafa":
        return ["chafa", "--size", size, str(path)]
    if binary == "timg":
        return ["timg", "-g", size, str(path)]
    # kitten icat has no cell-box geometry flag; fits to window (last resort)
    return ["kitten", "icat", "--align", "left", str(path)]


def render_cover(
    art: CoverArt | RBIAlbumArt, size: str = _DEFAULT_SIZE, left_pad: int = 0
) -> bool:
    """Display art inline in the terminal. Returns True if a renderer ran.

    *left_pad* prepends that many spaces to every output line for chafa/timg.
    kitten icat uses the Kitty graphics protocol (APC sequences) which cannot
    be prefixed safely, so *left_pad* is silently ignored for that renderer.

    Best-effort: logs and returns False on any failure (missing renderer,
    subprocess error, etc.).
    """
    if isinstance(art, RBIAlbumArt):
        data = art.image_data
        fmt = "jpeg" if art.image_format == ART_IMAGE_FORMAT_JPEG else "unknown"
    else:
        data = art.data
        fmt = art.fmt

    renderer = _pick_renderer()
    if renderer is None:
        log.debug("render_cover: no terminal image renderer found")
        return False

    ext = _ext_for(fmt)
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
            tf.write(data)
            tmp = Path(tf.name)
        try:
            argv = _render_argv(renderer, tmp, size)
            if left_pad > 0 and renderer != "kitten":
                result = subprocess.run(  # noqa: S603
                    argv, capture_output=True, check=False
                )
                prefix = " " * left_pad
                text = result.stdout.decode("utf-8", errors="replace")
                import sys

                sys.stdout.write(
                    prefix + text.rstrip("\n").replace("\n", "\n" + prefix) + "\n"
                )
                sys.stdout.flush()
            else:
                subprocess.run(argv, check=False)  # noqa: S603
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("render_cover: %s", exc)
        return False
    return True
