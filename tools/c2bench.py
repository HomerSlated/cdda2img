#!/usr/bin/env python3
"""c2bench.py — build the C2 error-pointer confusion matrix against an oracle.

The C2 experiment (item 8): quantify how trustworthy a drive's per-byte C2 error
pointers are, by comparing what the drive *flags* against what is *actually* wrong.
"Actually wrong" is decided by an AccurateRip-verified oracle (good.pcm), so this
is ground truth, not another guess.

Inputs per capture: a whole-disc ``c2read --full --pcm P --c2 C`` pair. c2read
records the raw audio and the C2 bitmap *from the same single read*, so they are
inherently aligned to that read — the essential property for the matrix to mean
anything.

For each capture:
  1. Lock the sample offset δ between the raw read and the offset-corrected oracle
     (measured on a clean, signal-bearing track — never assumed).
  2. Classify every stereo sample wrong/correct vs the oracle at δ.
  3. Collapse the per-byte C2 bitmap to per-sample and align it to the audio (offset k).
  4. Emit the TP / FP / FN / TN confusion matrix — FN (wrong but *not* flagged) first,
     because FN is the whole verdict: it is the case where "trust the unflagged
     samples" silently keeps corrupt audio.

Across captures it also reports stability (are the errors and the flags repeatable
read-to-read?), which is itself a finding about C2.

Usage:
    uv run python tools/c2bench.py private/testdata/c2/pass1 private/testdata/c2/pass2 ...
      --oracle private/testdata/ctanalyse/good.pcm --json private/testdata/c2/matrix.json
Each capture argument is a *base* path; <base>.pcm and <base>.c2 must both exist.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from ctdb_probe import (  # type: ignore[unresolved-import]
    _CDDB_ID,
    _FRAME,
    _LEADOUT,
    _LSNS,
    _TRACK,
)

_SPS = _FRAME // 4  # stereo samples (4-byte pairs) per sector = 588
_N_TRACKS = len(_LSNS)
_BOUNDS = [
    *_LSNS,
    _LEADOUT,
]  # track t occupies sample-pairs [_BOUNDS[t-1]*_SPS, _BOUNDS[t]*_SPS)
_FLAGGED_LBA = (112320, 115694)  # observed C2 cluster (track 8), for reporting only


# ---- oracle -----------------------------------------------------------------


def reverify_oracle(oracle: np.ndarray) -> bool:
    """Re-confirm the oracle still matches AccurateRip (guards a stale/edited good.pcm).

    Verifies the historically-damaged track plus one neighbour; the oracle is
    offset-corrected already, so match_track_pcm is called with no shift."""
    from cdda2img.accuraterip import fetch_ar_responses, match_track_pcm

    responses, transport, _b3 = fetch_ar_responses(_LSNS, _LEADOUT - 1, _CDDB_ID)
    if not responses:
        print("  oracle re-verify: disc not in AccurateRip — cannot confirm oracle")
        return False
    raw = oracle.tobytes()
    ok = True
    for t in sorted({1, _TRACK, _N_TRACKS}):
        seg = raw[_BOUNDS[t - 1] * _FRAME : _BOUNDS[t] * _FRAME]
        _v1, _v2, cv1, cv2 = match_track_pcm(seg, t, _N_TRACKS, responses)
        matched = bool(cv1 or cv2)
        ok &= matched
        print(
            f"  oracle re-verify ({transport}): track {t:2d} "
            f"{'MATCH' if matched else 'MISMATCH'} (v1={cv1} v2={cv2})"
        )
    return ok


# ---- capture loading + alignment --------------------------------------------


def load_u32(path: Path, expect_pairs: int) -> np.ndarray:
    """Load s16le PCM as one uint32 per stereo sample-pair (AccurateRip's unit)."""
    arr = np.fromfile(path, dtype="<u4")
    if arr.size != expect_pairs:
        msg = f"{path.name}: {arr.size} sample-pairs, expected {expect_pairs}"
        raise SystemExit(msg)
    return arr


def load_c2_flags(path: Path, n_sectors: int) -> np.ndarray:
    """Load the 294-byte/sector C2 bitmap → one bool per stereo sample-pair.

    A pair is flagged if *any* of its 4 audio bytes is flagged. unpackbits (big
    bit-order) maps C2 byte b bit 7..0 to audio bytes 8b..8b+7, matching the
    MSB-first-per-byte convention c2read documents."""
    raw = np.fromfile(path, dtype=np.uint8)
    if raw.size != n_sectors * 294:
        msg = f"{path.name}: {raw.size} C2 bytes, expected {n_sectors * 294}"
        raise SystemExit(msg)
    bits = np.unpackbits(
        raw.reshape(n_sectors, 294), axis=1
    )  # (sectors, 2352) audio-byte order
    per_pair = bits.reshape(n_sectors, _SPS, 4).any(axis=2)  # (sectors, 588)
    return per_pair.reshape(-1)  # length n_sectors*588, raw-read coordinates


def _mismatch_count(
    test: np.ndarray, oracle: np.ndarray, lo: int, hi: int, delta: int
) -> int:
    """Count sample-pair mismatches in test[lo:hi] vs oracle shifted by delta."""
    return int(np.count_nonzero(test[lo:hi] != oracle[lo + delta : hi + delta]))


def lock_offset(test: np.ndarray, oracle: np.ndarray, drange: int) -> tuple[int, float]:
    """Find δ (sample-pairs) minimising test-vs-oracle mismatch on a clean signal track.

    Picks a window inside a clean track (outside the flagged cluster) that carries
    real signal — silence matches at every δ and would not discriminate. Returns
    (delta, best_fraction); a best_fraction not near zero means byte-order trouble
    or a stale oracle, and the caller should abort rather than trust the classing."""
    # Prefer a long clean track well away from the flagged cluster; require signal.
    for t in (6, 3, 5, 2, 10):
        s0, s1 = _BOUNDS[t - 1] * _SPS, _BOUNDS[t] * _SPS
        mid = (s0 + s1) // 2
        lo = max(s0 + drange, mid - 2_000_000)
        hi = min(s1 - drange, mid + 2_000_000)
        if hi - lo < 100_000:
            continue
        if int(np.count_nonzero(oracle[lo:hi])) < (hi - lo) // 100:
            continue  # mostly silence — not discriminating
        counts = [
            (_mismatch_count(test, oracle, lo, hi, d), d)
            for d in range(-drange, drange + 1)
        ]
        best_n, best_d = min(counts)
        return best_d, best_n / (hi - lo)
    msg = "no clean signal-bearing track found for offset lock"
    raise SystemExit(msg)


def align_c2(wrong: np.ndarray, c2: np.ndarray, krange: int) -> tuple[int, int]:
    """Find the C2-vs-audio shift k (sample-pairs) maximising flag/error overlap.

    Distinct from the read offset δ: this is the drive's internal alignment of its
    C2 bitmap to its own audio stream (usually ~0). Positive k means the flag for
    raw audio position j describes the error at j+k. Returns (k, overlap_at_k)."""
    best_k, best_overlap = 0, -1
    for k in range(-krange, krange + 1):
        if k >= 0:
            a, b = wrong[k:], c2[: c2.size - k]
        else:
            a, b = wrong[:k], c2[-k:]
        overlap = int(np.count_nonzero(a & b))
        if overlap > best_overlap:
            best_k, best_overlap = k, overlap
    return best_k, best_overlap


def shift_to_wrong(c2: np.ndarray, k: int) -> np.ndarray:
    """Shift the C2 flag array into the wrong[] index space for the winning k.

    Mirrors align_c2's pairing: c2_aligned[i] == c2[i-k]. Positions that shift off
    an end become False (they land in the first-track head / last-track tail, both
    inside AccurateRip's boundary-exclusion zone, so the loss is immaterial)."""
    out = np.zeros_like(c2)
    if k >= 0:
        out[k:] = c2[: c2.size - k]
    else:
        m = -k
        out[: c2.size - m] = c2[m:]
    return out


# ---- confusion matrix -------------------------------------------------------


@dataclass
class Matrix:
    capture: str
    delta: int
    delta_residual: float
    k: int
    total: int
    tp: int
    fp: int
    fn: int
    tn: int

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else float("nan")

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else float("nan")


def confusion(
    wrong: np.ndarray,
    c2_aligned: np.ndarray,
    k: int,
    capture: str,
    delta: int,
    resid: float,
) -> Matrix:
    """TP/FP/FN/TN over the whole disc, C2 already aligned into wrong[]'s index space."""
    tp = int(np.count_nonzero(c2_aligned & wrong))
    fp = int(np.count_nonzero(c2_aligned & ~wrong))
    fn = int(np.count_nonzero(~c2_aligned & wrong))
    tn = int(wrong.size - tp - fp - fn)
    return Matrix(capture, delta, resid, k, int(wrong.size), tp, fp, fn, tn)


def per_track_errors(wrong: np.ndarray, c2: np.ndarray) -> list[dict]:
    """Per-track wrong / flagged / FN counts, for the tracks that have any error."""
    out = []
    for t in range(1, _N_TRACKS + 1):
        s0, s1 = _BOUNDS[t - 1] * _SPS, _BOUNDS[t] * _SPS
        w, f = wrong[s0:s1], c2[s0:s1]
        nwrong = int(np.count_nonzero(w))
        if not nwrong and not int(np.count_nonzero(f)):
            continue
        out.append({
            "track": t,
            "wrong": nwrong,
            "flagged": int(np.count_nonzero(f)),
            "fn": int(np.count_nonzero(w & ~f)),
            "tp": int(np.count_nonzero(w & f)),
        })
    return out


# ---- driver -----------------------------------------------------------------


def analyse_capture(
    base: Path, oracle: np.ndarray, n_pairs: int, drange: int, krange: int
) -> tuple[Matrix, np.ndarray, np.ndarray, list[dict]]:
    test = load_u32(base.with_suffix(".pcm"), n_pairs)
    c2 = load_c2_flags(base.with_suffix(".c2"), _LEADOUT)

    delta, resid = lock_offset(test, oracle, drange)
    # Classify whole disc at δ; exclude the wrap region where the shift runs off an end.
    lo, hi = max(0, -delta), min(n_pairs, n_pairs - delta)
    wrong = np.zeros(n_pairs, dtype=bool)
    wrong[lo:hi] = test[lo:hi] != oracle[lo + delta : hi + delta]

    k, _overlap = align_c2(wrong, c2, krange)
    c2_aligned = shift_to_wrong(c2, k)
    mtx = confusion(wrong, c2_aligned, k, base.name, delta, resid)
    tracks = per_track_errors(wrong, c2_aligned)
    return mtx, wrong, c2_aligned, tracks


def print_matrix(m: Matrix, tracks: list[dict]) -> None:
    print(f"\n=== {m.capture} ===")
    print(
        f"  offset lock : δ={m.delta:+d} sample-pairs  (clean-track residual {m.delta_residual:.2e})"
    )
    print(f"  C2 align    : k={m.k:+d} sample-pairs")
    print(f"  samples     : {m.total:,}")
    print(f"  TP (flagged & wrong)   : {m.tp:,}")
    print(f"  FP (flagged & correct) : {m.fp:,}")
    print(f"  FN (wrong & NOT flagged): {m.fn:,}   <-- the verdict")
    print(f"  TN (correct & clean)   : {m.tn:,}")
    print(f"  precision {m.precision:.4f}   recall {m.recall:.4f}")
    if m.fn == 0 and m.tp > 0:
        print(
            "  => on this read, EVERY wrong sample was C2-flagged (FN=0): C2 is a sound gate here."
        )
    elif m.fn:
        print(
            f"  => {m.fn} wrong samples had NO C2 flag: trusting unflagged audio would keep them."
        )
    for row in tracks:
        print(
            f"    track {row['track']:2d}: wrong {row['wrong']:6d}  flagged {row['flagged']:6d}"
            f"  TP {row['tp']:6d}  FN {row['fn']:6d}"
        )


def cross_pass(wrongs: list[np.ndarray], c2s: list[np.ndarray]) -> dict:
    """Stability across passes: how repeatable are the errors and the flags."""
    wr = np.stack(wrongs)
    fl = np.stack(c2s)
    w_any, w_all = wr.any(axis=0), wr.all(axis=0)
    f_any, f_all = fl.any(axis=0), fl.all(axis=0)
    return {
        "passes": len(wrongs),
        "wrong_union": int(np.count_nonzero(w_any)),
        "wrong_intersection": int(np.count_nonzero(w_all)),
        "flag_union": int(np.count_nonzero(f_any)),
        "flag_intersection": int(np.count_nonzero(f_all)),
        # wrong samples that were flagged in NO pass — the hard false negatives
        "fn_all_passes": int(np.count_nonzero(w_any & ~f_any)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "captures", nargs="+", type=Path, help="capture base paths (<base>.pcm/.c2)"
    )
    ap.add_argument(
        "--oracle", type=Path, default=Path("private/testdata/ctanalyse/good.pcm")
    )
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--delta-range", type=int, default=64)
    ap.add_argument("--k-range", type=int, default=16)
    ap.add_argument(
        "--no-reverify", action="store_true", help="skip the AccurateRip oracle check"
    )
    args = ap.parse_args()

    n_pairs = _LEADOUT * _SPS
    print(f"oracle: {args.oracle}")
    oracle = load_u32(args.oracle, n_pairs)

    if not args.no_reverify and not reverify_oracle(oracle):
        print(
            "ABORT: oracle failed AccurateRip re-verification — do not trust the matrix."
        )
        return 1

    matrices, wrongs, c2s, all_tracks = [], [], [], []
    for base in args.captures:
        mtx, wrong, c2, tracks = analyse_capture(
            base, oracle, n_pairs, args.delta_range, args.k_range
        )
        if mtx.delta_residual > 1e-3:
            print(
                f"\nABORT ({base.name}): clean-track residual {mtx.delta_residual:.2e} too high — "
                "byte-order mismatch or stale oracle. Matrix would be meaningless."
            )
            return 1
        print_matrix(mtx, tracks)
        matrices.append(mtx)
        wrongs.append(wrong)
        c2s.append(c2)
        all_tracks.append(tracks)

    summary = {"matrices": [asdict(m) for m in matrices]}
    if len(wrongs) > 1:
        stab = cross_pass(wrongs, c2s)
        summary["stability"] = stab
        print("\n=== cross-pass stability ===")
        print(f"  passes                : {stab['passes']}")
        print(
            f"  wrong  union / ∩      : {stab['wrong_union']:,} / {stab['wrong_intersection']:,}"
        )
        print(
            f"  flags  union / ∩      : {stab['flag_union']:,} / {stab['flag_intersection']:,}"
        )
        print(
            f"  FN in ALL passes      : {stab['fn_all_passes']:,}  (never flagged in any read)"
        )

    if args.json:
        args.json.write_text(json.dumps(summary, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
