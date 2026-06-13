#!/usr/bin/env python3
"""albumart.py — standalone album-art fetch/convert/render experiment.

A throwaway-grade probe for the planned cdda2img album-art feature. It does the
three stages in isolation, with **no RBI container integration and no rip/create
pipeline** — just enough to find the right fetch sources, the right on-the-wire
sizes, and the right terminal renderer before any of this is wired into the
real code.

Stages
------
1. fetch   — pull one front-cover image from a source (Cover Art Archive,
             Discogs, an arbitrary URL, or a local file).
2. convert — for now this is *inspect only*: sniff format + dimensions from the
             magic bytes (no Pillow in v1; every renderer below eats JPEG/PNG
             natively, so there is nothing to transcode yet).
3. render  — display it inline via the first available terminal-image backend
             (chafa -> timg -> kitten icat), sized to a small thumbnail. If no
             backend exists, skip silently — exactly the "terminal has no
             graphics support" fallback the real feature needs.

Run from the project root:

    uv run python tools/albumart.py --rgid <release-group-uuid>
    uv run python tools/albumart.py --mbid <release-uuid> --size 24x12
    uv run python tools/albumart.py --discogs 249504 --label "Thriller — Michael Jackson"
    uv run python tools/albumart.py --file /path/to/cover.png --dry-run

Note: a real graphics render only happens when stdout is an actual graphics
terminal (e.g. kitty). Piped/captured stdout will fall back to text blocks or
skip; use --dry-run to exercise just the fetch/convert path.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# Identify ourselves politely to MB/CAA/Discogs (their etiquette asks for it).
_UA = "cdda2img-albumart-experiment/0.1 (https://github.com/HomerSlated/cdda2img)"

_CAA_BASE = "https://coverartarchive.org"
_DISCOGS_API = "https://api.discogs.com"

# Renderer backends in priority order: (binary, argv-builder). chafa and timg
# both take a clean cell geometry; kitten icat fits-to-window (last resort).
_DEFAULT_SIZE = "24x12"
_DEFAULT_MAX_MB = 16


# ---------------------------------------------------------------------------
# Stage 1 — fetch
# ---------------------------------------------------------------------------


def _http_get(url: str, max_bytes: int, headers: dict[str, str] | None = None) -> bytes:
    """GET *url*, following redirects, capping the body at *max_bytes*."""
    req = urllib.request.Request(  # noqa: S310 — http/https only; --url is operator-supplied
        url, headers={"User-Agent": _UA, **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        msg = f"image exceeds cap of {max_bytes} bytes"
        raise ValueError(msg)
    return data


def fetch_caa(entity: str, mbid: str, caa_size: str, max_bytes: int) -> bytes:
    """Fetch the front cover from the Cover Art Archive.

    *entity* is "release" or "release-group". *caa_size* is one of
    250/500/1200/original; CAA serves the scaled derivative server-side, so we
    almost never have to downscale locally.
    """
    suffix = "front" if caa_size in ("", "original") else f"front-{caa_size}"
    url = f"{_CAA_BASE}/{entity}/{mbid}/{suffix}"
    return _http_get(url, max_bytes)


def fetch_discogs(release_id: str, max_bytes: int) -> bytes:
    """Fetch the primary image of a Discogs release (requires $DISCOGS_TOKEN)."""
    token = os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        msg = "DISCOGS_TOKEN not set in environment"
        raise RuntimeError(msg)
    auth = {"Authorization": f"Discogs token={token}"}
    meta_raw = _http_get(
        f"{_DISCOGS_API}/releases/{release_id}", 4_000_000, headers=auth
    )
    meta = json.loads(meta_raw)
    images = meta.get("images") or []
    if not images:
        msg = f"Discogs release {release_id} has no images"
        raise RuntimeError(msg)
    # Prefer the primary image; fall back to the first listed.
    primary = next((im for im in images if im.get("type") == "primary"), images[0])
    uri = primary.get("uri")
    if not uri:
        msg = "Discogs image entry has no uri"
        raise RuntimeError(msg)
    # The image host (i.discogs.com) also wants the token-bearing UA.
    return _http_get(uri, max_bytes, headers=auth)


# ---------------------------------------------------------------------------
# Stage 2 — convert / inspect (no transcode in v1; magic-byte sniff only)
# ---------------------------------------------------------------------------


def sniff(data: bytes) -> tuple[str, int | None, int | None]:
    """Best-effort (format, width, height) from the leading bytes.

    Returns ("jpeg"|"png"|"gif"|"webp"|"unknown", w, h); dimensions are None
    when they cannot be read cheaply (we never decode the pixels).
    """
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
        return ("webp", None, None)  # dims need chunk parsing; skip for v1
    return ("unknown", None, None)


def _jpeg_dims(data: bytes) -> tuple[int | None, int | None]:
    """Scan JPEG segment markers for an SOF frame header (best effort)."""
    i, n = 2, len(data)
    try:
        while i + 9 < n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            # SOF0..SOF15 carry frame dimensions, excluding DHT/JPG/DAC markers.
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h = int.from_bytes(data[i + 5 : i + 7], "big")
                w = int.from_bytes(data[i + 7 : i + 9], "big")
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
# Stage 3 — render
# ---------------------------------------------------------------------------


def _pick_renderer() -> str | None:
    """Return the first available terminal-image backend, or None."""
    for binary in ("chafa", "timg", "kitten"):
        if shutil.which(binary):
            return binary
    return None


def _render_argv(binary: str, path: Path, size: str) -> list[str]:
    """Build the argv for *binary* to render *path* at cell geometry *size*."""
    if binary == "chafa":
        return ["chafa", "--size", size, str(path)]
    if binary == "timg":
        return ["timg", "-g", size, str(path)]
    # kitten icat has no simple cell-box flag; it fits to the window (last resort).
    return ["kitten", "icat", "--align", "left", str(path)]


def render(data: bytes, fmt: str, size: str) -> bool:
    """Render *data* inline. Returns True if a backend ran, False if skipped."""
    binary = _pick_renderer()
    if binary is None:
        print("(no terminal image renderer found — skipping render)", file=sys.stderr)
        return False
    with tempfile.NamedTemporaryFile(suffix=_ext_for(fmt), delete=False) as tf:
        tf.write(data)
        tmp = Path(tf.name)
    try:
        # Inherit stdout so the backend can probe the real terminal and emit its
        # graphics escapes directly to it (we deliberately do NOT capture).
        subprocess.run(_render_argv(binary, tmp, size), check=False)  # noqa: S603
    finally:
        tmp.unlink(missing_ok=True)
    return True


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _fetch(args: argparse.Namespace, max_bytes: int) -> tuple[bytes, str]:
    """Dispatch to the selected source; return (bytes, human source label)."""
    if args.file:
        return Path(args.file).read_bytes(), f"file:{args.file}"
    if args.url:
        return _http_get(args.url, max_bytes), f"url:{args.url}"
    if args.mbid:
        return fetch_caa("release", args.mbid, args.caa_size, max_bytes), (
            f"caa:release:{args.mbid}"
        )
    if args.rgid:
        return fetch_caa("release-group", args.rgid, args.caa_size, max_bytes), (
            f"caa:release-group:{args.rgid}"
        )
    if args.discogs:
        return fetch_discogs(args.discogs, max_bytes), f"discogs:{args.discogs}"
    msg = "no source given (need one of --file/--url/--mbid/--rgid/--discogs)"
    raise SystemExit(msg)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Album-art fetch/convert/render probe.")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--mbid", help="MusicBrainz release UUID (CAA front)")
    src.add_argument("--rgid", help="MusicBrainz release-group UUID (CAA front)")
    src.add_argument("--discogs", help="Discogs release id (primary image)")
    src.add_argument("--url", help="arbitrary image URL")
    src.add_argument("--file", help="local image path (skip fetch)")
    p.add_argument("--size", default=_DEFAULT_SIZE, help="render cells WxH")
    p.add_argument(
        "--caa-size",
        default="1200",
        choices=["250", "500", "1200", "original"],
        help="CAA derivative size",
    )
    p.add_argument(
        "--max-mb", type=int, default=_DEFAULT_MAX_MB, help="download hard cap (MiB)"
    )
    p.add_argument("--label", default="", help="caption printed below the image")
    p.add_argument("--save", help="write fetched bytes to this path")
    p.add_argument("--dry-run", action="store_true", help="fetch+sniff, skip render")
    args = p.parse_args(argv)

    max_bytes = args.max_mb * 1024 * 1024
    try:
        data, source = _fetch(args, max_bytes)
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as exc:
        print(f"fetch failed ({source_hint(args)}): {exc}", file=sys.stderr)
        return 1

    fmt, w, h = sniff(data)
    dims = f"{w}x{h}" if w and h else "unknown"
    print(
        f"fetched {len(data):,} bytes  format={fmt}  dims={dims}  source={source}",
        file=sys.stderr,
    )

    if args.save:
        Path(args.save).write_bytes(data)
        print(f"saved: {args.save}", file=sys.stderr)

    if args.dry_run:
        return 0

    render(data, fmt, args.size)
    if args.label:
        # One-shot caption — mirrors the future "Disc: Album — Artist" line that
        # will be printed *once* before the TUI starts (not repainted inside it).
        print(f"Disc: {args.label}")
    return 0


def source_hint(args: argparse.Namespace) -> str:
    for k in ("file", "url", "mbid", "rgid", "discogs"):
        v = getattr(args, k)
        if v:
            return f"{k}={v}"
    return "?"


if __name__ == "__main__":
    raise SystemExit(main())
