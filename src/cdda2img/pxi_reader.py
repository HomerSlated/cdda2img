"""
pxi_reader.py — PlexTools ``.pxi`` disc image importer.

PlexTools was Plextor's own bundled ripping/burning application; the format is
undocumented and was reverse-engineered here against a known-good disc (ABBA
*Gold*, whose TOC and audio we also hold as a verified AccuDisc rip).

Layout — every multi-byte field is little-endian **except** the CD-Text length,
which is a verbatim MMC big-endian READ TOC header::

    0x00000000  magic ``PXI\\0``
    0x0000000B  CD-Text: a raw READ TOC format 0x05 response — BE u16 length,
                2 reserved bytes, then 18-byte packs.  ``cdtext.py`` consumes
                this shape directly, CRCs and SIZE_INFO included.
    0x00008000  TOC block: lead-out at +0x0B (u32), track count at +0x47 (u8),
                MCN at +0x4C (13 ASCII bytes, all-``0`` when absent)
    0x00008067  index table: 36-byte records, session +0, track +4,
                position +16, length +20.  Terminated by an all-zero record.
    0x0006007B  audio, raw s16le, contiguous to EOF

**Positions are absolute frame addresses (LBA + 150), not LBAs** — the single
fact the whole format turns on.  Each record is one INDEX point: the first
record of a track is INDEX 00, the rest are INDEX 01, 02, …  Track 1's INDEX 00
sits at absolute frame 0, i.e. LBA -150, describing lead-in that no image can
contain; its start is clamped to LBA 0.

Audio origin (``_AUDIO_OFFSET``) is not derivable from any field, but it *is*
derivable from each file's own arithmetic: ``size - lead_out * 2352`` is one
equation in one unknown, so every image determines its origin exactly rather
than modulo the sector size.  All four images we hold answer **0x60003**, across
three discs and a month apart (``tools/pxi_probe.py``).

**PXI stores RAW audio** — settled 2026-08-11 (N7), having been explicitly
recorded as unresolved before that, correctly: the first image could not settle
it.  The reader used to start at ``0x6007B``, which is this origin plus 120
bytes, and 120 bytes is exactly the +30-sample read offset of the drive that
wrote these images.  So the file appeared to stop 120 bytes short of a whole
final sector when in truth the assumed origin was 120 bytes too high, and those
bytes are present at the *head*, not missing from the tail.  kgr called that in
advance: not a bug, a purpose.

What settled it is AccurateRip, which is offset-sensitive by construction.
Feeding it the audio from the measured origin, **+30 verifies and 0 does not**:

    disc A (11 tr)   +30: 11/11 conf 4400      0: 0/11 conf 0
    disc A (re-rip)  +30: 11/11 conf 4400      0: 0/11 conf 0
    disc B (19 tr)   +30: 18/19               0: no match
    disc C (12 tr)   +30: 11/12               0: no match

That refutes "already corrected" without needing to know the drive: correction
is correction, so a corrected stream would verify at 0 whatever wrote it.  Every
*other* verifying offset (-639, -1967, +1573 …) differs per disc — pressing
cohorts, as ``accuraterip.detect_offset`` warns.  +30 is the only offset common
to all four, which is the signature of a drive rather than a disc.

Consequence for this reader: the stored audio needs the writing drive's read
offset applied, and the file records no drive identity — see
``_PLEXTOOLS_READ_OFFSET``.  The previous code reached byte-identical output by
accident, reading from origin+120 and zero-filling the tail; that is now stated
rather than implied, so a ``.pxi`` from a drive with a different offset is a
known limitation instead of a silent 30-sample shift.
"""

from __future__ import annotations

import logging
import struct
from collections.abc import Callable
from pathlib import Path
from typing import IO, NamedTuple

from cdda2img.cdtext import CDTextBlock, parse_cdtext
from cdda2img.rbi_format import FLAG_MASTER_MODE, RBIDisc, RBITocEntry

log = logging.getLogger(__name__)

_CHUNK_BYTES = 1 << 20  # 1 MiB copy buffer

