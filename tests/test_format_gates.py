"""Reader gates that ``test``, ``list`` and ``extract`` must agree on.

rbi_spec §1 (major version), §4.2 (must-understand flag bits) and §3/§5.3 (unknown
block types need ``BLOCK_FLAG_SKIP``) are obligations on every reader. Until
2026-09-13 ``verify_container`` accepted majors 4-6 while ``read_header`` accepted
only 6, and neither enforced the unknown-block rule at all — so ``cdda2img test``
could pass a file ``list`` and ``extract`` refused.

Every case patches a known-good container in place and asserts **both** consumers.
Patching the fixed header or a directory entry leaves every block digest valid
(digests cover block content only), so the gate under test is the only thing that
can fail — and the unpatched container passing both is the control that says so.
"""

from __future__ import annotations

import inspect
import re
import struct
import wave
from pathlib import Path

import pytest

from cdda2img.concat import concat_wav
from cdda2img.container import (
    build_container,
    pad_pcm_to_declared_frames,
    read_header,
    verify_container,
    wav_to_raw_pcm,
)
from cdda2img.rbi_format import (
    BLOCK_FLAG_SKIP,
    BLOCK_TYPE_PCM,
    BLOCK_TYPE_PROV,
    BLOCK_TYPE_TOC,
    DIR_ENTRY_SIZE,
    HEADER_FIXED_SIZE,
    HEADER_STRUCT,
    VERSION_MAJOR,
    RBIDisc,
)
from cdda2img.toc import build_toc_entries, generate_toc, track_frame_durations

_VERSION_MAJOR_OFFSET = 8  # rbi_spec §4.1
_FLAGS_OFFSET = 10


