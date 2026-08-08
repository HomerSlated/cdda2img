from __future__ import annotations

import argparse
import bisect
import copy
import importlib.metadata
import logging
import re
import textwrap
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdda2img.accudisc_reader import ReadLanes
    from cdda2img.config import Config
    from cdda2img.ctdb_repair import CtdbRepairResult
    from cdda2img.field_resolver import FieldProposal
    from cdda2img.lookup_result import DiscMeta
    from cdda2img.mb_lookup import MBPrepopResult
    from cdda2img.rbi_format import RipInfo
    from cdda2img.recovery_profile import ResolvedStrategy
    from cdda2img.rip_log import RipLogBuilder
    from cdda2img.terminal_ui import TerminalUI
    from cdda2img.track_preview import TrackPreview

from cdda2img.concat import concat_wav
from cdda2img.container import (
    TempFiles,
    build_container,
    extract_data,
    resolve_temp_dir,
    wav_to_raw_pcm,
)
from cdda2img.input_selector import (
    MAX_RUNTIME_MINUTES,
    MAX_TRACKS,
    get_audio_duration_minutes,
    select_batches,
)
from cdda2img.metadata import derive_album_info
from cdda2img.rbi_format import (
    CD_FRAMES_PER_SECOND,
    FLAG_MASTER_MODE,
    RBIDisc,
    format_original,
)
from cdda2img.silence import trim_silence_cd_da
from cdda2img.toc import (
    build_toc_entries,
    generate_toc,
    get_track_durations,
    sanitize_title,
)
from cdda2img.transcode import transcode_audio

log = logging.getLogger(__name__)