_MAGIC = b"PXI\x00"

# CD-Text — a verbatim MMC READ TOC format 0x05 response.
_CDTEXT_OFFSET = 0x0B
_CDTEXT_LEN_BYTES = 2  # the BE u16 length counts everything after itself

# TOC block, and field offsets within it.
_TOC_OFFSET = 0x8000
_TOC_LEADOUT = 0x0B  # u32, absolute frame
_TOC_TRACK_COUNT = 0x47  # u8
_TOC_MCN = 0x4C  # 13 ASCII bytes
_TOC_MCN_LEN = 13

# Index table: 36-byte records, first at this absolute file offset.
_INDEX_TABLE_OFFSET = 0x8067
_INDEX_RECORD_BYTES = 36
_IDX_SESSION = 0  # u32
_IDX_TRACK = 4  # u32
_IDX_POSITION = 16  # u32, absolute frame (LBA + 150)
_IDX_LENGTH = 20  # u32, frames
_MAX_INDEX_RECORDS = 99 * 100  # 99 tracks x 100 index points, a runaway guard

# Audio. See the module docstring — measured, not derived.
#: Raw-audio origin. Measured, not derived: ``size - lead_out * 2352`` on every
#: image we hold (three discs, four rips) — see the module docstring.
_AUDIO_OFFSET = 0x60003

#: Read offset, in samples, of the drive that wrote the image. The file records
#: no drive identity, so this is an assumption, and it is the one assumption in
#: this module that silently changes the audio rather than failing: a ``.pxi``
#: written by a drive with a different offset imports shifted by the difference.
#: +30 is the PX-716A, measured through AccurateRip on all four images we hold
#: (+30 verifies, 0 does not). Kept as a named parameter so PROV can record what
#: was assumed and a future image from another drive has somewhere to say so.
_PLEXTOOLS_READ_OFFSET = 30

_CDDA_SECTOR_BYTES = 2352
_BYTES_PER_SAMPLE = 4  # 16-bit stereo
_LEAD_IN_FRAMES = 150  # absolute frame 150 == LBA 0
_MAX_TRACKS = 99


class PXIError(ValueError):
    """A ``.pxi`` file that cannot be parsed as a single-session CD-DA image."""


# ---------------------------------------------------------------------------
# Index table
# ---------------------------------------------------------------------------


def _read_index_records(buf: bytes, filename: str) -> list[tuple[int, int, int, int]]:
    """Return ``(session, track, position, length)`` per index record.

    Reads until an all-zero record or the end of the buffer.  Positions are
    absolute frames as stored; no LBA conversion happens here.
    """
    records: list[tuple[int, int, int, int]] = []
    off = _INDEX_TABLE_OFFSET - _TOC_OFFSET
    while len(records) < _MAX_INDEX_RECORDS:
        if off + _INDEX_RECORD_BYTES > len(buf):
            break
        session = struct.unpack_from("<I", buf, off + _IDX_SESSION)[0]
        track = struct.unpack_from("<I", buf, off + _IDX_TRACK)[0]
        position = struct.unpack_from("<I", buf, off + _IDX_POSITION)[0]
        length = struct.unpack_from("<I", buf, off + _IDX_LENGTH)[0]
        if not (session or track or position or length):
            break
        records.append((session, track, position, length))
        off += _INDEX_RECORD_BYTES
    else:
        # Log when the guard binds: a truncated table would otherwise surface
        # as "the table does not reach the lead-out", a confident diagnosis of
        # a different fault (cf. acoustid_lookup._MAX_RELEASE_PAGES).
        log.warning(
            "%s: index table still had records at the %d-record guard;"
            " what follows describes a truncated table, not the disc",
            filename,
            _MAX_INDEX_RECORDS,
        )

    if not records:
        msg = f"{filename}: index table is empty — not a PlexTools CD-DA image"
        raise PXIError(msg)
    return records


