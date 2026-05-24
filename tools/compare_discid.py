"""compare_discid.py — read the physical disc TOC with multiple tools and
compare the resulting MusicBrainz disc-IDs.

The MB disc-ID is a SHA-1 over the disc's TOC (track LBAs + lead-out + first/last
track numbers). It is NOT affected by drive read-offset — sample-level offset is
sub-sector noise. We provide an --offset flag for completeness/testing only.

Sources probed:
  - cd-paranoia -Q  (libcdio-paranoia)
  - cdrdao read-toc
  - libdiscid       (canonical reference, via the `discid` Python package)

Run from project root:

    uv run python tools/compare_discid.py /dev/sr0
    uv run python tools/compare_discid.py /dev/sr0 --offset 30

Each successful probe prints the per-track LBAs, the lead-out LBA, and the
computed disc-ID. A mismatch between two sources indicates one of them reports
the TOC differently — typically subcode parsing or pregap-vs-INDEX-01 disagreement.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_LEAD_IN_SECTORS = 150


# ---------------------------------------------------------------------------
# Canonical MB disc-ID computation (mirrors mb_lookup.compute_disc_id)
# ---------------------------------------------------------------------------


def compute_mb_disc_id(
    first_track: int, last_track: int, track_lbas: list[int], lead_out_lba: int
) -> str:
    """SHA-1 over the 804-char ASCII hex TOC string → URL-safe base64.

    The MB spec hashes ASCII hex characters (uppercase, zero-padded), not raw
    binary. Mirrors `cdda2img.mb_lookup.compute_disc_id` — keep both in sync.
    """
    parts = [f"{first_track:02X}", f"{last_track:02X}", f"{lead_out_lba:08X}"]
    for i in range(99):
        parts.append(f"{(track_lbas[i] if i < len(track_lbas) else 0):08X}")
    sha1 = hashlib.sha1("".join(parts).encode("ascii")).digest()  # noqa: S324
    b64 = base64.b64encode(sha1).decode("ascii")
    return b64.replace("+", ".").replace("/", "_").replace("=", "-")


# ---------------------------------------------------------------------------
# Source: cd-paranoia -Q
# ---------------------------------------------------------------------------


def probe_cd_paranoia(device: str) -> dict | None:
    if not shutil.which("cd-paranoia"):
        return None
    try:
        proc = subprocess.run(  # noqa: S603
            ["cd-paranoia", "-Q", "-d", device],  # noqa: S607
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        return {"error": f"cd-paranoia failed: {exc}"}
    # cd-paranoia writes its TOC to stderr (audio-tool tradition)
    output = proc.stderr + proc.stdout
    # Per-track row: "  1.    18330 [04:04.30]        0 [00:00.00]    no   no  2"
    row_re = re.compile(r"^\s*(\d+)\.\s+(\d+)\s+\[[^\]]+\]\s+(\d+)\s+\[", re.MULTILINE)
    total_re = re.compile(r"^TOTAL\s+(\d+)\s+\[", re.MULTILINE)
    rows = row_re.findall(output)
    if not rows:
        return {"error": "no TOC rows in cd-paranoia output", "raw": output[:500]}
    tracks = [(int(n), int(length), int(begin)) for n, length, begin in rows]
    total_m = total_re.search(output)
    # When the TOTAL line is absent (some cd-paranoia variants), derive from the
    # last track's begin + length.
    total = int(total_m.group(1)) if total_m else tracks[-1][2] + tracks[-1][1]
    # cd-paranoia "begin" is audio-frame offset relative to disc audio start;
    # absolute LBA = begin + lead-in (150). Lead-out LBA = total_audio + 150.
    return {
        "tool": "cd-paranoia -Q",
        "first_track": tracks[0][0],
        "last_track": tracks[-1][0],
        "track_lbas": [begin + _LEAD_IN_SECTORS for _, _, begin in tracks],
        "lead_out_lba": total + _LEAD_IN_SECTORS,
        "raw": output[:1200],
    }


# ---------------------------------------------------------------------------
# Source: cdrdao read-toc
# ---------------------------------------------------------------------------


_MSF_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")


def _msf_to_frames(s: str) -> int:
    m = _MSF_RE.fullmatch(s.strip())
    if not m:
        msg = f"invalid MSF {s!r}"
        raise ValueError(msg)
    mm, ss, ff = (int(x) for x in m.groups())
    return (mm * 60 + ss) * 75 + ff


def probe_cdrdao(device: str, timeout: int = 1600) -> dict | None:
    if not shutil.which("cdrdao"):
        return None
    tmpdir = Path(tempfile.mkdtemp(prefix="cmp_discid_"))
    toc_path = tmpdir / "disc.toc"
    try:
        # read-toc with rw_raw subchannel reads the full Q-channel of every
        # sector across the whole disc -- at 1x this is ~70 minutes of audio
        # data scanned, often 3-6 minutes wall-clock on modern drives. The
        # default 600 s ceiling accommodates slow drives or long discs.
        proc = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "cdrdao",
                "read-toc",
                "--device",
                device,
                "--read-subchan",
                "rw_raw",
                str(toc_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if not toc_path.exists():
            return {
                "error": "cdrdao did not produce a TOC file",
                "stderr": proc.stderr[:1000],
            }
        toc_text = toc_path.read_text()
    except (subprocess.SubprocessError, OSError) as exc:
        return {"error": f"cdrdao failed: {exc}"}
    finally:
        # Always clean up — keep TOC on disk only if user wants to inspect later
        # (could be promoted to a --keep flag if useful)
        pass

    # Parse cdrdao TOC: each TRACK is followed by zero or more lines, including
    # FILE "name" start length  and optional START position
    track_re = re.compile(
        r"^TRACK\s+AUDIO[^\n]*\n"
        r"(?:(?!^TRACK\s).)*?"
        r'FILE\s+"[^"]*"\s+(?P<file_start>\d{1,2}:\d{2}:\d{2}|0)\s+'
        r"(?P<file_length>\d{1,2}:\d{2}:\d{2})"
        r"(?:(?!^TRACK\s).)*?"
        r"(?:START\s+(?P<start>\d{1,2}:\d{2}:\d{2})\s*)?",
        re.MULTILINE | re.DOTALL,
    )

    cumulative_pcm = 0
    track_lbas: list[int] = []
    track_count = 0
    for m in track_re.finditer(toc_text):
        track_count += 1
        # file_start may be "0" or MSF; treat both as frame count
        fs_raw = m.group("file_start") or "0"
        file_length = _msf_to_frames(m.group("file_length"))
        start_raw = m.group("start")
        pregap = _msf_to_frames(start_raw) if start_raw else 0
        # cdrdao places the track's data at cumulative_pcm; INDEX 01 sits at
        # cumulative_pcm + pregap.
        track_lbas.append(cumulative_pcm + pregap + _LEAD_IN_SECTORS)
        cumulative_pcm += file_length
        del fs_raw  # silence "unused" warning; kept for documentation

    if not track_lbas:
        return {
            "error": "cdrdao TOC parsed 0 tracks",
            "toc_excerpt": toc_text[:800],
        }
    return {
        "tool": "cdrdao read-toc",
        "first_track": 1,
        "last_track": track_count,
        "track_lbas": track_lbas,
        "lead_out_lba": cumulative_pcm + _LEAD_IN_SECTORS,
        "raw": toc_text[:1200],
    }


# ---------------------------------------------------------------------------
# Source: libdiscid (canonical) via python-discid
# ---------------------------------------------------------------------------


def probe_libdiscid(device: str) -> dict | None:
    try:
        import discid  # type: ignore[import-untyped]
    except ImportError:
        return {
            "error": (
                "python `discid` package not installed. "
                "Install with: uv pip install discid (requires libdiscid on system)"
            )
        }
    try:
        d = discid.read(device)
    except Exception as exc:
        return {"error": f"libdiscid read failed: {exc}"}
    # python-discid API: d.tracks is a list of Track objects with .offset; d.sectors
    # is the lead-out LBA. Track offsets are absolute LBA including the 150-sector
    # lead-in (libdiscid does the add-lead-in internally).
    return {
        "tool": "libdiscid",
        "first_track": d.first_track_num,
        "last_track": d.last_track_num,
        "track_lbas": [t.offset for t in d.tracks],
        "lead_out_lba": d.sectors,
        "reported_id": d.id,  # canonical — what libdiscid itself computes
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _apply_sample_offset(lbas: list[int], lead_out: int, offset: int) -> tuple:
    """Shift LBAs by *offset* SAMPLES (rounded to nearest sector). For testing only.

    1 sector = 588 sample pairs. 30 samples ≈ 0.05 sector → rounds to 0.
    Included because the user asked; expected to be a no-op for any real drive
    offset (smaller than a sector).
    """
    samples_per_sector = 588
    sector_shift = round(offset / samples_per_sector)
    if sector_shift == 0:
        return lbas, lead_out, sector_shift
    return [lba + sector_shift for lba in lbas], lead_out + sector_shift, sector_shift


def _print_result(name: str, r: dict | None, offset: int) -> None:
    print(f"\n=== {name} ===")
    if r is None:
        print("  (tool not installed)")
        return
    if "error" in r:
        print(f"  ERROR: {r['error']}")
        if "raw" in r:
            print(f"  --- raw output (first 500 bytes) ---\n{r['raw'][:500]}")
        elif "toc_excerpt" in r:
            print(f"  --- toc excerpt ---\n{r['toc_excerpt']}")
        elif "stderr" in r:
            print(f"  --- stderr ---\n{r['stderr']}")
        return
    track_lbas = r["track_lbas"]
    lead_out = r["lead_out_lba"]
    first, last = r["first_track"], r["last_track"]
    shifted_lbas, shifted_lead_out, sector_shift = _apply_sample_offset(
        track_lbas, lead_out, offset
    )

    print(f"  first_track:  {first}")
    print(f"  last_track:   {last}")
    print("  per-track LBA (INDEX 01):")
    for i, lba in enumerate(track_lbas, start=first):
        print(f"    track {i:>2}: {lba}")
    print(f"  lead_out:     {lead_out}")
    disc_id = compute_mb_disc_id(first, last, track_lbas, lead_out)
    print(f"  Disc ID (no offset):      {disc_id}")
    if "reported_id" in r:
        ok = "✓ matches" if disc_id == r["reported_id"] else "✗ DIFFERS"
        print(f"  libdiscid-reported ID:    {r['reported_id']}  ({ok})")
    if sector_shift != 0:
        disc_id_off = compute_mb_disc_id(first, last, shifted_lbas, shifted_lead_out)
        print(
            f"  Disc ID (+{offset} samples = +{sector_shift} sector(s)): {disc_id_off}"
        )
    elif offset != 0:
        print(
            f"  Note: {offset} samples = "
            f"{offset / 588:.4f} sector(s) → rounds to 0; no shift applied"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("device", help="CD device path (e.g. /dev/sr0)")
    ap.add_argument(
        "--offset",
        type=int,
        default=0,
        help=(
            "Drive read offset in samples (default: 0). "
            "Included for completeness; offsets smaller than one sector (~588 samples) "
            "round to zero and do not affect the disc-ID."
        ),
    )
    args = ap.parse_args()

    print(f"Device: {args.device}")
    if args.offset:
        print(
            f"Offset: {args.offset} samples "
            f"({args.offset / 588:.4f} sectors after rounding)"
        )

    cdp = probe_cd_paranoia(args.device)
    cdr = probe_cdrdao(args.device)
    ldi = probe_libdiscid(args.device)

    _print_result("cd-paranoia -Q", cdp, args.offset)
    _print_result("cdrdao read-toc", cdr, args.offset)
    _print_result("libdiscid (CANONICAL)", ldi, args.offset)

    # Summary table at the bottom for quick scan
    print("\n=== Summary ===")
    for name, r in [
        ("cd-paranoia", cdp),
        ("cdrdao", cdr),
        ("libdiscid", ldi),
    ]:
        if r and "error" not in r:
            did = compute_mb_disc_id(
                r["first_track"], r["last_track"], r["track_lbas"], r["lead_out_lba"]
            )
            print(f"  {name:<14}{did}")
        elif r is None:
            print(f"  {name:<14}(not installed)")
        else:
            print(f"  {name:<14}ERROR")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