DEFAULT_STRATEGY = "aatc"
_MIN_AR_CONFIDENCE = (
    3  # minimum AccurateRip submissions for auto-applying a drive offset
)
SILENCE_PAD_DUR = "2"  # seconds of post-track silence (Red Book inter-track gap)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cdda2img",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            diagnostics:
              cdda2img doctor       Report every dependency (Python packages, the
                                    AccuDisc engine, external binaries, native
                                    libraries) and exit 1 if a required one is
                                    missing. Checks only — installs nothing.
                                    Handled before this parser is reached, so it
                                    still works when a dependency is missing.

            create options:
              --silence {trim|notrim}
                                    trim: remove leading/trailing silence and add a
                                          2 s inter-track gap (default)
                                    notrim: preserve source audio as-is
              --silence-threshold N Silence detection threshold in -dBFS (default: 55)
              --loudness {rg|none}  rg: measure EBU R128 and embed RG block (default)
                                    none: skip loudness analysis
              --strategy {fcfs,aatc,best,meta}
                                    Disc batching strategy (default: aatc)
                fcfs  first-come-first-served: fill one disc in input order, stop
                aatc  all-as-they-come: fill discs in input order, as many as needed
                best  global bin-packing to minimise total disc count (order not preserved)
                meta  group tracks by embedded disc-number tag; untagged tracks form a final group
              --preserve-pregaps    Preserve pre-gaps in trim mode (no-op for audio-file sources)

            extract options:
              --tracks              Write per-track FLAC files and CUE sheet
              --raw                 Write TOC + WAV (s16le) to extracted/
              --rg                  Write ReplayGain block as .rg.json sidecar
              --ar                  Write AccurateRip report as .accurip
              --log                 Write rip log as .log
              --all                 Extract all block types (default when no flags given)
              --normalize           Normalise extracted FLACs to -18 LUFS (EBU R128);
                                    applies with --tracks/--all; skips RG tag embedding
              (flags are additive; omitting all flags is equivalent to --all)

            list options:
              --info                Show container structure and track index (default)
              --rg                  Render ReplayGain block
              --ar                  Render AccurateRip report
              --log                 Render rip log
              (flags are additive)

            rip options:
              --loudness {rg|none}  rg: embed EBU R128 ReplayGain block (default); none: skip
              --output <path>       Output .rbi file, or a directory to receive an album-derived filename (default: CWD, name from album)
              Note: rip captures audio verbatim (1:1 via AccuDisc, single pass); no silence trim or gap insertion

            import options:
              --loudness {rg|none}  rg: embed EBU R128 ReplayGain block (default); none: skip
              --output <path>       Output .rbi file, or a directory to receive an album-derived filename (default: CWD, name from album)
              --info                Dry-run: parse and display image metadata; do not import
              Note: import preserves source audio verbatim (s16be→s16le byte-swap only); no silence trim or gap insertion
              Accepts: cdrdao .toc file, or a DDP 2.0 image directory (must contain DDPID)

            burn options:
              --device DEVICE       CD drive device (default: from config default_device, fallback /dev/sr0)
              --speed N             Burn speed in CD-DA drive units (default: 4)
              --write-offset N      Write offset override in samples (default: from config)
              --simulate            Test write (laser off) — validate without consuming a blank
              --yes                 Skip confirmation prompt (non-interactive burn)

            mount options:
              --slot N              cdemu slot to load into (default: first free)
              --mnt-dir PATH        Directory for extracted TOC+BIN (default: ./mnt)

            examples:
              cdda2img rip
              cdda2img rip --device /dev/sr0 --loudness none --output mydisc.rbi
              cdda2img create /music/album
              cdda2img create /music/album --silence notrim --loudness none
              cdda2img create /music/album --strategy best
              cdda2img create /music/album --silence-threshold 60
              cdda2img extract album.rbi
              cdda2img extract album.rbi --tracks
              cdda2img extract album.rbi --raw
              cdda2img extract album.rbi --tracks --raw --rg
              cdda2img extract album.rbi --normalize
              cdda2img import disc.toc
              cdda2img import disc.toc --loudness none --output mydisc.rbi
              cdda2img import /path/to/ddp_dir
              cdda2img import /path/to/ddp_dir --output mydisc.rbi
              cdda2img burn album.rbi
              cdda2img burn album.rbi --device /dev/sr0 --speed 8
              cdda2img burn album.rbi --simulate
              cdda2img burn album.rbi --write-offset -30 --yes
              cdda2img list album.rbi
              cdda2img list album.rbi --ar
              cdda2img test album.rbi
              cdda2img mount album.rbi
              cdda2img mount album.rbi --slot 1 --mnt-dir /tmp/mnt
        """),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"cdda2img {importlib.metadata.version('cdda2img')}",
    )
    # R10: process-wide offline-mode toggle. When set, every remote metadata
    # Diagnostic-level logging. Surfaces every URL queried (AccurateRip,
    # MusicBrainz, Discogs, AcoustID, CDDB) and the HTTP outcome, so
    # "disc not found" / "no match" failures can be traced to the exact
    # request that was made.
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose (DEBUG) logging — traces remote queries and HTTP outcomes.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser(
        "create", help="Create an RBI image from a directory of audio files"
    )
    c.add_argument("input_dir", type=Path, help="Directory containing audio files")
    c.add_argument(
        "--silence",
        default="trim",
        choices=["trim", "notrim"],
        help="trim: remove silence and add inter-track gap (default); notrim: preserve source as-is",
    )
    c.add_argument(
        "--loudness",
        default="rg",
        choices=["rg", "none"],
        help="rg: embed EBU R128 ReplayGain block (default); none: skip loudness analysis",
    )
    c.add_argument(
        "--strategy",
        default=DEFAULT_STRATEGY,
        choices=["fcfs", "aatc", "best", "meta"],
        help="Disc batching strategy (default: aatc)",
    )
    c.add_argument(
        "--preserve-pregaps",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Preserve pre-gaps in trim mode (default: disabled; no-op for audio-file sources)",
    )
    c.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .rbi file, or a directory to receive album-derived names (default: CWD, name from album); multi-disc: stem used as base name",
    )
    c.add_argument(
        "--silence-threshold",
        type=int,
        default=None,
        metavar="N",
        help="Silence detection threshold in -dBFS (default: from config, 55)",
    )
    c.add_argument(
        "--capacity",
        type=int,
        default=None,
        metavar="N",
        help="Disc capacity in minutes (default: from config, 80)",
    )
    c.add_argument(
        "--tui",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use terminal UI for the metadata menu (default: from config, true; "
        "--no-tui renders plainly without clearing the screen)",
    )
    c.add_argument(
        "--duplicate",
        default=None,
        choices=["skip", "replace", "add"],
        metavar="{skip,replace,add}",
        help="How to handle a duplicate catalogue entry (overrides config duplicate_catalogue_entry for this run)",
    )
    c.add_argument(
        "--auto",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Skip the interactive metadata menu; accept the best-guess result automatically (default: from config, false)",
    )

    x = sub.add_parser("extract", help="Extract blocks from an RBI image")
    x.add_argument("rbi_file", type=Path, help="RBI file to extract")
    x.add_argument(
        "--raw", action="store_true", help="Extract TOC + WAV (s16le) to output dir"
    )
    x.add_argument(
        "--tracks",
        action="store_true",
        help="Extract per-track FLAC + CUE to <artist>/<album>/ within output dir",
    )
    x.add_argument(
        "--rg",
        action="store_true",
        help="Extract ReplayGain block to <stem>.rg.json",
    )
    x.add_argument(
        "--ar",
        action="store_true",
        help="Extract AccurateRip report to <stem>.accurip",
    )
    x.add_argument("--log", action="store_true", help="Extract rip log to <stem>.log")
    x.add_argument(
        "--albumart",
        action="store_true",
        help="Extract album art sidecar ({stem}.jpg with --raw, folder.jpg with --tracks)",
    )
    x.add_argument(
        "--embedart",
        action="store_true",
        help="Embed album art as a PICTURE block in each extracted FLAC (~600 px JPEG; modifier for --tracks/--all)",
    )
    x.add_argument(
        "--all",
        action="store_true",
        dest="all_blocks",
        help="Extract all blocks (default when no flags given)",
    )
    x.add_argument(
        "--normalize",
        action="store_true",
        help="Apply EBU R128 normalisation to extracted FLACs (modifier for --tracks/--all; skips RG tag embedding)",
    )
    x.add_argument(
        "--output",
        type=Path,
        metavar="PATH",
        help="Output directory (default: ./extracted)",
    )

    l_cmd = sub.add_parser("list", help="List the contents of an RBI image")
    l_cmd.add_argument("rbi_file", type=Path, help="RBI file to list")
    l_cmd.add_argument(
        "--info",
        action="store_true",
        help="Show disc/block/track summary (default if no flags)",
    )
    l_cmd.add_argument("--rg", action="store_true", help="Show ReplayGain data")
    l_cmd.add_argument("--ar", action="store_true", help="Show AccurateRip report")
    l_cmd.add_argument("--log", action="store_true", help="Show rip log")
    l_cmd.add_argument(
        "--prov",
        action="store_true",
        help="Dump the raw provenance (PROV) block as decoded key=value lines",
    )

    t_cmd = sub.add_parser("test", help="Test/validate an RBI image against the spec")
    t_cmd.add_argument("rbi_file", type=Path, help="RBI file to validate")

    r_cmd = sub.add_parser("rip", help="Rip a physical CD-DA disc to an RBI container")
    r_cmd.add_argument(
        "--profile",
        metavar="NAME",
        help="Recovery profile (see --list-profiles). Overrides config default_profile.",
    )
    r_cmd.add_argument(
        "--list-profiles",
        action="store_true",
        help="List installed recovery profiles and exit",
    )
    # AccuDisc passthrough. Supplying ANY of these bypasses profiles entirely (§9.4
    # rung 1) — it is an escape hatch for driving the engine directly, and merging it
    # with a profile would produce a configuration nobody asked for.
    _ad = r_cmd.add_argument_group(
        "AccuDisc passthrough",
        "Drive AccuDisc's recovery knobs directly. Any of these disables profiles.",
    )
    _ad.add_argument("--ad-speed", type=int, metavar="X")
    _ad.add_argument("--ad-retries", type=int, metavar="K")
    _ad.add_argument("--ad-c2-retries", type=int, metavar="N", dest="ad_c2")
    _ad.add_argument("--ad-verify", type=int, metavar="P")
    _ad.add_argument("--ad-overlap", type=int, metavar="K")
    _ad.add_argument("--ad-ladder", metavar="LIST")
    _ad.add_argument("--ad-recovery", metavar="FLAGS")
    r_cmd.add_argument(
        "--device",
        default=None,
        metavar="DEVICE",
        help="Optical drive device (default: from config default_device, fallback /dev/sr0)",
    )
    r_cmd.add_argument(
        "--loudness",
        default="rg",
        choices=["rg", "none"],
        help="rg: embed EBU R128 ReplayGain block (default); none: skip loudness analysis",
    )
    r_cmd.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .rbi file, or a directory to receive an album-derived filename (default: CWD, name from album)",
    )
    r_cmd.add_argument(
        "--preview",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Show album art + disc title before rip starts, and play track 1 in the background (default: from config, true)",
    )
    r_cmd.add_argument(
        "--tui",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use terminal UI for live progress rendering (default: from config, true; no-op when stdin is not a TTY)",
    )
    r_cmd.add_argument(
        "--duplicate",
        default=None,
        choices=["skip", "replace", "add"],
        metavar="{skip,replace,add}",
        help="How to handle a duplicate catalogue entry (overrides config duplicate_catalogue_entry for this run)",
    )
    r_cmd.add_argument(
        "--auto",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Skip the interactive metadata menu; accept the best-guess result automatically (default: from config, false)",
    )
    r_cmd.add_argument(
        "--extract",
        action="store_true",
        default=False,
        help="Extract to per-track FLAC + CUE after building the RBI container",
    )
    r_cmd.add_argument(
        "--no-keep-rbi",
        action="store_true",
        default=False,
        dest="no_keep_rbi",
        help="Delete the RBI container after successful extraction (only valid with --extract)",
    )
    i_cmd = sub.add_parser(
        "import",
        help="Import a foreign disc image as an RBI container (master mode): cdrdao .toc, DDP 2.0, or Nero .nrg",
    )
    i_cmd.add_argument(
        "source",
        type=Path,
        help="cdrdao .toc file, DDP 2.0 image directory, or Nero .nrg file",
    )
    i_cmd.add_argument(
        "--loudness",
        default="rg",
        choices=["rg", "none"],
        help="rg: embed EBU R128 ReplayGain block (default); none: skip loudness analysis",
    )
    i_cmd.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .rbi file, or a directory to receive an album-derived filename (default: CWD, name from album)",
    )
    i_cmd.add_argument(
        "--info",
        action="store_true",
        help="Dry-run: parse and display image metadata without importing",
    )
    i_cmd.add_argument(
        "--tui",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use terminal UI for the metadata menu (default: from config, true; "
        "--no-tui renders plainly without clearing the screen)",
    )
    i_cmd.add_argument(
        "--duplicate",
        default=None,
        choices=["skip", "replace", "add"],
        metavar="{skip,replace,add}",
        help="How to handle a duplicate catalogue entry (overrides config duplicate_catalogue_entry for this run)",
    )
    i_cmd.add_argument(
        "--auto",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Skip the interactive metadata menu; accept the best-guess result automatically (default: from config, false)",
    )

    d_cmd = sub.add_parser("catalogue", help="Browse disc catalogue")
    d_cmd.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="catalogue database path (default: from config or XDG data dir)",
    )

    w_cmd = sub.add_parser(
        "burn", help="Burn an RBI image to a blank CD-DA disc via AccuDisc"
    )
    w_cmd.add_argument("rbi_file", type=Path, help="RBI file to burn")
    w_cmd.add_argument(
        "--device",
        default=None,
        metavar="DEVICE",
        help="CD drive device (default: from config default_device, fallback /dev/sr0)",
    )
    w_cmd.add_argument(
        "--speed",
        type=int,
        default=4,
        metavar="N",
        help="Burn speed in CD-DA drive units (default: 4)",
    )
    w_cmd.add_argument(
        "--write-offset",
        type=int,
        default=None,
        dest="write_offset",
        metavar="N",
        help="Write offset override in samples (default: from config)",
    )
    w_cmd.add_argument(
        "--simulate",
        action="store_true",
        help="Test write (laser off) — validate the burn without consuming a blank",
    )
    w_cmd.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )

    m_cmd = sub.add_parser(
        "mount", help="Mount an RBI image as a virtual disc via cdemu"
    )
    m_cmd.add_argument("rbi_file", type=Path, help="RBI file to mount")
    m_cmd.add_argument(
        "--slot",
        type=int,
        default=None,
        metavar="N",
        help="cdemu slot to load into (default: first free)",
    )
    m_cmd.add_argument(
        "--mnt-dir",
        type=Path,
        default=None,
        dest="mnt_dir",
        metavar="PATH",
        help="Directory for extracted TOC+BIN (default: ./mnt)",
    )

    s_cmd = sub.add_parser("setup", help="Setup wizard and maintenance tools")
    s_cmd.add_argument(
        "--create-config",
        action="store_true",
        dest="create_config",
        help="Create config from template",
    )
    s_cmd.add_argument(
        "--update-config",
        action="store_true",
        dest="update_config",
        help="Update config from template (preserves user values)",
    )
    s_cmd.add_argument(
        "--create-profile",
        action="store_true",
        dest="create_profile",
        help="Create a recovery profile in the user profiles directory",
    )
    s_cmd.add_argument(
        "--edit-config",
        action="store_true",
        dest="edit_config",
        help="Open the config in $EDITOR, then re-validate on save",
    )
    s_cmd.add_argument(
        "--validate-config",
        action="store_true",
        dest="validate_config",
        help="Validate and optionally repair config",
    )
    s_cmd.add_argument(
        "--read-offset",
        action="store_true",
        dest="read_offset",
        help="Detect drive read offset via AccurateRip",
    )
    s_cmd.add_argument(
        "--write-offset",
        action="store_true",
        dest="write_offset",
        help="Measure drive write offset via burn-and-read-back",
    )
    s_cmd.add_argument(
        "--create-catalogue",
        action="store_true",
        dest="create_catalogue",
        help="Create the disc catalogue database",
    )
    s_cmd.add_argument(
        "--validate-catalogue",
        action="store_true",
        dest="validate_catalogue",
        help="Validate catalogue structure (integrity + VACUUM)",
    )
    s_cmd.add_argument(
        "--verify-catalogue",
        action="store_true",
        dest="verify_catalogue",
        help="Verify RBI file locations in catalogue",
    )
    s_cmd.add_argument(
        "--test",
        action="store_true",
        help="Run full RBI verify per entry (with --verify-catalogue)",
    )
    s_cmd.add_argument("--device", default=None)
    s_cmd.add_argument("--speed", type=int, default=4)

    return parser.parse_args()


def _add_release_provenance(provenance: dict, disc: RBIDisc) -> None:  # noqa: C901
    """Append release-intelligence fields to *provenance* if populated on *disc*.

    C901 noqa: branchy logic is inherent — one ``if`` per RBIDisc field that
    has a corresponding PROV key. Splitting into sub-helpers would obscure
    the 1:1 correspondence with the spec table.
    """
    if disc.low_dynamic_range is not None:
        provenance["low_dynamic_range"] = "YES" if disc.low_dynamic_range else "NO"
    if disc.original_release_found:
        provenance["original_release_found"] = "YES"
        if disc.original_release_title:
            provenance["original_release_title"] = disc.original_release_title
        if disc.original_release_year is not None:
            provenance["original_release_year"] = str(disc.original_release_year)
    if disc.release_date:
        provenance["release_date"] = disc.release_date
    if disc.catalog_number:
        provenance["catalog_number"] = disc.catalog_number
    if disc.label:
        provenance["label"] = disc.label
    if disc.country:
        provenance["country"] = disc.country
    if disc.mb_release_id:
        provenance["mb_release_id"] = disc.mb_release_id
    if disc.mb_release_group_id:
        provenance["mb_release_group_id"] = disc.mb_release_group_id
    if disc.discogs_release_id is not None:
        provenance["discogs_release_id"] = str(disc.discogs_release_id)
    if disc.set_title:
        provenance["set_title"] = disc.set_title
    # R14: aggregate pre-emphasis flag (cdrdao path populates; other parsers
    # leave None and the key is omitted).
    if disc.pre_emphasis is not None:
        provenance["pre_emphasis"] = "YES" if disc.pre_emphasis else "NO"


def _finalize_identifiers(provenance: dict, disc: RBIDisc) -> None:
    """Settle the MCN/barcode pair at finalisation, just before TOC generation.

    Two identifiers, two destinations (identifier_trust_model.md §1a):

    - **barcode** (the service UPC/EAN, the disambiguation key) -> PROV only. Never
      written to the TOC or the physical layer.
    - **catalog** (the on-disc MCN) -> TOC CATALOG line, burned back to disc. When
      the disc carries no MCN of its own, it is *synthesised* from the barcode: the
      MCN field is defined to hold the UPC/EAN (Q-ch Mode 2 = 13 BCD digits), so a
      normalised barcode is exactly its canonical content, not an alien value.

    ``mcn_source`` records provenance of the MCN so a later reader distinguishes a
    genuine disc reading (``disc``) from a reconstruction (``barcode_derived``).

    The synthesis runs the barcode through ``normalize_barcode`` (burnable form,
    check digit not required) rather than copying it raw: the MCN is burned to the
    TOC ``CATALOG`` and cdrdao demands 13 numeric digits. MB/Discogs barcodes are
    already normalised, but a manual/menu entry might not be — so this is the
    chokepoint that guarantees a burnable result. A barcode that cannot yield 13
    digits produces no synthesised MCN (catalog stays blank, no ``mcn_source``).
    """
    from cdda2img.barcode import normalize_barcode

    if disc.barcode:
        provenance["barcode"] = disc.barcode
    if disc.catalog:
        provenance["mcn_source"] = "disc"
    elif disc.barcode:
        synth = normalize_barcode(disc.barcode, require_check_digit=False)
        if synth:
            disc.catalog = synth  # synthesise a burnable archival MCN from the barcode
            provenance["mcn_source"] = "barcode_derived"


def _unique_path(stem: str, ext: str, parent: Path | None = None) -> Path:
    """Return a non-colliding Path for {stem}.{ext}, appending _1, _2... if needed."""
    base = parent if parent is not None else Path()
    p = base / f"{stem}.{ext}"
    if not p.exists():
        return p
    for i in range(1, 10000):
        p = base / f"{stem}_{i}.{ext}"
        if not p.exists():
            return p
    msg = f"Cannot find unique path for {stem}.{ext}"
    raise RuntimeError(msg)


def _resolve_output_path(output: Path | None, stem: str, disc_suffix: str = "") -> Path:
    """Resolve --output into a concrete file path.

    Three cases:
    - None: derive filename in CWD from `stem`.
    - Existing directory: derive filename in that directory from `stem`.
    - Anything else: treat as the file path the user named explicitly.

    `disc_suffix` (e.g. ``"_disc2"``) is appended to the derived stem only in
    the first two cases; an explicit user path is honoured verbatim.
    """
    if output is None:
        return _unique_path(f"{stem}{disc_suffix}", "rbi")
    if output.is_dir():
        return _unique_path(f"{stem}{disc_suffix}", "rbi", parent=output)
    if disc_suffix:
        return output.parent / f"{output.stem}{disc_suffix}{output.suffix or '.rbi'}"
    return output


def _check_batch_limits(
    batches: list[list[Path]], capacity_minutes: int = MAX_RUNTIME_MINUTES
) -> None:
    """Raise ValueError if any batch exceeds Red Book track or duration limits."""
    for disc_idx, batch in enumerate(batches, start=1):
        n = len(batch)
        total_min = sum(get_audio_duration_minutes(f) for f in batch)
        if n > MAX_TRACKS or total_min > capacity_minutes:
            msg = (
                f"Disc {disc_idx} would have {n} tracks and {total_min:.1f} min"
                f" (limit: {MAX_TRACKS} tracks / {capacity_minutes} min)."
                f" Re-tag files with correct disc numbers or choose a different strategy."
            )
            raise ValueError(msg)


def create_image(
    input_dir: Path,
    silence_mode: str = "trim",
    loudness: str = "rg",
    strategy: str = DEFAULT_STRATEGY,
    output: Path | None = None,
    silence_threshold: int = 55,
    capacity: int = MAX_RUNTIME_MINUTES,
    low_dr_threshold: float = 5.0,
    tui: bool = True,
    duplicate_policy: str | None = None,
    auto: bool = False,
) -> None:
    files = sorted(
        p for p in input_dir.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    batches = select_batches(files, strategy, capacity_minutes=capacity)
    if not batches:
        print("No audio files found.")
        return

    _check_batch_limits(batches, capacity_minutes=capacity)

    meta = derive_album_info(files)
    album = meta["album"]
    artist = meta["artist"]
    disc_total = len(batches)

    temp_base = resolve_temp_dir()

    for disc_num, batch in enumerate(batches, start=1):
        print(f"\nDisc {disc_num}/{disc_total}: {len(batch)} tracks")
        temp = TempFiles(temp_base)
        try:
            source_wavs: list[Path] = []

            for i, track in enumerate(batch, start=1):
                print(f"  Transcoding   {i:2}: {track.stem}")
                trans = temp.temp_track(i, "_trans.wav")
                transcode_audio(track, trans)

                if silence_mode == "trim":
                    print(f"  Trimming      {i:2}: {track.stem}")
                    trim = temp.temp_track(i, "_trim.wav")
                    trim_silence_cd_da(
                        str(trans),
                        str(trim),
                        SILENCE_PAD_DUR,
                        threshold_db=silence_threshold,
                    )
                    source_wavs.append(trim)
                else:
                    print(f"  Skipping silence trim (--silence notrim): {track.stem}")
                    source_wavs.append(trans)

            print("  Concatenating tracks")
            concat_wav(source_wavs, temp.pcm_pre)
            wav_to_raw_pcm(temp.pcm_pre, temp.pcm_file)

            durations = get_track_durations(source_wavs)
            disc = RBIDisc(
                album=album, artist=artist, disc_number=disc_num, disc_total=disc_total
            )
            disc.tracks = build_toc_entries(batch, durations, disc)

            provenance: dict[str, str] = {
                "mode": "create",
                "source": str(input_dir.resolve()),
                "ripper": "file",
                "lookup_status_mb": "disabled",
                "lookup_status_cddb": "disabled",
                "lookup_status_discogs": "disabled",
            }
            disc = _r6_acoustid_corroborate_wavs(disc, source_wavs, provenance)

            # Identify the original release before the menu so the user
            # sees "Original: <title> (<year>)" in the initial summary.
            from cdda2img.original_release import populate_original_release

            populate_original_release(disc)

            import sys

            from cdda2img.album_art import (
                cover_from_file_tags,
                render_cover,
                to_album_art,
            )

            _art_raw = cover_from_file_tags(batch[0]) if batch else None
            if _art_raw is not None and sys.stdin.isatty():
                render_cover(_art_raw)
            provenance["lookup_status_art"] = _r12_status(
                attempted=True, has_data=_art_raw is not None, errored=False
            )

            from cdda2img.match_distance import build_match_distance
            from cdda2img.metadata_menu import run_metadata_menu

            match_dist = build_match_distance(disc, provenance)
            provenance["match_confidence"] = f"{match_dist.score:.3f}"
            provenance["match_recommendation"] = match_dist.recommendation.value
            # Menu shown unless --auto; confidence is informational (see
            # _finalize_import for the same policy). A failed §10.4 AcoustID gate
            # additionally suppresses --auto (warn-only) — a no-op in create,
            # which has no MB disc-ID match to gate, but kept for symmetry.
            auto_apply = _gate_adjusted_auto(auto, provenance)
            if auto_apply:
                print(f"  Metadata auto-confirmed — {match_dist.summary()}")
            else:
                print(f"  Metadata: {match_dist.summary()}")
            disc = run_metadata_menu(
                disc, source_wavs=source_wavs, tui=tui, auto_apply=auto_apply
            )
            album_art = to_album_art(_art_raw) if _art_raw is not None else None

            raw_titles = [re.sub(r"^\d{1,2}[-. ]+", "", p.stem) for p in batch]

            # Loudness first, so disc.low_dynamic_range is set before PROV is built.
            rg_block: bytes | None = None
            if loudness == "rg":
                from cdda2img.replaygain import analyse, pack_rg_block

                print("  Measuring loudness (EBU R128)...")
                rg_result = analyse(source_wavs)
                disc.low_dynamic_range = rg_result.album_lra < low_dr_threshold
                print(
                    f"  Album gain: {rg_result.album_gain:+.2f} dB  "
                    f"peak: {rg_result.album_peak:.4f}  "
                    f"LRA: {rg_result.album_lra:.1f} LU"
                )
                rg_block = pack_rg_block(rg_result)

            _add_release_provenance(provenance, disc)
            _finalize_identifiers(provenance, disc)
            toc_data = generate_toc(disc, raw_titles=raw_titles)

            disc_suffix = "" if disc_total == 1 else f"_disc{disc_num}"
            output_file = _resolve_output_path(output, album, disc_suffix)
            container_flags = FLAG_MASTER_MODE if silence_mode == "notrim" else 0
            build_container(
                temp.pcm_file,
                toc_data,
                disc,
                output_file,
                rg_block=rg_block,
                prov_data=provenance,
                extra_flags=container_flags,
                album_art=album_art,
            )
            from cdda2img.catalogue import register_rbi

            register_rbi(output_file, duplicate_policy=duplicate_policy)
        finally:
            temp.cleanup()


def _fmt_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GiB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MiB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.1f} KiB"
    return f"{n} B"


def _print_source_info(
    label: str,
    name: str,
    total_bytes: int,
    disc: RBIDisc,
    has_cdtext: bool,
) -> None:
    """Display parsed metadata for an import source image (--info mode)."""
    total_frames = sum(t.duration_frames for t in disc.tracks)
    total_s = total_frames // 75
    dm, ds = divmod(total_s, 60)
    dur_str = f"{dm}:{ds:02}"

    print(f"\n{label}: {name}  ({_fmt_size(total_bytes)})")
    print(f"CD-Text:  {'YES' if has_cdtext else 'NO'}")
    if disc.catalog:
        print(f"Catalog:  {disc.catalog}")
    if disc.album:
        print(f"Album:    {disc.album}")
    if disc.artist:
        print(f"Artist:   {disc.artist}")
    print(f"Tracks:   {len(disc.tracks)}  ({dur_str})")

    if not disc.tracks:
        return

    has_isrc = any(t.isrc for t in disc.tracks)
    title_w = max((len(t.title or "") for t in disc.tracks), default=5)
    title_w = max(min(title_w, 52), 5)

    print()
    hdr = f"  {'#':>2}  {'Title':<{title_w}}  {'Duration':>8}"
    sep = f"  {'─' * 2}  {'─' * title_w}  {'─' * 8}"
    if has_isrc:
        hdr += "  ISRC"
        sep += "  ────────────"
    print(hdr)
    print(sep)

    for t in disc.tracks:
        dur_s = t.duration_frames // 75
        tm, ts = divmod(dur_s, 60)
        row_dur = f"{tm}:{ts:02}"
        title = t.title or ""
        if len(title) > title_w:
            title = title[: title_w - 1] + "…"
        row = f"  {t.track_number:>2}  {title:<{title_w}}  {row_dur:>8}"
        if has_isrc:
            row += f"  {t.isrc or ''}"
        print(row)


def info_image(source: Path) -> None:
    """Parse and display metadata for a foreign disc image without importing it."""
    if not source.exists():
        msg = f"{source}: no such file or directory"
        raise FileNotFoundError(msg)

    if source.is_dir():
        from cdda2img.ddp_reader import info_ddp

        disc, has_cdtext, total_bytes = info_ddp(source)
        _print_source_info("DDP 2.0 Image", source.name, total_bytes, disc, has_cdtext)

    elif source.suffix.lower() == ".toc":
        from cdda2img.cdrdao_reader import _find_bin_filename, parsed_to_rbi_disc
        from cdda2img.toc_parser import parse_toc

        toc_text = source.read_text(encoding="utf-8")
        bin_name = _find_bin_filename(toc_text)
        bin_path = source.parent / bin_name
        if not bin_path.exists():
            msg = f"BIN file not found: {bin_path}"
            raise FileNotFoundError(msg)
        disc = parsed_to_rbi_disc(parse_toc(toc_text.encode("utf-8")))
        total_bytes = bin_path.stat().st_size
        _print_source_info("cdrdao TOC Image", source.name, total_bytes, disc, False)

    elif source.suffix.lower() == ".nrg":
        from cdda2img.nrg_reader import info_nrg

        disc, has_cdtext, total_bytes = info_nrg(source)
        _print_source_info("Nero NRG Image", source.name, total_bytes, disc, has_cdtext)

    elif source.suffix.lower() == ".ccd":
        from cdda2img.ccd_reader import info_ccd

        disc, has_cdtext, total_bytes = info_ccd(source)
        _print_source_info("CloneCD Image", source.name, total_bytes, disc, has_cdtext)

    else:
        msg = (
            f"{source.name}: expected a cdrdao .toc file, DDP 2.0 image directory,"
            " Nero .nrg file, or CloneCD .ccd file"
        )
        raise ValueError(msg)


def import_image(
    source: Path,
    loudness: str = "rg",
    output: Path | None = None,
    low_dr_threshold: float = 5.0,
    tui: bool = True,
    duplicate_policy: str | None = None,
    auto: bool = False,
) -> None:
    import sys

    from cdda2img.config import load_config

    if not source.exists():
        msg = f"{source}: no such file or directory"
        raise FileNotFoundError(msg)

    cfg = load_config()
    temp_base = resolve_temp_dir()
    temp = TempFiles(temp_base)

    ui: TerminalUI | None = None
    if sys.stdin.isatty():
        from cdda2img.terminal_ui import TerminalUI as _TUI

        ui = _TUI().start()

    try:
        if source.is_dir():
            from cdda2img.ddp_reader import import_ddp

            _ui_status(ui, f"Importing DDP image {source.name}…")
            disc, _ = import_ddp(source, temp.pcm_file)
            output_stem = sanitize_title(disc.album) or source.name
            provenance = {
                "mode": "import",
                "source": str(source.resolve()),
                "ripper": "ddp",
            }
        elif source.suffix.lower() == ".toc":
            from cdda2img.cdrdao_reader import (
                _find_bin_filename,
                convert_cdrdao_bin_to_wav,
                parsed_to_rbi_disc,
            )
            from cdda2img.toc_parser import parse_toc

            _ui_status(ui, f"Importing {source.name}…")
            toc_text = source.read_text(encoding="utf-8")
            bin_name = _find_bin_filename(toc_text)
            bin_path = source.parent / bin_name
            if not bin_path.exists():
                msg = f"BIN file not found: {bin_path}"
                raise FileNotFoundError(msg)

            disc = parsed_to_rbi_disc(parse_toc(toc_text.encode("utf-8")))

            _ui_status(ui, f"Converting {bin_name} (s16be → s16le)…")
            convert_cdrdao_bin_to_wav(bin_path, temp.pcm_pre)
            wav_to_raw_pcm(temp.pcm_pre, temp.pcm_file)
            output_stem = sanitize_title(disc.album) or source.stem
            provenance = {
                "mode": "import",
                "source": str(source.resolve()),
                "ripper": "toc",
            }
        elif source.suffix.lower() == ".nrg":
            from cdda2img.nrg_reader import import_nrg

            _ui_status(ui, f"Importing {source.name}…")
            disc, _ = import_nrg(source, temp.pcm_file)
            output_stem = sanitize_title(disc.album) or source.stem
            provenance = {
                "mode": "import",
                "source": str(source.resolve()),
                "ripper": "nrg",
            }
        elif source.suffix.lower() == ".ccd":
            from cdda2img.ccd_reader import import_ccd

            _ui_status(ui, f"Importing {source.name}…")
            disc, _ = import_ccd(source, temp.pcm_file)
            output_stem = sanitize_title(disc.album) or source.stem
            provenance = {
                "mode": "import",
                "source": str(source.resolve()),
                "ripper": "ccd",
            }
        else:
            msg = (
                f"{source.name}: expected a cdrdao .toc file, DDP 2.0 image directory,"
                " Nero .nrg file, or CloneCD .ccd file"
            )
            raise ValueError(msg)

        cddb_track_lsns: list[int] | None = None
        cddb_disc_last_lsn: int | None = None
        if disc.tracks:
            cddb_track_lsns = [t.start_frame + t.pregap_frames for t in disc.tracks]
            _last = disc.tracks[-1]
            cddb_disc_last_lsn = (
                _last.start_frame + _last.pregap_frames + _last.duration_frames - 1
            )

        _finalize_import(
            disc,
            temp.pcm_file,
            provenance,
            output_stem,
            loudness,
            output,
            ui=ui,
            low_dr_threshold=low_dr_threshold,
            cddb_track_lsns=cddb_track_lsns,
            cddb_disc_last_lsn=cddb_disc_last_lsn,
            cddb_server=cfg.cddb_server,
            tui=tui,
            duplicate_policy=duplicate_policy,
            auto=auto,
            preferred_country=cfg.preferred_country,
        )
    finally:
        if ui is not None:
            ui.stop()
        temp.cleanup()


def _collect_barcode_candidates(
    disc: RBIDisc,
    barcode_hints: list[tuple[str, str]] | None,
) -> list[str]:
    """Build the de-duplicated 13-digit service-barcode candidate list.

    Sources, in order: any already-set ``disc.barcode`` (a normalised service
    UPC/EAN), then the MB ``barcode_hints`` (R16 ``(mb_release_id, barcode)``
    tuples — MBID unused here; all check-digit-valid at MB ingest).

    The on-disc MCN (``disc.catalog``) is **deliberately not a candidate**: per the
    decided rule (identifier_trust_model.md §1a, user 2026-06-30) the MCN never
    seeds a lookup. It is archival only; the data flow is barcode -> MCN (synthesis
    at finalisation), never MCN -> barcode -> lookup. So a disc whose only
    identifier is a readable MCN yields no candidate here and no Discogs query.
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(value: str | None) -> None:
        if value and value not in seen:
            candidates.append(value)
            seen.add(value)

    _add(disc.barcode)
    for _mbid, hint in barcode_hints or []:
        _add(hint)
    return candidates