def _group_by_track(
    records: list[tuple[int, int, int, int]], filename: str
) -> list[list[tuple[int, int]]]:
    """Group index records into one ``[(position, length), …]`` list per track.

    Tracks must appear once, contiguously, in ascending order — the format has
    no facility for anything else and a file that violates it is not one we
    understand well enough to import.
    """
    sessions = {r[0] for r in records}
    if sessions != {1}:
        msg = (
            f"{filename}: multi-session images are not supported"
            f" (sessions found: {sorted(sessions)})"
        )
        raise PXIError(msg)

    groups: list[list[tuple[int, int]]] = []
    seen: list[int] = []
    for _session, track, position, length in records:
        if not seen or track != seen[-1]:
            if track in seen:
                msg = f"{filename}: track {track} appears in two separate runs of the index table"
                raise PXIError(msg)
            if seen and track < seen[-1]:
                msg = f"{filename}: track {track} follows track {seen[-1]} — index table is not ascending"
                raise PXIError(msg)
            seen.append(track)
            groups.append([])
        groups[-1].append((position, length))

    if seen != list(range(seen[0], seen[0] + len(seen))):
        msg = f"{filename}: track numbers are not consecutive: {seen}"
        raise PXIError(msg)
    if len(groups) > _MAX_TRACKS:
        msg = f"{filename}: {len(groups)} tracks exceeds the Red Book maximum of {_MAX_TRACKS}"
        raise PXIError(msg)
    return groups


def _check_track_count(
    declared: int, groups: list[list[tuple[int, int]]], first_track: int, filename: str
) -> None:
    """Cross-check the ``0x47`` byte against the index table, without overruling it.

    Whether that byte holds a track *count* or the *last track number* is
    **unresolved**: the two coincide on every disc whose first track is 1, which
    is every sample we have, and MMC TOC structures more often store the last
    track.  Both readings are therefore accepted.

    A disagreement warns rather than raises, and the table wins.  The table is
    independently validated — contiguous, and it reaches the lead-out — so
    refusing a file this parser demonstrably read correctly would be the worse
    failure, and it is the one an unresolved field would cause.
    """
    last_track = first_track + len(groups) - 1
    if declared in (len(groups), last_track):
        return
    log.warning(
        "%s: TOC byte 0x%02x says %d, but the index table describes %d track(s)"
        " numbered %d-%d; trusting the table",
        filename,
        _TOC_TRACK_COUNT,
        declared,
        len(groups),
        first_track,
        last_track,
    )


def _validate_layout(
    records: list[tuple[int, int, int, int]],
    groups: list[list[tuple[int, int]]],
    leadout: int,
    filename: str,
) -> None:
    """Check the index table describes a gapless disc ending at the lead-out."""
    for (_s, track, position, length), (_s2, _t2, next_position, _l2) in zip(
        records, records[1:]
    ):
        if position + length != next_position:
            msg = (
                f"{filename}: gap in the index table at track {track}:"
                f" frame {position}+{length} does not meet {next_position}"
            )
            raise PXIError(msg)

    _s, _t, last_position, last_length = records[-1]
    if last_position + last_length != leadout:
        msg = (
            f"{filename}: index table ends at frame {last_position + last_length}"
            f" but the lead-out is at {leadout}"
        )
        raise PXIError(msg)

    for group in groups:
        if len(group) < 2:
            msg = (
                f"{filename}: a track has {len(group)} index point(s);"
                " INDEX 00 and INDEX 01 are both required"
            )
            raise PXIError(msg)


# ---------------------------------------------------------------------------
# CD-Text
# ---------------------------------------------------------------------------


