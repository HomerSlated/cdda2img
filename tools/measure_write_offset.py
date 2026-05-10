#!/usr/bin/env python3
"""
measure_write_offset.py — Measure CD burn drive write offset via burn-and-read-back.

Usage (from project root):
    uv run python scripts/measure_write_offset.py [--device DEV] \\
        [--read-offset N] [--speed N]

Sign convention
---------------
    write_offset W = (found pulse position) - (expected position)
    Positive W  → drive burns audio W samples late  (audio delayed on disc).
    Negative W  → drive burns audio |W| samples early (audio ahead on disc).

Burn correction (future b subcommand) — apply -W shift to the full disc stream:
    if W > 0: trim W samples from the start of the source before burning
              (drive burns late; shift source left so content lands correctly)
    if W < 0: prepend |W| silence samples to the start before burning
              (drive burns early; shift source right so content lands correctly)
    Equivalently: correction = -W samples.  Same maths as fixoffset.py but
    applied before the burn rather than after the rip.

Each cycle burns a synthetic test signal to a blank disc, rips it back with the
read offset corrected, and measures where the known noise pulses landed.
Results accumulate in rips/write_offset_results.toml; re-run to add cycles.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import wave
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[import-not-found,no-redef]  # LINT-011

# ── test signal parameters ────────────────────────────────────────────────────

_SAMPLE_RATE = 44100
_CHANNELS = 2
_SAMPLE_WIDTH = 2  # bytes per channel per sample (s16)
_FRAME_BYTES = _CHANNELS * _SAMPLE_WIDTH  # 4 bytes per stereo sample pair

_DURATION_S = 75
_DURATION = _DURATION_S * _SAMPLE_RATE  # 3_307_500 stereo sample pairs

# Both pulses are well inside the 2940-sample AccurateRip exclusion zone boundary
# (i.e. > 2940 samples from disc start/end), so a real AR verification would see them.
_PULSE_A = 1 * _SAMPLE_RATE  # 44_100   (1.0 s)
_PULSE_B = 60 * _SAMPLE_RATE  # 2_646_000 (60.0 s)
_PULSE_LEN = 588  # one CD frame; sharp enough to locate precisely
_PULSE_SEED = 42  # deterministic — same signal every run

_SEARCH_WINDOW = 8820  # ±samples around expected position when scanning
_RMS_THRESHOLD = 500.0  # above noise floor, below any clipping artefact


# ── test signal generation ────────────────────────────────────────────────────


def _generate_test_signal(wav_path: Path, toc_path: Path) -> None:
    """Write 75-second WAV with noise bursts at two known sample positions."""
    rng = np.random.default_rng(_PULSE_SEED)
    pulse = rng.integers(-32767, 32767, (_PULSE_LEN, _CHANNELS), dtype=np.int16)

    audio = np.zeros((_DURATION, _CHANNELS), dtype=np.int16)
    audio[_PULSE_A : _PULSE_A + _PULSE_LEN] = pulse
    audio[_PULSE_B : _PULSE_B + _PULSE_LEN] = pulse

    wav_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(_CHANNELS)
        wf.setsampwidth(_SAMPLE_WIDTH)
        wf.setframerate(_SAMPLE_RATE)
        wf.writeframes(audio.tobytes())

    # cdrdao FILE path is relative to the TOC file's directory
    toc_path.write_text(
        "CD_DA\n\n"
        "TRACK AUDIO\n"
        "  NO COPY\n"
        "  NO PRE_EMPHASIS\n"
        "  TWO_CHANNEL_AUDIO\n"
        f'  FILE "{wav_path.name}" 0\n'
    )
    print(
        f"  Generated {wav_path.name}  "
        f"({_DURATION_S}s, pulses at "
        f"{_PULSE_A / _SAMPLE_RATE:.1f}s and {_PULSE_B / _SAMPLE_RATE:.1f}s)"
    )


# ── cdrdao wrappers ───────────────────────────────────────────────────────────


def _burn(toc_path: Path, device: str, speed: int) -> None:
    # cwd=TOC directory so cdrdao resolves FILE "test.wav" relative to it, not CWD
    result = subprocess.run(  # noqa: S603  # LINT-013
        [  # noqa: S607  # LINT-013
            "cdrdao",
            "write",
            "--device",
            device,
            "--speed",
            str(speed),
            "--eject",
            toc_path.name,
        ],
        cwd=str(toc_path.parent.resolve()),
    )
    if result.returncode != 0:
        msg = f"cdrdao write failed (exit {result.returncode})"
        raise RuntimeError(msg)


def _rip(device: str, bin_path: Path, toc_path: Path) -> None:
    bin_path.unlink(missing_ok=True)
    toc_path.unlink(missing_ok=True)
    result = subprocess.run(  # noqa: S603  # LINT-013
        [  # noqa: S607  # LINT-013
            "cdrdao",
            "read-cd",
            "--device",
            device,
            "--datafile",
            str(bin_path.resolve()),
            str(toc_path.resolve()),
        ],
    )
    if result.returncode != 0:
        msg = f"cdrdao read-cd failed (exit {result.returncode})"
        raise RuntimeError(msg)


def _eject(device: str) -> None:
    subprocess.run(["eject", device], check=False)  # noqa: S603, S607


# ── PCM analysis ──────────────────────────────────────────────────────────────


def _swap_be_to_le(data: bytes) -> bytes:
    """Byte-swap s16be → s16le. cdrdao BIN output is big-endian."""
    return np.frombuffer(data, dtype=np.int16).byteswap().tobytes()


def _apply_read_offset(pcm: bytes, read_offset: int) -> bytes:
    """
    Correct for drive read offset (positive = reads early = extra samples at start).
    Mirrors the zero-padding logic in accuraterip.py verify_rip().
    """
    shift = read_offset * _FRAME_BYTES
    if shift > 0:
        return pcm[shift:] + bytes(shift)
    if shift < 0:
        return bytes(-shift) + pcm[: len(pcm) + shift]
    return pcm


def _find_pulse(pcm: bytes, expected: int) -> int | None:
    """
    Locate the noise burst near expected sample position.
    Scans a ±_SEARCH_WINDOW window; returns the first sample above the RMS
    threshold, or None if the pulse is not found.
    """
    arr = np.frombuffer(pcm, dtype=np.int16).reshape(-1, _CHANNELS)
    lo = max(0, expected - _SEARCH_WINDOW)
    hi = min(len(arr), expected + _SEARCH_WINDOW + _PULSE_LEN)
    window = arr[lo:hi].astype(np.float32)
    rms = np.sqrt(np.mean(window**2, axis=1))
    above = np.where(rms > _RMS_THRESHOLD)[0]
    return int(lo + above[0]) if len(above) else None


def _analyse(bin_path: Path, read_offset: int) -> dict | None:
    """
    Compute write offset from a ripped BIN file.
    Returns a cycle result dict, or None if pulse detection fails.
    """
    pcm = _apply_read_offset(_swap_be_to_le(bin_path.read_bytes()), read_offset)

    pos_a = _find_pulse(pcm, _PULSE_A)
    pos_b = _find_pulse(pcm, _PULSE_B)

    if pos_a is None or pos_b is None:
        print(f"  Pulse detection failed (A={pos_a}  B={pos_b}) — skipping cycle")
        return None

    off_a = pos_a - _PULSE_A
    off_b = pos_b - _PULSE_B
    consistent = off_a == off_b

    print(f"  Pulse A  expected {_PULSE_A:>9}  found {pos_a:>9}  offset {off_a:+d}")
    print(f"  Pulse B  expected {_PULSE_B:>9}  found {pos_b:>9}  offset {off_b:+d}")
    if not consistent:
        print(
            f"  WARNING: A and B disagree ({off_a:+d} vs {off_b:+d})"
            " — disc may be defective"
        )

    return {
        "date": datetime.now(timezone.utc).isoformat(),
        "pulse_a_expected": _PULSE_A,
        "pulse_a_found": pos_a,
        "pulse_b_expected": _PULSE_B,
        "pulse_b_found": pos_b,
        "measured_offset": off_a,
        "internally_consistent": consistent,
    }


# ── summary ───────────────────────────────────────────────────────────────────


def _summarise(cycles: list[dict]) -> dict:
    offsets = [c["measured_offset"] for c in cycles]
    if not offsets:
        return {"tests": 0, "variance": False, "write_offset": 0, "confidence": 0}
    counts = Counter(offsets)
    best, n = counts.most_common(1)[0]
    return {
        "tests": len(offsets),
        "variance": len(counts) > 1,
        "write_offset": best,
        "confidence": round(n / len(offsets) * 100),
    }


# ── TOML I/O ──────────────────────────────────────────────────────────────────


def _load(path: Path) -> dict:
    if not path.exists():
        return {"drive": {}, "cycles": [], "summary": {}}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    data.setdefault("cycles", [])
    return data


def _bool(v: bool) -> str:
    return "true" if v else "false"


def _save(path: Path, data: dict) -> None:
    drive = data.get("drive", {})
    lines = [
        "[drive]",
        f'name = "{drive.get("name", "")}"',
        f"read_offset = {drive.get('read_offset', 0)}",
        "",
    ]
    for c in data.get("cycles", []):
        lines += [
            "[[cycles]]",
            f'date = "{c["date"]}"',
            f"pulse_a_expected = {c['pulse_a_expected']}",
            f"pulse_a_found = {c['pulse_a_found']}",
            f"pulse_b_expected = {c['pulse_b_expected']}",
            f"pulse_b_found = {c['pulse_b_found']}",
            f"measured_offset = {c['measured_offset']}",
            f"internally_consistent = {_bool(c['internally_consistent'])}",
            "",
        ]
    s = data.get("summary", {})
    lines += [
        "[summary]",
        f"tests = {s.get('tests', 0)}",
        f"variance = {_bool(s.get('variance', False))}",
        f"write_offset = {s.get('write_offset', 0)}",
        f"confidence = {s.get('confidence', 0)}",
        "",
    ]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)


# ── main ──────────────────────────────────────────────────────────────────────


def _run_one_cycle(
    results: dict,
    device: str,
    read_offset: int,
    speed: int,
    toc: Path,
    ripped_bin: Path,
    ripped_toc: Path,
    results_path: Path,
) -> bool:
    """Run one burn-rip-analyse cycle. Returns False when the user wants to quit."""
    print(f"\n{'─' * 60}")
    print(
        f"Cycle {len(results['cycles']) + 1}  [device={device}  read_offset={read_offset:+d}]"
    )
    if (
        input("Insert a blank disc and press Enter, or 'q' to quit: ").strip().lower()
        == "q"
    ):
        return False

    print("Burning...")
    try:
        _burn(toc, device, speed)
    except RuntimeError as exc:
        print(f"  {exc}")
        return True

    input("Disc ejected.  Reinsert the burned disc and press Enter: ")

    print("Ripping...")
    try:
        _rip(device, ripped_bin, ripped_toc)
    except RuntimeError as exc:
        print(f"  {exc}")
        _eject(device)
        return True

    _eject(device)

    cycle = _analyse(ripped_bin, read_offset)
    if cycle is None:
        return True

    results["cycles"].append(cycle)
    results["summary"] = _summarise(results["cycles"])
    _save(results_path, results)

    s = results["summary"]
    print(
        f"\n  → write_offset = {s['write_offset']:+d}  ({s['tests']} test(s), {s['confidence']}% confidence)"
    )
    if s["variance"]:
        print("  WARNING: variance detected — consider more cycles")

    return input("Another disc? [Enter / q]: ").strip().lower() != "q"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Measure CD burn drive write offset via burn-and-read-back.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run from the project root.  Results accumulate in\n"
            "rips/write_offset_results.toml across sessions."
        ),
    )
    ap.add_argument(
        "--device",
        default="/dev/sr0",
        metavar="DEV",
        help="CD drive device (default: /dev/sr0)",
    )
    ap.add_argument(
        "--read-offset",
        type=int,
        default=0,
        metavar="N",
        help="Drive read offset in samples — check cdda2img config (default: 0)",
    )
    ap.add_argument(
        "--speed",
        type=int,
        default=4,
        metavar="N",
        help="Burn speed (default: 4)",
    )
    args = ap.parse_args()

    work = Path("rips/write_offset")
    results_path = Path("rips/write_offset_results.toml")
    wav = work / "test.wav"
    toc = work / "test.toc"
    ripped_bin = work / "ripped.bin"
    ripped_toc = work / "ripped.toc"

    if not wav.exists() or not toc.exists():
        print("Generating test signal...")
        _generate_test_signal(wav, toc)

    results = _load(results_path)
    results.setdefault("drive", {})["read_offset"] = args.read_offset

    if results["cycles"]:
        s = results.get("summary", {})
        print(
            f"Resuming: {s.get('tests', 0)} existing test(s)  "
            f"write_offset={s.get('write_offset', '?'):+}  "
            f"confidence={s.get('confidence', 0)}%"
        )

    try:
        while _run_one_cycle(
            results,
            args.device,
            args.read_offset,
            args.speed,
            toc,
            ripped_bin,
            ripped_toc,
            results_path,
        ):
            pass
    except KeyboardInterrupt:
        print()

    if results["cycles"]:
        s = results["summary"]
        print(f"\n{'═' * 60}")
        print(f"  Tests:        {s['tests']}")
        print(f"  Write offset: {s['write_offset']:+d} samples")
        print(f"  Variance:     {s['variance']}")
        print(f"  Confidence:   {s['confidence']}%")
        print(f"\n  Results saved to {results_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
