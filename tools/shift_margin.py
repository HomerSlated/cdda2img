#!/usr/bin/env python3
"""How far is real audio from itself at a wrong alignment? (AccuDisc design note 2)

AccuDisc's proposed tolerant shift test (their 2026-11-25h, B) calls two copies of a
sector "shifted" when the best non-zero offset differs in at most HALF as many bytes
as offset 0. Its false-positive risk is a damaged copy that is NOT shifted: the test
fires on it only if the damage at offset 0 exceeds twice the bytes by which the
audio differs from itself at its closest wrong alignment. That closest wrong
alignment is a property of the audio, so it can be measured on verified rips with no
drive at all, and that is what this does.

For sampled sectors of each container's PCM (raw s16le, stored offset-corrected), it
compares the sector with the same-length run displaced by k samples, for every k in
[-588, +588] except 0, and reports the minimum byte difference over k (the margin),
the k that achieved it, and the sector's level. Sectors with no alignment signal
(digital silence, or a run that matches itself at some k != 0) are counted apart,
because the engine's test already declines those.

Nothing is written: the containers are read through a memory map.

    uv run python tools/shift_margin.py ~/"Tracy Chapman.rbi" rips/cdda2img/*.rbi
"""

from __future__ import annotations

import argparse
import math
import random
from pathlib import Path

import numpy as np

from cdda2img import container
from cdda2img.rbi_format import BLOCK_TYPE_PCM

SECTOR = 2352
SAMPLE = 4  # stereo s16le
REACH = 588  # samples: one sector either way, the reach AccuDisc proposes to search


def _pcm(path: Path) -> np.ndarray:
    header = container.read_header(path)
    entry = header.find_block(BLOCK_TYPE_PCM)
    if entry is None:
        msg = f"{path}: no PCM block"
        raise SystemExit(msg)
    return np.memmap(
        path, dtype=np.uint8, mode="r", offset=entry.offset, shape=(entry.length,)
    )


def _level_dbfs(sector: np.ndarray) -> float:
    s = sector.view("<i2").astype(np.float64)
    rms = math.sqrt(float(np.mean(s * s)))
    return -math.inf if rms == 0 else 20 * math.log10(rms / 32768)


def _margin(pcm: np.ndarray, index: int) -> tuple[int, int, int]:
    """(bytes differing at the closest wrong alignment, that k, bytes at k=+-1)."""
    b = index * SECTOR
    lo, hi = b - REACH * SAMPLE, b + SECTOR + REACH * SAMPLE
    window = np.asarray(pcm[lo:hi])
    rows = np.lib.stride_tricks.sliding_window_view(window, SECTOR)[::SAMPLE]
    centre = rows[REACH]
    diff = (rows != centre).sum(axis=1)
    diff[REACH] = SECTOR + 1  # exclude k = 0
    k = int(np.argmin(diff))
    one = int(min(diff[REACH - 1], diff[REACH + 1]))
    return int(diff[k]), k - REACH, one


