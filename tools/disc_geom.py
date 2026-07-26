#!/usr/bin/env python3
"""Shared disc geometry for the raw-PCM check tools (``ar_check``, ``ctdb_check``).

Exists for exactly one reason: **the ±1 lead-out convention**. Three functions in
the AccurateRip/CTDB chain want three different numbers for "the end of the disc" —

    accuraterip.verify_rip(..., disc_last_lsn)   # last audio sector  = leadout - 1
    accuraterip._ar_disc_ids(...)                # internally adds 1 back
    ctdb_repair.track_crc_at(..., bounds)        # bounds[-1] = leadout itself

— and getting it wrong produces a *well-formed* wrong answer: a valid-looking
disc ID that 404s, or a CRC window shifted by one sector. Nothing type-checks it
and nothing downstream notices. So the conversion lives here once, and
:meth:`Geometry.self_test` gives every caller a way to prove it before trusting
a single verdict.

PCM domain note: an ``accudisc read --start 0 --count <leadout>`` capture spans
``[0, leadout)`` — it includes any track-1 program-area pre-gap. CTDB's own image
is ``[bounds[0], bounds[-1])``. They coincide only when track 1's INDEX 01 is at
LBA 0; ``track_crc_at`` handles the difference, but ``expected_bytes`` below is
about *our* file, so it uses the ``[0, leadout)`` span.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

from cdda2img.cddb import compute_cddb_disc_id

BYTES_PER_SECTOR = 2352
SAMPLES_PER_SECTOR = 588  # stereo sample-pairs


@dataclass(frozen=True)
class Geometry:
    """Track geometry of one disc. *track_lsns* are INDEX 01 LBAs (audio starts);
    *leadout* is the lead-out LBA, i.e. the first sector past the last audio."""

    track_lsns: tuple[int, ...]
    leadout: int

    @property
    def n_tracks(self) -> int:
        return len(self.track_lsns)

    @property
    def disc_last_lsn(self) -> int:
        """Last audio sector — what ``verify_rip`` and ``compute_cddb_disc_id`` want."""
        return self.leadout - 1

    @property
    def bounds(self) -> list[int]:
        """Track starts plus the lead-out — what ``track_crc_at`` wants."""
        return [*self.track_lsns, self.leadout]

    @property
    def lsns(self) -> list[int]:
        return list(self.track_lsns)

    @property
    def cddb_id(self) -> int:
        return int(compute_cddb_disc_id(self.lsns, self.disc_last_lsn), 16)

    @property
    def expected_bytes(self) -> int:
        """Size of a whole-disc ``[0, leadout)`` raw PCM capture."""
        return self.leadout * BYTES_PER_SECTOR

    def toc_string(self) -> str:
        return ":".join(str(x) for x in self.bounds)

    def describe(self) -> str:
        return (
            f"{self.n_tracks} tracks, lead-out {self.leadout}, "
            f"cddb {self.cddb_id:08x}, toc {self.toc_string()}"
        )

    def self_test(self, expect_cddb: str | None) -> None:
        """Abort unless the derived CDDB id matches *expect_cddb*.

        A geometry that is off by one sector still yields a plausible 8-hex id, and
        every AccurateRip URL built from it 404s — which reads as "disc not in the
        database" rather than "I computed the wrong key". Opt-in, because the
        expected value has to come from somewhere trusted."""
        if expect_cddb is None:
            return
        got = f"{self.cddb_id:08x}"
        want = expect_cddb.lower().removeprefix("0x")
        if got != want:
            msg = (
                f"geometry self-test FAILED: cddb id {got} != expected {want}. "
                "The track LSNs or the lead-out are wrong; every AR/CTDB lookup "
                "built from them would silently miss."
            )
            raise SystemExit(msg)
        print(f"# geometry self-test OK (cddb {got})")


def from_toc(toc: str) -> Geometry:
    """Parse a colon TOC ``L0:L1:…:LEADOUT`` (the CTDB lookup form)."""
    try:
        nums = [int(x) for x in toc.split(":")]
    except ValueError as exc:
        msg = f"--toc must be colon-separated integers: {exc}"
        raise SystemExit(msg) from exc
    if len(nums) < 3:
        msg = "--toc needs at least 2 tracks and a lead-out"
        raise SystemExit(msg)
    return Geometry(tuple(nums[:-1]), nums[-1])


def from_device(device: str) -> Geometry:
    """Read the geometry off the loaded disc via ``accudisc toc``."""
    from cdda2img.accudisc_reader import read_toc

    geom = read_toc(device)
    # TocGeometry reports the last audio sector; Geometry wants the lead-out LBA.
    return Geometry(tuple(geom.track_lsns), geom.disc_last_lsn + 1)


def add_geometry_args(ap: argparse.ArgumentParser) -> None:
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--toc", help="colon TOC L0:L1:…:LEADOUT")
    src.add_argument("--device", help="derive the TOC from the loaded disc")
    ap.add_argument(
        "--expect-cddb",
        metavar="HEX",
        help="abort unless the derived CDDB disc id equals this (geometry self-test)",
    )


def resolve_geometry(args: argparse.Namespace) -> Geometry:
    geom = from_device(args.device) if args.device else from_toc(args.toc)
    geom.self_test(getattr(args, "expect_cddb", None))
    return geom


def check_size(geom: Geometry, path: Path) -> None:
    """Warn (do not abort) when a PCM is not a whole-disc ``[0, leadout)`` capture.

    Not fatal: a deliberately truncated or offset-corrected file is a legitimate
    thing to test. But a size mismatch explains a table of misses far better than
    the table does, so it must be said out loud rather than inferred."""
    size = path.stat().st_size
    if size != geom.expected_bytes:
        delta = (size - geom.expected_bytes) / BYTES_PER_SECTOR
        print(
            f"# WARNING {path.name}: {size} bytes, expected {geom.expected_bytes} "
            f"for [0, {geom.leadout}) — {delta:+.2f} sectors"
        )