@pytest.fixture
def rbi(tmp_path: Path) -> Path:
    """A small, fully valid container with an optional (PROV) block to relabel."""
    ramp = bytes(range(256))
    tracks = []
    for i, frames in enumerate((150, 225), start=1):
        path = tmp_path / f"{i:02d}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            n = frames * 2352
            w.writeframes((ramp * (n // len(ramp) + 1))[:n])
        tracks.append(path)

    durations, total_frames = track_frame_durations(tracks)
    concat = tmp_path / "all.wav"
    pcm = tmp_path / "all.pcm"
    concat_wav(tracks, concat)
    wav_to_raw_pcm(concat, pcm)
    pad_pcm_to_declared_frames(pcm, total_frames)

    disc = RBIDisc(album="Gates", artist="Test", disc_number=1, disc_total=1)
    disc.tracks = build_toc_entries(tracks, durations, disc)
    out = tmp_path / "gates.rbi"
    build_container(
        pcm, generate_toc(disc), disc, out, prov_data={"mode": "create"}, quiet=True
    )
    return out


def _patch(path: Path, offset: int, fmt: str, value: object) -> None:
    with open(path, "r+b") as f:
        f.seek(offset)
        f.write(struct.pack(fmt, value))


def _entry_offset(path: Path, type_id: bytes) -> int:
    with open(path, "rb") as f:
        fields = struct.unpack(HEADER_STRUCT, f.read(HEADER_FIXED_SIZE))
        dir_offset, dir_count = fields[10], fields[11]
        f.seek(dir_offset)
        raw = f.read(dir_count * DIR_ENTRY_SIZE)
    for i in range(dir_count):
        if raw[i * DIR_ENTRY_SIZE : i * DIR_ENTRY_SIZE + 4] == type_id:
            return dir_offset + i * DIR_ENTRY_SIZE
    msg = f"no {type_id!r} entry"
    raise AssertionError(msg)


def _outcome(path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[bool, bool, str]:
    """(``test`` passes, ``list``/``extract`` can open it, verifier output)."""
    capsys.readouterr()
    verified = verify_container(path)
    out = capsys.readouterr().out
    try:
        read_header(path)
    except ValueError:
        opened = False
    else:
        opened = True
    return verified, opened, out


def test_control_unpatched_container_passes_both(rbi, capsys):
    verified, opened, out = _outcome(rbi, capsys)
    assert (verified, opened) == (True, True)
    assert "[FAIL]" not in out
    # The new rules actually ran, rather than passing by being unreachable.
    assert "[OK]   32." in out
    assert "[OK]   33." in out


@pytest.mark.parametrize("major", [4, 5, VERSION_MAJOR + 1])
def test_test_refuses_every_major_version_list_and_extract_refuse(rbi, capsys, major):
    """v5 is the discriminating case: its checksums are BLAKE3 like v6, so before
    the fix every other rule passed and ``test`` reported the file valid."""
    _patch(rbi, _VERSION_MAJOR_OFFSET, "<B", major)
    verified, opened, out = _outcome(rbi, capsys)
    assert (verified, opened) == (False, False)
    assert "[FAIL] 2. Format version major" in out


def test_unknown_must_understand_flag_is_refused_by_both(rbi, capsys):
    _patch(rbi, _FLAGS_OFFSET, "<I", 0x00000002)  # bit 1: odd position
    verified, opened, out = _outcome(rbi, capsys)
    assert (verified, opened) == (False, False)
    assert "[FAIL] 4." in out


def test_unknown_ignorable_flag_is_accepted_by_both_with_a_warning(rbi, capsys):
    """Control for the test above: the position of the bit, not its presence, is
    what decides (rbi_spec §4.2)."""
    _patch(rbi, _FLAGS_OFFSET, "<I", 0x00000010)  # bit 4: even position
    verified, opened, out = _outcome(rbi, capsys)
    assert (verified, opened) == (True, True)
    assert "[WARN] 4." in out


@pytest.mark.parametrize(
    ("block_flags", "accepted"),
    [(BLOCK_FLAG_SKIP, True), (0, False)],
    ids=["skippable", "not-skippable"],
)
def test_unknown_block_type_is_accepted_only_when_skippable(
    rbi, capsys, block_flags, accepted
):
    at = _entry_offset(rbi, BLOCK_TYPE_PROV)
    _patch(rbi, at, "4s", b"ZZZZ")
    _patch(rbi, at + 4, "<H", block_flags)
    verified, opened, out = _outcome(rbi, capsys)
    assert (verified, opened) == (accepted, accepted)
    if not accepted:
        assert "[FAIL] 32." in out


@pytest.mark.parametrize(
    ("type_id", "block_flags"),
    [
        (BLOCK_TYPE_TOC, BLOCK_FLAG_SKIP),
        (BLOCK_TYPE_PCM, BLOCK_FLAG_SKIP),
        (BLOCK_TYPE_PROV, 0),
        (BLOCK_TYPE_PROV, BLOCK_FLAG_SKIP | 0x0002),
    ],
    ids=["TOC-skippable", "PCM-skippable", "PROV-not-skippable", "PROV-reserved-bit"],
)
def test_misflagged_known_block_fails_test_but_stays_readable(
    rbi, capsys, type_id, block_flags
):
    """Rule 33 is conformance, not a read gate: a reader that defines the block
    type can read it whatever its flags say, so ``read_header`` must still open
    it. Asserted, so that tightening ``read_header`` later is a decision."""
    _patch(rbi, _entry_offset(rbi, type_id) + 4, "<H", block_flags)
    verified, opened, out = _outcome(rbi, capsys)
    assert (verified, opened) == (False, True)
    assert "[FAIL] 33." in out


def test_every_spec_rule_has_a_verifier_check_and_vice_versa():
    """The §7 list and the verifier's numbered labels are kept in step by hand;
    this makes a rule added to one without the other fail in either direction."""
    spec = Path(__file__).resolve().parents[1] / "docs" / "reference" / "rbi_spec.md"
    section = spec.read_text(encoding="utf-8").split("## 7. Validation Rules", 1)[1]
    section = section.split("\n## ", 1)[0]
    spec_rules = {int(n) for n in re.findall(r"^(\d+)\. ", section, re.MULTILINE)}
    declared = re.search(r"\((\d+) rules\)", section)
    assert declared is not None
    assert spec_rules == set(range(1, int(declared.group(1)) + 1))

    source = inspect.getsource(verify_container)
    checked = {int(n) for n in re.findall(r'(?:"|\] )(\d+)\. ', source)}
    assert checked == spec_rules
