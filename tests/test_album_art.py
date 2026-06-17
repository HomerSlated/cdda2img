"""Tests for album_art.py — sniff, transcode, downscale, to_album_art, render_cover."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import av
import blake3
from av.video.codeccontext import VideoCodecContext

from cdda2img.album_art import (
    CoverArt,
    downscale_jpeg,
    render_cover,
    sniff,
    to_album_art,
    transcode_to_jpeg,
)
from cdda2img.rbi_format import (
    ART_BLOCK_VERSION,
    ART_IMAGE_FORMAT_JPEG,
    BLOCK_TYPE_ART,
    RBIAlbumArt,
    RBIDisc,
    RBITocEntry,
)


def _make_jpeg(w: int, h: int) -> bytes:
    """Synthesise a minimal valid JPEG of size w x h via PyAV."""
    enc = cast(VideoCodecContext, av.CodecContext.create("mjpeg", "w"))
    enc.width = w
    enc.height = h
    enc.pix_fmt = "yuvj420p"
    frame = av.VideoFrame(w, h, "rgb24").reformat(format="yuvj420p")
    packets = list(enc.encode(frame)) + list(enc.encode(None))
    return b"".join(bytes(p) for p in packets)


def _make_disc(n_tracks: int = 1) -> RBIDisc:
    tracks = []
    frame = 150
    for i in range(n_tracks):
        t = RBITocEntry(
            track_number=i + 1,
            title=f"Track {i + 1}",
            performer="Test Artist",
            start_frame=frame,
            duration_frames=75 * 30,  # 30 seconds
        )
        tracks.append(t)
        frame += t.duration_frames
    return RBIDisc(album="Test Album", artist="Test Artist", tracks=tracks)


class TestSniff:
    def test_jpeg_magic(self) -> None:
        fmt, _, _ = sniff(b"\xff\xd8\xff" + b"\x00" * 20)
        assert fmt == "jpeg"

    def test_png_magic(self) -> None:
        fmt, _, _ = sniff(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        assert fmt == "png"

    def test_unknown(self) -> None:
        fmt, w, h = sniff(b"garbage bytes")
        assert fmt == "unknown"
        assert w is None
        assert h is None

    def test_jpeg_dims_from_real_bytes(self) -> None:
        jpeg = _make_jpeg(32, 32)
        fmt, w, h = sniff(jpeg)
        assert fmt == "jpeg"
        assert w == 32
        assert h == 32


class TestTranscodeToJpeg:
    def test_jpeg_is_identity(self) -> None:
        jpeg = _make_jpeg(32, 32)
        result = transcode_to_jpeg(jpeg, "jpeg")
        assert result is jpeg  # same object — no re-encode


class TestToAlbumArt:
    def test_jpeg_roundtrip(self) -> None:
        jpeg = _make_jpeg(64, 64)
        art = CoverArt(data=jpeg, fmt="jpeg", width=64, height=64, source="test")
        rbi = to_album_art(art)
        assert rbi is not None
        assert rbi.image_format == ART_IMAGE_FORMAT_JPEG
        assert rbi.art_version == ART_BLOCK_VERSION
        assert rbi.image_data == jpeg
        assert rbi.width == 64
        assert rbi.height == 64

    def test_unknown_format_returns_none(self) -> None:
        art = CoverArt(
            data=b"garbage", fmt="unknown", width=None, height=None, source="t"
        )
        rbi = to_album_art(art)
        assert rbi is None


class TestDownscaleJpeg:
    def test_small_jpeg_unchanged(self) -> None:
        jpeg = _make_jpeg(200, 200)
        result = downscale_jpeg(jpeg, max_edge=600)
        fmt, w, h = sniff(result)
        assert fmt == "jpeg"
        assert w == 200
        assert h == 200

    def test_wide_jpeg_downscaled(self) -> None:
        jpeg = _make_jpeg(1200, 900)
        result = downscale_jpeg(jpeg, max_edge=600)
        fmt, w, _ = sniff(result)
        assert fmt == "jpeg"
        assert w is not None and w <= 600

    def test_tall_jpeg_downscaled(self) -> None:
        jpeg = _make_jpeg(900, 1200)
        result = downscale_jpeg(jpeg, max_edge=600)
        fmt, _, h = sniff(result)
        assert fmt == "jpeg"
        assert h is not None and h <= 600


class TestRenderCover:
    def test_no_renderer_returns_false(self) -> None:
        art = CoverArt(
            data=b"\xff\xd8\xff" + b"\x00" * 20,
            fmt="jpeg",
            width=None,
            height=None,
            source="test",
        )
        with patch("cdda2img.album_art._pick_renderer", return_value=None):
            assert render_cover(art) is False

    def test_renderer_runs_subprocess_returns_true(self) -> None:
        jpeg = _make_jpeg(32, 32)
        art = CoverArt(data=jpeg, fmt="jpeg", width=32, height=32, source="test")
        with (
            patch("cdda2img.album_art._pick_renderer", return_value="chafa"),
            patch("cdda2img.album_art.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            assert render_cover(art) is True


class TestArtBlockRoundtrip:
    def test_build_container_with_art_block(self, tmp_path: Path) -> None:
        from cdda2img.container import build_container, read_header, verify_container
        from cdda2img.toc import generate_toc

        disc = _make_disc(1)
        pcm_path = tmp_path / "audio.raw"
        pcm_bytes = bytes(
            2352 * sum(t.pregap_frames + t.duration_frames for t in disc.tracks)
        )
        pcm_path.write_bytes(pcm_bytes)

        jpeg = _make_jpeg(64, 64)
        rbi_art = RBIAlbumArt(
            art_version=ART_BLOCK_VERSION,
            image_format=ART_IMAGE_FORMAT_JPEG,
            width=64,
            height=64,
            image_data=jpeg,
        )
        toc_data = generate_toc(disc)
        out_path = tmp_path / "test_art.rbi"
        build_container(pcm_path, toc_data, disc, out_path, album_art=rbi_art)

        header = read_header(out_path)
        art_entry = header.find_block(BLOCK_TYPE_ART)
        assert art_entry is not None, "ART block not found in directory"

        with open(out_path, "rb") as f:
            f.seek(art_entry.offset)
            block_bytes = f.read(art_entry.length)
        assert art_entry.checksum == blake3.blake3(block_bytes).digest()

        assert verify_container(out_path) is True


# ---------------------------------------------------------------------------
# OPT-2 — in-process cover fetch cache
# ---------------------------------------------------------------------------


def test_fetch_cover_caches_success(monkeypatch) -> None:
    # A successful fetch is memoised per source; the second fetch_cover with the
    # same IDs is served from cache (no re-download).
    from cdda2img import album_art

    album_art._COVER_CACHE.clear()
    calls = {"n": 0}
    art = CoverArt(
        data=b"x", fmt="jpeg", width=1, height=1, source="caa:release-group:rg-x"
    )

    def _fake_try(_entity, _mbid):
        calls["n"] += 1
        return art

    monkeypatch.setattr(album_art, "_try_caa", _fake_try)
    disc = RBIDisc(album="a", artist="b", mb_release_group_id="rg-x")
    assert album_art.fetch_cover(disc) is art
    assert album_art.fetch_cover(disc) is art
    assert calls["n"] == 1  # second call served from cache


def test_fetch_cover_does_not_cache_miss(monkeypatch) -> None:
    # A miss (None) is a cheap 404 and is NOT cached — the next call retries.
    from cdda2img import album_art

    album_art._COVER_CACHE.clear()
    calls = {"n": 0}

    def _fake_try(_entity, _mbid):
        calls["n"] += 1
        return None

    monkeypatch.setattr(album_art, "_try_caa", _fake_try)
    disc = RBIDisc(album="a", artist="b", mb_release_group_id="rg-x")
    assert album_art.fetch_cover(disc) is None
    assert album_art.fetch_cover(disc) is None
    assert calls["n"] == 2  # miss not cached
