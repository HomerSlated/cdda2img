#!/usr/bin/env python3
"""
trace_album_live.py — LIVE companion to ``trace_album.py``.

``trace_album.py`` is a *static model*: it takes the resolved per-service album
strings as command-line inputs and applies the precedence logic. This tool is
the *live* counterpart: it reads the disc with cdrdao and queries the actual
metadata services (CDDB + MusicBrainz), driving the **real** cdda2img functions
in the same order ``cdda2img._finalize_import`` does — then it feeds the raw
per-service candidates back through ``trace_album.resolve_seed_rip`` and asserts
the model predicts the same album the real pipeline produced.

Why this proves something
-------------------------
The tool does NOT reimplement the merge logic — it calls ``query_cddb`` /
``prepopulate_from_mb`` / ``_merge_into_disc`` directly, in the pipeline's
precedence order (MB first, CDDB last as a zero-trust gap-filler). So the
"real" album it prints is, by construction, what the rip pipeline would put in
``RBIDisc.album``. The cross-check then asks a separate
question: does the condensed model in ``trace_album.py`` faithfully reproduce
that, given only the *raw* candidate strings (CD-Text seed, CDDB ``matches[0]``,
MB winner)? If the two disagree, the model is wrong and must be refined; if they
agree, the model is a trustworthy stand-in for the pipeline's album precedence.

Scope: the album-TITLE component of the "Album:" line only — the same single
value ``trace_album.py`` traces. The ``(year)`` suffix and other fields are out
of scope. AcoustID and Discogs are deliberately skipped: both are blank-fill on
the album and run *after* CD-Text/CDDB/MB, so neither can change a non-blank
title (verified against the codebase). The tool dumps the full MB match list +
barcode hints because that is the raw material the next diagnosis step needs —
it captures, it does not analyse.

Local query: ``cdrdao read-toc --fast-toc`` (CD-Text + ISRC + MCN + offsets, no
audio extraction). Verified to produce the identical MB disc ID to a full
``read-cd`` rip for the test disc, so it is a faithful, fast stand-in.

Usage (from project root):
    # read the disc in /dev/sr0 live, query the real services
    uv run python tools/trace_album_live.py --device /dev/sr0

    # reuse a previously-captured fast-toc (no disc access), still query live
    uv run python tools/trace_album_live.py --toc /tmp/disc.toc

    # point at a non-default CDDB server
    uv run python tools/trace_album_live.py --device /dev/sr0 --server host:888

Respects the same R10 offline mode and R7 lookup cache as the pipeline: a re-run
hits the cache identically. Use ``--device`` to force a fresh disc read.
"""

from __future__ import annotations

import argparse
import copy
import subprocess
import sys
import tempfile
from pathlib import Path

# tools/ is on sys.path[0] when run as a script, so the sibling import works at
# runtime; ty searches only src/ + repo root, so the resolution is suppressed.
from trace_album import resolve_seed_rip  # ty: ignore[unresolved-import]

from cdda2img.cddb import compute_cddb_disc_id, query_cddb
from cdda2img.cdrdao_reader import parsed_to_rbi_disc
from cdda2img.mb_lookup import (
    _merge_into_disc,
    disc_id_from_rbi,
    lookup_disc_id,
    prepopulate_from_mb,
)
from cdda2img.rbi_format import RBIDisc
from cdda2img.toc_parser import parse_toc


def _rule(char: str = "─", width: int = 78) -> None:
    print(char * width)


