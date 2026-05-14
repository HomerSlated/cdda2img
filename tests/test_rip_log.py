"""Tests for rip_log.py — RipLogBuilder and RLOG block SHA-256 self-seal."""

from __future__ import annotations

import hashlib
from pathlib import Path

from cdda2img.accuraterip import ARTrackResult
from cdda2img.rbi_format import RBIDisc, RBITocEntry
from cdda2img.rip_log import RipLogBuilder


def _make_disc(n_tracks: int = 2) -> RBIDisc:
    tracks = []
    frame = 150  # standard 2s pre-gap before track 1 audio
    for i in range(n_tracks):
        t = RBITocEntry(
            track_number=i + 1,
            title=f"Track {i + 1}",
            performer="Test Artist",
            start_frame=frame,
            duration_frames=75 * 60 * 3,  # 3 minutes
        )
        tracks.append(t)
        frame += t.duration_frames
    return RBIDisc(album="Test Album", artist="Test Artist", tracks=tracks)


def _make_ar_ok(n: int) -> list[ARTrackResult]:
    return [
        ARTrackResult(
            track=i + 1,
            v1_crc="aabbccdd",
            v2_crc="11223344",
            confidence_v1=5,
            confidence_v2=None,
            max_confidence=10,
            total_confidence=30,
        )
        for i in range(n)
    ]


def _make_ar_not_in_db(n: int) -> list[ARTrackResult]:
    return [
        ARTrackResult(
            track=i + 1,
            v1_crc="00000000",
            v2_crc="00000000",
            confidence_v1=None,
            confidence_v2=None,
            max_confidence=None,
        )
        for i in range(n)
    ]


def _make_ar_mismatch(n: int) -> list[ARTrackResult]:
    return [
        ARTrackResult(
            track=i + 1,
            v1_crc="deadbeef",
            v2_crc="cafebabe",
            confidence_v1=None,
            confidence_v2=None,
            max_confidence=8,
            total_confidence=20,
        )
        for i in range(n)
    ]