def _pick_canonical_barcode(candidates: list[str]) -> str | None:
    """Choose the canonical service barcode from *candidates*.

    Candidates are check-digit-valid EAN-13 service barcodes, already ordered by
    ``_collect_barcode_candidates`` (an existing ``disc.barcode`` first, then MB
    hints). First wins; a blank field is worse than a best-guess the user can
    correct via [c] in the menu. ``None`` when there are no candidates.
    """
    return candidates[0] if candidates else None


def _albums_match(disc_album: str | None, result_album: str | None) -> bool:
    """Heuristic: does *result_album* plausibly name the same release as *disc_album*?

    Returns True when either side is empty (nothing to compare against), when the
    two are casefold-equal, or when one is a substring of the other AND neither
    side contains a compilation/combined-release separator (``" / "``, ``" & "``,
    ``" + "``) that the other lacks. The separator asymmetry catches "Eliminator"
    vs. "Afterburner / Eliminator" — same disc-ID in MB, but different releases
    that must not auto-merge.
    """
    if not disc_album or not result_album:
        return True
    a = disc_album.strip().casefold()
    b = result_album.strip().casefold()
    if a == b:
        return True
    separators = (" / ", " & ", " + ")
    a_comp = any(sep in a for sep in separators)
    b_comp = any(sep in b for sep in separators)
    if a_comp != b_comp:
        return False
    return a in b or b in a


def _prepopulate_from_discogs(
    disc: RBIDisc,
    ui: TerminalUI | None = None,
    *,
    barcode_hints: list[tuple[str, str]] | None = None,
    provenance: dict[str, str] | None = None,
) -> tuple[RBIDisc, str | None, DiscMeta | None]:
    """Pre-populate disc.barcode and optionally enrich via Discogs barcode lookup.

    Returns ``(disc, chosen_barcode, applied_hit)`` (B-3 part 2): besides the merged
    disc, it surfaces the §10 canonical barcode it picked (``chosen_barcode``, the
    barcode overwrite of phase A) and the Discogs ``DiscMeta`` it actually *merged*
    (``applied_hit``, ``None`` on every path that did not merge one). The trust
    resolver reproduces this step from those two values
    (``canonical_barcode_proposal`` + ``meta_to_proposals``), so they must reflect
    exactly what the live merge did — hence ``applied_hit`` is ``None`` whenever
    the merge was skipped (no/ambiguous result, album mismatch), even when a hit
    object exists.

    Two distinct phases:

    A. **Canonical barcode.** Build a candidate list from any already-set
       disc.barcode and the MB barcode hints; pick one via `_pick_canonical_barcode`.
       The chosen barcode is *always* written to disc.barcode — provenance trumps
       a blank field, and the menu's [c] flow lets the user correct a wrong guess.
       The on-disc MCN never seeds this (§1a); a disc with only a readable MCN
       reaches phase A with no candidate and is left alone.

    B. **Metadata enrichment.** Query Discogs by the chosen barcode. If exactly
       one result comes back and its album matches disc.album, merge full metadata
       (label, country, year, track listing). Otherwise, leave the additional
       fields alone — the barcode is still set from phase A.

    *provenance*, when given, receives ``discogs_barcode_matches=<n>`` — how many
    releases the barcode search returned — and ``discogs_barcode_outcome``, why
    the merge did or did not happen. Those are **disambiguation** facts and are
    reported separately from ``lookup_status_discogs`` on purpose: 25 rows means
    Discogs answered richly and could not be narrowed to one, which is the
    opposite of the "empty" this used to be folded into.
    """
    from cdda2img import discogs_lookup
    from cdda2img.mb_lookup import _merge_into_disc

    def _note(key: str, value: str) -> None:
        if provenance is not None:
            provenance[key] = value

    candidates = _collect_barcode_candidates(disc, barcode_hints)
    chosen = _pick_canonical_barcode(candidates)
    if chosen and disc.barcode != chosen:
        disc.barcode = chosen
    if not chosen:
        _note("discogs_barcode_outcome", "no_barcode_to_search")
        return disc, chosen, None
    if not discogs_lookup.is_available():
        return disc, chosen, None

    _ui_status(ui, f"Querying Discogs by barcode {chosen}…")
    results = discogs_lookup.search_by_barcode(chosen)
    _note("discogs_barcode_matches", str(len(results)))
    if len(results) != 1:
        _note(
            "discogs_barcode_outcome",
            "no_match" if not results else f"ambiguous_{len(results)}",
        )
        return disc, chosen, None
    hit = results[0]
    if not _albums_match(disc.album, hit.album):
        _note("discogs_barcode_outcome", "album_mismatch")
        return disc, chosen, None
    _note("discogs_barcode_outcome", "applied")
    return _merge_into_disc(hit, disc), chosen, hit


class _Notes:
    """Buffered diagnostic emitter for the rip/import pipeline.

    `_ui_status()` clears the TUI output region on every phase transition, so any
    in-phase `_ui_print` is transient. `emit()` writes live *and* records to the
    buffer; `flush()` prints the buffer to stdout once the TUI is paused. In
    non-TUI mode the live print already covered it, so `flush()` just clears.
    """

    def __init__(self, ui: TerminalUI | None) -> None:
        self._ui = ui
        self._buf: list[str] = []

    def emit(self, text: str) -> None:
        _ui_print(self._ui, text)
        self._buf.append(text)

    def flush(self) -> None:
        if self._ui is None:
            self._buf.clear()
            return
        for note in self._buf:
            print(note)
        self._buf.clear()


def _measure_loudness_phase(
    disc: RBIDisc,
    pcm_file: Path,
    loudness: str,
    ui: TerminalUI | None,
    emit: Callable[[str], None],
    low_dr_threshold: float = 5.0,
) -> bytes | None:
    """Run the EBU R128 phase and return the packed RG block, or None when skipped.

    Sets ``disc.low_dynamic_range`` from the measured album LRA against
    *low_dr_threshold*. Leaves it ``None`` when loudness analysis is skipped.
    """
    if loudness != "rg":
        return None
    from cdda2img.replaygain import analyse_raw, pack_rg_block

    _ui_status(ui, "Measuring loudness (EBU R128)…")
    rg_result = analyse_raw(
        disc,
        pcm_file,
        progress_cb=_phase_progress_cb(ui, "Measuring loudness (EBU R128)…"),
    )
    disc.low_dynamic_range = rg_result.album_lra < low_dr_threshold
    _ui_status(
        ui,
        f"Album gain: {rg_result.album_gain:+.2f} dB  "
        f"peak: {rg_result.album_peak:.4f}  "
        f"LRA: {rg_result.album_lra:.1f} LU",
    )
    return pack_rg_block(rg_result)


_R6_BYTES_PER_FRAME = 2352
_R6_MIN_TRACKS_FOR_SECOND_SAMPLE = 4


def _r9_normalise_for_compare(s: str | None) -> str:
    """NFC + casefold + collapse whitespace, for the R9 disagreement compare.

    Strips a small documented allow-list of release-suffix tokens
    (Remastered / Deluxe Edition / Anniversary / ...) — these are
    expected differences between CDDB's title-of-this-pressing and MB's
    canonical-album-title, and they don't constitute disagreement.
    """
    import re
    import unicodedata

    if not s:
        return ""
    out = unicodedata.normalize("NFC", s).strip().casefold()
    # Strip the documented suffix allow-list (mirrors original_release._REISSUE_ALLOWLIST
    # at the canonical-token level — keep this list short / well-known).
    for tok in (
        "remastered",
        "remaster",
        "deluxe edition",
        "deluxe",
        "anniversary edition",
        "expanded edition",
        "expanded",
        "special edition",
    ):
        out = re.sub(r"\(\s*" + re.escape(tok) + r"[^)]*\)", "", out)
        out = re.sub(r"\[\s*" + re.escape(tok) + r"[^\]]*\]", "", out)
    return re.sub(r"\s+", " ", out).strip()


def _r12_status(*, attempted: bool, has_data: bool, errored: bool) -> str:
    """R12 lookup_status mapping. *attempted*=False → disabled."""
    if not attempted:
        return "disabled"
    if errored:
        return "down"
    return "OK" if has_data else "empty"


def _r11_corroborate_with_discogs_master(
    disc: RBIDisc, provenance: dict[str, str]
) -> None:
    """R11: cross-check ``disc.original_release_year`` against Discogs master.

    Fires only when MB already produced an answer (``original_release_found``
    is True) and the disc has a known Discogs release MBID. On agreement
    (same 4-digit year) emits ``original_release_corroborated=discogs,mb``.
    On disagreement, emits ``original_release_disagreement=discogs:YYYY|mb:YYYY``
    *and* updates ``disc.original_release_year`` to the earlier of the two
    (per the analysis report's "prefer the earlier" rule).
    """
    if not disc.original_release_found or disc.original_release_year is None:
        return
    if disc.discogs_release_id is None:
        return
    from cdda2img.discogs_lookup import lookup_master_year

    discogs_year = lookup_master_year(disc.discogs_release_id)
    if discogs_year is None:
        return
    mb_year = disc.original_release_year
    if discogs_year == mb_year:
        provenance["original_release_corroborated"] = "discogs,mb"
        return
    provenance["original_release_disagreement"] = f"discogs:{discogs_year}|mb:{mb_year}"
    if discogs_year < mb_year:
        log.warning(
            "R11: Discogs master year %d earlier than MB %d — preferring Discogs",
            discogs_year,
            mb_year,
        )
        disc.original_release_year = discogs_year


def _discogs_barcode_corroborate(
    disc: RBIDisc, provenance: dict[str, str], *, selected_release_id: str | None
) -> bool:
    """§10.3.1 MB→Discogs link check — barcode agreement across the relation.

    Returns whether **Discogs answered**, which is a different question from
    whether it agreed and is what ``lookup_status_discogs`` is derived from.

    On the **selected** release only: follow the MB→Discogs url-relation, fetch
    the linked Discogs release, and compare its barcode to MusicBrainz's.
    Agreement → ``discogs_corroborates=YES``; disagreement →
    ``discogs_corroborates=NO`` plus
    ``discogs_barcode_conflict=mb:<bc>|discogs:<bc>``.

    **What this does and does not claim.** The two sides are not independent: an
    MB editor chose both the barcode and the relation, so agreement validates
    *the link*, not the pressing. It catches a mis-linked relation — a real and
    useful thing — but it is not a second source confirming which pressing this
    is. That is why the key was once named ``discogs_barcode_corroborates`` and
    narrowed to ``discogs_link_barcode_agrees`` on 2026-08-04.

    **kgr restored the mirror-of-AcoustID shape on 2026-08-08**, and the reason
    stands: a reader comparing ``lookup_status_*`` and ``*_corroborates`` across
    sources should not have to learn a different vocabulary per source. The
    scope lives here and in ``discogs_barcode_conflict``'s explicitness rather
    than in the key name — but do not read this key as independent evidence
    about the pressing, and be aware the distinction gets sharper if the linked
    release is ever promoted to a metadata *source*, at which point the check
    would compare a record against itself.

    Deliberately does **not** feed release selection (the rung has already
    chosen) and skips cleanly — no fetch, no PROV key — when there is no MB
    disc-ID match, no Discogs link, no barcode on either side, or no token.

    Low-yield by design: the MB-vs-Discogs corpus showed barcode agreement is
    near-universal (0/63 conflicts), so this is mostly a provenance record and a
    deliberate foothold for a future Discogs-primary experiment — not a decision
    signal. Costs up to two network round-trips (one MB url-rels fetch, one
    Discogs release fetch), both skipped when no Discogs token is configured.
    """
    # B-2: gate on the Layer-1 selected pressing, not the mutated disc — identical
    # today, survives the B-4 flip.
    if not selected_release_id:
        return False
    from cdda2img import discogs_lookup as _discogs

    if not _discogs.is_available():
        return False
    from cdda2img.mb_lookup import discogs_link_and_barcode

    discogs_id, mb_barcode = discogs_link_and_barcode(selected_release_id)
    if discogs_id is None or not mb_barcode:
        return False
    d_meta = _discogs.fetch_release(discogs_id)
    if d_meta is None:
        return False
    # A release came back, so Discogs answered — even if it carries no barcode
    # and the comparison cannot run. "Answered" and "agreed" are separate facts
    # and conflating them is the defect this whole change exists to fix.
    if not d_meta.barcode:
        return True
    if d_meta.barcode == mb_barcode:
        provenance["discogs_corroborates"] = "YES"
    else:
        provenance["discogs_corroborates"] = "NO"
        provenance["discogs_barcode_conflict"] = (
            f"mb:{mb_barcode}|discogs:{d_meta.barcode}"
        )
    return True


_R9_DISAGREE_THRESH = 0.15


def _emit_r9_disagreement(
    provenance: dict[str, str],
    pre_mb_album: str | None,
    pre_mb_artist: str | None,
    mb_album: str | None,
    mb_artist: str | None,
) -> None:
    """Emit ``provenance["disagreement_cddb_mb"]`` when both services disagree.

    The pre-MB album/artist were filled by CDDB (or by raw embedded
    metadata); the MB-candidate values come from the candidate that drove
    the merge. Disagreement is computed via pattern-weighted Levenshtein
    (``string_dist``) after NFC + casefold + reissue-suffix stripping.
    Fires when the distance exceeds ``_R9_DISAGREE_THRESH`` (0.15), so
    minor punctuation/article differences are not flagged. Value is a
    comma-separated list of fields that disagreed (``album``, ``artist``,
    or ``album,artist``). Absent when one side is blank or below threshold.
    """
    from cdda2img.string_dist import string_dist

    fields: list[str] = []
    if pre_mb_album and mb_album:
        dist = string_dist(
            _r9_normalise_for_compare(pre_mb_album),
            _r9_normalise_for_compare(mb_album),
        )
        if dist > _R9_DISAGREE_THRESH:
            fields.append("album")
            provenance["disagreement_album_dist"] = f"{dist:.3f}"
    if (
        pre_mb_artist
        and mb_artist
        and pre_mb_artist != "Unknown Artist"  # raw default; not from CDDB
    ):
        dist = string_dist(
            _r9_normalise_for_compare(pre_mb_artist),
            _r9_normalise_for_compare(mb_artist),
        )
        if dist > _R9_DISAGREE_THRESH:
            fields.append("artist")
            provenance["disagreement_artist_dist"] = f"{dist:.3f}"
    if fields:
        provenance["disagreement_cddb_mb"] = ",".join(fields)