def obtain_toc(device: str | None, toc_path: Path | None) -> bytes:
    """Return cdrdao TOC bytes — from *toc_path* if given, else read the disc.

    The disc read uses ``read-toc --fast-toc``: it captures CD-Text, per-track
    ISRC, the MCN, and track offsets without extracting audio. ``--fast-toc``
    skips only the slow pre-gap/index analysis, which does not affect the album
    title or any of the disc IDs.
    """
    if toc_path is not None:
        print(f"Using pre-captured TOC: {toc_path}")
        return toc_path.read_bytes()

    if device is None:
        msg = "either --toc or --device is required"
        raise SystemExit(msg)

    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir) / "trace.toc"
        cmd = ["cdrdao", "read-toc", "--fast-toc", "--device", device, str(out)]
        print(f"Reading disc TOC: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)  # noqa: S603
        if result.returncode != 0 or not out.exists():
            sys.stderr.write(result.stderr)
            msg = f"cdrdao read-toc failed (exit {result.returncode})"
            raise SystemExit(msg)
        return out.read_bytes()


def _seed_disc(toc_bytes: bytes) -> tuple[RBIDisc, list[int], int]:
    """Parse the TOC into the rip-time seed disc and its CDDB LSN inputs.

    Mirrors ``cdrdao_ripper.rip_cdrdao``: same ``parsed_to_rbi_disc`` seed and
    the same track-LSN / last-LSN arithmetic.
    """
    parsed = parse_toc(toc_bytes)
    disc = parsed_to_rbi_disc(parsed)
    track_lsns = [pt.start_frame + pt.pregap_frames for pt in parsed.tracks]
    last = parsed.tracks[-1]
    disc_last_lsn = last.start_frame + last.pregap_frames + last.duration_frames - 1
    return disc, track_lsns, disc_last_lsn


def _dump_mb_matches(seed: RBIDisc) -> None:
    """Print the raw MB disc-ID match list — the P2-B diagnosis raw material.

    This is the same ``lookup_disc_id`` the pipeline calls; the disambiguator
    (``prepopulate_from_mb``) picks one of these. Showing every candidate's
    barcode/year/country is what the next step needs to see why the wrong one
    was chosen. Captured, not analysed.
    """
    matches = lookup_disc_id(seed)
    print(f"  MB disc-ID matches: {len(matches)}")
    for i, m in enumerate(matches):
        print(
            f"    [{i}] album={m.album!r} year={m.release_date!r} "
            f"barcode={m.barcode!r} country={m.country!r}"
        )
        print(
            f"        mbid={m.mb_release_id!r} rg={m.mb_release_group_id!r} "
            f"label={m.label!r} cat#={m.catalog_number!r}"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--device", default=None, help="optical device, e.g. /dev/sr0")
    p.add_argument("--toc", type=Path, default=None, help="reuse a captured fast-toc")
    p.add_argument("--server", default=None, help="CDDB server host:port override")
    args = p.parse_args(argv)

    toc_bytes = obtain_toc(args.device, args.toc)
    seed, track_lsns, disc_last_lsn = _seed_disc(toc_bytes)

    _rule("═")
    print("  STAGE 0 — seed from disc (cdrdao TOC)")
    _rule()
    print(f"  album (CD-Text TITLE) : {seed.album!r}")
    print(f"  catalog (MCN)         : {seed.catalog!r}")
    print(f"  tracks                : {len(seed.tracks)}")
    print(f"  ISRCs                 : {[t.isrc for t in seed.tracks]}")
    print(f"  MB disc ID            : {disc_id_from_rbi(seed)}")
    print(
        f"  CDDB disc ID          : {compute_cddb_disc_id(track_lsns, disc_last_lsn)}"
    )
    cdtext_seed = seed.album or ""

    # ----- STAGE 1: CDDB (live) — mirrors _finalize_import's cddb_future -----
    _rule("═")
    print("  STAGE 1 — CDDB (live query)")
    _rule()
    cddb_matches = query_cddb(track_lsns, disc_last_lsn, args.server)
    cddb_raw = cddb_matches[0].album if cddb_matches else None
    print(f"  CDDB matches          : {len(cddb_matches)}")
    print(f"  CDDB matches[0].album : {cddb_raw!r}  (raw candidate; applied LAST)")

    # ----- STAGE 2: MusicBrainz (live) — mirrors mb_future ------------------
    _rule("═")
    print("  STAGE 2 — MusicBrainz (live disc-ID lookup)")
    _rule()
    _dump_mb_matches(seed)
    mb_result = prepopulate_from_mb(copy.deepcopy(seed), verbose=False)
    print(
        f"  MB winner album       : {mb_result.mb_candidate_album!r}  (raw candidate)"
    )
    print(f"  MB match_count        : {mb_result.match_count}")
    print(f"  MB isrc_disambiguated : {mb_result.isrc_disambiguated}")
    print(f"  MB barcode_hints      : {mb_result.barcode_hints}")
    mb_raw = mb_result.mb_candidate_album

    # ----- STAGE 3: precedence merge exactly as _run_metadata_lookups --------
    # MB applied first (over the CD-Text seed); CDDB applied LAST as the
    # lowest-precedence gap-filler. (Discogs/AcoustID sit between in the real
    # pipeline but don't contest album.)
    _rule("═")
    print("  STAGE 3 — precedence merge (MB first, CDDB last / lowest)")
    _rule()
    disc = mb_result.disc
    if cddb_matches:
        disc = _merge_into_disc(cddb_matches[0], disc)
    real_album = disc.album
    print(f"  REAL pipeline album   : {real_album!r}")

    # ----- Cross-check against the static model -----------------------------
    _rule("═")
    print("  CROSS-CHECK — trace_album.resolve_seed_rip (raw candidates)")
    _rule()
    model_album, winner = resolve_seed_rip(cdtext_seed, cddb_raw, mb_raw)
    print(
        f"  model inputs          : cdtext={cdtext_seed!r} cddb={cddb_raw!r} "
        f"mb={mb_raw!r}"
    )
    print(f"  model album           : {model_album!r}  (winner: {winner})")
    print(f"  real  album           : {real_album!r}")
    match = (model_album or "") == (real_album or "")
    print()
    if match:
        print("  ✓ MODEL MATCHES PIPELINE — trace_album.py is faithful for this disc.")
    else:
        print("  ✗ MODEL DIVERGES — trace_album.py needs refinement.")
    return 0 if match else 1


if __name__ == "__main__":
    sys.exit(main())