class TestRipLogBuilderSeal:
    def test_seal_verifies_for_ok_results(self) -> None:
        builder = RipLogBuilder(
            rip_type="cdrdao", drive_name="PLEXTOR DVD-R PX-716A", read_offset=30
        )
        builder.ar_results = _make_ar_ok(2)
        builder.cddb_id = 0xABCD1234

        block = builder.finalize(_make_disc(2))
        lines = block.split(b"\n")

        # Block ends with "\n" so last element is empty
        assert lines[-1] == b""
        # Second-to-last must be the SHA-256 line
        seal_line = lines[-2]
        assert seal_line.startswith(b"SHA-256: ")
        stored_hex = seal_line[len(b"SHA-256: ") :].decode()
        assert len(stored_hex) == 64

        # Body is everything before the SHA-256 line, plus trailing \n
        body = b"\n".join(lines[:-2]) + b"\n"
        assert hashlib.sha256(body).hexdigest() == stored_hex

    def test_seal_verifies_for_not_in_db(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        builder.ar_results = _make_ar_not_in_db(1)

        block = builder.finalize(_make_disc(1))
        lines = block.split(b"\n")
        seal_line = lines[-2]
        stored_hex = seal_line[len(b"SHA-256: ") :].decode()
        body = b"\n".join(lines[:-2]) + b"\n"
        assert hashlib.sha256(body).hexdigest() == stored_hex

    def test_seal_verifies_without_ar_results(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")

        block = builder.finalize(_make_disc(2))
        lines = block.split(b"\n")
        seal_line = lines[-2]
        stored_hex = seal_line[len(b"SHA-256: ") :].decode()
        body = b"\n".join(lines[:-2]) + b"\n"
        assert hashlib.sha256(body).hexdigest() == stored_hex

    def test_block_is_valid_utf8(self) -> None:
        builder = RipLogBuilder(rip_type="cd-paranoia", read_offset=-30)
        builder.ar_results = _make_ar_mismatch(2)

        block = builder.finalize(_make_disc(2))
        # Must not raise
        block.decode("utf-8")


class TestRipLogContent:
    def test_header_lines_present(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        block = builder.finalize(_make_disc(1)).decode("utf-8")

        assert "Log created by: cdda2img" in block
        assert "Log creation date:" in block

    def test_drive_present_when_set(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao", drive_name="PLEXTOR DVD-R PX-716A")
        block = builder.finalize(_make_disc(1)).decode("utf-8")

        assert "Drive: PLEXTOR DVD-R PX-716A" in block

    def test_drive_absent_when_not_set(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        block = builder.finalize(_make_disc(1)).decode("utf-8")

        assert "Drive:" not in block

    def test_read_offset_in_output(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao", read_offset=30)
        block = builder.finalize(_make_disc(1)).decode("utf-8")

        assert "Read offset correction: +30" in block

    def test_cddb_id_present_when_set(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        builder.cddb_id = 0xABCDEF12
        block = builder.finalize(_make_disc(1)).decode("utf-8")

        assert "CDDB Disc ID: abcdef12" in block

    def test_cddb_id_absent_when_not_set(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        block = builder.finalize(_make_disc(1)).decode("utf-8")

        assert "CDDB Disc ID:" not in block

    def test_artist_and_album(self) -> None:
        disc = _make_disc(1)
        builder = RipLogBuilder(rip_type="cdrdao")
        block = builder.finalize(disc).decode("utf-8")

        assert "Artist: Test Artist" in block
        assert "Title: 'Test Album'" in block

    def test_toc_section(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        block = builder.finalize(_make_disc(2)).decode("utf-8")

        assert "TOC:" in block
        assert "  1:" in block
        assert "  2:" in block
        assert "Start:" in block
        assert "Length:" in block

    def test_ar_ok_summary(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        builder.ar_results = _make_ar_ok(2)
        block = builder.finalize(_make_disc(2)).decode("utf-8")

        assert "All tracks accurately ripped" in block
        assert "Found, exact match" in block
        assert "Copy OK" in block

    def test_ar_not_in_db_summary(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        builder.ar_results = _make_ar_not_in_db(2)
        block = builder.finalize(_make_disc(2)).decode("utf-8")

        assert "Disc not present in AccurateRip database" in block
        assert "Disc not present in database" in block

    def test_ar_mismatch_summary(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        builder.ar_results = _make_ar_mismatch(2)
        block = builder.finalize(_make_disc(2)).decode("utf-8")

        assert "0/2 tracks accurately ripped" in block
        assert "Found, no match" in block
        assert "Copy error" in block

    def test_partial_mismatch_summary(self) -> None:
        """One track OK, one mismatch → N/M summary."""
        results = [
            ARTrackResult(
                track=1,
                v1_crc="aabbccdd",
                v2_crc="11223344",
                confidence_v1=5,
                confidence_v2=None,
                max_confidence=10,
            ),
            ARTrackResult(
                track=2,
                v1_crc="deadbeef",
                v2_crc="cafebabe",
                confidence_v1=None,
                confidence_v2=None,
                max_confidence=8,
            ),
        ]
        builder = RipLogBuilder(rip_type="cdrdao")
        builder.ar_results = results
        block = builder.finalize(_make_disc(2)).decode("utf-8")

        assert "1/2 tracks accurately ripped" in block

    def test_conclusive_report_footer(self) -> None:
        builder = RipLogBuilder(rip_type="cdrdao")
        block = builder.finalize(_make_disc(1)).decode("utf-8")

        assert "Health status: No errors occurred" in block
        assert "EOF: End of status report" in block


class TestRipLogContainerRoundtrip:
    def test_build_and_verify_rlog_block(self, tmp_path: Path) -> None:
        """RLOG block written by build_container passes verify_container rules 23+27."""
        from cdda2img.container import build_container, verify_container

        # Build a minimal RBI with an RLOG block
        pcm_path = tmp_path / "audio.raw"
        disc = _make_disc(2)
        pcm_bytes = bytes(
            2352 * sum(t.pregap_frames + t.duration_frames for t in disc.tracks)
        )
        pcm_path.write_bytes(pcm_bytes)

        builder = RipLogBuilder(
            rip_type="cdrdao", drive_name="TestDrive", read_offset=0
        )
        builder.ar_results = _make_ar_ok(2)
        builder.cddb_id = 0x12345678

        rlog_block = builder.finalize(disc)

        from cdda2img.toc import generate_toc

        toc_data = generate_toc(disc)

        out_path = tmp_path / "test.rbi"
        build_container(pcm_path, toc_data, disc, out_path, rlog_block=rlog_block)

        # verify_container returns True when all checks pass
        assert verify_container(out_path) is True
