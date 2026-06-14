"""
write_offset.py — Write-offset measurement logic (burn-and-read-back).

Public API used by `cdda2img setup --write-offset`.  No user prompts here;
all interactive loops live in setup.py.

Sign convention
---------------
    write_offset W = (found pulse position) - (expected position)
    Positive W  -> drive burns audio W samples late  (audio delayed on disc).
    Negative W  -> drive burns audio |W| samples early (audio ahead on disc).

Burn correction -- apply -W shift to the full disc stream before burning:
    W > 0: trim W samples from start (drive burns late → shift source left).
    W < 0: prepend |W| silence samples (drive burns early → shift source right).
"""

from __future__ import annotations

import os
import re
import subprocess
import wave
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

if __import__("sys").version_info >= (3, 11):
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

# Both pulses are well inside the AccurateRip 2940-sample exclusion zone boundary
# so a real AR verification would see them.
_PULSE_A = 1 * _SAMPLE_RATE  # 44_100   (1.0 s)
_PULSE_B = 60 * _SAMPLE_RATE  # 2_646_000 (60.0 s)
_PULSE_LEN = 588  # one CD frame; sharp enough to locate precisely
_PULSE_SEED = 42  # deterministic — same signal every run

_SEARCH_WINDOW = 8820  # ±samples around expected position when scanning
_RMS_THRESHOLD = 500.0  # above noise floor, below any clipping artefact


# ── XDG paths ─────────────────────────────────────────────────────────────────


def _xdg_data() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "cdda2img"


def work_dir() -> Path:
    """Return the persistent work directory for burn/rip scratch files."""
    return _xdg_data() / "write_offset_work"


def results_path(slug: str) -> Path:
    """Return the XDG path for the TOML results file for *slug*."""
    return _xdg_data() / f"write_offset_{slug}.toml"


# ── test signal generation ────────────────────────────────────────────────────


def generate_test_signal(wav_path: Path, toc_path: Path) -> None:
    """Write a 75-second WAV with noise bursts at two known sample positions."""
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


# ── cdrdao wrappers ───────────────────────────────────────────────────────────


def burn_disc(toc_path: Path, device: str, speed: int) -> None:
    """Burn the TOC+WAV at *toc_path* to the disc in *device*.

    cwd is set to the TOC directory so cdrdao resolves the relative FILE path.
    Raises RuntimeError on non-zero exit.
    """
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


def rip_disc(device: str, bin_path: Path, toc_path: Path) -> None:
    """Rip *device* to *bin_path* / *toc_path* via cdrdao read-cd.

    Raises RuntimeError on non-zero exit.
    """
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


def eject(device: str) -> None:
    """Eject the disc from *device* (best-effort; never raises)."""
    subprocess.run(["eject", device], check=False)  # noqa: S603, S607


# ── PCM analysis ──────────────────────────────────────────────────────────────


def _swap_be_to_le(data: bytes) -> bytes:
    """Byte-swap s16be → s16le. cdrdao BIN output is big-endian."""
    return np.frombuffer(data, dtype=np.int16).byteswap().tobytes()


def _apply_read_offset(pcm: bytes, read_offset: int) -> bytes:
    """Correct for drive read offset. Mirrors the zero-padding in accuraterip.py."""
    shift = read_offset * _FRAME_BYTES
    if shift > 0:
        return pcm[shift:] + bytes(shift)
    if shift < 0:
        return bytes(-shift) + pcm[: len(pcm) + shift]
    return pcm


def _find_pulse(pcm: bytes, expected: int) -> int | None:
    """Locate the noise burst near *expected* sample position.

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


def analyse_cycle(bin_path: Path, read_offset: int) -> dict | None:
    """Compute write offset from a ripped BIN file.

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


def summarise_cycles(cycles: list[dict]) -> dict:
    """Aggregate cycle results into a summary dict."""
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


def load_results(path: Path) -> dict:
    """Load results TOML from *path*; return an empty structure if absent."""
    if not path.exists():
        return {"drive": {}, "cycles": [], "summary": {}}
    with open(path, "rb") as f:
        data = tomllib.load(f)
    data.setdefault("cycles", [])
    return data


def _bool_toml(v: bool) -> str:
    return "true" if v else "false"


def save_results(path: Path, data: dict) -> None:
    """Write *data* atomically as TOML to *path*."""
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
            f"internally_consistent = {_bool_toml(c['internally_consistent'])}",
            "",
        ]
    s = data.get("summary", {})
    lines += [
        "[summary]",
        f"tests = {s.get('tests', 0)}",
        f"variance = {_bool_toml(s.get('variance', False))}",
        f"write_offset = {s.get('write_offset', 0)}",
        f"confidence = {s.get('confidence', 0)}",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)


# ── drive helpers ─────────────────────────────────────────────────────────────


def drive_slug(name: str | None, device: str) -> str:
    """Return a filesystem-safe slug for use in result filenames."""
    if name:
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "unknown"
    return re.sub(r"[^a-z0-9]+", "-", device.lower().lstrip("/")).strip("-") or "device"


def probe_drive(
    device: str, read_offset_override: int | None
) -> tuple[str | None, int]:
    """Return ``(drive_name, read_offset)`` for *device*.

    *drive_name* is ``None`` when the sysfs probe fails.
    *read_offset* comes from *read_offset_override* when given, otherwise from
    the cdda2img config entry for the detected drive (falls back to 0).
    """
    from cdda2img.config import load_config
    from cdda2img.drive_info import probe_drive_name

    drive_name = probe_drive_name(device)
    if read_offset_override is not None:
        return drive_name, read_offset_override
    cfg = load_config()
    if drive_name is not None:
        for d in cfg.drives:
            if d.name == drive_name:
                return drive_name, d.read_offset
    return drive_name, 0
