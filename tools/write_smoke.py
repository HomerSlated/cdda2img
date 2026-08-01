#!/usr/bin/env python3
"""Burn through `accudisc_reader.write_disc` onto a CDEmu blank, then read it back.

`write_disc` was migrated to the binding on 2026-07-29 and there was no blank
CD-R to exercise it with. That looked like a hard stop — `--simulate` needs blank
media too — until AccuDisc pointed out (§cg.3) that CDEmu will hand you a
recordable blank. `accudisc disc` reports `kind=BLANK profile=0x0009` on it, so
the library's own blank check agrees it is a burn target rather than being talked
into one.

**The round trip is the point.** `WriteResult.OK` proves only the return path: a
burn that wrote the wrong bytes, or the right bytes at the wrong LBA, returns OK
just as cheerfully. Reading the disc back and comparing to the source is what
separates "the call succeeded" from "the disc is correct" — and on an operation
that cannot be undone those are very different claims.

The payload is a deterministic pseudo-random pattern, never silence. Zeros would
be reproduced by a burn that wrote nothing at all, by one that wrote the wrong
region, and by a read that returned an empty buffer; a pattern distinguishes all
three. Same reason `_FakeReadChunk` in the test suite fills each stream with a
different byte.

What this does NOT establish, and both are inherited deliberately:

* **CDEmu is not a drive.** It exercises the command path, the option
  marshalling and the engine — not laser timing, media quality or a real DAO
  lead-in. A burn on the PX-716A remains the acceptance test.
* **`CAVEATS` is unreachable here.** It needs a CD-Text blob whose SIZE_INFO
  disagrees with the `.toc`, which this fixture does not build. That mapping is
  covered by the fake-module tests and by nothing on hardware.

So the sentence this buys is "exercised on a virtual writer, byte-verified,
untested on physical media" — much better than "untested", and available today.

Usage::

    uv run python tools/write_smoke.py            # device 0 -> /dev/sr1
    uv run python tools/write_smoke.py --keep     # leave the medium for inspection
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img import accudisc_reader as ar

SECTOR = 2352
#: 150 sectors = 2 s, the Red Book minimum track. Long enough to be a real DAO
#: burn with a lead-in and lead-out, short enough that a failure costs seconds.
SECTORS = 150


def _cdemu(*args: str) -> None:
    subprocess.run(["cdemu", *args], check=True)  # noqa: S603, S607 — fixed argv


def _cdemu_quiet(*args: str) -> None:
    """`cdemu` where failure is an acceptable answer — unloading an empty slot.

    Separate from :func:`_cdemu` rather than a flag, because every *other* call
    here must fail loudly: a `create-blank` that silently did nothing would leave
    the burn pointed at whatever medium happened to be in the slot.
    """
    cmd = ["cdemu", *args]
    subprocess.run(cmd, check=False, capture_output=True)  # noqa: S603 — fixed argv


def _wait_unloaded(slot: str, timeout_s: float = 10.0) -> None:
    """Unload *slot* and wait until the daemon agrees it is empty.

    `cdemu unload` returns before the daemon has finished, so a `create-blank`
    issued straight afterwards fails with `AlreadyLoaded` — and the tool then
    reports a cdemu error for what is really its own race. Polling the daemon's
    own view is the only honest test: sleeping a fixed interval would work until
    it did not.
    """
    _cdemu_quiet("unload", slot)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        cmd = ["cdemu", "status"]
        out = subprocess.run(  # noqa: S603 — fixed argv
            cmd, check=False, capture_output=True, text=True
        ).stdout
        row = next((ln for ln in out.splitlines() if ln.split()[:1] == [slot]), None)
        if row is None or "True" not in row:
            return
        time.sleep(0.2)


def _make_source(workdir: Path) -> tuple[Path, Path, bytes]:
    """A one-track cdrdao TOC and its s16le BIN, filled with a known pattern."""
    payload = hashlib.sha256(b"cdda2img write smoke").digest()
    data = (payload * (SECTOR * SECTORS // len(payload) + 1))[: SECTOR * SECTORS]
    binp = workdir / "smoke.bin"
    binp.write_bytes(data)
    toc = workdir / "smoke.toc"
    # No byteswap anywhere in this pipeline: write_disc leaves `byteswap` false
    # and the BIN is s16le, which is what the RBI stores and what `extract --raw`
    # emits. A .toc naming a .bin is read big-endian by cdrdao convention, so the
    # extension here is load-bearing rather than cosmetic.
    # MSF is MM:SS:FF at 75 frames/s — NOT MM:SS with a spare field. Writing 150
    # sectors as "02:00:00" declares two MINUTES against a 150-sector file and
    # the library rejects it as an invalid argument, which is the correct answer
    # to a question nobody meant to ask.
    msf = f"{SECTORS // (75 * 60):02d}:{SECTORS // 75 % 60:02d}:{SECTORS % 75:02d}"
    # The start offset is "00:00:00", NOT the bare "0" that cdrdao's grammar
    # allows and our own docs/reference/reference.toc shows ( FILE "track01.wav"
    # 0 ). AccuDisc's write parser requires MM:SS:FF in both fields — parse_msf
    # returns -1 on anything without two colons — and rejects the whole TOC with
    # ACCUDISC_ERR_INVAL, surfacing as a bare "invalid argument" that names
    # neither the line nor the field. Reported to them; our own generate_toc
    # emits timestamps, so `cdda2img burn` was never affected.
    toc.write_text(
        "CD_DA\n\nTRACK AUDIO\nNO COPY\nNO PRE_EMPHASIS\nTWO_CHANNEL_AUDIO\n"
        f'FILE "{binp.name}" 00:00:00 {msf}\n'
    )
    return toc, binp, data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--device", default="/dev/sr1", help="the CDEmu device node")
    ap.add_argument("--slot", default="0", help="the CDEmu device slot")
    ap.add_argument("--workdir", type=Path, default=Path("/var/tmp"))  # noqa: S108
    ap.add_argument("--keep", action="store_true", help="leave the medium in place")
    args = ap.parse_args()

    medium = args.workdir / "cdda2img_smoke"
    toc, binp, source = _make_source(args.workdir)

    print(f"# engine   : {ar.engine_version()}")

    # A previous failed run leaves the slot loaded, and create-blank refuses.
    # Clearing it up front rather than only on the way out means a crashed run
    # does not require manual repair before the next attempt.
    _wait_unloaded(args.slot)
    _cdemu(
        "create-blank",
        "--writer-id=WRITER-TOC",
        "--medium-type=cdr74",
        args.slot,
        str(medium),
    )
    print(f"# blank created: {medium}.toc on {args.device}")

    try:
        return _burn_and_verify(args, medium, toc, binp, source)
    finally:
        # Cleanup belongs here, not on the success path: the first version left
        # the slot loaded on every failure, so the SECOND run failed for a
        # different reason than the first and the original error was buried.
        if not args.keep:
            _wait_unloaded(args.slot)
            for leftover in (Path(f"{medium}.toc"), Path(f"{medium}.bin"), toc, binp):
                leftover.unlink(missing_ok=True)


def _burn_and_verify(
    args: argparse.Namespace, medium: Path, toc: Path, binp: Path, source: bytes
) -> int:
    seen: list[tuple[int, int]] = []
    rc, err, token = ar.write_disc(
        args.device, toc, binp, speed=0, progress_cb=lambda d, t: seen.append((d, t))
    )
    print(f"# write_disc -> rc={rc} token={token!r}")
    if err.strip():
        print(f"# log: {err.strip()}")
    if rc not in (0, 3):
        print(f"FAIL: the burn did not complete (rc={rc}, {token})", file=sys.stderr)
        return 1

    # A callback that never fires and one that fires wrongly look identical from
    # outside a completed burn — this is the only place the progress contract is
    # observable at all, since the not-blank refusal never reaches it.
    if not seen:
        print("FAIL: progress callback never fired", file=sys.stderr)
        return 1
    print(f"# progress: {len(seen)} callbacks, last {seen[-1]}")
    if seen[-1][0] != seen[-1][1]:
        print(
            f"FAIL: progress ended at {seen[-1]}, never reached its total",
            file=sys.stderr,
        )
        return 1

    _wait_unloaded(args.slot)
    _cdemu("load", args.slot, f"{medium}.toc")

    geom = ar.read_toc(args.device)
    # TocGeometry reports disc_last_lsn (leadout - 1), not the leadout itself —
    # it is the last readable sector, which is what the AR/CTDB call sites need.
    print(
        f"# read back: last_lsn={geom.disc_last_lsn} "
        f"tracks={len(geom.track_lsns)} source={geom.source}"
    )

    got = ar.read_span_bytes(args.device, 0, SECTORS)
    if got == source:
        print(
            f"\nPASS: {len(got)} bytes round-tripped byte-identical "
            f"(blake2b {hashlib.blake2b(got, digest_size=8).hexdigest()})"
        )
        status = 0
    else:
        first = next((i for i, (a, b) in enumerate(zip(got, source)) if a != b), None)
        print(
            f"\nFAIL: read-back differs from source at byte {first} "
            f"(got {len(got)} bytes, expected {len(source)})",
            file=sys.stderr,
        )
        status = 1

    return status


if __name__ == "__main__":
    raise SystemExit(main())
