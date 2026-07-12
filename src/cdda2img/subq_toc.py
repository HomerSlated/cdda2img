"""Assemble a rip's disc metadata from an AccuDisc single-pass capture (F7).

This is the join point of the read-only-ripping upgrade plan (docs/reference/
c2read-upgrade-plan.md): it turns the raw artefacts one AccuDisc capture produces
(audio + C2 + raw P-W sub from ``read``, plus the ``fulltoc``/``cdtext`` lead-in
dumps) into the same :class:`RipInfo` the cdrdao read paths return, letting the
C2 rip path drop its second full-disc ``cdrdao read-toc`` metadata pass. (The tool
was the ``c2read`` prototype; the pipeline now drives its successor, AccuDisc.)

Inputs and their roles:

- **full TOC** (``parse_fulltoc``) — authoritative track starts, session
  structure, lead-out. Error-corrected lead-in data; never overridden by Q.
- **Q sub-channel stream** (``derive_track_layout`` + ``scan_subcode``) —
  pre-gap lengths, INDEX >= 02 points, per-track CONTROL flags, and the
  majority-voted MCN/ISRC. Raw, unprotected data; every datum is aggregated,
  and a stream that cannot be anchored degrades to no-pregap defaults rather
  than failing the rip.
- **CD-Text** (``parse_cdtext``, optional) — album/track titles and performers,
  DISC_ID (the label catalogue string). Q remains authoritative for MCN/ISRC;
  the CD-Text 0x8E copies are ignored.

Geometry mirrors the cdrdao paths exactly: the PCM from an AccuDisc ``read`` is
the contiguous audio area ``[0, lead-out)``, so disc LBA == PCM frame offset. A
track's slot starts at its pre-gap (``start_frame``), and ``track_lsns`` carry the
INDEX 01 (audio start) positions for CDDB/AR. Track 1's standard 150-frame pre-gap
lies before LBA 0 (not in the PCM), matching cdrdao read-cd BIN layout — but when
track 1's INDEX 01 sits at LBA > 0 (a *program-area* pre-gap, e.g. ABBA *Gold*'s
33 frames), those frames ARE in the ``[0, lead-out)`` PCM and are declared as
track 1's pre-gap; dropping them would shift every boundary and the lead-out (and
so the disc ID) down by that amount.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cdda2img.cdtext import PTI_TITLE, parse_cdtext
from cdda2img.disc_reader import RipInfo
from cdda2img.rbi_format import RBIDisc, RBITocEntry
from cdda2img.subchannel import (
    SubcodeScan,
    TrackLayout,
    derive_track_layout,
    parse_fulltoc,
    scan_subcode,
    session1_audio_tracks,
)
from cdda2img.validators import validate_isrc

if TYPE_CHECKING:
    from cdda2img.cdtext import CDTextBlock

log = logging.getLogger(__name__)

_ALL_ZEROS_MCN = "0000000000000"


def build_rip_info(
    fulltoc_raw: bytes,
    sub_data: bytes,
    cdtext_raw: bytes | None = None,
) -> RipInfo:
    """Build a :class:`RipInfo` from single-pass captures.

    The returned RipInfo carries read-stage provenance keys in ``prov``
    (``toc_source``, Q-frame stats, per-track ISRC vote counts). Raises
    ValueError for discs outside the CD-DA archival model (mixed-mode
    session 1, no session-1 lead-out). A Q stream that cannot be anchored or
    read degrades to no-pregap defaults with a warning — the rip continues.
    """
    toc = parse_fulltoc(fulltoc_raw)
    toc_tracks, leadout = session1_audio_tracks(toc)
    if not toc_tracks:
        msg = "full TOC contains no session-1 tracks"
        raise ValueError(msg)
    track_starts = {t.track: t.start_lba for t in toc_tracks}

    prov: dict[str, str] = {"toc_source": "subq@accudisc"}
    layout = _derive_layout(sub_data, track_starts, leadout, prov)
    scan = scan_subcode(sub_data, leadout_lba=leadout)
    isrcs = _voted_isrcs(scan, prov)
    mcn = _voted_mcn(scan)
    cdtext = _first_cdtext_block(cdtext_raw)
    if cdtext is not None and not _cdtext_matches_disc(cdtext, set(track_starts)):
        # The CD-Text describes a different track set than the disc actually has:
        # a stale sidecar from a prior rip, or lead-in the drive cached from the
        # previously-loaded disc. Trusting it bakes a wrong album into the image
        # (a no-CD-Text disc inheriting the last disc's titles), so discard it and
        # let the online lookups supply the metadata. Prefer no CD-Text over wrong.
        log.warning(
            "CD-Text track range does not match disc tracks %d-%d "
            "(cdtext first/last=%s/%s) - discarding as stale/foreign",
            min(track_starts),
            max(track_starts),
            cdtext.first_track,
            cdtext.last_track,
        )
        prov["cdtext_rejected"] = "track_range_mismatch"
        cdtext = None

    entries: list[RBITocEntry] = []
    starts = sorted(track_starts.items())
    for i, (number, start) in enumerate(starts):
        next_start = starts[i + 1][1] if i + 1 < len(starts) else leadout
        next_pregap = (
            layout.pregap_frames.get(starts[i + 1][0], 0)
            if layout is not None and i + 1 < len(starts)
            else 0
        )
        pregap = layout.pregap_frames.get(number, 0) if layout is not None else 0
        if number == starts[0][0]:
            # Track 1's slot begins at LBA 0 == PCM frame 0. When its INDEX 01 sits
            # at LBA ``start`` > 0, those ``start`` frames (LBA 0..start-1) are a
            # *program-area* pre-gap that the ``[0, lead-out)`` read already captured
            # into the PCM, so they must be declared as track 1's pre-gap. Dropping
            # them (the old ``pregap = 0``) left ``start_frame`` = ``start`` with no
            # ``START`` line, which made ``generate_toc`` emit ``FILE … <start>`` —
            # skipping and orphaning those frames, shifting every track start and the
            # lead-out down by ``start`` and changing the MB/CDDB disc ID (ABBA
            # *Gold*: start=33). A normal disc has INDEX 01 at LBA 0 (``start`` == 0),
            # so this is a no-op there; the standard 150-frame lead-in pre-gap lies
            # before LBA 0 and is genuinely not in the PCM, so it stays unrepresented.
            pregap = start
        control = layout.control.get(number) if layout is not None else None
        audio_start = start
        indices = (
            [lba - audio_start for _, lba in layout.index_points.get(number, [])]
            if layout is not None
            else []
        )
        entries.append(
            RBITocEntry(
                track_number=number,
                title=(cdtext.track_title(number) if cdtext else None) or "",
                performer=(cdtext.track_performer(number) if cdtext else None) or "",
                start_frame=audio_start - pregap,
                duration_frames=(next_start - next_pregap) - audio_start,
                pregap_frames=pregap,
                isrc=isrcs.get(number),
                pre_emphasis=bool(control and control.pre_emphasis),
                copy_permitted=bool(control and control.copy_permitted),
                index_points=indices,
            )
        )

    disc = RBIDisc(
        album=(cdtext.album_title if cdtext else None) or "",
        artist=(cdtext.album_performer if cdtext else None) or "",
        catalog=mcn,
        cdtext_catalog_ref=cdtext.disc_id if cdtext else None,
        pre_emphasis=(
            any(c.pre_emphasis for c in layout.control.values())
            if layout is not None and layout.control
            else None
        ),
        tracks=entries,
    )
    return RipInfo(
        disc=disc,
        track_lsns=[start for _, start in starts],
        disc_last_lsn=leadout - 1,
        prov=prov,
    )


def _derive_layout(
    sub_data: bytes,
    track_starts: dict[int, int],
    leadout: int,
    prov: dict[str, str],
) -> TrackLayout | None:
    """Track layout from the Q stream; None (degrade, don't fail) when unusable."""
    try:
        layout = derive_track_layout(sub_data, track_starts, leadout)
    except ValueError as exc:
        log.warning("Q stream unusable for track layout (%s) — no pre-gap data", exc)
        prov["subq_layout"] = "unanchored"
        return None
    prov["subq_frames"] = f"{layout.frames_used}used/{layout.frames_dropped_slip}slip"
    return layout


def _voted_isrcs(scan: SubcodeScan, prov: dict[str, str]) -> dict[int, str]:
    """Majority-voted per-track ISRCs from the Q scan (F4)."""
    isrcs: dict[int, str] = {}
    for d in scan.data:
        if d.type != "ISRC" or not d.region.startswith("track") or d.value is None:
            continue
        isrc = validate_isrc(d.value)
        if isrc is None:
            continue
        number = int(d.region.removeprefix("track "))
        isrcs[number] = isrc
        prov[f"isrc_vote_track_{number}"] = str(d.count)
        if d.runner_up is not None:
            prov[f"isrc_vote_track_{number}_runner_up"] = (
                f"{d.runner_up[0]}x{d.runner_up[1]}"
            )
    return isrcs


def _voted_mcn(scan: SubcodeScan) -> str | None:
    """Majority-voted MCN, all-zeros treated as absent (as the TOC parser does)."""
    for d in scan.data:
        if d.type == "MCN" and d.value and d.value != _ALL_ZEROS_MCN:
            return d.value
    return None


def _first_cdtext_block(cdtext_raw: bytes | None) -> CDTextBlock | None:
    """Block 0 of the CD-Text capture, or None when absent/undecodable."""
    if not cdtext_raw:
        return None
    try:
        blocks = parse_cdtext(cdtext_raw)
    except ValueError as exc:
        log.warning("CD-Text capture undecodable (%s) — ignored", exc)
        return None
    return blocks[0] if blocks else None


def _cdtext_matches_disc(cdtext: CDTextBlock, track_numbers: set[int]) -> bool:
    """True when *cdtext* describes THIS disc's tracks, not a foreign/stale set.

    The SIZE_INFO pack is CD-Text's own declaration of the track range it covers;
    when present it must match the disc's first and last audio-track numbers
    exactly. Without SIZE_INFO, fall back to the observed per-track titles:
    genuine disc CD-Text titles every audio track, so the lowest and highest
    titled track must coincide with the disc's own range. An album-level-only
    block (no per-track titles) carries nothing that can contradict the disc, so
    it is allowed through.
    """
    first, last = min(track_numbers), max(track_numbers)
    if cdtext.first_track is not None and cdtext.last_track is not None:
        return cdtext.first_track == first and cdtext.last_track == last
    titled = {n for n in cdtext.text.get(PTI_TITLE, {}) if n > 0}
    if not titled:
        return True
    return min(titled) == first and max(titled) == last