def _pct(values: list[int], p: float) -> int:
    v = sorted(values)
    return v[min(len(v) - 1, max(0, math.ceil(p / 100 * len(v)) - 1))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("rbi", nargs="+", type=Path)
    ap.add_argument("--sectors", type=int, default=3000, help="sampled per container")
    ap.add_argument("--seed", type=int, default=20261125)
    ap.add_argument(
        "--predicate",
        action="store_true",
        help="measure AccuDisc's proposed shift test (corrected form) instead",
    )
    args = ap.parse_args()
    if args.predicate:
        predicate(args.rbi, args.sectors, args.seed)
        return 0
    rng = random.Random(args.seed)  # noqa: S311 - a reproducible sample, not a secret
    bands = (
        ("quiet  < -40 dBFS", -math.inf, -40.0),
        ("middle -40..-20", -40.0, -20.0),
        ("loud   > -20 dBFS", -20.0, math.inf),
    )
    print(
        f"{'container':34} {'band':18} {'n':>5} {'min':>5} {'p0.1':>5} {'p1':>5} "
        f"{'p5':>5} {'p50':>5}  {'|k|=1 p1':>8}  {'no-signal':>9}"
    )
    for path in args.rbi:
        pcm = _pcm(path)
        n_sec = len(pcm) // SECTOR
        picks = rng.sample(range(2, n_sec - 2), min(args.sectors, n_sec - 4))
        rows: dict[str, list[tuple[int, int, int]]] = {b[0]: [] for b in bands}
        no_signal = 0
        for i in picks:
            sector = np.asarray(pcm[i * SECTOR : (i + 1) * SECTOR])
            m, k, one = _margin(pcm, i)
            if m == 0 or not sector.any():
                no_signal += 1
                continue
            lvl = _level_dbfs(sector)
            for name, a, b in bands:
                if a <= lvl < b:
                    rows[name].append((m, k, one))
        for name, _a, _b in bands:
            got = rows[name]
            if not got:
                continue
            m = [r[0] for r in got]
            ones = [r[2] for r in got]
            print(
                f"{path.name[:34]:34} {name:18} {len(got):5} {min(m):5} {_pct(m, 0.1):5} "
                f"{_pct(m, 1):5} {_pct(m, 5):5} {_pct(m, 50):5}  {_pct(ones, 1):8}  "
                f"{no_signal:9}"
            )
            no_signal = 0  # printed once per container
        smallest = sorted((r for g in rows.values() for r in g), key=lambda r: r[0])[:3]
        print(
            f"{'':34} smallest margins (bytes, k): "
            + ", ".join(f"{m} at k={k:+d}" for m, k, _ in smallest)
        )
    return 0


# ---- --predicate: AccuDisc's proposed shift test, measured (2026-11-25) -------------
#
# Their 25l reference searches k in [-588, +588]. At |k| = 588 a 2352-byte sector has
# NO overlap with itself shifted, so diff = 0 there and their has_signal() is False for
# every sector: the literal predicate can never call anything shifted. Near the edge the
# overlap is a few bytes, which chance matches. So this measures a corrected form:
#   * |k| <= MAX_K, keeping at least half a sector of overlap;
#   * "rates": every condition compares differing bytes PER BYTE OF OVERLAP, since
#     raw counts over shrinking overlaps favour large |k| by construction.
# False positives: y = x damaged in place (not shifted). True positives: y = the
# stream displaced by k samples, then damaged. Damage = N random bytes replaced.

MAX_K = 294
DAMAGE = (16, 64, 256, 624)  # bytes; 09-16g measured 2-624 at the true offset
TRUE_K = (1, 6, 12, 24)


def _rates(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Per k in [-MAX_K, MAX_K]: bytes differing per byte of overlap (x vs y@k)."""
    out = np.empty(2 * MAX_K + 1)
    n = len(x)
    for j, k in enumerate(range(-MAX_K, MAX_K + 1)):
        b = SAMPLE * k
        a, c = (x[: n - b], y[b:]) if b >= 0 else (x[-b:], y[: n + b])
        out[j] = np.count_nonzero(a != c) / len(a)
    return out


def _shifted(self_r: np.ndarray, x: np.ndarray, y: np.ndarray) -> int | None:
    r = _rates(x, y)
    r0 = r[MAX_K]
    ok = (r <= r0 / 2) & (r <= 0.5) & (r <= self_r / 2)
    ok[MAX_K] = False
    if not ok.any():
        return None
    j = int(np.argmin(np.where(ok, r, np.inf)))
    return j - MAX_K


def _damage(x: np.ndarray, n: int, rng: random.Random) -> np.ndarray:
    y = x.copy()
    for i in rng.sample(range(len(x)), n):
        y[i] = (int(y[i]) + rng.randrange(1, 256)) % 256
    return y


def predicate(paths: list[Path], sectors: int, seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - a reproducible sample, not a secret
    print(
        f"{'container':26} {'band':8} {'n':>4}  "
        + "  ".join(f"FP@{d:<3}" for d in DAMAGE)
        + "  "
        + "  ".join(f"TP k={k:<2}" for k in TRUE_K)
        + "   (TP at 624 B damage)"
    )
    for path in paths:
        pcm = _pcm(path)
        n_sec = len(pcm) // SECTOR
        rows: dict[str, list] = {"quiet": [], "middle": [], "loud": []}
        for i in rng.sample(range(4, n_sec - 4), sectors):
            x = np.asarray(pcm[i * SECTOR : (i + 1) * SECTOR])
            self_r = _rates(x, x)
            self_r[MAX_K] = np.inf
            if not x.any() or (self_r == 0).any():
                continue  # no alignment signal: the engine declines these already
            lvl = _level_dbfs(x)
            band = "quiet" if lvl < -40 else "middle" if lvl < -20 else "loud"
            fp = [_shifted(self_r, x, _damage(x, d, rng)) is not None for d in DAMAGE]
            tp = []
            for k in TRUE_K:
                lo = i * SECTOR - SAMPLE * k
                y = _damage(np.asarray(pcm[lo : lo + SECTOR]), DAMAGE[-1], rng)
                tp.append(_shifted(self_r, x, y) == k)
            rows[band].append((fp, tp))
        for band, got in rows.items():
            if not got:
                continue
            m = len(got)
            fps = "  ".join(
                f"{sum(g[0][j] for g in got):3}/{m:<3}" for j in range(len(DAMAGE))
            )
            tps = "  ".join(
                f"{sum(g[1][j] for g in got):3}/{m:<3}" for j in range(len(TRUE_K))
            )
            print(f"{path.name[:26]:26} {band:8} {m:4}  {fps}  {tps}")


if __name__ == "__main__":
    raise SystemExit(main())