def _acoustid_gate(
    disc: RBIDisc,
    per_track_hits: list[list],
    provenance: dict[str, str],
) -> None:
    """§10.4 AcoustID gate — post-selection, set-level corroboration check.

    Asks "does the disc audio match the *album* the disc-ID/rung selected?" to
    catch a wrong disc-ID, a TOC collision, or a mispress. Matched at the
    **release-group** level, not the release-id: AcoustID is edition-blind
    (it identifies recordings, not pressings), so a release-id comparison would
    false-fail whenever the disc-ID pressing differs from AcoustID's pressing of
    the same album. A single probed track suffices (union membership across all
    fingerprinted tracks == "appears in >= 1 track").

    Only writes ``acoustid_gate=failed`` on a genuine miss, and only when there
    was evidence on both sides to compare (the disc carries a matched
    release-group AND AcoustID supplied at least one release-group). Absence of
    the key means pass / not-evaluated — both treated as "do not suppress" by
    ``_gate_adjusted_auto`` (per spec: the key is fail-only).
    """
    if not disc.mb_release_group_id:
        return
    rg_seen = {
        h.mb_release_group_id
        for hits in per_track_hits
        for h in hits
        if h.mb_release_group_id
    }
    if rg_seen and disc.mb_release_group_id not in rg_seen:
        provenance["acoustid_gate"] = "failed"


def _gate_adjusted_auto(auto: bool, provenance: dict[str, str]) -> bool:
    """Return the effective ``auto_apply`` after applying the §10.4 gate.

    Warn-only policy (user decision 2026-06-20): on a failed gate the disc-ID
    result is still produced and the failure is recorded in PROV
    (``acoustid_gate=failed``) for later auditing (``list --prov | grep
    acoustid_gate``); we only refuse to *auto-commit* it. On a TTY this drops to
    the interactive menu so the user can review; on a headless ``--auto`` run the
    result is committed but flagged (revisit if a real failing disc shows a
    better policy). A WARNING is printed whenever the gate failed, so the user
    sees why the menu opened even without ``--auto``.
    """
    if provenance.get("acoustid_gate") == "failed":
        print(
            "  Warning: AcoustID gate — disc audio does not corroborate the "
            "matched release (album level); not auto-confirming, please review."
        )
        return False
    return auto


def _r6_tally_and_merge(
    per_track_hits: list[list],
    disc: RBIDisc,
    provenance: dict[str, str],
    *,
    selected_release_id: str | None,
) -> RBIDisc:
    """Tally AcoustID release MBIDs across fingerprinted tracks.

    A "consistent winner" appears in every per-track result set. When a pressing
    was selected upstream, ``acoustid_corroborates`` records whether it is among
    the winners, and the §10.4 gate runs. When none was, a converged winner sets
    ``acoustid_corroborates=YES`` and nothing else.

    **The name is a leftover: this merges nothing on either branch.** The
    no-MBID merge was removed (see the comment on that branch); the name was not.
    ``resolver_adapter`` depends on the absence and asserts it by test, so read
    the name as historical, not as a description.
    """

    all_rids: set[str] = set()
    for hits in per_track_hits:
        for hit in hits:
            if hit.mb_release_id:
                all_rids.add(hit.mb_release_id)
    if not all_rids:
        return disc

    consistent_rids = [
        rid
        for rid in all_rids
        if all(any(h.mb_release_id == rid for h in hits) for hits in per_track_hits)
    ]

    if selected_release_id:
        # B-2: read the Layer-1 selected pressing (mb_result.selected_release_id),
        # not the mutated disc.mb_release_id — identical today, but survives the B-4
        # flip that stops the mutate-as-you-go merge.
        # Membership, not equality with consistent_rids[0]: the list is derived
        # from a set (nondeterministic order), so when AcoustID converges on more
        # than one consistent release, indexing [0] could miss the disc's own MBID
        # even though AcoustID corroborates it.
        provenance["acoustid_corroborates"] = (
            "YES" if selected_release_id in consistent_rids else "NO"
        )
        _acoustid_gate(disc, per_track_hits, provenance)
        return disc

    # No disc-ID match from MB. AcoustID identifies *recordings*, not pressings;
    # the same recording appears on every compilation that includes it, so
    # "consistent across N fingerprinted tracks" is weak evidence for any specific
    # release title. Merging album-level metadata here routinely picks the wrong
    # compilation when MB disc-ID found nothing. Preserve the +0.25 confidence
    # signal (acoustid_corroborates) but let CDDB / stage-7 supply the title.
    if consistent_rids:
        provenance["acoustid_corroborates"] = "YES"
    return disc