def _read_cdtext(raw_head: bytes, filename: str) -> CDTextBlock | None:
    """Decode block 0 of the embedded CD-Text, or None when absent/undecodable."""
    if len(raw_head) < _CDTEXT_OFFSET + _CDTEXT_LEN_BYTES:
        return None
    declared = struct.unpack_from(">H", raw_head, _CDTEXT_OFFSET)[0]
    if declared <= _CDTEXT_LEN_BYTES:
        return None  # header present, no packs

    end = _CDTEXT_OFFSET + _CDTEXT_LEN_BYTES + declared
    if end > len(raw_head):
        log.warning(
            "%s: CD-Text declares %d bytes but the header region holds %d — ignoring",
            filename,
            declared,
            len(raw_head) - _CDTEXT_OFFSET - _CDTEXT_LEN_BYTES,
        )
        return None

    try:
        blocks = parse_cdtext(raw_head[_CDTEXT_OFFSET:end])
    except ValueError as exc:
        log.warning("%s: CD-Text did not decode (%s) — ignoring", filename, exc)
        return None
    return next((b for b in blocks if b.block == 0), None)


def _cdtext_matches_disc(block: CDTextBlock, first: int, last: int) -> bool:
    """True when the CD-Text SIZE_INFO track range agrees with the disc's.

    Same guard as the rip path's (``subq_toc._cdtext_matches_disc``): prefer no
    CD-Text over CD-Text belonging to a different disc.  Absent SIZE_INFO is not
    a mismatch — it is no evidence either way.
    """
    if block.first_track is None or block.last_track is None:
        return True
    return block.first_track == first and block.last_track == last


# ---------------------------------------------------------------------------
# RBIDisc construction
# ---------------------------------------------------------------------------


def _build_disc(
    groups: list[list[tuple[int, int]]],
    first_track: int,
    catalog: str | None,
    cdtext: CDTextBlock | None,
) -> RBIDisc:
    """Build an RBIDisc from the grouped index points.

    ``start_frame`` is the PCM offset of the track's pre-gap and
    ``start_frame + pregap_frames`` its audio start — the LSN that AccurateRip,
    CDDB and the MB disc ID are all computed from.  Track 1's INDEX 00 addresses
    lead-in that the image cannot contain, so its start clamps to LBA 0 and the
    frames between LBA 0 and INDEX 01 become its pre-gap (the ABBA *Gold*
    program-area pre-gap case; see ``subq_toc.build_rip_info``).
    """
    album = (cdtext.album_title if cdtext else None) or ""
    artist = (cdtext.album_performer if cdtext else None) or ""

    disc = RBIDisc(
        album=album,
        artist=artist,
        catalog=catalog,
        cdtext_catalog_ref=cdtext.disc_id if cdtext else None,
    )

    for offset, group in enumerate(groups):
        number = first_track + offset
        index00_lba = group[0][0] - _LEAD_IN_FRAMES
        index01_lba = group[1][0] - _LEAD_IN_FRAMES

        start_frame = max(index00_lba, 0)
        pregap_frames = index01_lba - start_frame
        duration_frames = sum(length for _position, length in group[1:])
        index_points = [
            position - _LEAD_IN_FRAMES - index01_lba for position, _length in group[2:]
        ]

        title = (cdtext.track_title(number) if cdtext else None) or ""
        performer = (cdtext.track_performer(number) if cdtext else None) or artist

        disc.tracks.append(
            RBITocEntry(
                track_number=number,
                title=title,
                performer=performer,
                start_frame=start_frame,
                duration_frames=duration_frames,
                pregap_frames=pregap_frames,
                index_points=index_points,
            )
        )

    return disc


# ---------------------------------------------------------------------------
# PCM extraction
# ---------------------------------------------------------------------------


class OffsetCandidate(NamedTuple):
    """One sample offset at which the stored audio agrees with AccurateRip."""

    offset: int
    tracks_matched: int
    total_tracks: int


class OffsetResolution(NamedTuple):
    """The read offset to apply, and how it was arrived at."""

    offset: int
    source: str
    detail: str


#: Supplies the *confirmed* AccurateRip offsets for an image, given the parsed
#: disc, the file, and the measured audio origin.  ``None`` means "no evidence"
#: (offline, or the disc is not in the database) — distinct from an empty list,
#: which means the disc IS in AccurateRip and nothing verifies.  Injected rather
#: than imported so this module stays free of the network and of ``numpy``; the
#: same reason ``report`` is a sink rather than a ``print``.
OffsetCandidateSource = Callable[[RBIDisc, Path, int], list[OffsetCandidate] | None]


