"""subq_toc: RipInfo assembly from single-pass c2read captures (F7)."""

from __future__ import annotations

from pathlib import Path

from cdda2img.cdtext import PTI_TITLE, CDTextBlock
from cdda2img.subchannel import ADR_POSITION, CD_SUBCODE_SIZE, crc16_gsm
from cdda2img.subq_toc import _cdtext_matches_disc, build_rip_info

# ---------------------------------------------------------------------------
# Synthetic capture builders (Q frames mirror tests/test_subchannel.py)
# ---------------------------------------------------------------------------


def _pack_q(q: bytes) -> bytes:
    sector = bytearray(CD_SUBCODE_SIZE)
    for i in range(CD_SUBCODE_SIZE):
        if q[i >> 3] & (1 << (7 - (i & 7))):
            sector[i] |= 0x40
    return bytes(sector)


def _bcd_enc(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


def _position(track: int, lba: int, index: int = 1, control: int = 0) -> bytes:
    frames = lba + 150
    amin, rem = divmod(frames, 60 * 75)
    asec, aframe = divmod(rem, 75)
    payload = bytes([
        _bcd_enc(track),
        _bcd_enc(index),
        0,
        0,
        0,
        0,
        _bcd_enc(amin),
        _bcd_enc(asec),
        _bcd_enc(aframe),
    ])
    head = bytes([(control << 4) | ADR_POSITION]) + payload
    crc = crc16_gsm(head)
    return _pack_q(head + bytes([crc >> 8, crc & 0xFF]))


def _sub_stream(*segments: tuple[int, int, int, int]) -> bytes:
    out: list[bytes] = []
    lba = 0
    for track, index, control, n in segments:
        for _ in range(n):
            out.append(_position(track, lba, index=index, control=control))
            lba += 1
    return b"".join(out)


def _ftd(session: int, point: int, lba_or_pmin: tuple[int, int, int]) -> bytes:
    return bytes([session, 0x10, 0x00, point, 0, 0, 0, 0, *lba_or_pmin])


def _msf(lba: int) -> tuple[int, int, int]:
    frames = lba + 150
    m, rem = divmod(frames, 60 * 75)
    s, f = divmod(rem, 75)
    return (m, s, f)


def _fulltoc(track_starts: dict[int, int], leadout: int) -> bytes:
    hdr = bytes([0x00, 0x00, 0x01, 0x01])
    body = _ftd(1, 0xA0, (min(track_starts), 0x00, 0))
    body += _ftd(1, 0xA1, (max(track_starts), 0, 0))
    body += _ftd(1, 0xA2, _msf(leadout))
    for n, start in sorted(track_starts.items()):
        body += _ftd(1, n, _msf(start))
    return hdr + body


# ---------------------------------------------------------------------------
# Synthetic assembly
# ---------------------------------------------------------------------------

# 2 tracks: t1 audio [0,10), t2 pregap [10,15) + audio [15,30); leadout 30.
_TOC = _fulltoc({1: 0, 2: 15}, 30)
_SUB = _sub_stream((1, 1, 0, 10), (2, 0, 0, 5), (2, 1, 0, 15))


def test_build_geometry_and_pregap():
    info = build_rip_info(_TOC, _SUB)
    t1, t2 = info.disc.tracks
    assert (t1.start_frame, t1.pregap_frames, t1.duration_frames) == (0, 0, 10)
    assert (t2.start_frame, t2.pregap_frames, t2.duration_frames) == (10, 5, 15)
    assert info.track_lsns == [0, 15]
    assert info.disc_last_lsn == 29
    assert info.prov is not None
    assert info.prov["toc_source"] == "subq@c2read"


def test_build_track1_pregap_never_set():
    # Track 1's pre-gap precedes LBA 0; even a (bogus) index-00 span for track 1
    # in the stream must not produce one.
    sub = _sub_stream((1, 0, 0, 3), (1, 1, 0, 12), (2, 1, 0, 15))
    info = build_rip_info(_fulltoc({1: 3, 2: 15}, 30), sub)
    assert info.disc.tracks[0].pregap_frames == 0


def test_build_control_flags_and_aggregate():
    sub = _sub_stream((1, 1, 0x1, 10), (2, 0, 0x2, 5), (2, 1, 0x2, 15))
    info = build_rip_info(_TOC, sub)
    t1, t2 = info.disc.tracks
    assert t1.pre_emphasis is True
    assert t2.copy_permitted is True
    assert info.disc.pre_emphasis is True


def test_build_index_points_relative_to_audio_start():
    # INDEX 02 span starting 5 frames into track 2's audio.
    sub = _sub_stream((1, 1, 0, 10), (2, 0, 0, 5), (2, 1, 0, 5), (2, 2, 0, 10))
    info = build_rip_info(_TOC, sub)
    assert info.disc.tracks[1].index_points == [5]


def test_build_degrades_without_anchorable_sub():
    info = build_rip_info(_TOC, b"\x00" * (CD_SUBCODE_SIZE * 4))
    _t1, t2 = info.disc.tracks
    assert (t2.start_frame, t2.pregap_frames) == (15, 0)  # TOC-only geometry
    assert info.disc.pre_emphasis is None  # uncaptured, not False
    assert info.prov is not None
    assert info.prov["subq_layout"] == "unanchored"


# ---------------------------------------------------------------------------
# Real captures (PX-716A, Tracy Chapman disc) — cross-checked against a fresh
# cdrdao read-toc of the same disc (tools/toc_parity.py: ALL MATCH 2026-07-04)
# ---------------------------------------------------------------------------


def test_real_fulltoc_with_boundary_sub_slice(fixtures_dir=None):
    fixtures = Path(__file__).parent / "fixtures"
    fulltoc = (fixtures / "tracy.fulltoc").read_bytes()
    sub = (fixtures / "subq_track2_boundary.sub").read_bytes()
    info = build_rip_info(fulltoc, sub)
    assert len(info.disc.tracks) == 11
    assert info.disc.catalog == "7559607740206"  # 3 MCN frames clear the floor
    t2 = info.disc.tracks[1]
    # The 300-sector slice covers the track 1->2 boundary: the real 52-frame
    # pre-gap is derived; tracks outside the slice get TOC-only geometry.
    assert (t2.start_frame, t2.pregap_frames) == (12032 - 52, 52)
    assert info.disc.tracks[2].pregap_frames == 0
    assert info.track_lsns[1] == 12032
    assert info.disc_last_lsn == 162891


# ---------------------------------------------------------------------------
# CD-Text <-> disc binding guard: reject a stale/foreign CD-Text sidecar whose
# track range does not describe the disc actually in the drive. Regression for
# the ABBA-Gold-tagged-as-Tracy-Chapman bug: a no-CD-Text disc read a prior
# rip's leftover all_tracks.cdtext and baked in the wrong album.
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"


def test_matching_cdtext_is_kept():
    # tracy.fulltoc (11 tracks) + the Tracy Chapman CD-Text (SIZE_INFO 1..11):
    # ranges agree, so the titles must be applied.
    fulltoc = (_FIXTURES / "tracy.fulltoc").read_bytes()
    sub = (_FIXTURES / "subq_track2_boundary.sub").read_bytes()
    cdtext = (_FIXTURES / "cdemu_utf8.cdtext").read_bytes()
    info = build_rip_info(fulltoc, sub, cdtext)
    assert info.disc.album == "Tracy Chapman"
    assert info.disc.tracks[0].title == "Talkin\u2019 Bout a Revolution"
    assert info.prov is not None
    assert "cdtext_rejected" not in info.prov


def test_mismatched_cdtext_is_discarded():
    # The same 11-track Tracy CD-Text against a 2-track disc: the range cannot
    # describe this disc, so it is dropped and no title survives.
    cdtext = (_FIXTURES / "cdemu_utf8.cdtext").read_bytes()
    info = build_rip_info(_TOC, _SUB, cdtext)
    assert info.disc.album == ""
    assert info.disc.tracks[0].title == ""
    assert info.prov is not None
    assert info.prov["cdtext_rejected"] == "track_range_mismatch"


def test_cdtext_binding_uses_size_info_range():
    block = CDTextBlock(block=0, first_track=1, last_track=11)
    assert _cdtext_matches_disc(block, set(range(1, 12)))
    assert not _cdtext_matches_disc(block, set(range(1, 20)))
    assert not _cdtext_matches_disc(block, {1, 2})


def test_cdtext_binding_falls_back_to_titled_tracks_without_size_info():
    # No SIZE_INFO: the observed per-track titles must span the disc's range.
    titled = {PTI_TITLE: {0: "Album", 1: "a", 2: "b", 3: "c"}}
    block = CDTextBlock(block=0, text=titled)
    assert _cdtext_matches_disc(block, {1, 2, 3})
    assert not _cdtext_matches_disc(block, {1, 2, 3, 4})


def test_cdtext_binding_allows_album_only_block():
    # Album-level-only CD-Text has nothing per-track to contradict the disc.
    block = CDTextBlock(block=0, text={PTI_TITLE: {0: "Album"}})
    assert _cdtext_matches_disc(block, {1, 2, 3})