def _r6_acoustid_corroborate(
    disc: RBIDisc,
    pcm_file: Path,
    provenance: dict[str, str],
    ui: TerminalUI | None,
    *,
    selected_release_id: str | None,
) -> RBIDisc:
    """R6: pre-menu AcoustID fingerprint of tracks 1 and ceil(N/2).

    Emits ``provenance["acoustid_corroborates"]`` as YES (the selected MB release
    is among those carrying the fingerprinted recordings) or NO (it is not), and
    runs the §10.4 release-group gate.

    **Merges nothing.** An earlier version of this docstring claimed that a disc
    with no MB release MBID got the converged AcoustID release merged in via
    ``_merge_into_disc``; that merge was removed deliberately and the docstring
    outlived it by long enough to be found during the N3 investigation. See the
    comment in ``_r6_tally_and_merge`` for why (the same recording appears on
    every compilation that includes it, so the converged release is routinely the
    wrong album), and ``resolver_adapter`` for the test that pins the absence.

    No-op when ``acoustid_lookup.is_available()`` is False. Failures from
    AcoustID propagate as empty results; the function never raises.
    """
    import tempfile
    import wave

    from cdda2img import acoustid_lookup

    if not disc.tracks or not acoustid_lookup.is_available():
        return disc

    n = len(disc.tracks)
    indexes = [1]
    if n >= _R6_MIN_TRACKS_FOR_SECOND_SAMPLE:
        indexes.append((n + 1) // 2)
    _ui_status(ui, "Fingerprinting via AcoustID…")

    per_track_hits: list[list] = []
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        for idx in indexes:
            entry = disc.tracks[idx - 1]
            byte_offset = (
                sum(
                    (t.pregap_frames + t.duration_frames) * _R6_BYTES_PER_FRAME
                    for t in disc.tracks[: idx - 1]
                )
                + entry.pregap_frames * _R6_BYTES_PER_FRAME
            )
            byte_count = entry.duration_frames * _R6_BYTES_PER_FRAME
            with open(pcm_file, "rb") as f:
                f.seek(byte_offset)
                pcm = f.read(byte_count)
            wav_path = td_path / f"r6-track-{idx:02d}.wav"
            with wave.open(str(wav_path), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(pcm)
            per_track_hits.append(acoustid_lookup.fingerprint_and_lookup(wav_path))

    return _r6_tally_and_merge(
        per_track_hits, disc, provenance, selected_release_id=selected_release_id
    )


def _r6_acoustid_corroborate_wavs(
    disc: RBIDisc,
    source_wavs: list[Path],
    provenance: dict[str, str],
) -> RBIDisc:
    """R6 pre-menu AcoustID corroboration for the *create* pipeline.

    Identical tally/merge logic to ``_r6_acoustid_corroborate`` but
    fingerprints the already-transcoded per-track WAV files directly —
    no temp-WAV creation, no PCM seek arithmetic. Sets
    ``provenance["lookup_status_acoustid"]``.
    """
    from cdda2img import acoustid_lookup

    if not disc.tracks or not acoustid_lookup.is_available():
        provenance["lookup_status_acoustid"] = _r12_status(
            attempted=False, has_data=False, errored=False
        )
        return disc

    n = len(disc.tracks)
    indexes = [1]
    if n >= _R6_MIN_TRACKS_FOR_SECOND_SAMPLE:
        indexes.append((n + 1) // 2)

    per_track_hits: list[list] = []
    for idx in indexes:
        per_track_hits.append(
            acoustid_lookup.fingerprint_and_lookup(source_wavs[idx - 1])
        )

    # Create path: no Layer-1 MB disc-ID selection runs, so the disc's own
    # mb_release_id (e.g. from a mutagen musicbrainz_albumid tag) is the gating
    # signal — pass it through explicitly.
    disc = _r6_tally_and_merge(
        per_track_hits, disc, provenance, selected_release_id=disc.mb_release_id
    )
    provenance["lookup_status_acoustid"] = _r12_status(
        attempted=True,
        has_data="acoustid_corroborates" in provenance,
        errored=False,
    )
    return disc


def _emit_mb_provenance(
    provenance: dict[str, str],
    mb_result: MBPrepopResult,
    preferred_country: list[str],
) -> None:
    """Write the MB-disc-ID-derived provenance keys (multi-match resolution +
    §10.3 release-selection provenance + Unit-G rejection count)."""
    if mb_result.isrc_disambiguated:
        provenance["multi_match_isrc_disambiguated"] = "YES"
    if mb_result.release_selected_via:
        # §10.3: which lexicographic key pinned the release among several
        # album-consistent pressings. Records the config-dependent preference
        # that shaped the choice (R10 reproducibility).
        provenance["release_selected_via"] = mb_result.release_selected_via
        if mb_result.release_selected_via == "preferred_country" and preferred_country:
            provenance["preferred_country_applied"] = ",".join(preferred_country)
    if mb_result.release_tied_after:
        # N4: `release_selected_via` names the first rung that *varies*, which is
        # not the rung that decides — on the reference disc it read
        # `preferred_country` while five candidates were still tied and the
        # alphabetical `mbid` sort picked among them (wrongly, as it turned out).
        # This key records what actually happened: the last rung that narrowed,
        # and how many candidates the terminal sort had to arbitrate. `:1` is a
        # determined result; anything higher is an admitted guess. Emitted
        # alongside `via` rather than replacing it — the `ctdb_declined`
        # precedent of recording the event, not a verdict.
        provenance["release_tied_after"] = mb_result.release_tied_after
    if mb_result.meta is not None and mb_result.meta.disambiguation:
        # N5: MB's own description of the pinned pressing, recorded so the
        # container states which physical object it claims to be rather than
        # only an MBID a later MB edit could redefine.
        #
        # Written here, for EVERY path that pins a release, not in the menu —
        # the menu's candidate list is empty on the single-match path, which is
        # the common case, so sourcing it there alone would leave the key absent
        # on most discs while the spec says absence means "MB has no
        # description". One signal, two causes: the shape N4 was about.
        # `_record_pressing_outcome` overwrites this on a manual pick.
        provenance["release_disambiguation"] = mb_result.meta.disambiguation
    if mb_result.rejected_inconsistent:
        # Unit G: record that N MB candidates were discarded for contradicting a
        # gospel on-disc MCN/ISRC — preserves the *why* behind a blanked field.
        provenance["mb_rejected_inconsistent"] = str(mb_result.rejected_inconsistent)


def _note_corroboration_target(
    provenance: dict[str, str], checked_release_id: str | None, disc: RBIDisc
) -> None:
    """Record when the corroboration checks ran against a different release than
    the one finally selected (N5).

    ``acoustid_corroborates`` and ``discogs_corroborates`` are computed
    *before* the menu, against the release the ladder pinned. If the user then
    picks a different pressing, both keys silently describe a release the
    container no longer claims to be — the same "reads true, and reads
    identically under the alternative" shape N3 and N4 were about.

    So name the release they were taken against. This is cheaper than
    recomputing (AcoustID would need the per-track hits, which are not retained
    past R6) and it is exact: an auditor can see which claim covers what. The
    §10.4 gate is unaffected either way — it matches at release-GROUP level, and
    every menu candidate is drawn from the one plurality release-group.
    """
    if not checked_release_id or checked_release_id == disc.mb_release_id:
        return
    if "acoustid_corroborates" in provenance or "discogs_corroborates" in (provenance):
        provenance["corroborated_release"] = checked_release_id


def _collect_metadata_proposals(
    baseline: RBIDisc,
    mb_meta: DiscMeta | None,
    chosen_barcode: str | None,
    discogs_hit: DiscMeta | None,
    stage7_meta: DiscMeta | None,
    cddb_meta: DiscMeta | None,
) -> list[FieldProposal]:
    """Collect every metadata source's proposals in the live ``_run_metadata_lookups``
    call order (B-3 part 2 / trust_model_design §11.4): baseline -> MB -> §10
    canonical-barcode overwrite -> Discogs -> stage-7 -> CDDB.

    AcoustID is deliberately absent: both corroborate steps merge no fields, so
    they contribute no proposals (proven by ``test_merge_sequence`` and now by the
    integration shadow-equivalence proof riding the real wrapper). ``stage7_meta``
    is the ``strip_pressing_mbid``-ed duration match; ``discogs_hit`` is the
    Discogs ``DiscMeta`` that was actually merged (``None`` when none was).

    This is the B-4 committer's input. Today it feeds only the shadow-equivalence
    proof (resolve == live merge sequence); at B-4 the live merges are replaced by
    ``disc_from_resolution(resolve(...), baseline)``.
    """
    from cdda2img.field_resolver import Source
    from cdda2img.resolver_adapter import (
        baseline_proposals,
        canonical_barcode_proposal,
        meta_to_proposals,
    )

    props = baseline_proposals(baseline)
    if mb_meta is not None:
        # mb_meta (MBPrepopResult.meta, R8) has three producing paths, all safe
        # under the MB_DISC_ID label: (1) single disc-ID match and (2) R1 multi-
        # match ISRC disambiguation are genuinely pressing-level (every candidate
        # shares the disc-ID fingerprint); (3) the R4 zero-match ISRC tally is
        # recording-level but its winner is stripped to mb_release_id=None at the
        # source (mb_lookup._resolve_via_isrc_tally -> strip_pressing_mbid, pinned
        # by test_merge_invariants.test_resolve_via_isrc_tally_strips_pressing_mbid).
        # So no recording-level pressing id ever reaches the resolver mislabelled
        # as disc-ID-proven — the C2 chokepoint upstream is what makes this safe,
        # and the accumulator reproduces today's reliance on it.
        props += meta_to_proposals(mb_meta, Source.MB_DISC_ID)
    props += canonical_barcode_proposal(chosen_barcode)
    if discogs_hit is not None:
        props += meta_to_proposals(discogs_hit, Source.DISCOGS)
    if stage7_meta is not None:
        props += meta_to_proposals(stage7_meta, Source.DURATION)
    if cddb_meta is not None:
        props += meta_to_proposals(cddb_meta, Source.CDDB)
    return props


def _cddb_consensus(
    cddb_matches: list[DiscMeta], provenance: dict[str, str], cddb_verbose: bool
) -> DiscMeta | None:
    """Collapse gnudb's many TOC-matched entries to one consensus record.

    Heuristic prune (implausible pre-CD years, degenerate entries) then per-field
    plurality vote, instead of trusting the arbitrary first entry. Records the
    candidate count and any pruned years in PROV. Still applied at the lowest
    (fill-blank) precedence by the caller.
    """
    from cdda2img.cddb import consensus_from_candidates

    cddb_meta, years_pruned = consensus_from_candidates(cddb_matches)
    if cddb_matches:
        provenance["cddb_candidates"] = str(len(cddb_matches))
        if years_pruned:
            provenance["cddb_years_pruned"] = str(years_pruned)
    if cddb_meta is not None and cddb_verbose:
        extra = (
            f" (consensus of {len(cddb_matches)} matches)"
            if len(cddb_matches) > 1
            else ""
        )
        by = f" by {cddb_meta.artist}" if cddb_meta.artist else ""
        print(f'  CDDB: matched "{cddb_meta.album}"{by}{extra} (lowest priority)')
    return cddb_meta


def _run_metadata_lookups(
    disc: RBIDisc,
    pcm_file: Path,
    provenance: dict[str, str],
    *,
    do_cddb: bool,
    cddb_track_lsns: list[int] | None,
    cddb_disc_last_lsn: int | None,
    cddb_server: str | None,
    cddb_verbose: bool,
    mb_verbose: bool,
    preferred_country: list[str],
    ui: TerminalUI | None,
    _shadow_out: dict[str, object] | None = None,
) -> tuple[RBIDisc, MBPrepopResult]:
    """Run every remote metadata lookup and merge results into *disc* in
    precedence order: disc-baked CD-Text > MusicBrainz > Discogs > AcoustID
    > CDDB.

    Every merge is fill-blank (an existing non-blank field wins), so applying
    CDDB **last** makes it a zero-trust, last-resort gap-filler — every other
    source overwrites it, and CDDB only supplies fields nothing richer did.
    CDDB's flat freedb ``TTITLE`` ("Artist / Title" in one string) cannot
    cleanly separate a track's title from its performer the way MB's distinct
    title / artist-credit fields can, so it is no longer allowed to win a
    contested field. The CDDB *query* still runs in parallel with the MB
    lookup so a slow or failing gnudb never gates the rip.

    Mutates *provenance* with the R9 disagreement surface and R12 per-service
    status. Returns ``(merged_disc, mb_result)``.

    B-3 part 2 shadow seam: when *_shadow_out* is given, the collect->resolve
    trust resolver is run alongside the live merge from the same source metas and
    the result is stashed for the integration-test equivalence proof
    (``disc_from_resolution(resolve(acc), baseline) == live_disc``). It is a pure
    side-computation — gated off in production so the proposal build (which can
    raise on a C2 violation) can never abort a best-effort metadata run. At B-4
    this seam becomes the sole committer. Keys written: ``disc`` (shadow disc),
    ``resolution`` (the full :class:`~cdda2img.field_resolver.Resolution`, whose
    ``contenders`` diagnoses any mismatch), ``proposals``, ``baseline``.
    """
    from concurrent.futures import ThreadPoolExecutor

    from cdda2img.cddb import query_cddb

    # B-3 part 2: snapshot the pre-merge baseline BEFORE any lookup runs. This is
    # the disc the resolver assembles onto (it carries the C1 physical fields no
    # source proposes); it must be captured before prepopulate_from_mb / the
    # in-place catalog overwrite touch *disc*.
    baseline_snapshot = copy.deepcopy(disc)
    stage7_meta: DiscMeta | None = None
    from cdda2img.mb_lookup import _merge_into_disc, prepopulate_from_mb

    original_album, original_artist = disc.album, disc.artist

    # CDDB query runs concurrently with MB for latency; its result is applied
    # last (lowest precedence). A flaky/slow gnudb must never gate the rip.
    cddb_matches: list[DiscMeta] = []
    if do_cddb:
        with ThreadPoolExecutor(max_workers=2) as ex:
            cddb_future = ex.submit(
                query_cddb, cddb_track_lsns, cddb_disc_last_lsn, cddb_server
            )
            mb_future = ex.submit(
                prepopulate_from_mb,
                disc,
                verbose=mb_verbose,
                preferred_country=preferred_country,
            )
            try:
                cddb_matches = cddb_future.result()
            except Exception as exc:
                log.warning("CDDB query failed; continuing without it: %s", exc)
            mb_result = mb_future.result()
    else:
        mb_result = prepopulate_from_mb(
            disc, verbose=mb_verbose, preferred_country=preferred_country
        )

    cddb_meta = _cddb_consensus(cddb_matches, provenance, cddb_verbose)

    # MusicBrainz applied first, over the CD-Text baseline.
    disc = mb_result.disc

    # R9: CDDB↔MB (or raw↔MB) disagreement. Compare the non-MB view — CDDB's
    # album/artist if present, else the disc's own embedded values — against
    # the MB candidate, directly (not via disc state) so the result is
    # independent of the merge order.
    pre_mb_album = (cddb_meta.album if cddb_meta else None) or original_album
    pre_mb_artist = (cddb_meta.artist if cddb_meta else None) or original_artist
    _emit_mb_provenance(provenance, mb_result, preferred_country)
    _emit_r9_disagreement(
        provenance,
        pre_mb_album,
        pre_mb_artist,
        mb_result.mb_candidate_album,
        mb_result.mb_candidate_artist,
    )
    provenance["lookup_status_mb"] = _r12_status(
        attempted=True, has_data=mb_result.match_count > 0, errored=False
    )

    # Discogs (catalogue / label / country).
    from cdda2img import discogs_lookup as _discogs

    discogs_attempted = _discogs.is_available()
    disc, chosen_barcode, discogs_hit = _prepopulate_from_discogs(
        disc, ui, barcode_hints=mb_result.barcode_hints, provenance=provenance
    )
    # §10.3.1: cross-source barcode corroboration on the rung-selected release.
    link_answered = _discogs_barcode_corroborate(
        disc, provenance, selected_release_id=mb_result.selected_release_id
    )
    # `lookup_status_discogs` answers ONE question — did Discogs reply — and it is
    # emitted after both queries because there are two of them: the barcode search
    # here, and the MB→Discogs link follow above.
    #
    # It has now been wrong twice in the same direction, and the shape is worth
    # keeping in view. First it was `disc.barcode changed during the call`, which
    # reads as `empty` whenever MB had already supplied the barcode — the normal
    # case. Then it was `a hit was merged`, which reads as `empty` whenever the
    # search was too RICH to narrow: measured on Tracy Chapman,
    # search_by_barcode('0075596077422') returns 25 rows and PROV said `empty`
    # while `discogs_corroborates=YES` sat two lines below it, having successfully
    # fetched a Discogs release in the same run.
    #
    # Both versions measured *what Discogs changed about our disc* and reported it
    # as *whether Discogs answered*. Under fill-blank merge semantics those two
    # diverge exactly when the rest of the metadata is good, so the better the
    # record, the more likely the status lied. The disambiguation outcome is a
    # real and useful fact — it is now `discogs_barcode_matches` and
    # `discogs_barcode_outcome`, reported under its own name.
    searched = int(provenance.get("discogs_barcode_matches", "0"))
    provenance["lookup_status_discogs"] = _r12_status(
        attempted=discogs_attempted,
        has_data=searched > 0 or link_answered,
        errored=False,
    )

    # AcoustID per-track corroboration (tracks 1 and ceil(N/2)).
    from cdda2img import acoustid_lookup as _acoustid

    acoustid_attempted = _acoustid.is_available()
    disc = _r6_acoustid_corroborate(
        disc,
        pcm_file,
        provenance,
        ui,
        selected_release_id=mb_result.selected_release_id,
    )
    provenance["lookup_status_acoustid"] = _r12_status(
        attempted=acoustid_attempted,
        has_data="acoustid_corroborates" in provenance,
        errored=False,
    )

    # CDDB status is recorded here regardless of merge order; the attempt and
    # its outcome are independent of when its fields are folded in.
    if do_cddb:
        provenance["lookup_status_cddb"] = _r12_status(
            attempted=True, has_data=cddb_meta is not None, errored=False
        )

    # Stage 7: last-resort duration match (OPT-3 — now runs BEFORE CDDB). Fires
    # only when nothing above identified the release in MusicBrainz (no release
    # id) but we still have an album/artist to search with. A duration-matched
    # MB release is a stronger guess than CDDB's flat "Artist / Title" string,
    # so it is given the higher precedence of the two by merging first.
    #
    # Tradeoff (the cost of this reorder): stage-7's gate needs disc.album or
    # disc.artist already populated as a search seed. On a disc whose ONLY
    # album/artist source is CDDB (no CD-Text, no MB/Discogs/AcoustID hit),
    # stage-7 now sees an empty seed and does not fire — the old CDDB-first
    # order would have seeded it. That CDDB-only-seed disc is the rare case we
    # give up to make the duration matcher outrank CDDB on every other disc.
    # B-2: gate on the Layer-1 selected pressing, not the mutated disc. Identical
    # today (selected_release_id == disc.mb_release_id here); survives the B-4 flip.
    if mb_result.selected_release_id is None and (disc.album or disc.artist):
        from cdda2img.mb_lookup import duration_match_lookup, strip_pressing_mbid

        dm = duration_match_lookup(disc, verbose=mb_verbose)
        if dm is not None:
            # Record which release matched for provenance, but do NOT bake its
            # (text+duration-matched, non-disc-ID) pressing MBID into disc as if
            # authoritative — route through the C2 chokepoint before merging,
            # keeping the release group, exactly as the ISRC-tally fallback does.
            provenance["duration_match_release"] = dm.mb_release_id or "?"
            stage7_meta = strip_pressing_mbid(dm)
            disc = _merge_into_disc(stage7_meta, disc)

    # CDDB applied DEAD LAST — zero-trust gap-filler, now the absolute lowest
    # precedence (below even stage-7). By now CD-Text, MB, Discogs, AcoustID and
    # the stage-7 duration match have all had their turn, so this only fills
    # fields none of them provided.
    if cddb_meta is not None:
        disc = _merge_into_disc(cddb_meta, disc)

    # B-4 FLIP: the collect->resolve trust resolver is now the SOLE COMMITTER. The
    # per-source merge chain above is retained (not yet deleted) and demoted to two
    # roles: it still feeds the mid-pipeline lookups their search context (Discogs
    # album-match, the stage-7 seed), and its final ``merged`` disc is BOTH the live
    # equivalence ORACLE (``_shadow_out["merged"]`` — a non-tautological check that
    # the resolver reproduces the legacy fold, since the returned disc is now the
    # resolver's) and the never-fail FALLBACK. Deleting the now-redundant trailing
    # merges is a follow-up after production soak (trust_model_design §11.4 B-4).
    merged = disc  # legacy fill-blank fold output: lookup context + oracle + fallback
    from cdda2img.field_resolver import disc_from_resolution, resolve
    from cdda2img.resolver_adapter import sanitize_base

    committed = merged  # fallback default
    proposals: list[FieldProposal] = []
    resolution = None
    try:
        # The proposal BUILD must be inside the guard: C2 raises in
        # FieldProposal.__post_init__ during construction (inside
        # _collect_metadata_proposals), not in resolve / disc_from_resolution.
        proposals = _collect_metadata_proposals(
            baseline_snapshot,
            mb_result.meta,
            chosen_barcode,
            discogs_hit,
            stage7_meta,
            cddb_meta,
        )
        resolution = resolve(proposals)
        # sanitize_base: drop invalid on-disc ISRCs uniformly (committed-disc
        # contract; see resolver_adapter.sanitize_base).
        committed = disc_from_resolution(resolution, sanitize_base(baseline_snapshot))
    except Exception as exc:
        # C2 cannot fire on the live domain (the sole recording-level source,
        # stage-7, is strip_pressing_mbid'd before construction), but metadata is
        # best-effort and must never abort a rip — any build/resolve/assemble
        # failure falls back to the faithful legacy fold.
        log.warning("trust resolver failed; using legacy merge fallback: %s", exc)

    if _shadow_out is not None:
        _shadow_out["proposals"] = proposals
        _shadow_out["resolution"] = resolution
        _shadow_out["baseline"] = baseline_snapshot
        _shadow_out["merged"] = merged  # legacy oracle — compared against committed
        _shadow_out["disc"] = committed

    return committed, mb_result


def _finalize_import(
    disc: RBIDisc,
    pcm_file: Path,
    provenance: dict[str, str],
    output_stem: str,
    loudness: str,
    output: Path | None,
    arip_block: bytes | None = None,
    rlog_builder: RipLogBuilder | None = None,
    ui: TerminalUI | None = None,
    low_dr_threshold: float = 5.0,
    cddb_track_lsns: list[int] | None = None,
    cddb_disc_last_lsn: int | None = None,
    cddb_server: str | None = None,
    ar_summary: str | None = None,
    tui: bool = True,
    duplicate_policy: str | None = None,
    auto: bool = False,
    preferred_country: list[str] | None = None,
) -> Path:
    """Shared post-rip/import pipeline: lookups → metadata menu → TOC → RG → container.

    The remote-metadata lookups and their precedence merge are delegated to
    ``_run_metadata_lookups`` (disc-baked CD-Text > MB > Discogs > AcoustID >
    CDDB, CDDB last as a zero-trust gap-filler). When *cddb_track_lsns* /
    *cddb_disc_last_lsn* are provided (rip path) the CDDB query runs in
    parallel with MB; when None (import path) only MB runs.
    """
    import sys

    from cdda2img.metadata_menu import run_metadata_menu

    diag = _Notes(ui)
    do_cddb = cddb_track_lsns is not None and cddb_disc_last_lsn is not None
    cddb_verbose = ui is None
    mb_verbose = (ui is None) and sys.stdin.isatty()

    _ui_status(
        ui, "Querying CDDB + MusicBrainz…" if do_cddb else "Querying MusicBrainz…"
    )

    disc, mb_result = _run_metadata_lookups(
        disc,
        pcm_file,
        provenance,
        do_cddb=do_cddb,
        cddb_track_lsns=cddb_track_lsns,
        cddb_disc_last_lsn=cddb_disc_last_lsn,
        cddb_server=cddb_server,
        cddb_verbose=cddb_verbose,
        mb_verbose=mb_verbose,
        preferred_country=preferred_country or [],
        ui=ui,
    )

    # Identify the original release BEFORE the menu so the user sees
    # "Original: <title> (<year>)" in the initial summary. The menu's
    # [r] flow lets the user override; populate_original_release's
    # internal gate (skip when original_release_found is already True)
    # makes the call here idempotent with any later override.
    from cdda2img.original_release import populate_original_release

    _ui_status(ui, "Identifying original release…")
    # P1: thread the disc-ID prepop meta so the RG verify reuses it instead of
    # re-fetching the disc's own MB release (saves one round-trip at 1 req/s).
    populate_original_release(disc, verify_meta=mb_result.meta)
    # Trace the canonical result (DEBUG: the menu summary already shows it; keep
    # the normal UI clean). Same string as the menu/list/catalogue surfaces.
    log.debug("%s", format_original(disc))
    # R11: corroborate with Discogs master if both sources are present.
    _r11_corroborate_with_discogs_master(disc, provenance)

    # Compute match confidence after all lookup signals are baked into prov.
    from cdda2img.match_distance import build_match_distance

    match_dist = build_match_distance(disc, provenance)
    provenance["match_confidence"] = f"{match_dist.score:.3f}"
    provenance["match_recommendation"] = match_dist.recommendation.value
    # The interactive menu is shown unless --auto (or config auto=true). Match
    # confidence is informational only — it is surfaced as a hint but never
    # skips the menu on its own (user decision, 2026-06-20). A failed §10.4
    # AcoustID gate additionally suppresses --auto (warn-only): the disc-ID
    # result is kept and flagged in PROV, but not auto-committed.
    auto_apply = _gate_adjusted_auto(auto, provenance)

    # Hand the terminal over to the interactive metadata menu. The
    # ar_summary kwarg is passed through for completeness (rip pipeline only).
    if ui is not None:
        ui.pause()
    if auto_apply:
        print(f"  Metadata auto-confirmed — {match_dist.summary()}")
    else:
        print(f"  Metadata: {match_dist.summary()}")
    # N5: the pressing menu's population, and the claim to record about how the
    # pressing was chosen. Note `selected_release_id` is already pinned by the
    # full ladder — including the preference rungs the menu excludes. That pin
    # is deliberate and is what everything upstream of here (the §10.3.1 Discogs
    # check, R6, stage-7's gate, R9) has been reading. The menu OVERRIDES it; it
    # does not replace the act of pinning, because deferring the pin would
    # silently disable four checks in order to defer one choice.
    pressing_candidates = list(mb_result.menu_candidates)
    if len(pressing_candidates) > 1:
        provenance["release_selection"] = "auto_tiebreak"
    disc = run_metadata_menu(
        disc,
        source_pcm=pcm_file,
        ar_summary=ar_summary,
        tui=tui,
        auto_apply=auto_apply,
        pressing_candidates=pressing_candidates,
        provenance=provenance,
    )
    _note_corroboration_target(provenance, mb_result.selected_release_id, disc)
    if ui is not None:
        ui.resume()

    # Fetch and embed album art using the confirmed post-menu MB IDs.
    from cdda2img.album_art import fetch_cover, to_album_art

    _ui_status(ui, "Fetching album art…")
    _art_raw = fetch_cover(disc)
    album_art = to_album_art(_art_raw) if _art_raw is not None else None
    if album_art is not None:
        provenance["art_source"] = _art_raw.source  # type: ignore[union-attr]
        provenance["lookup_status_art"] = "OK"
    else:
        provenance["lookup_status_art"] = "empty"

    if output is None:
        new_stem = sanitize_title(disc.album)
        if new_stem:
            output_stem = new_stem

    # Loudness analysis must run BEFORE provenance is finalised: it sets
    # disc.low_dynamic_range which _add_release_provenance writes into PROV.
    rg_block = _measure_loudness_phase(
        disc, pcm_file, loudness, ui, diag.emit, low_dr_threshold
    )

    rlog_block: bytes | None = None
    if rlog_builder is not None:
        rlog_block = rlog_builder.finalize(disc)

    _add_release_provenance(provenance, disc)
    _finalize_identifiers(provenance, disc)
    toc_data = generate_toc(disc)

    _ui_status(ui, "Building container…")
    output = _resolve_output_path(output, output_stem)

    build_container(
        pcm_file,
        toc_data,
        disc,
        output,
        rg_block=rg_block,
        arip_block=arip_block,
        rlog_block=rlog_block,
        prov_data=provenance,
        extra_flags=FLAG_MASTER_MODE,
        quiet=ui is not None,
        album_art=album_art,
    )
    # register_rbi() has interactive input() prompts — pause TUI before handing
    # the terminal over so they're visible. No resume needed; this is the last step.
    if ui is not None:
        # pause() clears the TUI output region, so any post-menu notes (RG
        # warnings) must be printed *after* pause.
        ui.pause()
        diag.flush()
        print(f"   Container: {output}")
    from cdda2img.catalogue import register_rbi

    register_rbi(output, duplicate_policy=duplicate_policy)
    return output


def _resolve_drive_offsets(
    device: str, cfg
) -> tuple[
    int, int | None, str | None
]:  # cfg: Config (inline import avoids top-level dep)
    """Return ``(read_offset, write_offset, drive_name)`` for *device*.

    *drive_name* is the normalised sysfs name (e.g. ``"PLEXTOR DVDR PX-716A"``),
    or ``None`` when the sysfs probe fails.

    Resolution order for *read_offset*:
      1. Per-drive entry in cfg.drives (user-confirmed; always takes precedence).
      2. AccurateRip catalog (auto-applied when submissions >= _MIN_AR_CONFIDENCE;
         prompts the user when confidence is lower).
      3. 0 with a warning when the drive is not configured.

    *write_offset* comes from cfg.drives only; ``None`` when not configured.

    Confirmed read offsets are persisted to [[drives]] in cdda2img.toml so that
    subsequent rips skip the catalog lookup entirely.
    """
    import sys

    from cdda2img.config import save_drive_read_offset
    from cdda2img.db import open_drive_offsets_db
    from cdda2img.drive_info import (
        ensure_drive_offsets,
        find_drive_offset,
        probe_drive_name,
    )

    drive_name = probe_drive_name(device)
    if drive_name is None:
        print(_fmt_kv("Drive", "unknown (sysfs probe failed) — using read offset +0"))
        return 0, None, None

    print(_fmt_kv("Drive", drive_name))

    # 1. User-confirmed per-drive entry in config takes precedence over AR catalog.
    for d in cfg.drives:
        if d.name == drive_name:
            print(_fmt_kv("Read offset", f"{d.read_offset:+d} samples (from config)"))
            return d.read_offset, d.write_offset, drive_name

    # 2. AccurateRip catalog lookup.
    conn = open_drive_offsets_db(cfg)
    try:
        ensure_drive_offsets(conn)
        ar = find_drive_offset(conn, drive_name)
    finally:
        conn.close()

    if ar is not None:
        offset, submissions = ar
        if submissions >= _MIN_AR_CONFIDENCE:
            use_it = True
        elif sys.stdin.isatty():
            answer = (
                input(
                    f"  AccurateRip: offset {offset:+d} ({submissions} submission(s)). Use? [y/N] "
                )
                .strip()
                .lower()
            )
            use_it = answer == "y"
        else:
            use_it = False

        if use_it:
            print(
                _fmt_kv(
                    "Read offset",
                    f"{offset:+d} samples (AccurateRip, {submissions} submission(s))",
                )
            )
            try:
                save_drive_read_offset(drive_name, offset)
            except OSError as exc:
                log.warning("Could not persist drive read offset to config: %s", exc)
            return offset, None, drive_name

    # 3. Drive not configured — warn and use 0.
    if ar is None:
        print(_fmt_kv("Read offset", "+0 samples (drive not in AccurateRip catalog)"))
    else:
        print(_fmt_kv("Read offset", "+0 samples (AccurateRip match not applied)"))
    return 0, None, drive_name


def _fmt_kv(label: str, value: str) -> str:
    """Aligned ``key: value`` line for the rip header.

    Three-space indent + a 13-wide label field puts every value at column 16,
    which lines the values up under the spinner's content column (``⠷  text``).
    """
    return f"   {label + ':':<13}{value}"


def _ui_status(ui: TerminalUI | None, text: str, prog: float = -1.0) -> None:
    """Set TUI status on a phase transition: clears the output region, then updates the line."""
    if ui is not None:
        ui.clear_output()
        ui.set_status(text, prog)
    else:
        print(f"  {text}")


def _ui_print(ui: TerminalUI | None, text: str) -> None:
    """Append *text* to the TUI output region, or print it directly when no TUI."""
    if ui is not None:
        ui.add_output(text)
    else:
        print(text)


def _phase_progress_cb(
    ui: TerminalUI | None, label: str
) -> Callable[[int, int], None] | None:
    """Build a CD-frame progress callback that drives the TUI bar for *label*.

    Returns None when there is no TUI, so the phase runs without reporting.
    *done*/*total* are CD frames; the detail shows elapsed/total time as M:SS.
    """
    if ui is None:
        return None

    def _cb(done: int, total: int) -> None:
        mins, secs = divmod(done // CD_FRAMES_PER_SECOND, 60)
        tmins, tsecs = divmod(total // CD_FRAMES_PER_SECOND, 60)
        ui.set_status(
            label,
            done / total if total else 0.0,
            detail=f"({mins:02d}:{secs:02d}/{tmins:02d}:{tsecs:02d})",
        )

    return _cb


def _fast_scan_disc(device: str):
    """Read the disc geometry + CD-Text quickly via AccuDisc → RBIDisc (M3).

    Cosmetic only — used to derive the disc-title preview line. Returns None on
    any failure (no disc, drive busy, parse error); never raises into the rip.
    Must run *before* the track-1 grab: both touch the single optical drive.

    Uses the two standalone lead-in subcommands (``fulltoc`` + ``cdtext``) rather
    than a ``read``: both answer from the lead-in without spinning the program
    area, which is what made the old ``--fast-toc`` cheap enough to run in the
    banner. CD-Text absence is normal and is not an error.
    """
    import tempfile

    from cdda2img.accudisc_reader import read_lead_in
    from cdda2img.cdtext import parse_cdtext
    from cdda2img.rbi_format import RBIDisc, RBITocEntry
    from cdda2img.subchannel import parse_fulltoc, session1_audio_tracks

    try:
        with tempfile.TemporaryDirectory() as tmp:
            fulltoc_path = Path(tmp) / "preview.fulltoc"
            cdtext_path = Path(tmp) / "preview.cdtext"
            read_lead_in(device, fulltoc_path, cdtext_path)
            if not fulltoc_path.exists():
                return None
            full = parse_fulltoc(fulltoc_path.read_bytes())
            cdtext_raw = cdtext_path.read_bytes() if cdtext_path.exists() else b""

        audio, leadout = session1_audio_tracks(full)
        if not audio:
            return None
        blocks = parse_cdtext(cdtext_raw) if cdtext_raw else []
        block = blocks[0] if blocks else None

        bounds = [t.start_lba for t in audio] + [leadout]
        tracks = [
            RBITocEntry(
                track_number=t.track,
                title=(block.track_title(t.track) if block else None) or "",
                performer=(block.track_performer(t.track) if block else None) or "",
                start_frame=bounds[i],
                duration_frames=bounds[i + 1] - bounds[i],
            )
            for i, t in enumerate(audio)
        ]
        return RBIDisc(
            album=(block.album_title if block else None) or "",
            artist=(block.album_performer if block else None) or "",
            tracks=tracks,
        )
    except Exception as exc:
        log.debug("fast TOC scan for disc preview failed: %s", exc)
        return None


def _disc_preview_label(disc) -> str:
    """Best-guess ``Album - Artist`` label for the disc-title preview line.

    Disc-baked CD-Text (captured by the fast TOC scan) is authoritative and used
    as-is. Otherwise fall back to a non-authoritative MusicBrainz disc-ID lookup,
    picking the album/artist by *plurality* across the matching releases — this is
    only a nicety (the menu later confirms), so a popular guess is acceptable.
    """
    from collections import Counter

    from cdda2img.mb_lookup import lookup_disc_id

    if disc.album:
        return f"{disc.album} - {disc.artist}" if disc.artist else disc.album

    pairs = [(m.album, m.artist) for m in lookup_disc_id(disc) if m.album]
    if not pairs:
        return "(unknown)"
    album, artist = Counter(pairs).most_common(1)[0][0]
    return f"{album} - {artist}" if artist else album


def _start_disc_preview(
    device: str, ui: TerminalUI, disc: RBIDisc | None = None
) -> None:
    """Show a best-guess disc title in the TUI header (preview pipeline only).

    Accepts an already-scanned *disc* to avoid a second drive round-trip when
    the caller has already run ``_fast_scan_disc``. When *disc* is None the scan
    runs here. Resolves the label on a background thread so the MB lookup overlaps
    the track-1 grab. The header starts as ``Disc: (identifying…)`` and is
    repainted once the lookup returns.
    """
    ui.set_header([_fmt_kv("Disc", "(identifying…)")])
    if disc is None:
        disc = _fast_scan_disc(device)
    if disc is None:
        ui.set_header([])  # nothing to show — drop the line
        return

    _disc = disc  # capture for closure — disc may be rebound in caller

    def _worker() -> None:
        try:
            label = _disc_preview_label(_disc)
        except Exception as exc:
            log.debug("disc preview lookup failed: %s", exc)
            label = "(unknown)"
        ui.set_header([_fmt_kv("Disc", label)])

    threading.Thread(target=_worker, daemon=True).start()


def _start_track_preview(
    device: str, work_dir: Path, ui: TerminalUI | None, enabled: bool = True
) -> TrackPreview | None:
    """Grab track 1 and start looping background playback (TTY sessions only).

    Cosmetic only — start_preview() swallows every failure and returns None,
    so the rip is never affected. Returns None when *enabled* is False (--no-preview)
    or when stdout is not an interactive TTY (e.g. piped output, CI).
    Works with or without the TUI — the progress callback is None when ui is None.
    """
    import sys

    if not enabled or not sys.stdin.isatty():
        return None
    from cdda2img.track_preview import start_preview

    _ui_status(ui, "Grabbing track 1…")
    return start_preview(device, work_dir, _phase_progress_cb(ui, "Grabbing track 1…"))


def _stop_preview(preview: TrackPreview | None) -> None:
    """Stop background track-1 playback, if a preview is running."""
    if preview is not None:
        preview.stop()


class _RipPhase:
    """The rip's left-hand status text, as a function of the read position.

    Three phases, and the first is not cosmetic. Until the engine has read the
    TOC there is no track list, and the drive is genuinely spinning up and
    seeking the lead-in rather than reading track 1 — so "Ripping disc…" is what
    is true, and naming a track there would be a guess dressed as a measurement.

    ``widest_status`` exists because the map line pins every width for the life
    of the read: a cell's sector span is derived from the column count, so one
    column of drift re-buckets every cell and already-drawn damage jumps. The
    widest text has to be known *before* the first frame, which means computing
    it rather than measuring whichever phase happens to be showing.
    """

    def __init__(self) -> None:
        self._lanes: ReadLanes | None = None

    def begin(self, lanes: ReadLanes) -> None:
        self._lanes = lanes

    @property
    def text(self) -> str:
        return self._disc_text()

    def _disc_text(self) -> str:
        speed = self._lanes.speed_x if self._lanes else None
        # No speed clause when the drive did not report one: "at 0x" or "at Nonex"
        # reads as a measurement, and this one was never made.
        return f"Ripping disc at {speed}x…" if speed else "Ripping disc…"

    def at(self, done: int) -> str:
        """The status for a read that has delivered *done* sectors."""
        lanes = self._lanes
        if lanes is None or not lanes.track_starts or done <= 0:
            return self._disc_text()
        # Sector `done - 1` is the last one actually delivered. Using `done`
        # would name the next track one sector early at every boundary.
        n = bisect.bisect_right(lanes.track_starts, done - 1)
        return f"Ripping track {n:02d}…" if n else self._disc_text()

    def widest_status(self) -> int:
        lanes = self._lanes
        tracks = len(lanes.track_starts) if lanes else 0
        widths = [len(self._disc_text())]
        if tracks:
            # Every track label is the same width up to the digit count, so the
            # last track bounds them all.
            widths.append(len(f"Ripping track {tracks:02d}…"))
        return max(widths)


def _rip_disc_stage(
    device: str,
    pcm_file: Path,
    c2_file: Path,
    read_offset: int,
    cfg: Config,
    ui: TerminalUI | None = None,
) -> tuple[RipInfo, str, Path | None]:
    """Read the disc, returning (info, rip_type, c2_path).

    **One engine, one pass (M1/M2/M4).** A single AccuDisc ``read`` captures audio +
    C2 error-pointer bitmap + raw P-W subchannel, plus the ``fulltoc``/``cdtext``
    lead-in dumps in the same spin-up, and ``subq_toc.build_rip_info`` assembles the
    disc metadata from those captures: pre-gaps/INDEX/CONTROL from the Q stream,
    majority-voted MCN/ISRC (structurally immune to cdrdao bug #75), track starts from
    the error-corrected full TOC. There is no second metadata pass and no other engine
    — cdrdao and cd-paranoia are gone from the read path.

    ``cfg.c2_recovery = "off"`` skips the C2 bitmap (the audio + Q capture is unchanged);
    the bitmap is what lets ctanalyse treat flagged sectors as erasures downstream, so
    turning it off costs recovery power, not fidelity.

    The PCM returned is RAW — the drive offset is not applied here. The caller works raw
    through AR + ctanalyse and calls apply_offset exactly once, at storage.
    """
    from cdda2img.accudisc_reader import read_disc_c2

    want_c2 = cfg.c2_recovery != "off"
    sub_file = pcm_file.with_suffix(".sub")
    cdtext_file = pcm_file.with_suffix(".cdtext")
    fulltoc_file = pcm_file.with_suffix(".fulltoc")

    # The spin-up phase. It stands until the engine has read the TOC and handed
    # over the lanes, which is the first moment a track number can be honest —
    # the drive is genuinely spinning up and seeking the lead-in, and no sector
    # of track 1 has been delivered.
    phase = _RipPhase()
    _ui_status(ui, phase.text)

    def _cb(done: int, total: int) -> None:
        if ui is not None:
            ui.set_status(
                phase.at(done),
                done / total if total > 0 else 0.0,
                detail=f"({done}/{total})",
            )

    def _map_cb(lanes: ReadLanes) -> None:
        # The progress bar becomes the disc map for the length of the read: the
        # lanes are handed over at allocation, filled by the reader as sectors
        # arrive, and polled by the render thread. Note this is NOT gated on
        # want_c2 — C2 pointers are requested on the wire unconditionally, and
        # cfg.c2_recovery = "off" suppresses only the bitmap *file*. Gating here
        # would blank the map for anyone using that escape hatch, and a blank map
        # is indistinguishable from a clean disc.
        phase.begin(lanes)
        if ui is not None:
            ui.set_map(
                lanes.damage, subq=lanes.subq, status_width=phase.widest_status()
            )

    try:
        read_disc_c2(
            device,
            pcm_file,
            c2_file if want_c2 else None,
            output_sub=sub_file,
            output_cdtext=cdtext_file,
            output_fulltoc=fulltoc_file,
            progress_cb=_cb if ui is not None else None,
            map_cb=_map_cb if ui is not None else None,
        )
    finally:
        # Release the buffer on the failure path too — the renderer holds a
        # reference and would keep polling a map whose read has stopped, leaving
        # a frozen frontier on screen that reads as a stalled drive.
        if ui is not None:
            ui.set_map(None)
    try:
        from cdda2img.subq_toc import build_rip_info

        info = build_rip_info(
            fulltoc_file.read_bytes(),
            sub_file.read_bytes(),
            cdtext_file.read_bytes() if cdtext_file.exists() else None,
        )
    except (ValueError, OSError) as exc:
        # subq_toc already degrades to TOC-only geometry when the Q stream cannot be
        # anchored, so reaching here means the full TOC itself is unusable — there is
        # nothing left to build a disc from, and no second engine to ask.
        msg = f"disc metadata assembly failed: {exc}"
        raise RuntimeError(msg) from exc
    return info, "accudisc", (c2_file if want_c2 else None)


def _drive_supports_c2(device: str) -> bool:
    from cdda2img.accudisc_reader import drive_supports_c2

    return drive_supports_c2(device)


def _ar_has_partial_mismatch(results: list) -> bool:
    """True when some (but not all) disc-in-database tracks have AR mismatches.

    All-tracks mismatch means offset misconfiguration; partial mismatch means
    sector read errors — only the latter benefits from a targeted re-read.
    """
    in_db = [r for r in results if r.max_confidence is not None]
    if not in_db:
        return False
    n_ok = sum(
        1 for r in in_db if r.confidence_v1 is not None or r.confidence_v2 is not None
    )
    return 0 < n_ok < len(in_db)


def _ar_has_total_mismatch(results: list) -> bool:
    """True when the disc IS in AccurateRip and **no** track verified.

    The other side of :func:`_ar_has_partial_mismatch`, which that function's
    docstring has always diagnosed ("all-tracks mismatch means offset
    misconfiguration") without anything acting on it. The recovery ladder is
    gated on *partial*, correctly — re-reading every track is not a read-error
    remedy — so a whole-disc miss produced no recovery, no diagnosis and no PROV
    key: a rip that silently failed to verify with nothing saying why.

    The Step-D CD-R is the worked example: a `+30` read offset applied to a disc
    burned uncorrected on the same drive shifted every track, so every track
    missed, so the partial condition never held.
    """
    in_db = [r for r in results if r.max_confidence is not None]
    if not in_db:
        return False  # not in the database at all — a different, honest answer
    return not any(
        r.confidence_v1 is not None or r.confidence_v2 is not None for r in in_db
    )


def _diagnose_total_ar_miss(
    pcm_path: Path,
    track_lsns: list[int],
    disc_last_lsn: int,
    cddb_id: int,
    read_offset: int,
) -> dict[str, str]:
    """PROV keys explaining a whole-disc AccurateRip miss. Never raises.

    Diagnostic only — **the audio is not touched.** An offset that makes the disc
    verify is not automatically the right one to store: `detect_offset`'s own
    docstring warns that a widely-pressed disc verifies at several offsets at
    once (Tracy Chapman at 0, -669, -1333 and -1997, the first two at identical
    confidence), so choosing between them needs evidence from outside the audio.
    Silently re-storing at the winner would be picking one pressing's cohort and
    calling it the truth.

    What this does is convert "verification failed, no reason given" into a named
    cause the user can act on. `ar_offset_candidates` lists what would have
    verified; `ar_offset_suggests` names the delta from the offset actually used.
    """
    try:
        from cdda2img.accuraterip import detect_offset, fetch_ar_responses

        # Fetched here rather than passed in: the responses live inside the
        # partial-mismatch branch, and this path is mutually exclusive with it.
        # One extra request on a rip that has already failed to verify.
        responses, _transport, _b3 = fetch_ar_responses(
            track_lsns, disc_last_lsn, cddb_id
        )
        matches = detect_offset(pcm_path, track_lsns, disc_last_lsn, responses)
    except Exception as exc:
        log.debug("AR offset diagnosis failed: %s", exc)
        return {"ar_total_miss": "offset_probe_failed"}

    if not matches:
        # In the database, verifies at no offset in the swept radius. That is a
        # real result: the audio differs from every submitted copy, which is
        # damage or a different pressing — not a misconfiguration.
        return {"ar_total_miss": "no_offset_verifies"}

    cands = ",".join(str(m.offset) for m in matches[:4])
    return {
        "ar_total_miss": "offset_mismatch",
        "ar_offset_candidates": cands,
        "ar_offset_suggests": str(matches[0].offset - read_offset),
    }


def _recovery_status_cb(
    ui: TerminalUI | None, status_line: list[str]
) -> Callable[[int, int], None] | None:
    """Build the recovery-loop progress callback (None when there is no TUI).

    The callback keeps the current attempt's status text (``status_line[0]``, updated by
    the loop per attempt) and moves only the bar + detail — it never rewrites the status,
    so the "Recover track N (x/y)" banner stays put for the whole read. Consumes
    c2read's ``progress <done> <total>`` sector counts.
    """
    if ui is None:
        return None

    def _cb(done: int, total: int) -> None:
        ui.set_status(
            status_line[0],
            done / total if total > 0 else 0.0,
            detail=f"({done}/{total})",
        )

    return _cb


def _read_track_window(
    device: str,
    track_lsns: list[int],
    disc_last_lsn: int,
    idx: int,
    read_offset: int,
    speed: int,
    prog_cb: Callable[[int, int], None] | None,
) -> bytes:
    """One targeted c2read re-read of track ``idx`` (0-based); returns the track's
    offset-CORRECTED PCM bytes, ready for AR verification.

    The raw read window carries ``ceil(|read_offset| / 588)`` margin sectors on the
    side the offset points to; where the window would cross a disc edge it is clamped
    and zero-padded instead (the pad lands inside AccurateRip's first/last
    2940-sample exclusion zone — the same invariant accuraterip.py relies on).
    """
    from math import ceil

    from cdda2img.accudisc_reader import read_span_bytes

    leadout = disc_last_lsn + 1
    s = track_lsns[idx]
    e = track_lsns[idx + 1] if idx + 1 < len(track_lsns) else leadout
    track_bytes = (e - s) * _R6_BYTES_PER_FRAME
    lead = ceil(-read_offset / 588) if read_offset < 0 else 0
    tail = ceil(read_offset / 588) if read_offset > 0 else 0
    lo = max(0, s - lead)
    hi = min(leadout, e + tail)
    window = read_span_bytes(device, lo, hi - lo, read_speed=speed, progress_cb=prog_cb)
    pad_front = (lo - (s - lead)) * _R6_BYTES_PER_FRAME
    pad_back = ((e + tail) - hi) * _R6_BYTES_PER_FRAME
    if pad_front or pad_back:
        window = bytes(pad_front) + window + bytes(pad_back)
    base = lead * _R6_BYTES_PER_FRAME + read_offset * 4
    corrected = window[base : base + track_bytes]
    if len(corrected) < track_bytes:
        msg = f"short window read: {len(corrected)} of {track_bytes} bytes"
        raise RuntimeError(msg)
    return corrected


def _recover_failed_tracks(
    device: str,
    failed_tracks: list,
    track_lsns: list[int],
    disc_last_lsn: int,
    pcm_file: Path,
    responses: list,
    n_tracks: int,
    ladder: list[int],
    n_passes: int,
    read_offset: int,
    ui: TerminalUI | None,
) -> dict[int, str]:
    """Re-read each AR-failed track across the drive's speed ladder until it matches.

    For every failed track, sweep the ladder *fastest→slowest*, ``n_passes`` times (total
    ``n_passes x len(ladder)`` attempts), re-reading the track's raw sector window via
    ``c2read`` at each speed, AR-verifying the offset-corrected slice against the cached
    *responses*, and splicing the first match into the RAW *pcm_file*. The splice is
    sample-exact — the verified corrected bytes land at ``track_start*2352 +
    read_offset*4`` — so a neighbouring track's already-verified audio is never
    perturbed. A track that never matches keeps its original audio (no unverified
    splice). *pcm_file* stays in the raw offset domain throughout; the caller applies
    ``apply_offset`` once, at storage. Speed is set per read and NOT restored between
    attempts — the caller restores once after the loop.

    Validated live: 6/6 recoveries on the damaged reference disc across two days
    (tools/c2read_recovery_test.py + tools/c2timing.py baseline arm), every recovery
    byte-identical at AR confidence 200. The sweep across passes x speeds is the
    recovery mechanism — no paranoia engine involved.

    The recovery loop owns the TUI status line for its whole duration: a compact
    per-attempt status (``Recover track N (x/y)``) with the live read progress routed
    into the bar + detail only.

    Returns ``outcomes`` mapping track number → ``"matched@<N>X"`` or ``"unrecovered"``.
    """
    from cdda2img.accuraterip import match_track_pcm

    outcomes: dict[int, str] = {}
    # fastest→slowest, repeated n_passes times: an early high-speed match exits sooner.
    speeds = [s for _ in range(n_passes) for s in reversed(ladder)]
    total_attempts = len(speeds)
    file_size = (disc_last_lsn + 1) * _R6_BYTES_PER_FRAME

    status_line = [""]
    prog_cb = _recovery_status_cb(ui, status_line)

    with pcm_file.open("r+b") as pcm_fh:
        for result in failed_tracks:
            t = result.track  # 1-indexed
            idx = t - 1

            if ui is None:
                print(f"  Recovering track {t}…")

            matched = False
            for attempt, speed in enumerate(speeds, 1):
                status_line[0] = f"Recover track {t} ({attempt}/{total_attempts})"
                if ui is not None:
                    ui.set_status(status_line[0], 0.0)
                try:
                    corrected = _read_track_window(
                        device,
                        track_lsns,
                        disc_last_lsn,
                        idx,
                        read_offset,
                        speed,
                        prog_cb,
                    )
                except (RuntimeError, OSError) as exc:
                    log.warning("track %d re-read at %dX failed: %s", t, speed, exc)
                    continue

                _v1, _v2, conf_v1, conf_v2 = match_track_pcm(
                    corrected, t, n_tracks, responses
                )
                if conf_v1 or conf_v2:
                    # Splice the VERIFIED corrected bytes at their raw-file position,
                    # clamped to the file (samples beyond a disc edge were zero-pad
                    # inside AR's exclusion zone — they have no file position).
                    dst_lo = track_lsns[idx] * _R6_BYTES_PER_FRAME + read_offset * 4
                    src = corrected
                    if dst_lo < 0:
                        src = src[-dst_lo:]
                        dst_lo = 0
                    if dst_lo + len(src) > file_size:
                        src = src[: file_size - dst_lo]
                    pcm_fh.seek(dst_lo)
                    pcm_fh.write(src)
                    outcomes[t] = f"matched@{speed}X"
                    matched = True
                    break

            if not matched:
                outcomes[t] = "unrecovered"  # keep the original audio
    return outcomes


def rip_image(  # noqa: C901
    device: str | None = None,
    loudness: str = "rg",
    output: Path | None = None,
    preview: bool = True,
    tui: bool = True,
    low_dr_threshold: float = 5.0,
    duplicate_policy: str | None = None,
    auto: bool = False,
    extract: bool = False,
    keep_rbi: bool = True,
    strategy: ResolvedStrategy | None = None,
) -> None:
    import sys

    from cdda2img.accuraterip import (
        format_ar_report,
        pack_arip_block,
        print_ar_report,
        verify_rip,
    )
    from cdda2img.cddb import compute_cddb_disc_id
    from cdda2img.config import load_config

    cfg = load_config()
    device = device or cfg.default_device
    # Drive offset resolution may prompt the user (input()) — must happen before TUI.
    read_offset, _write_offset, drive_name = _resolve_drive_offsets(device, cfg)

    # A drive left throttled by a previous run (recovery ladder, an interrupted read)
    # keeps that speed; un-throttle before the lead-in scan so it — and the album-art
    # fetch — run at full speed.
    from cdda2img import drive_speed

    drive_speed.restore_drive_speed(device)

    # A whole-disc rip needs the raw PCM (~up to 850 MB) plus apply_offset's transient
    # copy plus the C2 bitmap — require ~1.5 GB so a near-full tmpfs is rejected up front
    # rather than ENOSPC-ing mid-rip at apply_offset.
    temp_base = resolve_temp_dir(min_required_bytes=1_500_000_000)
    temp = TempFiles(temp_base)

    # Pre-TUI one-shot banner: fast disc scan + background MB/art fetch, then render
    # the cover on the main thread before the TUI starts. The TUI does not use an
    # alternate screen buffer, so art printed here stays visible above the TUI rows.
    _preview_disc: RBIDisc | None = None
    _preview_result: dict = {}
    if preview and sys.stdin.isatty():
        _preview_disc = _fast_scan_disc(device)

    if _preview_disc is not None and preview:

        def _preview_worker() -> None:
            from collections import Counter

            from cdda2img.album_art import fetch_cover
            from cdda2img.mb_lookup import lookup_disc_id

            try:
                disc_p = _preview_disc
                matches = lookup_disc_id(disc_p)  # type: ignore[arg-type]
                if disc_p.album:  # type: ignore[union-attr]
                    label = (
                        f"{disc_p.album} - {disc_p.artist}"  # type: ignore[union-attr]
                        if disc_p.artist  # type: ignore[union-attr]
                        else disc_p.album  # type: ignore[union-attr]
                    )
                elif matches:
                    pairs = [(m.album, m.artist) for m in matches if m.album]
                    if pairs:
                        album_g, artist_g = Counter(pairs).most_common(1)[0][0]
                        label = f"{album_g} - {artist_g}" if artist_g else album_g
                    else:
                        label = "(unknown)"
                else:
                    label = "(unknown)"
                # Prefer release-group for art; fall back to release.
                best_rg = next(
                    (m.mb_release_group_id for m in matches if m.mb_release_group_id),
                    None,
                )
                best_r = next(
                    (m.mb_release_id for m in matches if m.mb_release_id), None
                )
                disc_for_art = replace(
                    disc_p,  # type: ignore[arg-type]
                    mb_release_group_id=best_rg,
                    mb_release_id=best_r,
                )
                _preview_result["label"] = label
                _preview_result["art"] = fetch_cover(disc_for_art)
            except Exception as exc:
                log.debug("pre-TUI preview worker: %s", exc)

        _t = threading.Thread(target=_preview_worker, daemon=True)
        _t.start()
        # Wait at least one full HTTP socket timeout (plus a margin for the MB
        # disc-ID lookup that precedes the fetch), so a slow-but-successful cover
        # fetch is displayed rather than abandoned. A shorter cap embedded the art
        # but silently skipped the banner render. The worker is a daemon thread, so
        # an overrun never blocks the rip past this bound.
        from cdda2img.album_art import HTTP_TIMEOUT, render_cover

        _t.join(timeout=HTTP_TIMEOUT + 5.0)
        # Main thread renders — race-free (thread is done or timed out; no concurrent stdout).

        _banner_art = _preview_result.get("art")
        _banner_label = _preview_result.get("label", "(unknown)")
        if _banner_art is not None:
            render_cover(_banner_art, left_pad=3)
        print(_fmt_kv("Disc", _banner_label))

    ui: TerminalUI | None = None
    if tui and sys.stdin.isatty():
        from cdda2img.terminal_ui import TerminalUI as _TUI

        ui = _TUI().start()

    track_preview: TrackPreview | None = None
    rbi_path: Path | None = None
    try:
        # Grab track 1 first (drive is single-use), then play it on a loop in
        # the background while the rest of the rip runs.
        track_preview = _start_track_preview(device, temp.base, ui, enabled=preview)

        c2_file = temp.pcm_file.with_suffix(".c2")
        info, rip_type, c2_path = _rip_disc_stage(
            device, temp.pcm_file, c2_file, read_offset, cfg, ui=ui
        )

        track_count = len(info.disc.tracks)
        total_s = info.disc.total_seconds
        _ui_status(
            ui,
            f"{track_count} track(s), {int(total_s) // 60}:{int(total_s) % 60:02d} total",
        )

        # Offset domain (unified model): AccuDisc returns RAW PCM — always, now that it
        # is the only read engine. We work raw through AR + ctanalyse + the recovery
        # ladder and apply the drive offset exactly once, at storage. `raw_domain` is
        # therefore a *state* flag, not an engine test: it starts True and flips when
        # apply_offset runs below.
        from cdda2img.offset_correct import apply_offset

        raw_domain = True
        ar_offset = read_offset

        # R8: CDDB query now happens inside _finalize_import in parallel
        # with the MB disc-ID lookup. The standalone call is gone; we just
        # capture the disc as-is and pass the LSN data through.
        disc = info.disc
        cddb_id = int(compute_cddb_disc_id(info.track_lsns, info.disc_last_lsn), 16)
        # Track which LSNs fed the final verify_rip call — may change if paranoia fallback fires.
        final_track_lsns = info.track_lsns
        final_disc_last_lsn = info.disc_last_lsn
        # Speed-laddered AR recovery outcome (populated only if a track fails AR).
        recovery_outcomes: dict[int, str] = {}
        recovery_ladder: list[int] = []
        # CTDB attempt outcome, kept whether it succeeded or declined — a declined
        # repair is the interesting case and used to leave no trace at all.
        ctdb_result: CtdbRepairResult | None = None

        _ui_status(ui, "Verifying AccurateRip…")
        ar_verify = verify_rip(
            temp.pcm_file,
            final_track_lsns,
            final_disc_last_lsn,
            read_offset=ar_offset,
            cddb_id=cddb_id,
        )
        if ui is not None:
            ui.pause()
        print_ar_report(ar_verify.tracks, read_offset=read_offset)
        if ui is not None:
            ui.resume()

        # CTDB parity repair FIRST (above the re-read ladder): error-only ctanalyse on the
        # raw PCM (network parity, zero extra reads), with C2 erasures if the C2 path
        # captured them. Skipped on a cd-paranoia read fallback (already corrected — no
        # raw domain for ctanalyse's offset detection). On success the PCM is repaired in
        # place and the ladder is skipped; on failure we fall through, still raw — the
        # c2read ladder works in the raw domain too.
        if raw_domain and _ar_has_partial_mismatch(ar_verify.tracks):
            from cdda2img.ctdb_repair import repair_whole_disc

            _ui_status(ui, "Trying CTDB parity repair…")
            ctdb_result = _ctdb = repair_whole_disc(
                temp.pcm_file,
                final_track_lsns,
                final_disc_last_lsn,
                cddb_id,
                read_offset,
                c2_path=c2_path,
            )
            if _ctdb.repaired:
                # Suffix the engine we actually used. This read
                # `"accudisc+ctdb" if c2_path is not None else "cdrdao+ctdb"`,
                # which was correct while there were two read engines and became
                # a false provenance record when there was one: `c2_path` is None
                # whenever `c2_recovery=off`, so a perfectly ordinary AccuDisc rip
                # would have been stamped `cdrdao+ctdb` in its container. The
                # branch tested for a C2 capture and labelled it an *engine*.
                rip_type = f"{rip_type}+ctdb"
                for _t in _ctdb.damaged_tracks:
                    recovery_outcomes[_t] = f"ctdb_repaired@{_ctdb.entry_id}"
                _ui_status(ui, "Re-verifying AccurateRip (CTDB repair)…")
                ar_verify = verify_rip(
                    temp.pcm_file,
                    final_track_lsns,
                    final_disc_last_lsn,
                    read_offset=read_offset,
                    cddb_id=cddb_id,
                )
                if ui is not None:
                    ui.pause()
                print_ar_report(ar_verify.tracks, read_offset=read_offset)
                if ui is not None:
                    ui.resume()
        # AR-triggered fallback: partial mismatch → read error on specific tracks.
        # Re-read only the failed tracks via AccuDisc (raw targeted window reads) and
        # splice the verified corrected bytes into the still-raw PCM. Disc metadata
        # (ISRC/MCN/CD-Text from subchannel) is untouched — only audio is re-read.
        if _ar_has_partial_mismatch(ar_verify.tracks):
            failed_tracks = [
                r
                for r in ar_verify.tracks
                if r.max_confidence is not None
                and r.confidence_v1 is None
                and r.confidence_v2 is None
            ]
            n_bad = len(failed_tracks)
            _ui_status(
                ui,
                f"{n_bad} track(s) failed AccurateRip — re-reading with c2read…",
            )

            # Speed-laddered recovery: re-read each failed track across the drive's own
            # speed ladder (fastest→slowest, cfg.recovery_passes sweeps), AR-verifying
            # each attempt and splicing the first match; a track that never matches
            # keeps its original audio. The sweep across passes x speeds is the recovery
            # mechanism (validated 6/6 on the damaged reference disc).
            from cdda2img import drive_speed, recovery_profile
            from cdda2img.accuraterip import fetch_ar_responses

            ar_responses, _ar_transport, _ar_b3 = fetch_ar_responses(
                final_track_lsns, final_disc_last_lsn, cddb_id
            )
            # The ladder comes from the resolved profile bound to THIS drive and
            # THIS disc (§9.3) — a self-throttling governor caps a degraded disc
            # regardless of drive capability, so it must be probed per rip.
            # `cfg.recovery_passes = 0` remains the global kill switch; otherwise
            # the profile owns the sweep count.
            bound = (
                recovery_profile.bind_ladder(strategy, device)
                if strategy is not None and strategy.profile is not None
                else None
            )
            recovery_ladder = (
                list(bound.ladder)
                if bound is not None and ar_responses and cfg.recovery_passes > 0
                else []
            )
            recovery_passes = (
                bound.profile.passes
                if bound is not None and bound.profile is not None
                else cfg.recovery_passes
            )
            if recovery_ladder:
                outcomes = _recover_failed_tracks(
                    device,
                    failed_tracks,
                    final_track_lsns,
                    final_disc_last_lsn,
                    temp.pcm_file,
                    ar_responses,
                    len(final_track_lsns),
                    recovery_ladder,
                    recovery_passes,
                    read_offset,
                    ui,
                )
                recovery_outcomes.update(outcomes)
                drive_speed.restore_drive_speed(device)  # one restore after the loop

            rip_type = f"{rip_type}+c2rec"
            _ui_status(ui, "Verifying AccurateRip (re-read)…")
            ar_verify = verify_rip(
                temp.pcm_file,
                final_track_lsns,
                final_disc_last_lsn,
                read_offset=read_offset,  # PCM is still raw; corrected once at storage
                cddb_id=cddb_id,
            )
            if ui is not None:
                ui.pause()
            print_ar_report(ar_verify.tracks, read_offset=read_offset)
            if ui is not None:
                ui.resume()

        # Storage domain: apply the drive offset exactly once, here, if we're still raw
        # (clean disc, CTDB-repaired, or c2read-ladder-recovered). Only a cd-paranoia
        # *read* fallback arrives already corrected.
        if raw_domain:
            _ui_status(ui, "Applying drive offset correction…")
            apply_offset(temp.pcm_file, read_offset)
            raw_domain = False
        if c2_path is not None:
            from cdda2img.accudisc_reader import park_spindle

            park_spindle(device)

        arip_block = pack_arip_block(
            ar_verify.tracks, final_track_lsns, final_disc_last_lsn, cddb_id
        )

        from cdda2img.rip_log import RipLogBuilder

        rlog_builder = RipLogBuilder(
            rip_type=rip_type,
            drive_name=drive_name,
            read_offset=read_offset,
        )
        rlog_builder.ar_results = ar_verify.tracks
        rlog_builder.cddb_id = cddb_id

        output_stem = sanitize_title(disc.album) or device.lstrip("/").replace("/", "_")
        provenance: dict[str, str] = {
            "mode": "rip",
            "source": device,
            "ripper": rip_type,
        }
        # Read-stage provenance from the metadata assembly (subq_toc: toc_source,
        # Q-frame stats, per-track ISRC vote counts).
        if info.prov:
            provenance.update(info.prov)
        if drive_name is not None:
            provenance["drive_name"] = drive_name
            provenance["drive_read_offset"] = f"{read_offset:+d}"
        # R2: surface the AccurateRip transport choice + dBAR body hash so
        # later re-fetches can detect AR-side changes / mirror tampering.
        if ar_verify.transport is not None:
            provenance["arip_transport"] = ar_verify.transport
        if ar_verify.dbar_b3sum is not None:
            provenance["arip_dbar_b3sum"] = ar_verify.dbar_b3sum
        # CTDB provenance. The declined case matters most: without it a failed parity
        # repair is invisible in the container and has to be reverse-engineered from
        # the finished RBI (which is exactly what happened on 2026-07-25).
        if ctdb_result is not None:
            if ctdb_result.entry_id is not None:
                provenance["ctdb_entry"] = ctdb_result.entry_id
            if ctdb_result.ctdb_offset is not None:
                provenance["ctdb_offset"] = f"{ctdb_result.ctdb_offset:+d}"
            if ctdb_result.used_c2:
                provenance["ctdb_erasures"] = "c2"
            # A repair that rested on AccuDisc's weaker success claim: some column
            # carried exactly npar erasures, so its errata were *determined* and
            # the re-verification that vouches for every other column was an
            # identity there. Both gates still passed, which is what makes it
            # committable — but "which claim did this audio rest on" is not
            # recoverable from the finished RBI, so it is recorded here or nowhere.
            if ctdb_result.unverified_columns:
                provenance["ctdb_unverified_columns"] = str(
                    ctdb_result.unverified_columns
                )
            if not ctdb_result.repaired:
                provenance["ctdb_declined"] = ctdb_result.reason
        # Speed-laddered recovery provenance: what was tried and the per-track outcome.
        if recovery_ladder:
            provenance["recovery_passes"] = str(recovery_passes)
            provenance["recovery_ladder"] = ",".join(f"{x}X" for x in recovery_ladder)
            if strategy is not None:
                provenance["recovery_source"] = strategy.source
                if strategy.profile is not None:
                    provenance["recovery_profile"] = strategy.profile.name
                if strategy.ad_flags:
                    provenance["recovery_ad_flags"] = ",".join(
                        f"{k}={v}" for k, v in sorted(strategy.ad_flags.items())
                    )
        for _t, _outcome in sorted(recovery_outcomes.items()):
            provenance[f"recovery_track_{_t}"] = _outcome
        # A whole-disc AR miss runs no recovery — the ladder is gated on a PARTIAL
        # mismatch, correctly, since re-reading every track is not a read-error
        # remedy. Without this the rip just fails to verify and says nothing about
        # why. Diagnostic only: the audio is untouched, because an offset that
        # verifies is not automatically the right one to store (a widely-pressed
        # disc verifies at several at once).
        if _ar_has_total_mismatch(ar_verify.tracks):
            provenance.update(
                _diagnose_total_ar_miss(
                    temp.pcm_file,
                    final_track_lsns,
                    final_disc_last_lsn,
                    cddb_id,
                    read_offset,
                )
            )
        # Frame-450 partial verification: a track that still fails full AR but
        # matches the crc450 sub-CRC is graded "damaged, right pressing" —
        # recorded so an unrecovered track carries the strongest statement the
        # evidence supports.
        for _r in ar_verify.tracks:
            if (
                _r.confidence_v1 is None
                and _r.confidence_v2 is None
                and _r.confidence_450 is not None
            ):
                provenance[f"ar450_track_{_r.track}"] = f"matched@{_r.confidence_450}"
        ar_summary = format_ar_report(ar_verify.tracks, read_offset=read_offset)
        rbi_path = _finalize_import(
            disc,
            temp.pcm_file,
            provenance,
            output_stem,
            loudness,
            output,
            arip_block=arip_block,
            rlog_builder=rlog_builder,
            ui=ui,
            low_dr_threshold=low_dr_threshold,
            cddb_track_lsns=info.track_lsns,
            cddb_disc_last_lsn=info.disc_last_lsn,
            cddb_server=cfg.cddb_server,
            ar_summary=ar_summary,
            tui=tui,
            duplicate_policy=duplicate_policy,
            auto=auto,
            preferred_country=cfg.preferred_country,
        )
    finally:
        _stop_preview(track_preview)
        if ui is not None:
            ui.stop()
        temp.cleanup()

    if extract and rbi_path is not None:
        extract_image(
            rbi_path,
            raw=False,
            tracks=True,
            rg=False,
            ar=False,
            log=False,
            all_blocks=False,
            embedart=cfg.embedart,
            normalize=False,
            output=None,
        )
        if not keep_rbi:
            rbi_path.unlink()


def _confirm_overwrite(output_paths: list[Path]) -> bool:
    """Return True if the user approves overwriting existing files (or none exist)."""
    existing = [p for p in output_paths if p.exists()]
    if not existing:
        return True
    print(f"\n{len(existing)} output file(s) already exist and would be overwritten:")
    for p in existing[:5]:
        print(f"  {p}")
    if len(existing) > 5:
        print(f"  ... and {len(existing) - 5} more")
    return input("Overwrite? [y/N] ").strip().lower() in ("y", "yes")


def extract_image(
    rbi_file: Path,
    raw: bool,
    tracks: bool,
    rg: bool,
    ar: bool,
    log: bool,
    all_blocks: bool,
    albumart: bool = False,
    embedart: bool = False,
    normalize: bool = False,
    output: Path | None = None,
) -> None:
    from cdda2img.container import ExtractOptions

    if output is not None:
        base_dir = output.expanduser().resolve()
        if base_dir.is_file():
            msg = f"--output: {base_dir} is a file, not a directory"
            raise ValueError(msg)
    else:
        base_dir = Path.cwd() / "extracted"

    use_all = all_blocks or not (raw or tracks or rg or ar or log or albumart)
    if use_all:
        opts = ExtractOptions(
            raw=True,
            tracks=True,
            rg=True,
            ar=True,
            log=True,
            albumart=True,
            embedart=embedart,
            normalize=normalize,
            warn_missing=False,
        )
    else:
        opts = ExtractOptions(
            raw=raw,
            tracks=tracks,
            rg=rg,
            ar=ar,
            log=log,
            albumart=albumart,
            embedart=embedart,
            normalize=normalize,
            warn_missing=True,
        )

    extract_data(rbi_file, opts, base_dir=base_dir)


def burn_image(
    rbi_file: Path,
    device: str | None = None,
    write_offset_override: int | None = None,
    speed: int = 4,
    simulate: bool = False,
    yes: bool = False,
) -> None:
    import sys

    from cdda2img.config import load_config
    from cdda2img.disc_writer import burn_disc
    from cdda2img.drive_info import probe_drive_name

    cfg = load_config()
    device = device or cfg.default_device
    if write_offset_override is not None:
        write_offset = write_offset_override
        log.debug("write_offset=%d (CLI override)", write_offset)
    else:
        write_offset = 0
        drive_name = probe_drive_name(device)
        if drive_name is not None:
            for d in cfg.drives:
                if d.name == drive_name and d.write_offset is not None:
                    write_offset = d.write_offset
                    log.debug(
                        "write_offset=%d (config, drive %s)", write_offset, drive_name
                    )
                    break
            else:
                from cdda2img import db as _db
                from cdda2img.drive_info import find_drive_write_offset

                _conn = _db.open_drive_offsets_db(cfg)
                try:
                    eac_wo = find_drive_write_offset(_conn, drive_name)
                finally:
                    _conn.close()
                if eac_wo is not None:
                    log.info(
                        "EAC OffsetBase suggests write_offset=%d for %s;"
                        " add `write_offset = %d` to the [[drives]] entry in"
                        " ~/.config/cdda2img/cdda2img.toml to apply it",
                        eac_wo,
                        drive_name,
                        eac_wo,
                    )

    ui: TerminalUI | None = None
    if sys.stdin.isatty():
        from cdda2img.terminal_ui import TerminalUI as _TUI

        ui = _TUI().start()

    try:
        burn_disc(
            rbi_file,
            device=device,
            write_offset=write_offset,
            speed=speed,
            simulate=simulate,
            yes=yes,
            ui=ui,
        )
    finally:
        if ui is not None:
            ui.stop()


def mount_image(
    rbi_file: Path,
    slot: int | None = None,
    mnt_dir: Path | None = None,
) -> None:
    from cdda2img.cdemu import mount_rbi

    slot_used, toc_path, device = mount_rbi(rbi_file, slot=slot, mnt_dir=mnt_dir)
    dev_str = device or f"/dev/sr?  (slot {slot_used} — run: cdemu device-mapping)"
    print(f"Mounted in cdemu slot {slot_used}: {toc_path}")
    print(f"Device:  {dev_str}")
    print(f"Play:    mpv av://libcdio:{dev_str}")
    print(f"Re-rip:  cdrdao read-cd --device {dev_str} disc.toc")
    print(f"Unload:  cdemu unload {slot_used}")


def _dispatch(args: argparse.Namespace) -> None:
    if args.cmd == "create":
        from cdda2img.config import load_config

        cfg = load_config()
        create_image(
            args.input_dir,
            silence_mode=args.silence,
            loudness=args.loudness,
            strategy=args.strategy,
            output=args.output,
            silence_threshold=(
                args.silence_threshold
                if args.silence_threshold is not None
                else cfg.silence_threshold
            ),
            capacity=args.capacity if args.capacity is not None else cfg.capacity,
            low_dr_threshold=cfg.low_dr_threshold,
            tui=args.tui if args.tui is not None else cfg.tui,
            duplicate_policy=args.duplicate,
            auto=args.auto if args.auto is not None else cfg.auto,
        )
    elif args.cmd == "rip":
        from cdda2img.config import load_config
        from cdda2img.recovery_profile import (
            AD_FLAGS,
            list_profiles,
            load_profile,
            resolve_recovery,
        )

        if getattr(args, "list_profiles", False):
            for pname, path in sorted(list_profiles().items()):
                prof = load_profile(pname)
                mark = " (experimental)" if prof.experimental else ""
                print(
                    f"{pname:16s} {prof.granularity:10s} ladder={prof.ladder:6s}{mark}"
                )
                print(f"{'':16s} {path}")
            return

        cfg = load_config()
        # Only flags the user actually supplied may appear: argparse hands over
        # every --ad-* key with None when unset, and treating those as present
        # would fire §9.4 rung 1 on every single invocation.
        ad = {f: getattr(args, f"ad_{f}", None) for f in AD_FLAGS}
        strategy = resolve_recovery(
            ad_flags={k: v for k, v in ad.items() if v is not None},
            profile_name=args.profile,
            config_default=cfg.default_profile,
        )
        rip_image(
            args.device,
            loudness=args.loudness,
            output=args.output,
            preview=args.preview if args.preview is not None else cfg.preview,
            tui=args.tui if args.tui is not None else cfg.tui,
            low_dr_threshold=cfg.low_dr_threshold,
            duplicate_policy=args.duplicate,
            auto=args.auto if args.auto is not None else cfg.auto,
            extract=args.extract,
            keep_rbi=not args.no_keep_rbi,
            strategy=strategy,
        )
    elif args.cmd == "import":
        if args.info:
            info_image(args.source)
        else:
            from cdda2img.config import load_config

            cfg = load_config()
            import_image(
                args.source,
                loudness=args.loudness,
                output=args.output,
                low_dr_threshold=cfg.low_dr_threshold,
                tui=args.tui if args.tui is not None else cfg.tui,
                duplicate_policy=args.duplicate,
                auto=args.auto if args.auto is not None else cfg.auto,
            )
    elif args.cmd == "extract":
        from cdda2img.config import load_config

        cfg = load_config()
        extract_image(
            args.rbi_file,
            raw=args.raw,
            tracks=args.tracks,
            rg=args.rg,
            ar=args.ar,
            log=args.log,
            all_blocks=args.all_blocks,
            albumart=args.albumart,
            embedart=args.embedart or cfg.embedart,
            normalize=args.normalize,
            output=args.output,
        )
    else:
        _dispatch_utility(args)


def _dispatch_utility(args: argparse.Namespace) -> None:
    if args.cmd == "list":
        from cdda2img.container import list_container

        show_info = args.info or not (args.rg or args.ar or args.log or args.prov)
        list_container(
            args.rbi_file,
            info=show_info,
            rg=args.rg,
            ar=args.ar,
            log=args.log,
            prov=args.prov,
        )
    elif args.cmd == "test":
        from cdda2img.container import verify_container

        if not verify_container(args.rbi_file):
            raise SystemExit(1)
    elif args.cmd == "catalogue":
        from cdda2img.catalogue_menu import run_catalogue_menu

        run_catalogue_menu(Path(args.db) if args.db else None)
    elif args.cmd == "burn":
        burn_image(
            args.rbi_file,
            device=args.device,
            write_offset_override=args.write_offset,
            speed=args.speed,
            simulate=args.simulate,
            yes=args.yes,
        )
    elif args.cmd == "mount":
        mount_image(args.rbi_file, slot=args.slot, mnt_dir=args.mnt_dir)
    elif args.cmd == "setup":
        from cdda2img.setup import run_setup_wizard

        section_flags = [
            "create_config",
            "update_config",
            "validate_config",
            "edit_config",
            "create_profile",
            "read_offset",
            "write_offset",
            "create_catalogue",
            "validate_catalogue",
            "verify_catalogue",
        ]
        section = next(
            (f.replace("_", "-") for f in section_flags if getattr(args, f, False)),
            None,
        )
        run_setup_wizard(
            section=section,
            device=getattr(args, "device", None),
            speed=getattr(args, "speed", 4),
            verify_test=getattr(args, "test", False),
        )


def _run_startup_checks(args: argparse.Namespace) -> None:
    """Warn (and optionally offer the wizard) when required files are absent."""
    import contextlib
    import sys

    from cdda2img.config import config_path, load_config

    path = config_path()
    cfg = None
    with contextlib.suppress(Exception):
        cfg = load_config()

    if cfg is None:
        print(f"  Warning: config not found or unreadable at {path}")
        print("  Run `cdda2img setup --create-config` to create it.")

    if cfg is not None and cfg.enable_catalogue:
        from cdda2img.catalogue import catalogue_db_path, open_catalogue_db

        db_path = cfg.catalogue_path or catalogue_db_path()
        if not db_path.is_file() and sys.stdin.isatty():
            print(f"  Warning: catalogue not found at {db_path}")
            print("  Run `cdda2img setup --create-catalogue` to create it.")
        elif db_path.is_file():
            try:
                conn = open_catalogue_db(db_path)
                conn.close()
            except Exception as exc:
                print(f"  Warning: catalogue error ({exc})")
                print("  Run `cdda2img setup --validate-catalogue` to repair it.")


def main() -> None:
    from cdda2img.recovery_profile import ProfileError

    args = parse_args()
    if args.verbose:
        # CLI entry only — keeps the library boundary clean per the
        # CLAUDE.md "no global logging mutation in library" rule.
        import logging

        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    # Validate the config once, before anything touches a drive or the network
    # (accudisc-migration-plan.md §9.6). `setup` is exempt: it is the tool for
    # repairing a broken config, so refusing to start on one would be a trap with
    # no exit. Every other subcommand stops here rather than running with a
    # configuration we have already found to be wrong.
    if getattr(args, "cmd", None) != "setup":
        from cdda2img.config import ConfigError, load_config

        try:
            load_config()
        except ConfigError as e:
            print(f"Error: {e.describe()}")
            print("\nRun `cdda2img setup` to fix it.")
            raise SystemExit(1) from None
        _run_startup_checks(args)
    try:
        _dispatch(args)
    except FileNotFoundError as e:
        print(f"Error: {e.filename}: no such file or directory")
        raise SystemExit(1) from None
    except ValueError as e:
        print(f"Error: {e}")
        raise SystemExit(1) from None
    except RuntimeError as e:
        print(f"Error: {e}")
        raise SystemExit(1) from None
    except ProfileError as e:
        # A named profile that cannot be loaded is fatal on purpose (§9.4): silently
        # substituting a default would mislabel every measurement taken afterwards.
        print(f"Error: {e}")
        raise SystemExit(1) from None
    except EOFError:
        print("\nAborted (no input).")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