def _resolve_read_offset(
    candidates: list[OffsetCandidate] | None,
    prior: int = _PLEXTOOLS_READ_OFFSET,
) -> OffsetResolution:
    """Choose the read offset to apply, given AccurateRip's verifying offsets.

    **Never picks by proximity to zero, and never takes the top candidate.**
    Both are wrong here, measured on the four images we hold:

    - ``detect_offset``'s own docstring disowns the proximity tiebreak for
      anything but verifying an already-corrected rip, because a widely-pressed
      disc verifies at several offsets at once — one per pressing cohort.
    - Taking ``matches[0]`` picks **+1573** on our 12-track image, where the true
      drive offset ``+30`` ranks second.  Both are fully confirmed; ranking does
      not separate a drive offset from a pressing cohort.
    - A plausibility band does not either: Tracy Chapman yields 13 confirmed
      offsets of which **10** fall inside +/-1500 (-639, -651, -817, -1288,
      -1303, -1315, -1239, +1188 ...).  A cohort is free to land next to zero.

    What *does* separate them is the N7 discriminator: **a drive offset is common
    across images, a cohort differs per image.**  That cannot be evaluated from
    one file — but it has already been evaluated, over four images and one drive,
    and its answer is *prior*.  So the prior enters as **evidence**, not as a
    fallback, and AccurateRip's role is to confirm or contradict it:

    ``accuraterip_confirmed``
        The prior is among the confirmed offsets.  Use it.  This is the expected
        outcome for a `.pxi` from the drive the prior describes, and it holds on
        all four images we have.
    ``accuraterip_sole``
        AccurateRip confirms exactly one offset and it is not the prior.  There
        is no cohort ambiguity to fear, so the evidence wins — this is the case
        that makes the whole feature worth having, and it is announced loudly.
    ``assumed_ambiguous``
        Several offsets verify and the prior is not among them.  Genuine
        ambiguity with no drive evidence: **decline**, keep the prior, and record
        every candidate.  Guessing here would silently store audio aligned to
        another pressing.
    ``assumed``
        No evidence at all — offline, or the disc is not in AccurateRip.
    ``assumed_unverified``
        The disc is in AccurateRip and nothing verifies, at any offset.  That is
        a statement about the *audio*, not the offset, so it changes nothing
        about which offset to apply — but it is worth recording, because it is
        also what a damaged or mis-parsed image looks like.
    """
    if candidates is None:
        return OffsetResolution(prior, "assumed", "no AccurateRip evidence")
    if not candidates:
        return OffsetResolution(
            prior, "assumed_unverified", "in AccurateRip, verifies at no offset"
        )

    listed = ", ".join(f"{c.offset:+d}" for c in candidates)
    for c in candidates:
        if c.offset == prior:
            others = len(candidates) - 1
            return OffsetResolution(
                prior,
                "accuraterip_confirmed",
                f"{c.tracks_matched}/{c.total_tracks} tracks"
                + (
                    f"; {others} other offset(s) also verify: {listed}"
                    if others
                    else ""
                ),
            )

    if len(candidates) == 1:
        only = candidates[0]
        return OffsetResolution(
            only.offset,
            "accuraterip_sole",
            f"sole verifying offset {only.offset:+d}"
            f" ({only.tracks_matched}/{only.total_tracks} tracks);"
            f" the assumed {prior:+d} does NOT verify",
        )

    return OffsetResolution(
        prior,
        "assumed_ambiguous",
        f"{len(candidates)} offsets verify and {prior:+d} is not among them: {listed}",
    )


