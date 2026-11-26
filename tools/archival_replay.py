#!/usr/bin/env python3
"""Replay an existing RBI's metadata lookups against the live services and the
archival gates of 2026-11-21, with no drive.

The gates (``rip_abort``; ``cdda2img._require_service_answers``,
``_require_acoustid_identity``, ``_require_cover_art``) were unit-tested on
hand-written PROV, which cannot show what the real lookup ORDER produces: for
instance a service that runs before the fallback that identifies the release.
This rebuilds each container's disc as the drive would have delivered it (track
geometry, CD-Text titles, album and artist, ISRCs, the MCN; no MusicBrainz,
Discogs or catalogue fields), runs ``_run_metadata_lookups`` for real, and
reports which gate, if any, would abort.

    uv run python tools/archival_replay.py "rips/cdda2img/Tracy Chapman.rbi"
    uv run python tools/archival_replay.py --digital-media "rips/cdda2img/Toca (20th Anniversary Edition).rbi"

The PCM is copied to ``--scratch`` (default ``/var/tmp``, never ``/tmp``) for the
fingerprinting and removed afterwards.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


def _as_ripped(disc, prov: dict[str, str], *, keep_isrcs: bool = False):
    """The disc with everything a lookup supplies stripped back to blank.

    The container's TOC is the FINISHED record, so its MCN and ISRCs may have
    come from the lookups rather than the disc (kgr, 2026-11-21: ABBA *Gold*'s
    disc carries neither). Feeding them back as if the drive had read them
    rejected all four of ABBA's disc-ID matches for "contradicting" ISRCs that
    were MusicBrainz's own from another release, and sent the replay down the
    duration match. So: the MCN is kept only when PROV ``mcn_source=disc``, and a
    track's ISRC only when PROV has ``isrc_vote_track_<n>`` (written when the
    rip read it from the Q channel; older rips record no such key, so their
    ISRCs are dropped unless *keep_isrcs*, for a disc known to carry them, e.g.
    a CD-R burned from the RBI's own TOC). CD-Text (album, artist, titles) is
    kept: the TOC holds what the disc's lead-in carried.
    """
    from cdda2img.rbi_format import RBIDisc

    keep = {
        "album",
        "artist",
        "tracks",
        "pre_emphasis",
        "cdtext_catalog_ref",
    }
    blank = {
        f.name: None
        for f in dataclasses.fields(RBIDisc)
        if f.name not in keep and f.default is None
    }
    if prov.get("mcn_source") == "disc":
        blank.pop("catalog", None)
    tracks = [
        t
        if keep_isrcs or f"isrc_vote_track_{t.track_number}" in prov
        else dataclasses.replace(t, isrc=None)
        for t in disc.tracks
    ]
    return dataclasses.replace(disc, **blank, tracks=tracks)


def _read_prov(f, header) -> dict[str, str]:
    from cdda2img.rbi_format import BLOCK_TYPE_PROV

    entry = header.find_block(BLOCK_TYPE_PROV)
    if entry is None:
        return {}
    f.seek(entry.offset)
    lines = f.read(entry.length).decode("utf-8", "replace").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def replay(
    rbi: Path, *, digital_media: bool, scratch: Path, keep_isrcs: bool = False
) -> int:
    from cdda2img import cdda2img as app
    from cdda2img import mb_lookup, query_log
    from cdda2img.cdrdao_reader import parsed_to_rbi_disc
    from cdda2img.container import read_header
    from cdda2img.rbi_format import BLOCK_TYPE_PCM, BLOCK_TYPE_TOC
    from cdda2img.rip_abort import RipAborted
    from cdda2img.toc_parser import parse_toc

    mb_lookup._setup_useragent()  # the pipeline does this; a lookup without it fails
    header = read_header(rbi)
    toc_e, pcm_e = header.find_block(BLOCK_TYPE_TOC), header.find_block(BLOCK_TYPE_PCM)
    if toc_e is None or pcm_e is None:
        print(f"== {rbi.name}: no TOC or PCM block, skipped")
        return 1
    with rbi.open("rb") as f:
        stored = _read_prov(f, header)
        f.seek(toc_e.offset)
        disc = _as_ripped(
            parsed_to_rbi_disc(parse_toc(f.read(toc_e.length))),
            stored,
            keep_isrcs=keep_isrcs,
        )
        print(
            f"-- {rbi.name} as ripped: MCN {disc.catalog or 'none'}, ISRCs on"
            f" {sum(1 for t in disc.tracks if t.isrc)}/{len(disc.tracks)} tracks"
        )
        with tempfile.TemporaryDirectory(dir=scratch) as td:
            pcm = Path(td) / "disc.pcm"
            f.seek(pcm_e.offset)
            with pcm.open("wb") as out:
                remaining = pcm_e.length
                while remaining:
                    chunk = f.read(min(remaining, 1 << 24))
                    out.write(chunk)
                    remaining -= len(chunk)
            lsns, lba = [], 0
            for t in disc.tracks:
                lsns.append(t.start_frame + t.pregap_frames)
                lba = t.start_frame + t.pregap_frames + t.duration_frames
            query_log.start()
            prov: dict[str, str] = {}
            disc, _mb = app._run_metadata_lookups(
                disc,
                pcm,
                prov,
                do_cddb=not digital_media,
                cddb_track_lsns=None if digital_media else lsns,
                cddb_disc_last_lsn=None if digital_media else lba - 1,
                cddb_server=None,
                cddb_verbose=False,
                mb_verbose=False,
                preferred_country=[],
                ui=None,
                digital_media=digital_media,
            )
            print(f"== {rbi.name} ({'digital media' if digital_media else 'pressed'})")
            for k in sorted(prov):
                if k.startswith((
                    "lookup_status",
                    "lookup_error",
                    "mb_",
                    "release_",
                    "discogs_barcode",
                    "discogs_release",
                    "acoustid",
                    "duration_match",
                    "digital_",
                )):
                    print(f"   {k} = {prov[k]}")
            # What the archive would carry, not just what PROV says was asked: a
            # gate that passed while both release ids were missing is how the
            # --digital-media identity gap went unseen (2026-11-21).
            print(f"   disc.mb_release_id = {disc.mb_release_id}")
            try:
                app._archival_gates(
                    disc, pcm, prov, None, archival=True, digital_media=digital_media
                )
                # No menu here, as under --auto: the pressing stays as pinned, and
                # the unattended post-menu step runs (trust_model_design.md §12 D7).
                disc = app._discogs_after_menu(
                    disc, prov, disc.mb_release_id, masters=not digital_media
                )
                app._settle_discogs_master(disc)
                print(f"   disc.discogs_release_id = {disc.discogs_release_id}")
                print(f"   disc.discogs_master_id = {disc.discogs_master_id}")
                print(f"   discogs_release = {prov.get('discogs_release')}")
                app._require_discogs_release(prov, archival=True)
            except RipAborted as e:
                print(f"   WOULD ABORT (exit {e.code}): {e.reason}")
                for d in e.details:
                    print(f"     {d}")
                return 1
    print("   would pass the gates (no menu, as under --auto)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("rbi", nargs="+", type=Path)
    ap.add_argument("--digital-media", action="store_true")
    ap.add_argument(
        "--keep-isrcs",
        action="store_true",
        help="the disc carries the TOC's ISRCs (e.g. a CD-R burned from this RBI)",
    )
    # Real disk, never /tmp (RAM-backed; the scratch scope rule).
    ap.add_argument("--scratch", type=Path, default=Path("/var/tmp"))  # noqa: S108
    args = ap.parse_args()
    rc = 0
    for rbi in args.rbi:
        rc |= replay(
            rbi,
            digital_media=args.digital_media,
            scratch=args.scratch,
            keep_isrcs=args.keep_isrcs,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