def _write_pcm(
    f: IO[bytes],
    total_frames: int,
    file_bytes: int,
    pcm_out: Path,
    filename: str,
    read_offset: int = _PLEXTOOLS_READ_OFFSET,
) -> int:
    """Copy the audio to *pcm_out*, offset-corrected; return the pad bytes added.

    The copy is verbatim — PXI stores s16le, the byte order the RBI PCM block
    wants — but it does not start at the audio origin.  The stored audio is raw
    (module docstring), and the rest of the pipeline expects the PCM block to be
    offset-corrected, so the read starts ``read_offset`` samples in.  That leaves
    the same number of samples missing at the far end, which are zero-filled: the
    PCM block and the TOC must agree on length because every downstream consumer
    slices the PCM using the TOC.  **This pad is expected and benign**, not a
    defect signal — it is the tail of the disc arriving one read-offset short, and
    it was measured silent on the reference disc.

    Truncation is a different thing and is checked **before** anything is written:
    the raw region must hold the whole declared lead-out exactly, because the
    origin is derived from that same length.  Unbounded padding would turn a
    truncated or half-copied file into a structurally perfect container — right
    TOC, right disc ID, hundreds of megabytes of silence — reported as a success
    with one warning line.  Silence is also what a file that was never fully
    written produces, so a short file has to fail rather than import
    (``tools/write_smoke.py``'s reasoning, inverted).
    """
    want = total_frames * _CDDA_SECTOR_BYTES
    raw_available = max(file_bytes - _AUDIO_OFFSET, 0)
    if raw_available < want:
        msg = (
            f"{filename}: audio region holds {raw_available} bytes but the lead-out"
            f" declares {want} — short by {want - raw_available}; the file looks"
            " truncated"
        )
        raise PXIError(msg)

    written = 0
    f.seek(_AUDIO_OFFSET + read_offset * _BYTES_PER_SAMPLE)
    with open(pcm_out, "wb") as out:
        while written < want:
            chunk = f.read(min(_CHUNK_BYTES, want - written))
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)

        pad = want - written
        if pad:
            out.write(b"\x00" * pad)
    return pad


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _parse_pxi(pxi_path: Path) -> tuple[RBIDisc, bool, int]:
    """Parse a PlexTools image without writing PCM.

    Returns ``(disc, has_cdtext, total_frames)`` where *total_frames* is the
    lead-out LBA, i.e. the full sector count of the audio region.
    """
    name = pxi_path.name
    with open(pxi_path, "rb") as f:
        head = f.read(_TOC_OFFSET)
        toc = f.read(
            _INDEX_TABLE_OFFSET - _TOC_OFFSET + _MAX_INDEX_RECORDS * _INDEX_RECORD_BYTES
        )

    if head[: len(_MAGIC)] != _MAGIC:
        msg = f"{name}: not a PlexTools image (missing {_MAGIC!r} magic)"
        raise PXIError(msg)
    if len(toc) < _INDEX_TABLE_OFFSET - _TOC_OFFSET:
        msg = f"{name}: file ends before the TOC block at 0x{_TOC_OFFSET:x}"
        raise PXIError(msg)

    leadout = struct.unpack_from("<I", toc, _TOC_LEADOUT)[0]
    if leadout <= _LEAD_IN_FRAMES:
        msg = f"{name}: lead-out at absolute frame {leadout} is not a readable disc"
        raise PXIError(msg)
    total_frames = leadout - _LEAD_IN_FRAMES

    declared_tracks = toc[_TOC_TRACK_COUNT]
    mcn_raw = toc[_TOC_MCN : _TOC_MCN + _TOC_MCN_LEN]
    mcn = mcn_raw.split(b"\x00")[0].decode("ascii", errors="replace").strip()
    catalog = mcn if mcn and set(mcn) != {"0"} else None

    records = _read_index_records(toc, name)
    groups = _group_by_track(records, name)
    _validate_layout(records, groups, leadout, name)

    first_track = records[0][1]
    last_track = first_track + len(groups) - 1
    _check_track_count(declared_tracks, groups, first_track, name)

    cdtext = _read_cdtext(head, name)
    if cdtext is not None and not _cdtext_matches_disc(cdtext, first_track, last_track):
        log.warning(
            "%s: CD-Text covers tracks %s-%s but the disc has %d-%d — discarding it",
            name,
            cdtext.first_track,
            cdtext.last_track,
            first_track,
            last_track,
        )
        cdtext = None

    disc = _build_disc(groups, first_track, catalog, cdtext)
    return disc, cdtext is not None, total_frames


def info_pxi(pxi_path: Path) -> tuple[RBIDisc, bool, int]:
    """Return ``(disc, has_cdtext, file_bytes)`` without importing."""
    disc, has_cdtext, _ = _parse_pxi(pxi_path)
    return disc, has_cdtext, pxi_path.stat().st_size


def import_pxi(
    pxi_path: Path,
    pcm_out: Path,
    prov: dict[str, str] | None = None,
    report: Callable[[str], None] | None = None,
    offset_candidates: OffsetCandidateSource | None = None,
) -> tuple[RBIDisc, int]:
    """Import a PlexTools ``.pxi`` disc image as master-mode RBI.

    Returns ``(disc, FLAG_MASTER_MODE)``.  When *prov* is given, the read offset
    applied is recorded as ``pxi_read_offset``, how it was arrived at as
    ``pxi_offset_source``, every verifying offset as ``pxi_offset_candidates``,
    and the tail padding the offset costs as ``pxi_tail_padded`` — so the
    assumption, the evidence for it and the fabricated samples all stay
    identifiable in the container.  The pad is the routine consequence of
    offset-correcting a raw image, not a damage signal — truncation raises
    instead.

    *offset_candidates* supplies AccurateRip's verifying offsets so the read
    offset can be **measured rather than assumed**; without it the module prior
    is used unchanged.  See :func:`_resolve_read_offset` for why the prior is
    treated as evidence and why neither the top candidate nor the one nearest
    zero is taken.

    *report* receives the human-readable notes.  It defaults to ``print``;
    under the TUI the caller passes a sink that appends to the output region,
    because a bare ``print`` moves the cursor without telling the renderer and
    leaves an orphaned progress bar behind.  The tail-padding note goes there
    too rather than to ``log.warning``: with no handler configured a warning
    falls through to :data:`logging.lastResort` on stderr, which orphans a bar
    in exactly the same way.  The durable record is ``pxi_tail_padded`` in
    PROV, which outlives any log line.

    Raises :class:`PXIError` for images this parser does not understand:
    multi-session, non-consecutive or non-ascending tracks, a gap in the index
    table, a lead-out that the index table does not reach, or an audio region
    that does not hold the whole declared lead-out.
    """
    say = report or print
    disc, has_cdtext, total_frames = _parse_pxi(pxi_path)
    say(f"  CD-Text: {'YES' if has_cdtext else 'NO'}")

    candidates = None
    if offset_candidates is not None:
        candidates = offset_candidates(disc, pxi_path, _AUDIO_OFFSET)
    res = _resolve_read_offset(candidates)

    with open(pxi_path, "rb") as f:
        pad = _write_pcm(
            f,
            total_frames,
            pxi_path.stat().st_size,
            pcm_out,
            pxi_path.name,
            res.offset,
        )

    if prov is not None:
        prov["pxi_read_offset"] = str(res.offset)
        prov["pxi_offset_source"] = res.source
        if candidates is not None:
            prov["pxi_offset_candidates"] = ",".join(
                f"{c.offset:+d}" for c in candidates
            )
    # The two outcomes that are not the routine one get a line of their own: a
    # detected offset changes the audio, and a declined ambiguity means the
    # audio rests on an assumption AccurateRip did not back.
    if res.source == "accuraterip_sole":
        say(f"  Offset: DETECTED {res.offset:+d} samples — {res.detail}")
    elif res.source == "assumed_ambiguous":
        say(f"  Offset: assuming {res.offset:+d} samples — {res.detail}")
    if pad:
        say(
            f"  Offset: {res.offset:+d} samples applied ({res.source});"
            f" {pad} bytes zero-filled at the lead-out"
        )
        log.debug("%s: tail zero-filled by %d bytes", pxi_path.name, pad)
        if prov is not None:
            prov["pxi_tail_padded"] = str(pad)

    return disc, FLAG_MASTER_MODE
