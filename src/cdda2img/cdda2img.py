from __future__ import annotations

import argparse
import bisect
import copy
import functools
import importlib.metadata
import logging
import re
import sys
import textwrap
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, NoReturn, TypeVar

if TYPE_CHECKING:
    import random
    from typing import BinaryIO

    from cdda2img.accudisc_reader import CaptureCounters, ReadLanes, SpanDetail
    from cdda2img.accuraterip import ARVerifyResult
    from cdda2img.config import Config
    from cdda2img.ctdb_repair import CtdbRepairResult
    from cdda2img.field_resolver import FieldProposal
    from cdda2img.lookup_result import DiscMeta
    from cdda2img.match_distance import MatchDistance
    from cdda2img.mb_lookup import MBPrepopResult
    from cdda2img.pxi_reader import OffsetCandidate as PXIOffsetCandidate
    from cdda2img.rbi_format import RipInfo
    from cdda2img.recovery_profile import Profile, ResolvedStrategy
    from cdda2img.rip_log import RipLogBuilder
    from cdda2img.strategy_reads import ReadReq
    from cdda2img.terminal_ui import TerminalUI
    from cdda2img.track_preview import TrackPreview

from cdda2img import drive_work, file_match
from cdda2img.concat import concat_wav
from cdda2img.container import (
    TempFiles,
    build_container,
    extract_data,
    pad_pcm_to_declared_frames,
    resolve_temp_dir,
    wav_to_raw_pcm,
)
from cdda2img.disc_writer import DEFAULT_BURN_SPEED
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
from cdda2img.rip_abort import (
    EXIT_FIX_INVOCATION,
    EXIT_INVESTIGATE,
    EXIT_RETRY_LATER,
    RipAborted,
)
from cdda2img.silence import trim_silence_cd_da
from cdda2img.toc import (
    build_toc_entries,
    generate_toc,
    sanitize_title,
    track_frame_durations,
)
from cdda2img.transcode import transcode_audio

log = logging.getLogger(__name__)

DEFAULT_STRATEGY = "aatc"
_MIN_AR_CONFIDENCE = (
    3  # minimum AccurateRip submissions for auto-applying a drive offset
)
SILENCE_PAD_DUR = "2"  # seconds of post-track silence (Red Book inter-track gap)


def _recovery_passes_arg(value: str) -> int:
    """``--recovery-passes``: a whole number 0..100 (config's range; 0 = no
    recovery)."""
    try:
        n = int(value)
    except ValueError:
        n = -1
    if not 0 <= n <= 100:
        msg = f"expected a whole number from 0 to 100, got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return n


def _effective_recovery_passes(cli: int | None, configured: int) -> int:
    """``--recovery-passes`` when given, else config ``recovery_passes``.

    Tested against ``None``, never for truth: 0 means "no recovery", so
    ``cli or configured`` would turn ``--recovery-passes 0`` into the config's
    value (kgr, 2026-11-25: one setting, merged from ``recovery_attempts``)."""
    return cli if cli is not None else configured


def _recovery_speed_arg(value: str) -> int | str:
    """``--recovery-speed``: an X rate 1..72, or ``auto``."""
    if value == "auto":
        return value
    try:
        speed = int(value)
    except ValueError:
        speed = 0
    if not 1 <= speed <= 72:
        msg = f"expected an X rate between 1 and 72, or 'auto': {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return speed


class _Parser(argparse.ArgumentParser):
    """argparse with usage errors on exit status 1, not 2.

    2 means "retry later" here (``rip_abort``), and a usage error's further action
    is "fix the command", which is 1's. Subparsers inherit the class
    (``add_subparsers`` defaults ``parser_class`` to the parent's type)."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def parse_args() -> argparse.Namespace:
    parser = _Parser(
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

            burn options:  (validated on a virtual CDEmu writer only — see cdda2img(1))
              --device DEVICE       CD drive device (default: from config default_device, fallback /dev/sr0)
              --speed N             Burn speed in CD-DA drive units (default: 8)
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
    c.add_argument(
        "--allow-offline",
        action="store_true",
        default=False,
        help="Proceed even if MusicBrainz is unreachable. Without it, a non-interactive run refuses to start (an interactive one asks); either way the container records what was unavailable (PROV network_preflight)",
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
    # AccuDisc passthrough. Supplying any of these EXCEPT --ad-speed bypasses profiles
    # entirely (§9.4 rung 1) — an escape hatch for driving the engine directly, and
    # merging it with a profile would produce a configuration nobody asked for.
    # --ad-speed is exempt because it sets the INITIAL pass rate, which no profile has
    # an opinion about, so there is nothing for it to conflict with.
    #
    # --ad-retries, --ad-c2-retries, --ad-verify, --ad-overlap and --ad-recovery
    # were deleted on 2026-11-25 (kgr). No read ever received them: they took rung 1
    # and were written to PROV recovery_ad_flags as if applied. Under the re-read
    # ownership ruling of that day, the engine's local re-read settings are
    # AccuDisc's to choose, not a cdda2img flag's.
    _ad = r_cmd.add_argument_group(
        "AccuDisc passthrough",
        "Set the drive's read speeds directly. --ad-ladder replaces the recovery "
        "profiles with the plain speed ladder; --ad-speed composes with them.",
    )
    _ad.add_argument(
        "--ad-speed",
        type=int,
        metavar="X",
        help="Read the disc at X (1-72). Verified against the drive after the "
        "request; a drive that settles elsewhere warns and the real rate is used. "
        "Composes with --profile",
    )
    _ad.add_argument(
        "--ad-ladder",
        metavar="LIST",
        help='Recovery speed rungs, in attempt order, e.g. "32,16,8". Taken verbatim '
        "— not filtered against the drive's admitted set",
    )
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
        "--recovery-speed",
        type=_recovery_speed_arg,
        default=None,
        metavar="X",
        help="The one speed the recovery chain's fixed-speed stages (track-constant, span-fixed) read at, snapped to the nearest speed the drive admits on the disc; 'auto' holds, per track, the speed whose earlier re-read flagged the fewest C2 errors. Every other stage changes speed on each read (default: from config, 32)",
    )
    r_cmd.add_argument(
        "--recovery-passes",
        type=_recovery_passes_arg,
        default=None,
        metavar="N",
        help="How many times each recovery profile runs its full sweep: every flagged sector (or the track, or the disc) at every speed the profile reads at. 0 turns recovery off. The first AccurateRip match ends a track's re-reads early (default: from config, 1)",
    )
    r_cmd.add_argument(
        "--profile-only",
        action="store_true",
        help="Testing (temporary, 2026-11-22): skip the two zero-read repairs, CTDB parity repair and the displacement check, so every track that fails AccurateRip goes to the recovery profile (use with --profile). Recorded in PROV as zero_read_repairs=skipped",
    )
    media = r_cmd.add_mutually_exclusive_group()
    media.add_argument(
        "--cdr",
        dest="media_override",
        action="store_const",
        const="cdr",
        default=None,
        help="The disc is a recordable copy of a pressed CD (or its type could not be determined): rip it with the normal pipeline. AccurateRip must still verify every track. Without this, a recordable disc is refused",
    )
    media.add_argument(
        "--digital-media",
        dest="media_override",
        action="store_const",
        const="digital_media",
        help="The disc was made from a digital-only release, which no disc-ID database can hold: skip AccurateRip, CTDB, CDDB and the MusicBrainz disc-ID lookup, prove the audio by byte-identical reads at different speeds, and identify it by AcoustID on every track",
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
        help="Import a foreign disc image as an RBI container (master mode): cdrdao .toc, DDP 2.0, Nero .nrg, CloneCD .ccd, or PlexTools .pxi",
    )
    i_cmd.add_argument(
        "source",
        type=Path,
        help="cdrdao .toc file, DDP 2.0 image directory, Nero .nrg file, CloneCD .ccd file, or PlexTools .pxi file",
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
    i_cmd.add_argument(
        "--allow-offline",
        action="store_true",
        default=False,
        help="Proceed even if MusicBrainz is unreachable. Without it, a non-interactive run refuses to start (an interactive one asks); either way the container records what was unavailable (PROV network_preflight)",
    )

    d_cmd = sub.add_parser("catalogue", help="Browse disc catalogue")
    d_cmd.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="catalogue database path (default: from config or XDG data dir)",
    )

    w_cmd = sub.add_parser(
        "burn",
        help="Burn an RBI image to a blank CD-DA disc via AccuDisc "
        "(validated on a virtual writer only — see cdda2img(1))",
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
        default=DEFAULT_BURN_SPEED,
        metavar="N",
        help=f"Burn speed in CD-DA drive units (default: {DEFAULT_BURN_SPEED})",
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
    s_cmd.add_argument(
        "--speed",
        type=int,
        default=DEFAULT_BURN_SPEED,
        metavar="N",
        help="Burn speed for --write-offset, in CD-DA drive units "
        f"(default: {DEFAULT_BURN_SPEED})",
    )

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
    if disc.discogs_master_id is not None:
        provenance["discogs_master_id"] = str(disc.discogs_master_id)
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
    elif disc.barcode and provenance.get("barcode_source") not in (
        "adopted",
        "unanimous",
    ):
        # §13.7 (kgr, 2026-11-26): a barcode adopted from the partner release, or
        # kept only because every candidate pressing states it, is not a confirmed
        # pressing's own, and the TOC's MCN is burned to every copy.
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
    network_preflight: str | None = None,
    preferred_country: Sequence[str] = (),
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
            trans_wavs: list[Path] = []

            for i, track in enumerate(batch, start=1):
                print(f"  Transcoding   {i:2}: {track.stem}")
                trans = temp.temp_track(i, "_trans.wav")
                transcode_audio(track, trans)
                trans_wavs.append(trans)

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

            provenance: dict[str, str] = {
                "mode": "create",
                "source": str(input_dir.resolve()),
                "ripper": "file",
                "lookup_status_mb": "disabled",
                "lookup_status_cddb": "disabled",
                "lookup_status_discogs": "disabled",
            }
            if network_preflight is not None:
                provenance["network_preflight"] = network_preflight

            # Identify before building (trust_model_design.md §14.4): the chosen
            # release decides which files the archive holds, and in what order.
            ident = _identify_files(
                batch,
                trans_wavs,
                provenance,
                interactive=_interactive(auto),
                tui=tui,
                preferred_country=preferred_country,
            )
            batch = [batch[i] for i in ident.keep]
            source_wavs = [source_wavs[i] for i in ident.keep]
            tag_meta = ident.meta
            discogs_fmt = ident.discogs_fmt

            disc = _build_create_disc(
                batch,
                source_wavs,
                temp,
                ident,
                RBIDisc(
                    album=album,
                    artist=artist,
                    disc_number=disc_num,
                    disc_total=disc_total,
                ),
            )
            # After the release is applied: AcoustID's gate reads disc.mb_release_id.
            disc = _r6_acoustid_corroborate_wavs(disc, source_wavs, provenance)

            # Identify the original release before the menu so the user
            # sees "Original: <title> (<year>)" in the initial summary.
            from cdda2img.original_release import populate_original_release

            populate_original_release(disc, verify_meta=tag_meta, provenance=provenance)

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

            # N6: printed hint only — the stored keys are written after the menu,
            # exactly as on the import/rip path. See `_finalize_import`.
            match_dist = build_match_distance(disc, provenance)
            # Menu shown unless --auto; confidence is informational (see
            # _finalize_import for the same policy). A failed §10.4 AcoustID gate
            # additionally suppresses --auto (warn-only) — a no-op in create,
            # which has no MB disc-ID match to gate, but kept for symmetry.
            auto_apply = _gate_adjusted_auto(auto, provenance)
            if auto_apply:
                print(f"   Metadata auto-confirmed — {match_dist.summary()}")
            else:
                print(f"   Metadata: {match_dist.summary()}")
            disc = run_metadata_menu(
                disc,
                source_wavs=source_wavs,
                tui=tui,
                auto_apply=auto_apply,
                discogs_fmt=discogs_fmt,
            )
            # N6, and deliberately symmetric with `_finalize_import` rather than
            # skipped. `create` passes no `provenance=` to the menu, so
            # `_record_pressing_outcome` never runs and `release_selection` is
            # never written here — the recompute is therefore a no-op today
            # unless the menu changed `disc.mb_release_id`, in which case the
            # stored score follows it, which is the point. Keeping the two call
            # sites the same shape is worth more than saving one call: they have
            # drifted before.
            _store_match_distance(provenance, disc)
            album_art = to_album_art(_art_raw) if _art_raw is not None else None

            # The files' names are the raw titles only when no release supplied
            # the titles: otherwise the TOC would archive a file name as the
            # Unicode original of a MusicBrainz title.
            raw_titles = (
                [re.sub(r"^\d{1,2}[-. ]+", "", p.stem) for p in batch]
                if tag_meta is None
                else None
            )

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


def _unsupported_source_msg(source: Path) -> str:
    """The one place the accepted import sources are spelled out for the user.

    Shared by ``info_image`` and ``import_image`` so a newly supported format
    cannot be listed in one message and missing from the other.
    """
    return (
        f"{source.name}: expected a cdrdao .toc file, DDP 2.0 image directory,"
        " Nero .nrg file, CloneCD .ccd file, or PlexTools .pxi file"
    )


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

    elif source.suffix.lower() == ".pxi":
        from cdda2img.pxi_reader import info_pxi

        disc, has_cdtext, total_bytes = info_pxi(source)
        _print_source_info(
            "PlexTools PXI Image", source.name, total_bytes, disc, has_cdtext
        )

    else:
        raise ValueError(_unsupported_source_msg(source))


def _pxi_offset_candidates(
    disc: RBIDisc, pxi_path: Path, origin: int
) -> list[PXIOffsetCandidate] | None:
    """AccurateRip's confirmed offsets for a `.pxi`'s stored audio.

    The seam that lets `pxi_reader` measure the writing drive's read offset
    instead of assuming it, without importing the network or ``numpy`` itself.
    Only **confirmed** matches are returned: an unconfirmed candidate is one the
    frame-450 prefilter nominated and no full-track checksum agreed with, and
    ABBA *Gold* has one of those at an offset where all 19 tracks pass the probe
    and not one passes the checksum — exactly the systematic false positive that
    must never reach a decision about which samples to store.

    ``None`` (no evidence) and ``[]`` (in the database, nothing verifies) are
    deliberately different returns: the first is silence from the network, the
    second is a statement about the audio.  `_resolve_read_offset` records them
    as different provenance and they must not be collapsed.

    The audio is memory-mapped at *origin* rather than read: a disc is 400-800 MB
    and ``detect_offset``'s prefilter touches a few kB per track, so nothing like
    the whole image is ever paged in — and nothing is written, which keeps this
    clear of the scratch-scope rule.
    """
    from cdda2img.pxi_reader import OffsetCandidate

    try:
        import numpy as np

        from cdda2img.accuraterip import detect_offset, fetch_ar_responses
        from cdda2img.cddb import compute_cddb_disc_id

        lsns = [t.start_frame + t.pregap_frames for t in disc.tracks]
        last = disc.tracks[-1]
        disc_last = last.start_frame + last.pregap_frames + last.duration_frames - 1
        cddb_id = int(compute_cddb_disc_id(lsns, disc_last), 16)

        responses, _transport, _b3 = fetch_ar_responses(lsns, disc_last, cddb_id)
        if not responses:
            return None
        frames = np.memmap(pxi_path, dtype="<u4", mode="r", offset=origin)
        frames = frames[: (disc_last + 1) * (2352 // 4)]
        matches = detect_offset(frames, lsns, disc_last, responses)
    except Exception as exc:
        # Never fail an import over a lookup: without evidence the reader falls
        # back to its measured prior, which is what it did before this existed.
        log.debug("PXI offset probe failed: %s", exc)
        return None

    return [
        OffsetCandidate(m.offset, m.tracks_matched, m.total_tracks)
        for m in matches
        if m.confirmed
    ]


def _import_source(
    source: Path, temp: TempFiles, ui: TerminalUI | None
) -> tuple[RBIDisc, str, dict[str, str]]:
    """Decode *source* into ``(disc, output_stem, provenance)``, PCM at ``temp.pcm_file``.

    One branch per supported foreign format.  Split out of ``import_image`` so
    the format dispatch can grow without the surrounding orchestration —
    metadata lookups, ReplayGain, container build — growing with it.

    Every reader is handed *say* rather than being left to ``print``.  A bare
    write moves the cursor without telling the TUI renderer, whose repaint
    rewinds a fixed number of lines; the rewind then lands in the wrong place
    and the previous frame is never erased, so each stray write leaves a dead
    copy of the progress bar on screen.
    """
    _ui_status(ui, f"Importing {'DDP image ' if source.is_dir() else ''}{source.name}…")
    provenance = {"mode": "import", "source": str(source.resolve())}
    suffix = source.suffix.lower()

    def say(text: str) -> None:
        _ui_print(ui, text)

    if source.is_dir():
        from cdda2img.ddp_reader import import_ddp

        disc, _ = import_ddp(source, temp.pcm_file, say)
        provenance["ripper"] = "ddp"
        return disc, sanitize_title(disc.album) or source.name, provenance

    if suffix == ".toc":
        from cdda2img.cdrdao_reader import (
            _find_bin_filename,
            convert_cdrdao_bin_to_wav,
            parsed_to_rbi_disc,
        )
        from cdda2img.toc_parser import parse_toc

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
        provenance["ripper"] = "toc"
        return disc, sanitize_title(disc.album) or source.stem, provenance

    if suffix == ".nrg":
        from cdda2img.nrg_reader import import_nrg

        disc, _ = import_nrg(source, temp.pcm_file, say)
        provenance["ripper"] = "nrg"
        return disc, sanitize_title(disc.album) or source.stem, provenance

    if suffix == ".ccd":
        from cdda2img.ccd_reader import import_ccd

        disc, _ = import_ccd(source, temp.pcm_file, say)
        provenance["ripper"] = "ccd"
        return disc, sanitize_title(disc.album) or source.stem, provenance

    if suffix == ".pxi":
        from cdda2img.pxi_reader import import_pxi

        provenance["ripper"] = "pxi"
        # The offset supplier goes by KEYWORD: every reader's report sink is its
        # last positional argument, and test_import_dispatch pins that shape for
        # all five formats at once. An optional injected dependency must not
        # displace it.
        disc, _ = import_pxi(
            source,
            temp.pcm_file,
            provenance,
            say,
            offset_candidates=_pxi_offset_candidates,
        )
        return disc, sanitize_title(disc.album) or source.stem, provenance

    raise ValueError(_unsupported_source_msg(source))


def import_image(
    source: Path,
    loudness: str = "rg",
    output: Path | None = None,
    low_dr_threshold: float = 5.0,
    tui: bool = True,
    duplicate_policy: str | None = None,
    auto: bool = False,
    network_preflight: str | None = None,
) -> list[str]:
    """Import *source* into an RBI container.

    Returns the required services the container records as ``down`` (see
    :func:`rip_image`).
    """
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
        disc, output_stem, provenance = _import_source(source, temp, ui)
        if network_preflight is not None:
            provenance["network_preflight"] = network_preflight

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
    return _unanswered_services(provenance)


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


def _punctuation_folded(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


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
    if a == b or _punctuation_folded(a) == _punctuation_folded(b):
        # Punctuation is house style, not identity: Discogs titles ABBA's
        # release "Gold (Greatest Hits)" where MusicBrainz and the disc say
        # "Gold: Greatest Hits" (2758131, the Discogs release MusicBrainz links).
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


class FileIdentity(NamedTuple):
    """What ``create``'s file match settled for one batch (trust_model_design.md
    §14): the release to apply (``None``: none), the batch indices to build the
    disc from, in the archive's order, and the Discogs format for ``[d]``."""

    meta: DiscMeta | None
    keep: list[int]
    discogs_fmt: str = "CD"
    group_only: bool = False


def _build_create_disc(
    batch: list[Path],
    source_wavs: list[Path],
    temp: TempFiles,
    ident: FileIdentity,
    disc: RBIDisc,
) -> RBIDisc:
    """Concatenate the kept files (already in the archive's order) into the PCM and
    lay out the disc, then apply the file match's release."""
    print("  Concatenating tracks")
    concat_wav(source_wavs, temp.pcm_pre)
    wav_to_raw_pcm(temp.pcm_pre, temp.pcm_file)
    durations, total_frames = track_frame_durations(source_wavs)
    pad_pcm_to_declared_frames(temp.pcm_file, total_frames)
    disc.tracks = build_toc_entries(batch, durations, disc)
    if ident.meta is None:
        return disc
    from cdda2img.mb_lookup import _overwrite_disc

    # The release's track k is batch[k]: the kept files are in the release's
    # order, so the overwrite by track number lines up.
    disc = _overwrite_disc(ident.meta, disc)
    if ident.group_only:
        disc.mb_release_id = None
    return disc


def _wav_ms(path: Path) -> int | None:
    import wave

    try:
        with wave.open(str(path), "rb") as w:
            return round(w.getnframes() * 1000 / w.getframerate())
    except (OSError, EOFError, wave.Error):
        return None


def _folder_evidence(
    batch: list[Path], trans_wavs: list[Path]
) -> tuple[file_match.FolderEvidence, list[int]]:
    """The folder as the matcher sees it, and the files' own order (indices into
    *batch*). Runtimes come from the transcodes BEFORE our silence trim, so our
    own trimming never moves them (§14.3)."""
    from cdda2img import release_match as rm
    from cdda2img.metadata import files_order, read_file_tag_release, read_file_tracks

    tags, album = read_file_tracks(batch)
    order = files_order(tags)
    files = tuple(
        file_match.FileTrack(
            position=pos,
            name=batch[i].name,
            title=tags[i].title,
            duration_ms=_wav_ms(trans_wavs[i]),
            tag_recording=tags[i].recording_id,
        )
        for pos, i in enumerate(order)
    )
    record = rm.ReleaseRecord(
        "tags",
        "",
        barcodes=tuple(v for v in (album.barcode,) if v),
        catnos=tuple(v for v in (album.catalognumber,) if v),
        labels=tuple(v for v in (album.label,) if v),
        country=album.country,
    )
    tagged = read_file_tag_release(batch)
    folder = file_match.FolderEvidence(files, record, album.date, tagged.release_id)
    return folder, order


def _gather_tagged(
    batch: list[Path], provenance: dict[str, str]
) -> tuple[list[DiscMeta], dict[str, tuple[str, ...]]]:
    """Step 1 (§14.2): the release the tags name, every medium of it."""
    from cdda2img.mb_lookup import release_media
    from cdda2img.metadata import read_file_tag_release

    tagged = read_file_tag_release(batch)
    if tagged.state is None:
        return [], {}
    provenance["file_tag_release"] = tagged.state
    if tagged.release_id is None:
        return [], {}
    print(f"  MusicBrainz: release {tagged.release_id[:8]} named by the tags")
    fetched = release_media(tagged.release_id)
    if fetched is None:
        provenance["file_tag_release_check"] = "unanswered"
        return [], {}
    provenance["file_tag_release_check"] = "fetched"
    media, formats = fetched
    return media, {tagged.release_id: formats}


def _choose_file_release(
    ranked: file_match.Ranked,
    folder: file_match.FolderEvidence,
    *,
    interactive: bool,
    tui: bool,
) -> tuple[file_match.Classified | None, str | None, tuple[file_match.Classified, ...]]:
    """(chosen, release_selection, group) per §14.4; chosen None = no release."""
    from cdda2img.menu_state import MenuController

    cands = ranked.candidates
    if not interactive:
        d = file_match.decide_unattended(ranked, folder.tagged_release)
        if d.outcome == "rejected":
            return None, None, ()
        return d.chosen, d.outcome, d.group
    if len(cands) == 1 and cands[0].tier == "A":
        return cands[0], "unique", ()
    # The marked row: the tagged release when it is not contradicted, else the
    # best of tiers A and B; never a tier-C release.
    pinned = next(
        (
            c
            for c in cands
            if c.tier != "C" and c.meta.mb_release_id == folder.tagged_release
        ),
        next((c for c in cands if c.tier != "C"), None),
    )
    notes = {c.meta.mb_release_id or "": _file_match_notes(c) for c in cands}
    provisional = RBIDisc(
        album=cands[0].meta.album or "",
        artist=cands[0].meta.artist or "",
        mb_release_id=pinned.meta.mb_release_id if pinned else None,
    )
    # `auto_tiebreak` with a mark: leaving it in place accepts it (§13). With no
    # mark (only tier C) there is nothing to accept, and leaving chooses nothing.
    ctl = MenuController(
        provisional,
        tui=tui,
        pressing_candidates=[c.meta for c in cands],
        pressing_outcome="auto_tiebreak" if pinned else "none",
        pressing_only=True,
        pressing_notes=notes,
    )
    ctl.run()
    outcome = ctl.pressing_outcome
    if outcome == "manual" and ctl.pressing_selected is not None:
        rid = ctl.pressing_selected.mb_release_id
        return next(c for c in cands if c.meta.mb_release_id == rid), "manual", ()
    if outcome == "accepted" and pinned is not None:
        return pinned, "accepted", ()
    if outcome == "rejected":
        return None, "rejected", ()
    return None, None, ()  # left with nothing marked and nothing chosen


def _file_match_notes(c: file_match.Classified) -> list[str]:
    lines = [file_match.note(c)]
    if c.excluded:
        lines.append("Not on this release, so left out of the archive:")
        lines += [f"  {name}" for name in c.excluded]
    if c.order == "release":
        lines.append("The archive will take the release's track order.")
    if c.contradicted:
        lines.append("Contradicted by: " + ", ".join(c.contradicted))
    lines.append("Evidence: " + c.describe())
    return lines


def _group_only_meta(group: tuple[file_match.Classified, ...]) -> DiscMeta:
    """The album, with only the pressing fields every candidate states alike."""
    from cdda2img import release_match as rm

    metas = [c.meta for c in group]
    meta = replace(metas[0], mb_release_id=None, discogs_release_id=None)
    for name, key in _pressing_field_keys().items():
        value = rm.unanimous([getattr(m, name) for m in metas], key, require_all=True)
        if name == "release_date" and value:
            value = rm.norm_year(value)
        setattr(meta, name, value)
    return meta


def _note_contradicted(ranked: file_match.Ranked, provenance: dict[str, str]) -> None:
    """The releases in the files' order a contradiction held back: kgr's analysis
    cases for tuning the "irreconcilable" thresholds (§14.4, §14.8)."""
    if not ranked.contradicted:
        return
    provenance["file_match_contradicted"] = ";".join(
        f"{c.meta.mb_release_id}:{'+'.join(c.contradicted)}"
        for c in ranked.contradicted
    )
    for c in ranked.contradicted:
        print(
            f"  Held back for analysis: {(c.meta.mb_release_id or '')[:8]} "
            f"is in your files' order but {', '.join(c.contradicted)} contradicts"
        )


def _record_file_match(
    provenance: dict[str, str],
    chosen: file_match.Classified,
    folder: file_match.FolderEvidence,
    selection: str | None,
) -> None:
    rid = chosen.meta.mb_release_id or ""
    provenance["file_match"] = chosen.describe()
    provenance["file_match_tier"] = chosen.tier
    provenance["file_match_order"] = chosen.order
    if chosen.excluded:
        provenance["file_match_excluded"] = ",".join(chosen.excluded)
        print("  Not on the chosen release, so left out of the archive:")
        for name in chosen.excluded:
            print(f"    {name}")
    provenance["mb_identified_via"] = (
        "file_tags" if rid and rid == folder.tagged_release else "file_match"
    )
    provenance["release_selection"] = selection or "unique"


def _identify_files(
    batch: list[Path],
    trans_wavs: list[Path],
    provenance: dict[str, str],
    *,
    interactive: bool,
    tui: bool,
    preferred_country: Sequence[str] = (),
) -> FileIdentity:
    """``create``: identify the release a folder of files is (trust_model_design.md
    §14), and which files, in which order, the archive is built from."""
    folder, order = _folder_evidence(batch, trans_wavs)
    metas, formats = _gather_tagged(batch, provenance)
    ranked = file_match.rank(folder, metas, preferred_country)
    _note_contradicted(ranked, provenance)
    if not ranked.candidates:
        if metas or ranked.rejected:
            provenance["file_match_rejected"] = (
                "missing_tracks" if ranked.rejected else "no_candidates"
            )
        elif provenance.get("file_tag_release_check") == "unanswered":
            provenance["file_match_rejected"] = "unanswered"
        return FileIdentity(None, order)
    chosen, selection, group = _choose_file_release(
        ranked, folder, interactive=interactive, tui=tui
    )
    if chosen is None:
        if selection == "rejected":
            provenance["release_selection"] = "rejected"
        else:
            d = file_match.decide_unattended(ranked, folder.tagged_release)
            provenance["file_match_rejected"] = (
                d.reason if not interactive and d.reason else "not_chosen"
            )
        return FileIdentity(None, order)
    keep = [order[p] for p in chosen.mapping]
    rid = chosen.meta.mb_release_id or ""
    _record_file_match(provenance, chosen, folder, selection)
    fmts = formats.get(rid, ())
    digital = bool(fmts) and all(f == "Digital Media" for f in fmts)
    fmt = "File" if digital else "CD"
    if selection == "release_group_only":
        return FileIdentity(_group_only_meta(group), keep, fmt, group_only=True)
    return FileIdentity(chosen.meta, keep, fmt)


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


# Widest map this report will draw, and the narrowest worth drawing at all.
# The cap is cosmetic — a full-width bar on a 200-column terminal is harder to
# read, not easier. The floor is NOT cosmetic: below it we print counts and no
# bars, because a bar that will not fit must never be emitted (see below).
_CTDB_MAP_MAX_COLS = 120
_CTDB_MAP_MIN_COLS = 16


def _ctdb_report_rows(
    damage: bytes | None, repaired: bytes, *, resolved: bool
) -> tuple[list[tuple[str, bytes, str]], str]:
    """The report's rows as ``(label, per-sector map, suffix)``, plus any note.

    Split out of :func:`_print_ctdb_repair_map` to keep it under the complexity
    gate, and it earns the split: everything here is *what to claim*, everything
    left there is *how wide to draw it*. The two were tangled and the second is
    where the bug was.

    The "before" row prefers the read's own C2 map, so it is continuous with the
    live map the user just watched. Without one it falls back to what parity
    rewrote — measured, but a lower **bound** on the damage, so the suffix names
    which quantity is being counted rather than quietly swapping one for the
    other under an unchanged label.
    """
    if damage is not None:
        before, note = damage, f"{len(damage) - damage.count(0)} flagged"
    else:
        before = repaired
        note = f"{len(before) - before.count(0)} repaired"
    rows: list[tuple[str, bytes, str]] = [("Before", before, note)]

    if resolved:
        return [*rows, ("After", bytes(len(before)), "clean")], ""
    if damage is not None:
        residual = bytes(0 if r else d for d, r in zip(damage, repaired))
        left = f"{len(residual) - residual.count(0)} remain"
        return [*rows, ("After", residual, left)], ""
    # The repair committed, AR still disagrees, and there is no C2 map to
    # localise what is left. There is no truthful "after" row to draw — but
    # silently emitting two lines where every other run emits three reads as
    # truncated output rather than as a refusal, so the block says why.
    return rows, "After   not shown — AR still disagrees and no C2 map was captured"


def _print_ctdb_repair_map(
    repaired_sectors: bytes | None,
    disc_damage: bytes | None = None,
    *,
    resolved: bool = True,
) -> None:
    """Draw the disc before and after CTDB parity repaired it (§5).

    A **result** map, not a progress bar, and the distinction is the whole design
    decision: CTDB repair does zero extra reads and finishes in ~0.8 s, so
    animating it would be theatre. This is drawn once, after the fact.

    **Before/after, not damage/repairs** (kgr, 2026-08-14). The first shipped
    version paired the read's C2 damage with what parity rewrote, on the reasoning
    that where the drive struggled is the backdrop the repairs are read against.
    That answered a question nobody asked. The one the user has is *"is the disc
    fixed?"*, and a damage row that still shows the original damage after a
    successful repair answers "no" in the only vocabulary the row has. The map
    the user watched during the read is the "before"; this draws the "after".

    **"After: clean" is earned, not assumed.** ``repair_whole_disc`` writes the
    PCM back only after ``verify_ctdb`` *and* ``verify_ar`` both pass
    (:mod:`ctdb_repair`), so on the only path that reaches this function every
    track CTDB called damaged now verifies against both references. *resolved*
    carries the one case that gate does not cover: AR and CTDB are different
    reference populations, so a track can still fail AR while passing the
    per-track CRC that admitted the repair. Then the "after" row shows what is
    left — ``damage & ~repaired`` — and the recovery ladder runs next.

    Deriving that residual is right **only** in the unresolved branch. When AR
    verifies, residual C2 flags are refuted evidence: C2 over-flags, so drawing
    them would paint phantom damage the gate has already disproved, directly
    above an AR report saying every track is OK.

    Both rows are C2-calibrated (:data:`disc_map.RAMP_BANDS`) — a zero healthy
    baseline is right here, unlike the Q lane, which needed its own bands.

    **Width.** Every column on the line is budgeted, because this row cannot clip
    the way the live map does (:meth:`TerminalUI._build_map` subtracts the whole
    line's furniture and then clips to ``avail``). The first version subtracted 4
    from the terminal width while the line carried 33 columns of label and
    suffix, and emitted a 153-column line under a cap that claimed to fit 120.
    A wrap here is cosmetic — ``pause()`` zeroes ``_prev_height`` before we
    print, so the repaint model cannot be corrupted by it — but it is still the
    difference between a report and a mess. Below :data:`_CTDB_MAP_MIN_COLS`
    there is no honest bar, so the counts are printed alone; a floor that forces
    a wider bar than fits is the original bug with a different constant.
    """
    if not repaired_sectors:
        return
    import shutil
    import sys

    from cdda2img import disc_map

    # `or None` folds an empty capture into "no map": a zero-length damage map
    # is the absence of evidence, not evidence of a clean disc.
    damage = disc_damage or None
    if damage is not None and len(damage) != len(repaired_sectors):
        # Bucket boundaries are derived from each map's own length, so two maps
        # of different lengths put cell i over different sectors and the rows
        # silently misalign. Truncate both to the common prefix instead: the
        # two are per-sector maps of the same disc and should already agree, so
        # a difference is small, but "should" is not a reason to skip the clamp.
        keep = min(len(damage), len(repaired_sectors))
        damage, repaired_sectors = damage[:keep], repaired_sectors[:keep]
        if not repaired_sectors:
            return

    rows, note_line = _ctdb_report_rows(damage, repaired_sectors, resolved=resolved)

    label_w = max(len(lbl) for lbl, _, _ in rows)
    note_w = max(len(note) for _, _, note in rows)
    cols = shutil.get_terminal_size((80, 24)).columns
    # 5 indent + label + 2 gap, bar, 2 gap + suffix. The suffix column is sized
    # across BOTH rows so the two bars get one width and stay column-aligned —
    # the same trap the live map has for its status banner.
    width = min(_CTDB_MAP_MAX_COLS, cols - (5 + label_w + 2) - 2 - note_w)

    # 3/5 indentation matches print_ar_report, which prints f"   {line}" over a
    # report whose own body is indented 2. This block sits between two AR
    # reports, so being one column out is maximally visible.
    print("   CTDB parity repair:")
    if width < _CTDB_MAP_MIN_COLS:
        for lbl, _, txt in rows:
            print(f"     {lbl:<{label_w}}  {txt}")
    else:
        colour = disc_map.colour_enabled(sys.stdout)
        for lbl, sectors, txt in rows:
            cells = disc_map.cells_from_damage(
                sectors, frontier=len(sectors), width=width
            )
            bar = disc_map.render(cells, colour=colour)
            print(f"     {lbl:<{label_w}}  {bar}  {txt}")
    if note_line:
        # Sliced, not wrapped: the whole point of the budget above is that no
        # line this function emits can exceed the terminal.
        print(f"     {note_line}"[:cols])


def _store_match_distance(
    provenance: dict[str, str], disc: RBIDisc, *, show: bool = False
) -> MatchDistance:
    """Write the container's ``match_confidence`` / ``match_recommendation`` (N6).

    Call this **after** the metadata menu has closed, on both the create and the
    import/rip paths. It is the only writer of those two keys.

    The split it enforces is the whole of N6. ``build_match_distance`` is called
    once *before* the menu to print a hint — how good the automatic guess is,
    which is what an operator about to review the guess wants to see — and
    :func:`~cdda2img.match_distance.final_match_distance` is called once *after*,
    to record what the container ended up believing. Those are two numbers about
    two moments; before 2026-08-13 they were one call, and every container from
    the N5 alternatives menu carried ``match_confidence=0.550`` beside
    ``release_selection=manual``.

    Nothing reads either key back — they are write-only tree-wide. Two lines are
    printed: the pre-menu hint (``build_match_distance``, **by design** the
    automatic guess) and, since 2026-11-26, this result after an interactive menu
    (*show*: "Metadata after your review"). So moving the write costs nothing and is not
    load-bearing for any decision; it is a provenance-honesty fix.
    """
    from cdda2img.match_distance import final_match_distance

    final = final_match_distance(disc, provenance)
    provenance["match_confidence"] = f"{final.score:.3f}"
    provenance["match_recommendation"] = final.recommendation.value
    if show:
        # The line printed before the menu grades the automatic guess (N6); this
        # one grades what the container ended up believing, so a release the
        # user confirmed with the disc in hand shows as such (kgr, 2026-11-26).
        print(f"   Metadata after your review: {final.summary()}")
    return final


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
    identity: DiscMeta | None = None,
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
    if identity is not None:
        from cdda2img.lookup_result import DiscMeta

        # The single Discogs release (_discogs_release) and, under --digital-media
        # only, the digital release's MBID (_identify_digital_release).
        props += meta_to_proposals(
            DiscMeta(mb_release_id=identity.mb_release_id), Source.DIGITAL_RELEASE
        )
        props += meta_to_proposals(
            DiscMeta(discogs_release_id=identity.discogs_release_id), Source.DISCOGS
        )
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


def _retry_mb_lookup(
    mb_result: MBPrepopResult,
    disc: RBIDisc,
    retry_service: Callable[[str, str], bool] | None,
    ui: TerminalUI | None,
    *,
    verbose: bool,
    preferred_country: list[str],
) -> MBPrepopResult:
    """While the MusicBrainz disc-ID lookup could not be asked, offer to try again.

    Only that lookup is repeated: CDDB, which ran beside it, already answered or
    failed on its own. A failed lookup leaves *disc* untouched, so the same disc is
    the right input again.
    """
    from cdda2img.mb_lookup import prepopulate_from_mb

    while (
        mb_result.lookup_error is not None
        and retry_service is not None
        and retry_service("MusicBrainz", _mb_error_detail(mb_result.lookup_error))
    ):
        _ui_status(ui, "Retrying MusicBrainz…")
        mb_result = prepopulate_from_mb(
            disc, verbose=verbose, preferred_country=preferred_country
        )
    return mb_result


def _mb_error_detail(reason: str) -> str:
    """``MBLookupError.reason`` in words, for the retry prompt."""
    return "no response" if reason == "network" else f"HTTP {reason}"


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
    retry_service: Callable[[str, str], bool] | None = None,
    digital_media: bool = False,
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
        # Daemon threads, not a ThreadPoolExecutor: leaving an executor's block,
        # and interpreter exit, both join its workers, so a Ctrl-C during a
        # stalled MusicBrainz request (up to ~5 min of retries) waited for it.
        cddb_future = _in_daemon_thread(
            query_cddb, cddb_track_lsns, cddb_disc_last_lsn, cddb_server
        )
        mb_future = _in_daemon_thread(
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
        mb_result = _mb_prepop_alone(
            disc, mb_verbose, preferred_country, digital_media=digital_media
        )

    mb_result = _retry_mb_lookup(
        mb_result,
        disc,
        retry_service,
        ui,
        verbose=mb_verbose,
        preferred_country=preferred_country,
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
        attempted=not digital_media,
        has_data=mb_result.match_count > 0,
        errored=mb_result.lookup_error is not None,
    )
    if mb_result.lookup_error is not None:
        provenance["lookup_error_mb"] = mb_result.lookup_error
    _record_mb_identification(provenance, mb_result)

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
    # Skipped when the disc-ID lookup could not reach MusicBrainz: stage 7 queries
    # the same service, and would retry for minutes to reach the same failure.
    if (
        mb_result.lookup_error is None
        and mb_result.selected_release_id is None
        and (disc.album or disc.artist)
    ):
        from cdda2img.mb_lookup import duration_match_lookup, strip_pressing_mbid

        dm = duration_match_lookup(disc, verbose=mb_verbose)
        if dm is not None:
            # Record which release matched for provenance, but do NOT bake its
            # (text+duration-matched, non-disc-ID) pressing MBID into disc as if
            # authoritative — route through the C2 chokepoint before merging,
            # keeping the release group, exactly as the ISRC-tally fallback does.
            provenance["duration_match_release"] = dm.mb_release_id or "?"
            provenance.setdefault("mb_identified_via", "duration_match")
            stage7_meta = strip_pressing_mbid(dm)
            disc = _merge_into_disc(stage7_meta, disc)

    # --digital-media: the data that belongs to a digital release is its own
    # MusicBrainz release and its Discogs record (kgr, 2026-11-21: "fetch the data
    # corresponding to the media type"); the archival gate aborts without them.
    digital = _identify_digital_release(provenance, mb_result, enabled=digital_media)

    # Discogs again, when its first look had no barcode and a MusicBrainz
    # fallback has since identified the release (found 2026-11-21 by replaying
    # real discs: ABBA *Gold* through the duration match, Toca under
    # --digital-media through the ISRC tally, both aborted on a Discogs that ran
    # before the release was known).
    disc, chosen_barcode, discogs_hit = _discogs_after_fallback(
        disc,
        ui,
        provenance,
        chosen_barcode,
        discogs_hit,
        identified_release=_identified_release(digital, provenance, mb_result),
    )
    # The one Discogs release this disc is (kgr, 2026-11-21: "require a single
    # Discogs release"), and under --digital-media the digital release's MBID.
    disc, identity = _apply_release_identity(
        disc, provenance, digital, discogs_hit, mb_result.selected_release_id
    )

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
            identity,
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
    archival: bool = False,
    digital_media: bool = False,
) -> Path:
    """Shared post-rip/import pipeline: lookups → metadata menu → TOC → RG → container.

    *archival* (``rip``): a service that did not answer aborts the run
    (:func:`_require_service_answers`) before the metadata menu, so nobody sits
    through a menu for a container that cannot be written (kgr, 2026-11-21).

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
        retry_service=functools.partial(
            _offer_service_retry, ui, interactive=_interactive(auto)
        ),
        digital_media=digital_media,
    )
    _archival_gates(
        disc, pcm_file, provenance, ui, archival=archival, digital_media=digital_media
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
    populate_original_release(disc, verify_meta=mb_result.meta, provenance=provenance)
    # Trace the canonical result (DEBUG: the menu summary already shows it; keep
    # the normal UI clean). Same string as the menu/list/catalogue surfaces.
    log.debug("%s", format_original(disc))
    # R11: corroborate with Discogs master if both sources are present.
    _r11_corroborate_with_discogs_master(disc, provenance)

    # Compute match confidence after all lookup signals are baked into prov.
    #
    # N6: this call is the PRINTED HINT and nothing else. It scores the automatic
    # guess, before a human has seen it, which is exactly what the operator wants
    # on screen — "here is how good my guess is, now look at it". The keys that go
    # into the container are written after the menu closes, by
    # `_store_match_distance` below, and are a different number by design.
    from cdda2img.match_distance import build_match_distance

    match_dist = build_match_distance(disc, provenance)
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
        print(f"   Metadata auto-confirmed — {match_dist.summary()}")
    else:
        print(f"   Metadata: {match_dist.summary()}")
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
    # §12 D6/D8: an interactive rip opens the Discogs master screen unless
    # MusicBrainz's own link settled the release; a single barcode hit is a
    # marked row there, not a decision.
    via = provenance.get("discogs_release_via")
    # A disc made from a download is matched against the master's digital
    # releases, never its CDs (kgr, 2026-11-26).
    discogs_fmt = "File" if digital_media else "CD"
    disc = run_metadata_menu(
        disc,
        source_pcm=pcm_file,
        ar_summary=ar_summary,
        tui=tui,
        auto_apply=auto_apply,
        pressing_candidates=pressing_candidates,
        provenance=provenance,
        discogs_open=archival and via != "mb_link",
        discogs_barcode_hit=disc.discogs_release_id if via == "barcode" else None,
        discogs_fmt=discogs_fmt,
    )
    disc = _settle_identity_after_menu(
        disc,
        provenance,
        mb_result,
        archival=archival,
        auto_apply=auto_apply,
        tui=tui,
        discogs_fmt=discogs_fmt,
    )
    # After the settlement, which may pair, derive or drop the pressing: the
    # corroboration keys were taken against the one the ladder pinned.
    _note_corroboration_target(provenance, mb_result.selected_release_id, disc)
    # N6: NOW store the score, against the post-menu disc and provenance. Sited
    # after `_note_corroboration_target` rather than straight after the menu —
    # that helper writes only `corroborated_release`, which is not a contributor,
    # so the position is not load-bearing today, but "everything the menu phase
    # writes has been written" is a cheaper invariant to keep than a per-key
    # audit each time a helper is added here.
    _store_match_distance(provenance, disc, show=_interactive(auto_apply))
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
    _require_cover_art(provenance, archival=archival)

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


def _lookup_drive_offset(device: str):  # -> DriveOffsetLookup | None
    """AccuDisc's table entry for *device*, or None when it cannot be had.

    Swallows a lookup failure into ``None`` deliberately. A missing binding is
    fatal for a *rip*, but this runs before one and rung 3 still yields a usable
    (if unshifted) read — refusing the disc because a metadata lookup failed
    would be a worse trade than an offset the user is told was not applied.
    """
    from cdda2img.accudisc_reader import drive_offset_lookup
    from cdda2img.drive_info import probe_drive_inquiry

    inquiry = probe_drive_inquiry(device)
    if inquiry is None:
        return None
    try:
        return drive_offset_lookup(*inquiry)
    except RuntimeError as exc:
        log.warning("AccuDisc offset lookup failed: %s", exc)
        return None


def _accept_lookup_offset(offset: int, detail: str, submissions: int) -> bool:
    """Whether to apply a looked-up read offset: auto, prompt, or decline.

    Auto-applies above ``_MIN_AR_CONFIDENCE`` submissions, prompts a TTY below
    it, and declines without one — a non-interactive run must not silently adopt
    a thinly-evidenced offset just because nobody was there to say no.
    """
    import sys

    if submissions >= _MIN_AR_CONFIDENCE:
        return True
    if not sys.stdin.isatty():
        return False
    answer = input(f"  AccuDisc: offset {offset:+d} ({detail}). Use? [y/N] ")
    return answer.strip().lower() == "y"


def _report_offset_disagreement(info, drive_name: str | None) -> None:
    """Print every candidate offset and how to settle it. Applies nothing.

    The sources disagree, AccuDisc deliberately refuses to pick, and so do we:
    an offset applied wrongly shifts the audio silently, which is worse than an
    unshifted rip the user knows is unshifted. The candidates are named so the
    config entry can be written without a second lookup.
    """
    cands = ", ".join(f"{v:+d} ({'+'.join(sorted(s))})" for v, s in info.candidates)
    more = ", …" if info.truncated else ""
    print(_fmt_kv("Read offset", "+0 samples (sources DISAGREE — not applied)"))
    print(f"   {'':13}candidates: {cands}{more}")
    print(
        f"   {'':13}choose one and add `read_offset = N` to a [[drives]]"
        f' entry named "{drive_name}" in cdda2img.toml'
    )


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
      2. AccuDisc's compiled offset table (auto-applied when submissions >=
         _MIN_AR_CONFIDENCE; prompts the user when confidence is lower).
      3. 0 with a warning when the drive is not configured.

    *write_offset* comes from cfg.drives only; ``None`` when not configured. It
    is **measured**, not looked up — the published write-offset data is too
    sparse to be worth a table — so a drive acquires one by running
    ``setup --write-offset`` once, after which rung 1 supplies it forever.

    Confirmed read offsets are persisted to [[drives]] in cdda2img.toml so that
    subsequent rips skip the lookup entirely.

    Rung 2 was the local ``drive_offsets.db`` AccurateRip scrape until
    2026-08-27. It is now AccuDisc's table, which is the same data better kept:
    keyed on the INQUIRY pair the drive actually reports rather than on a
    flattened ``"VENDOR MODEL"`` string, and able to say *the sources disagree*
    instead of silently returning one of them.
    """

    from cdda2img.config import save_drive_read_offset
    from cdda2img.drive_info import probe_drive_name

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

    # 2. AccuDisc's compiled offset table. Device-free: no drive is opened.
    info = _lookup_drive_offset(device)

    if info is not None and info.read_offset is None:
        _report_offset_disagreement(info, drive_name)
        return 0, None, drive_name

    if info is not None and info.read_offset is not None:
        offset, submissions = info.read_offset, info.ar_submissions
        # `sources` is provenance, NOT a confidence score: REDUMP and AccurateRip
        # are one source, so a row held by both means "unrevised since 2022"
        # rather than two witnesses (AccuDisc 2026-08-27a §2.2). The count that
        # carries evidence is AccurateRip's own submission tally, and 0 of them
        # is a real answer for a REDUMP-only row — not a missing measurement.
        src = "+".join(sorted(info.sources)) or "unknown source"
        detail = f"{src}, {submissions} AR submission(s)"
        if _accept_lookup_offset(offset, detail, submissions):
            print(_fmt_kv("Read offset", f"{offset:+d} samples ({detail})"))
            try:
                save_drive_read_offset(drive_name, offset)
            except OSError as exc:
                log.warning("Could not persist drive read offset to config: %s", exc)
            return offset, None, drive_name

    # 3. Drive not configured — warn and use 0.
    if info is None:
        print(_fmt_kv("Read offset", "+0 samples (drive not in AccuDisc's table)"))
    else:
        print(_fmt_kv("Read offset", "+0 samples (offset found but not applied)"))
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

    from cdda2img.mb_lookup import MBLookupError, lookup_disc_id

    if disc.album:
        return f"{disc.album} - {disc.artist}" if disc.artist else disc.album

    try:
        matches = lookup_disc_id(disc)
    except MBLookupError:
        return "(unknown)"
    pairs = [(m.album, m.artist) for m in matches if m.album]
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


# ``_probe_read_speed`` lived here until 2026-08-09 and was a second wrapper over
# the same seam call as ``drive_speed.current_speed_x`` — already compliant, merely
# duplicated. It now matters that there is one: the rip captures this value for the
# status line AND hands the same number back to the drive on the way out, and two
# functions computing "the current speed" is how those two uses drift apart.


def _apply_read_speed(device: str, want_x: int | None) -> int | None:
    """Request *want_x*, read back what the drive took, and return that for display.

    **The read-back is required, and deleting it was a regression** (2026-08-09,
    reported by kgr the same day). The reasoning that removed it was that AccuDisc's
    ``ReadStats.speed_honoured_x`` supersedes it, which is true of the *engine's*
    request and false of the status line. Two different things were being conflated:

    * The engine's answer arrives on the **read result** — i.e. after the read has
      finished. The spin-up line is drawn before the first sector.
    * So with the read-back gone, ``--ad-speed 16`` printed ``Ripping disc at 16x…``
      on a PX-716A, which has no 16x rung and quantizes to 8x. The drive was audibly
      at 8x. Nothing on screen ever said so; the engine's correct report landed in a
      DEBUG log after the fact.

    Measured on the PX-716A rather than assumed, because "did the drive report the
    quantization at set time?" is the question the whole fix rests on, and a drive
    that answered only after the read would make this impossible rather than merely
    wrong. Setting 4x then 16x reports 8x; 32x reports 32x; 32x then 16x reports 8x
    again. The baseline is moved between arms deliberately — read back at 8x after a
    drive already sitting at 8x cannot distinguish "quantized" from "unchanged".

    This does **not** un-adopt their field. Both are kept and they answer different
    questions: this one says what the drive took *before* the read, and
    :func:`accudisc_reader._log_speed_adopted` reports what the engine's own request
    achieved *during* it. The earlier one being superseded later is exactly why the
    display needs it — a status line cannot wait for a result that arrives at the end.

    ``want_x=None`` skips the request entirely: the drive keeps its own management,
    which is the default and by far the common case.
    """
    from cdda2img import drive_speed

    if want_x is None:
        return drive_speed.current_speed_x(device)

    if not drive_speed.request_speed(device, want_x):
        # Refused outright: the rate was never installed. Distinct from quantizing,
        # where the drive took the command and snapped it to a rung it has.
        log.warning("drive %s refused the %dX read-speed request", device, want_x)
        return drive_speed.current_speed_x(device)

    got_x = drive_speed.current_speed_x(device)
    if got_x is None:
        # Accepted, but the drive will not say where it landed. Return the request
        # and say which it is — a number nobody measured must not read as measured.
        log.warning(
            "drive %s accepted the %dX request but does not report its speed; "
            "the rate shown is what was asked for, not what was measured",
            device,
            want_x,
        )
        return want_x
    if got_x != want_x:
        log.info(
            "drive %s asked for %dX, adopted %dX (quantized) — reading at %dX",
            device,
            want_x,
            got_x,
            got_x,
        )
    return got_x


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

    #: Red Book's ceiling. Bounds the track label without knowing the disc, which
    #: is what lets the width be pinned from the very first frame — before the
    #: TOC has been read and therefore before there is a track count at all.
    _MAX_TRACK_LABEL = len("Ripping track 99…")

    def __init__(self, speed_x: int | None = None) -> None:
        self._lanes: ReadLanes | None = None
        self._speed = speed_x

    def begin(self, lanes: ReadLanes) -> None:
        self._lanes = lanes
        # The engine's own reading wins if it has one; the pre-read value stands
        # otherwise, so the clause never disappears once shown.
        self._speed = lanes.speed_x or self._speed

    @property
    def text(self) -> str:
        return self._pad(self._disc_text())

    def _disc_text(self) -> str:
        # No speed clause when the drive did not report one: "at 0x" or "at Nonex"
        # reads as a measurement, and this one was never made.
        return f"Ripping disc at {self._speed}x…" if self._speed else "Ripping disc…"

    def at(self, done: int) -> str:
        """The status for a read that has delivered *done* sectors."""
        lanes = self._lanes
        if lanes is None or not lanes.track_starts or done <= 0:
            return self._pad(self._disc_text())
        # Sector `done - 1` is the last one actually delivered. Using `done`
        # would name the next track one sector early at every boundary.
        n = bisect.bisect_right(lanes.track_starts, done - 1)
        return self._pad(f"Ripping track {n:02d}…" if n else self._disc_text())

    def _pad(self, text: str) -> str:
        """Every phase returns the same width, so nothing downstream can shift.

        The map pins its column count against this text, but the **plain bar**
        renders the spin-up frame before the map exists and sizes itself from
        `len(status)` directly. Padding here is what keeps the bar starting in
        the same column across that handover — otherwise the whole widget jumps
        sideways the moment the map appears, which is what it did.
        """
        return text.ljust(self.widest_status())

    def widest_status(self) -> int:
        """The widest text this rip can ever show — computable from the start.

        Bounded by 99 tracks rather than by the disc's own count, because the
        width has to be known before the TOC is read and a value that changes
        when the TOC lands is not a pinned width. The cost is a few spaces on a
        single-digit disc; the alternative is a map that re-buckets once.
        """
        return max(len(self._disc_text()), self._MAX_TRACK_LABEL)


def _capture_prov(counters: CaptureCounters) -> dict[str, str]:
    """PROV ``capture_*`` from what the engine re-read on its own during the
    capture (rbi_spec). A counter the engine did not report is left out: absent
    is not zero."""
    values = {
        "capture_sectors_recovered": counters.sectors_recovered,
        "capture_sectors_suspect": counters.sectors_suspect,
        "capture_subq_misposition": counters.subq_misposition,
        "capture_speed_honoured": counters.speed_honoured_x,
        "capture_speed_changes": counters.speed_changes,
    }
    prov = {k: str(v) for k, v in values.items() if v is not None}
    # The 0.53 effort tables: absent on an older engine, "none" when reported and
    # all zero, so "not reported" and "nothing happened" stay apart.
    for key, table in (
        ("capture_cmds", counters.cmds),
        ("capture_cmds_failed", counters.cmds_failed),
        ("capture_ms", _us_to_ms(counters.us)),
        ("capture_won_by", counters.won_by),
    ):
        if table is not None:
            prov[key] = ",".join(f"{k}:{v}" for k, v in table.items() if v) or "none"
    return prov


def _us_to_ms(us: dict[str, int] | None) -> dict[str, int] | None:
    """Microseconds per stage to whole milliseconds, rounded down."""
    return None if us is None else {k: v // 1000 for k, v in us.items()}


def _prov_int(prov: dict[str, str] | None, key: str) -> int | None:
    """An integer PROV value, or ``None`` when absent (absent is not zero)."""
    value = (prov or {}).get(key)
    return int(value) if value is not None else None


def _rip_disc_stage(
    device: str,
    pcm_file: Path,
    c2_file: Path,
    read_offset: int,
    cfg: Config,
    ui: TerminalUI | None = None,
    read_speed: int | None = None,
) -> tuple[RipInfo, str, Path | None, bytes | None, tuple[int, int] | None]:
    """Read the disc, returning (info, rip_type, c2_path, disc_damage, readable).

    *readable* is the session the audio was read from, ``(lo, hi)``, from the
    full TOC the capture saved (``accudisc_reader.readable_extent``). Every
    recovery re-read passes it to AccuDisc, so no far read, the cache defeat's or
    the engine's own before a retry or verify pass, lands between an Enhanced
    CD's sessions (2026-11-22, AccuDisc 0.51.0). ``None`` on an engine before
    0.51.0 or a TOC without sessions, where the engine's unbounded rule stands.

    *disc_damage* is the read's per-sector C2 damage map, one byte per sector,
    retained past the read so a later stage can draw against it (the CTDB repair
    map, and the recovery ladder). ``None`` only if the reader never handed the
    lanes over, which means the read did not start.

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

    *read_speed* is the requested rate in X (``--ad-speed``), or ``None`` to leave the
    drive at its own management. It is **requested twice, deliberately**: once here so
    the request can be verified and displayed, and again as ``read_disc_c2(read_speed=)``
    — which is the authoritative one, since AccuDisc opens its own ``Device`` and sets
    ``speed_x`` at the head of the read. See :func:`_apply_read_speed`.

    The PCM returned is RAW — the drive offset is not applied here. The caller works raw
    through AR + ctanalyse and calls apply_offset exactly once, at storage.
    """
    from cdda2img.accudisc_reader import read_disc_c2, readable_extent

    want_c2 = cfg.c2_recovery != "off"
    sub_file = pcm_file.with_suffix(".sub")
    cdtext_file = pcm_file.with_suffix(".cdtext")
    fulltoc_file = pcm_file.with_suffix(".fulltoc")

    # The spin-up phase. It stands until the engine has read the TOC and handed
    # over the lanes, which is the first moment a track number can be honest —
    # the drive is genuinely spinning up and seeking the lead-in, and no sector
    # of track 1 has been delivered.
    #
    # The speed is resolved HERE, before the read, and that is the whole point.
    # Taking it from the lanes instead put it one step too late: the handover
    # that carries the speed also carries the track list, so by the time the
    # speed was known `at()` was already returning a track number and the clause
    # this phase exists to show was unreachable. It is one MODE SENSE page 2A —
    # instant, no spin-up — and never fatal.
    phase = _RipPhase(speed_x=_apply_read_speed(device, read_speed))
    _ui_status(ui, phase.text)

    def _cb(done: int, total: int) -> None:
        if ui is not None:
            ui.set_status(
                phase.at(done),
                done / total if total > 0 else 0.0,
                detail=f"({done}/{total})",
            )

    # The read's per-sector C2 damage map, KEPT past the read. `ui.set_map(None)`
    # below releases the *renderer's* reference so it stops polling a finished
    # read; it does not mean the data stops being interesting. Where the drive
    # struggled is exactly the backdrop a repair map has to be read against — a
    # cell that was damaged and was NOT repaired is the one worth a human's
    # attention, and neither map alone can show that.
    captured: list[ReadLanes] = []
    # What the engine re-read on its own during the capture (its Q-position rescue
    # always runs with the raw subchannel; AccuDisc 2026-11-25a, A.1). Recorded in
    # PROV `capture_*` (kgr, 2026-11-25), so how often it fires on real rips is
    # measured rather than guessed.
    counters: list[CaptureCounters] = []

    def _map_cb(lanes: ReadLanes) -> None:
        # The progress bar becomes the disc map for the length of the read: the
        # lanes are handed over at allocation, filled by the reader as sectors
        # arrive, and polled by the render thread. Note this is NOT gated on
        # want_c2 — C2 pointers are requested on the wire unconditionally, and
        # cfg.c2_recovery = "off" suppresses only the bitmap *file*. Gating here
        # would blank the map for anyone using that escape hatch, and a blank map
        # is indistinguishable from a clean disc.
        captured.append(lanes)
        phase.begin(lanes)
        if ui is not None:
            ui.set_map(
                lanes.damage, subq=lanes.subq, status_width=phase.widest_status()
            )

    try:
        track1_pregap = read_disc_c2(
            device,
            pcm_file,
            c2_file if want_c2 else None,
            output_sub=sub_file,
            output_cdtext=cdtext_file,
            output_fulltoc=fulltoc_file,
            read_speed=read_speed,
            progress_cb=_cb if ui is not None else None,
            # Unconditional, where it used to be gated on a TUI. The handover is
            # what yields the damage map, and a `--no-tui` rip has exactly as
            # much use for a repair map afterwards as an interactive one. The
            # *rendering* stays gated; the capture never was the reason for it.
            map_cb=_map_cb,
            stats_cb=counters.append,
        )
    finally:
        # Release the buffer on the failure path too — the renderer holds a
        # reference and would keep polling a map whose read has stopped, leaving
        # a frozen frontier on screen that reads as a stalled drive.
        if ui is not None:
            ui.set_map(None)
    disc_damage = bytes(captured[0].damage) if captured else None
    try:
        from cdda2img.subq_toc import build_rip_info

        info = build_rip_info(
            fulltoc_file.read_bytes(),
            sub_file.read_bytes(),
            cdtext_file.read_bytes() if cdtext_file.exists() else None,
            track1_pregap=track1_pregap,
        )
    except (ValueError, OSError) as exc:
        # subq_toc already degrades to TOC-only geometry when the Q stream cannot be
        # anchored, so reaching here means the full TOC itself is unusable — there is
        # nothing left to build a disc from, and no second engine to ask.
        msg = f"disc metadata assembly failed: {exc}"
        raise RuntimeError(msg) from exc
    if counters:
        info = info._replace(prov={**(info.prov or {}), **_capture_prov(counters[0])})
    readable = readable_extent(fulltoc_file.read_bytes())
    return info, "accudisc", (c2_file if want_c2 else None), disc_damage, readable


# The PROV status keys of the services an IMPORTED container's provenance depends
# on, and the name to report. An import recording `down` for one of these was
# written without that service's answer and exits 2 (`rip_abort.EXIT_RETRY_LATER`).
# A rip never gets this far: it aborts instead (`_require_service_answers`).
_REQUIRED_SERVICE_STATUS = {
    "lookup_status_accuraterip": "AccurateRip",
    "lookup_status_mb": "MusicBrainz",
}


def _unanswered_services(provenance: dict[str, str]) -> list[str]:
    """Required services this container's provenance records as ``down``."""
    return [
        name
        for key, name in _REQUIRED_SERVICE_STATUS.items()
        if provenance.get(key) == "down"
    ]


#: Below this share of CRC-good Q frames a rip's subchannel was sliced from the
#: wrong bytes. The two bounds, both from 2026-09-14 evidence:
#:
#: * **Misframed, from above: 1/chunk.** Sub before C2 (the LITE-ON LH-20A1S)
#:   gives 0%, with or without libata's PIO padding (measured 0/23 both ways). An
#:   MMC-order drive under the same padding keeps only each chunk's FIRST record
#:   framed, so it reads 1/n for an n-sector chunk: 4.35% at the engine's default
#:   23, but 6.67% at the 15 AccuDisc used in its own prediction test. A threshold
#:   of 5% would miss that.
#: * **Good audio, from below: 13.43%.** A read while another process used the
#:   drive (RECOVERY.md §12.0), AR v2 passing throughout. It is the lowest yield on
#:   record with good audio. Without contention, specific speed requests crater
#:   whole-pass Q to ~33-39% (ZZ Top 32x, Tracy 16x, RECOVERY.md); ordinary reads
#:   measure 98-99.9%. cdda2img takes no drive lock, so contention is a case that
#:   recurs, not a curiosity.
#:
#: 7.5% sits at roughly equal ratio from both (4.35 x 1.72; 13.43 / 1.79). It
#: catches chunks of 14 or more sectors. It does not catch smaller chunks, and it
#: does not protect a contended read worse than the one measured. Move it only on
#: a new measurement, and say which bound moved. Not RECOVERY.md's ~90% crater gate:
#: that one discards a disturbed PASS for re-reading; this one flags misframing.
_MIN_Q_VALID_FRACTION = 0.075


def _q_valid_fraction(value: str | None) -> float | None:
    """``subq_q_valid`` (``"<valid>/<sectors>"``) as a fraction; None if absent or malformed.

    ``0/0`` is **0.0**, not None. It is what a rip that asked for raw subchannel and
    got none records, which is the limiting case of a collapse, not an absence of
    evidence. Absence of the key (a container written before it existed) stays
    None: that one really is no witness.
    """
    try:
        valid, total = (int(x) for x in (value or "").split("/"))
    except ValueError:
        return None
    if valid < 0 or valid > total:
        return None
    return valid / total if total > 0 else 0.0


def _require_usable_q(prov: dict[str, str]) -> None:
    """Abort when the subchannel Q stream cannot supply what the archive needs.

    kgr, 2026-11-21 (rule b): write only if the container can be built in full
    "without relying on damaged or missing Q channel data". Track starts and the
    lead-out come from the full TOC, but the pre-gaps, INDEX points, MCN and ISRCs
    come only from Q, each majority-voted with a two-sighting floor. So a partly
    degraded Q is fine and a collapsed one is not: the rip aborts when the stream
    could not be anchored to an LBA (``subq_layout=unanchored``) or when fewer
    than :data:`_MIN_Q_VALID_FRACTION` of its frames passed their CRC, the
    signature of a misframed read. A track that genuinely has no ISRC cannot be
    told from one whose ISRC was never seen, so health is judged across the disc,
    not per field.

    Until 2026-11-21 a collapse wrote the container and exited 4, because Q can
    collapse with good audio (the LITE-ON returns subchannel before C2). The
    audio may still be right; the container would not be complete.
    """
    fraction = _q_valid_fraction(prov.get("subq_q_valid"))
    collapsed = fraction is not None and fraction < _MIN_Q_VALID_FRACTION
    unanchored = prov.get("subq_layout") == "unanchored"
    if not (collapsed or unanchored):
        return
    details = []
    if collapsed:
        details.append(
            f"Subchannel Q passed its CRC on {prov.get('subq_q_valid')} sectors"
            f" ({fraction:.1%}; the floor is {_MIN_Q_VALID_FRACTION:.1%}), the"
            " signature of a misframed read."
        )
    if unanchored:
        details.append("The Q stream could not be anchored to any LBA.")
    details.append(
        "Pre-gaps, INDEX points, the MCN and the ISRCs come only from Q, so the"
        " container could not be complete. Check the drive's subchannel order and"
        " transfer framing (CLAUDE.md, the LITE-ON libata note) before re-ripping."
    )
    raise RipAborted(
        EXIT_INVESTIGATE,
        "the subchannel Q stream is unusable",
        details,
        {k: v for k, v in prov.items() if k.startswith(("subq_", "toc_", "capture_"))},
    )


def _require_accuraterip_answer(tracks: list, reachable: bool, cddb_id: int) -> None:
    """Abort unless AccurateRip answered and holds this disc.

    Unreachable is exit 2 (re-run when it answers). "Not in the database" is exit
    3, and treated as **our** defect first: every past instance turned out to be
    an error in our code, with the disc there all along (kgr, 2026-11-21;
    memory: verify the key before blaming the server). The exact URL asked and the
    answer are in the failure report's query log.
    """
    context = {"cddb_id": f"{cddb_id:08x}", "tracks": str(len(tracks))}
    if not reachable:
        raise RipAborted(
            EXIT_RETRY_LATER,
            "AccurateRip did not answer, so nothing could be verified",
            [
                "Both HTTPS and HTTP failed, or the server answered only with an"
                " error. The queries are in the report."
            ],
            context,
        )
    if not any(r.max_confidence is not None for r in tracks):
        raise RipAborted(
            EXIT_INVESTIGATE,
            "AccurateRip has no record of this disc ID",
            [
                "Every earlier 'not in the database' was a fault in how the disc ID"
                " or the URL was built, with the disc present all along. Check the"
                " exact URL in the report (its IDs come from the track starts and"
                " the lead-out) before concluding the disc is absent.",
                "A CD-R burned from a digital-only release can never be in"
                " AccurateRip; that case is --digital-media's.",
            ],
            context,
        )


def _require_media_decision(kind: str, evidence: str, override: str | None) -> None:
    """Refuse a disc whose type needs the user's word (kgr, 2026-11-21).

    A recordable disc, or one whose type could not be determined, needs
    ``--cdr`` (a copy of a pressed CD: the normal pipeline, AccurateRip still
    verifying every track) or ``--digital-media`` (made from a digital-only
    release: no disc-ID lookups). Unknown is refused rather than guessed, since
    the override costs the user one flag (AccuDisc 2026-11-21j). Exit 1: the
    action is to re-run with the right flag. ``--digital-media`` on a pressed disc
    is refused too: it would skip AccurateRip for a disc AccurateRip can verify.
    """
    hint = [
        f"Media check: {evidence}.",
        "If it is a recordable copy of a pressed CD, re-run with --cdr.",
        "If it was made from a digital-only release, re-run with --digital-media.",
    ]
    if kind == "pressed":
        if override == "digital_media":
            raise RipAborted(
                EXIT_FIX_INVOCATION,
                "--digital-media was given for a pressed disc",
                [
                    f"Media check: {evidence}.",
                    "A pressed disc can be verified against AccurateRip; re-run"
                    " without --digital-media.",
                ],
            )
        return
    if override is None:
        reason = (
            "the disc is recordable (CD-R/RW)"
            if kind == "recordable"
            else "the disc's type could not be determined"
        )
        raise RipAborted(EXIT_FIX_INVOCATION, reason, hint)


def _digital_media_consensus(
    device: str,
    pcm_file: Path,
    disc_damage: bytes | None,
    ui: TerminalUI | None,
    capture_speed: int | None = None,
    capture_honoured: int | None = None,
    readable: tuple[int, int] | None = None,
) -> dict[str, str]:
    """Prove a ``--digital-media`` rip's audio by byte-identical reads.

    No disc-ID database holds such a disc, so AccurateRip cannot. Instead the
    disc is read again in full, at a **different** admitted rung, and every
    sector is compared with the capture (AccuDisc 0.59.0 ``compare_read`` under
    its read policy's ``REFERENCE_VERIFY``; kgr, 2026-11-25). A different rung,
    not the same one: read errors are stable per speed, so two reads at one speed
    can agree on the same wrong bytes. With C2 the capture and every re-read must
    also flag nothing; a drive without C2 cannot vouch for a read that way, so a
    third read at a third rung is required instead.

    The capture's speed is the one the drive reported using (*capture_honoured*,
    PROV ``capture_speed_honoured``), else the one asked for (*capture_speed*,
    ``--ad-speed``), else the drive's fastest rung. The policy picks each
    re-read's rung (the nearest admitted one no earlier read used), and each
    re-read is checked afterwards against the speed the drive reports for it: a
    drive that quantizes 32x to 40x would otherwise hand back a second read at
    the capture's own speed. Too few distinct speeds, a re-read at a used speed,
    any disagreement (a byte, a displacement, an unreadable sector) or any C2
    flag aborts (exit 3): there is no AccurateRip to verify a repair against.
    Returns PROV ``digital_media_consensus`` and ``digital_media_policy``.
    """
    from cdda2img import drive_speed
    from cdda2img.accudisc_reader import (
        NoVerifyRung,
        drive_supports_c2,
        verify_against_capture,
    )

    has_c2 = drive_supports_c2(device)
    if has_c2 and disc_damage is not None and any(disc_damage):
        flagged = sum(1 for b in disc_damage if b)
        raise RipAborted(
            EXIT_INVESTIGATE,
            f"the capture flagged C2 errors on {flagged} sector(s)",
            [
                "Under --digital-media nothing can verify a repaired read, so the"
                " capture must be clean. Clean the disc or try another drive."
            ],
        )
    ladder = drive_speed.admitted_ladder(device)
    extra = 1 if has_c2 else 2
    used = [capture_honoured or capture_speed or ladder[0]]
    policies: list[str] = []
    for k in range(2, 2 + extra):
        try:
            got = verify_against_capture(
                device,
                pcm_file,
                ref_speed_x=used[-1],
                exclude_speed_x=used[-2] if len(used) > 1 else 0,
                admitted=ladder,
                c2_usable=has_c2,
                readable=readable,
                on_speed=lambda x, k=k: _ui_status(
                    ui, f"Digital media: verifying read {k} at {x}X…"
                ),
            )
        except NoVerifyRung as exc:
            raise RipAborted(
                EXIT_INVESTIGATE,
                f"the drive cannot give read {k} a speed no earlier read used",
                [
                    f"Reads so far ran at {', '.join(f'{x}X' for x in used)}; the"
                    f" drive admits {', '.join(f'{x}X' for x in ladder)} on this disc.",
                    f"--digital-media needs {extra + 1} distinct speeds"
                    f" ({'with' if has_c2 else 'without'} C2), because two reads at"
                    " one speed can agree on the same wrong bytes. Try another drive.",
                    f"AccuDisc: {exc}",
                ],
            ) from exc
        speed = got.speed_honoured or got.speed_requested
        if speed in used:
            raise RipAborted(
                EXIT_INVESTIGATE,
                f"read {k} asked for {got.speed_requested}X and the drive read at"
                f" {speed}X, a speed an earlier read already used",
                [
                    "Two reads at one speed can agree on the same wrong bytes, so"
                    " they prove nothing under --digital-media. Try another drive.",
                ],
            )
        used.append(speed)
        policies.append(got.policy)
        if got.first_disagreement is not None:
            raise RipAborted(
                EXIT_INVESTIGATE,
                f"read {k} at {speed}X disagrees with the capture",
                [
                    f"{got.differ} sector(s) differ, {got.shifted} are displaced,"
                    f" {got.hard} could not be read and {got.not_compared} were not"
                    f" compared; the first at LBA {got.first_disagreement}.",
                    "Under --digital-media the audio is proved only by reads that"
                    " agree; clean the disc or try another drive.",
                ],
            )
        if has_c2 and got.agree_c2:
            raise RipAborted(
                EXIT_INVESTIGATE,
                f"read {k} at {speed}X flagged C2 errors",
                ["Every read must be clean under --digital-media."],
            )
    speeds = ",".join(["capture", *(f"{x}X" for x in used[1:])])
    return {
        "digital_media_consensus": (
            f"{len(used)}@{speeds}:{'clean' if has_c2 else 'unavailable'}"
        ),
        "digital_media_policy": ",".join(policies),
    }


def _require_every_track_verified(
    tracks: list, outcomes: dict[int, str], provenance: dict[str, str]
) -> None:
    """Abort unless every track verified against AccurateRip after recovery.

    kgr, 2026-11-21: "any outcome other than 100% AR verified tracks should not
    result in an RBI being written". A track recovery could not match keeps its
    original audio in memory only; nothing is written. The report carries the
    whole provenance gathered so far, which includes every recovery read
    (``recovery_attempts_track_<n>``, ``strategy_reads_track_<n>``).
    """
    failed = [
        r.track for r in tracks if r.confidence_v1 is None and r.confidence_v2 is None
    ]
    if not failed:
        return
    details = [f"track {t}: {outcomes.get(t, 'no recovery ran')}" for t in failed]
    if "recovery_ceiling_hit" in provenance:
        details.append(
            f"The recovery ceiling ({provenance['recovery_ceiling_hit']} s) stopped the"
            " chain before every stage ran: raise recovery_ceiling_s (0 = none) to let"
            " it finish, rather than concluding the disc cannot be recovered."
        )
    if "recovery_budget_hit" in provenance:
        what, _, limit = provenance["recovery_budget_hit"].partition(":")
        key = {
            "reads": "recovery_max_reads",
            "engine_cmds": "recovery_max_engine_cmds",
        }.get(what, "recovery_max_speed_changes")
        details.append(
            f"The drive-work budget ({limit} {what.replace('_', ' ')}) stopped"
            f" recovery to protect the drive ({provenance.get('recovery_drive_work')})."
            f" Raise {key} (0 = no limit) only if the drive can take more, rather"
            " than concluding the disc cannot be recovered."
        )
    for t in failed:
        chain = provenance.get(f"recovery_chain_track_{t}")
        if chain:
            details.append(f"track {t} chain: {chain}")
    raise RipAborted(
        EXIT_INVESTIGATE,
        f"{len(failed)} of {len(tracks)} track(s) did not verify against"
        " AccurateRip after recovery",
        details,
        provenance,
    )


def _discogs_after_fallback(
    disc: RBIDisc,
    ui: TerminalUI | None,
    provenance: dict[str, str],
    chosen_barcode: str | None,
    discogs_hit: DiscMeta | None,
    *,
    identified_release: str | None,
) -> tuple[RBIDisc, str | None, DiscMeta | None]:
    """Give Discogs a second look once a MusicBrainz fallback knows the release.

    Discogs is asked straight after the disc-ID lookup, by barcode. When that
    lookup found nothing (a disc-ID miss, or ``--digital-media``, which skips it)
    there is no barcode to search, so Discogs records ``no_barcode_to_search``
    and, under the archival rule, the rip aborts, although the ISRC tally or the
    duration match then identify the release, barcode included. This repeats the
    Discogs search and the MB-to-Discogs link check against that release, and
    recomputes ``lookup_status_discogs``. A no-op whenever the first look had a
    barcode, or no fallback identified anything. Returns the possibly updated
    ``(disc, chosen_barcode, discogs_hit)`` the resolver reads.
    """
    if provenance.get("discogs_barcode_outcome") != "no_barcode_to_search":
        return disc, chosen_barcode, discogs_hit
    if identified_release is None and not disc.barcode:
        return disc, chosen_barcode, discogs_hit
    from cdda2img import discogs_lookup as _discogs

    disc, chosen, hit = _prepopulate_from_discogs(
        disc, ui, barcode_hints=[], provenance=provenance
    )
    linked = _discogs_barcode_corroborate(
        disc, provenance, selected_release_id=identified_release
    )
    searched = int(provenance.get("discogs_barcode_matches", "0"))
    provenance["lookup_status_discogs"] = _r12_status(
        attempted=_discogs.is_available(),
        has_data=searched > 0 or linked,
        errored=False,
    )
    provenance["discogs_second_look"] = identified_release or "barcode"
    return disc, chosen or chosen_barcode, hit or discogs_hit


def _mb_prepop_alone(
    disc: RBIDisc,
    verbose: bool,
    preferred_country: list[str],
    *,
    digital_media: bool,
) -> MBPrepopResult:
    """The MusicBrainz prepop when no CDDB query runs beside it. Under
    ``--digital-media`` the disc-ID lookup is skipped: no index can hold it."""
    from cdda2img.mb_lookup import prepopulate_from_mb, prepopulate_without_disc_id

    if digital_media:
        return prepopulate_without_disc_id(disc, verbose=verbose)
    return prepopulate_from_mb(
        disc, verbose=verbose, preferred_country=preferred_country
    )


def _record_mb_identification(provenance: dict[str, str], mb_result) -> None:
    """PROV ``mb_identified_via`` for the disc-ID and ISRC-tally paths (rbi_spec).

    ``lookup_status_mb`` describes the disc-ID lookup alone, so a fallback's hit
    needs its own key; the stage-7 duration match sets it where it runs.
    """
    if mb_result.match_count > 0:
        provenance["mb_identified_via"] = "disc_id"
    elif mb_result.meta is not None:
        provenance["mb_identified_via"] = "isrc_tally"


#: The services whose answer a rip container needs, by the PROV key that records
#: it and the ``query_log`` name its queries are filed under. Cover art has its
#: own gate because it is fetched after the menu (:func:`_require_cover_art`).
_ARCHIVAL_SERVICES = (
    ("MusicBrainz", "mb_identified_via"),
    ("CDDB", "lookup_status_cddb"),
    ("Discogs", "lookup_status_discogs"),
    ("AcoustID", "lookup_status_acoustid"),
)


def _discogs_release_gap(value: str | None, provenance: dict[str, str]) -> str | None:
    """Why no single Discogs release was found, or ``None`` when one was."""
    if value is not None and value.isdigit():
        return None
    if value == "disabled":
        return "is not configured"
    if provenance.get("lookup_status_discogs") == "down":
        return "did not answer"
    return f"identified no single release ({value or 'never looked up'})"


def _service_gap(service: str, key: str, provenance: dict[str, str]) -> str | None:
    """Why *service* contributed nothing, or ``None`` when it did."""
    value = provenance.get(key)
    if key == "mb_identified_via":
        if value:
            return None
        status = provenance.get("lookup_status_mb", "absent")
        if status == "down":
            return f"did not answer ({provenance.get('lookup_error_mb', 'unknown')})"
        return "identified no release (disc ID, ISRC tally and duration match)"
    if key == "discogs_release":
        return _discogs_release_gap(value, provenance)
    if value == "OK":
        return None
    if value is None:
        return "was never queried"
    if value == "disabled":
        return "is not configured"
    return "did not answer" if value == "down" else f"answered nothing found ({value})"


def _require_service_answers(
    provenance: dict[str, str], *, archival: bool, digital_media: bool = False
) -> None:
    """Abort a rip when a service it needs contributed nothing (kgr, 2026-11-21).

    "A service that answers 'nothing found' has failed to provide data needed to
    construct the archive, so we need to abort." Judged per **service**, after its
    own fallbacks: MusicBrainz counts as answered when any of its three paths
    identified the release. Called after the metadata lookups and before the menu,
    for an *archival* pipeline (``rip``) only.

    Exit 2 (re-run unchanged) only when every service that came up empty also
    failed to answer at the network level (``query_log.errored``); several
    lookups return an empty result on a network error, so their own status
    cannot tell the two apart. Anything else is 3: "nothing found" will not change
    on a re-run, and the exact queries in the report are what to check, since
    "this would not be the first time we searched for the wrong thing".
    """
    from cdda2img import query_log

    if not archival:
        return
    # --digital-media asks no disc-ID database, and CDDB is one.
    services = [
        (service, key)
        for service, key in _ARCHIVAL_SERVICES
        if not (digital_media and service == "CDDB")
    ]
    gaps = [
        (service, why)
        for service, key in services
        if (why := _service_gap(service, key, provenance)) is not None
    ]
    if not gaps:
        return
    transient = all(
        "did not answer" in why or query_log.errored(service) for service, why in gaps
    )
    raise RipAborted(
        EXIT_RETRY_LATER if transient else EXIT_INVESTIGATE,
        f"{', '.join(s for s, _ in gaps)} provided no data for the archive",
        [
            f"{service} {why}; its queries and answers are in the report."
            for service, why in gaps
        ],
        provenance,
    )


class _DigitalRelease(NamedTuple):
    release_id: str
    linked_discogs_id: int | None  # MusicBrainz's Discogs link, None without one


def _identified_release(
    digital: _DigitalRelease | None,
    provenance: dict[str, str],
    mb_result: MBPrepopResult,
) -> str | None:
    """The release a MusicBrainz fallback identified, for Discogs' second look."""
    if digital is not None:
        return digital.release_id
    return provenance.get("duration_match_release") or (
        mb_result.meta.mb_release_id if mb_result.meta else None
    )


def _apply_release_identity(
    disc: RBIDisc,
    provenance: dict[str, str],
    digital: _DigitalRelease | None,
    discogs_hit: DiscMeta | None,
    selected_release_id: str | None,
) -> tuple[RBIDisc, DiscMeta | None]:
    """Resolve the single Discogs release, and merge it (with the digital
    release's MBID under --digital-media) into the legacy fold. The resolver gets
    the same meta as ``identity``. ``None`` when there is nothing to add.

    The MusicBrainz link is followed from the digital release, or on a pressed
    disc from the release matched by **disc ID** (``selected_release_id``): a
    fallback's release is not proven to be this pressing (C2), so its link would
    not be either.
    """
    from cdda2img.lookup_result import DiscMeta

    discogs_id = _discogs_release(
        disc,
        provenance,
        discogs_hit,
        link_release=None if digital else selected_release_id,
        linked_id=digital.linked_discogs_id if digital else None,
    )
    release_id = digital.release_id if digital else None
    if discogs_id is None and release_id is None:
        return disc, None
    identity = DiscMeta(mb_release_id=release_id, discogs_release_id=discogs_id)
    from cdda2img.mb_lookup import _merge_into_disc

    return _merge_into_disc(identity, disc), identity


def _identify_digital_release(
    provenance: dict[str, str], mb_result: MBPrepopResult, *, enabled: bool = True
) -> _DigitalRelease | None:
    """``--digital-media``: the digital-only release this disc was made from.

    Two MusicBrainz paths can name it, the ISRC tally (``tally_release_id``) and
    the duration match (``duration_match_release``); where both ran they must
    agree. MusicBrainz must then call every medium of it ``Digital Media``: the
    disc is a copy of a download, so a CD release of the same album is the wrong
    record. Writes PROV ``digital_release`` (the MBID, or why there is none:
    ``none``, ``conflict:<a>|<b>``, ``not_digital:<id>``, ``down:<id>``) and
    ``digital_release_format``. A Digital Media release is its own pressing, so
    its id is kept, where a fallback's id on a pressed disc is stripped (C2; see
    ``field_resolver.Source.DIGITAL_RELEASE``).
    """
    if not enabled:
        return None
    from cdda2img.mb_lookup import MBLookupError, release_identity

    votes = {
        v
        for v in (mb_result.tally_release_id, provenance.get("duration_match_release"))
        if v and v != "?"
    }
    if not votes:
        provenance["digital_release"] = "none"
        return None
    if len(votes) > 1:
        provenance["digital_release"] = "conflict:" + "|".join(sorted(votes))
        return None
    (release_id,) = votes
    try:
        identity = release_identity(release_id)
    except MBLookupError as exc:
        provenance["digital_release"] = f"down:{release_id}"
        provenance["digital_release_error"] = exc.reason
        return None
    provenance["digital_release_format"] = (
        "+".join(f or "?" for f in identity.formats) or "?"
    )
    if not identity.formats or any(f != "Digital Media" for f in identity.formats):
        provenance["digital_release"] = f"not_digital:{release_id}"
        return None
    provenance["digital_release"] = release_id
    return _DigitalRelease(release_id, identity.discogs_release_id)


def _discogs_release(
    disc: RBIDisc,
    provenance: dict[str, str],
    discogs_hit: DiscMeta | None,
    *,
    link_release: str | None = None,
    linked_id: int | None = None,
) -> int | None:
    """The single Discogs release this disc is, or ``None`` (kgr, 2026-11-21:
    "require a single Discogs release").

    A barcode search that found exactly one release naming this album
    (``discogs_hit``) settles it. Otherwise MusicBrainz's Discogs link is followed:
    a barcode is shared by many pressings (measured: Tracy Chapman 72, Eliminator
    23, ABBA *Gold* 121), and a digital release often has none on Discogs at all
    (Toca: 22970945, search empty). *linked_id* is the link when already known
    (the digital release's); else it is read from *link_release*. The linked
    record must name the same album. Only its id is taken, never its catalogue
    fields.

    Writes PROV ``discogs_release`` (the id, or why there is none:
    ``<barcode outcome>+no_link``, ``unfetched:<id>``, ``album_mismatch:<id>``,
    ``disabled``) and ``discogs_release_via`` (``barcode`` | ``mb_link``).
    """
    from cdda2img import discogs_lookup

    if not discogs_lookup.is_available():
        provenance["discogs_release"] = "disabled"
        return None
    if discogs_hit is not None and discogs_hit.discogs_release_id:
        provenance["discogs_release"] = str(discogs_hit.discogs_release_id)
        provenance["discogs_release_via"] = "barcode"
        if disc.barcode:
            provenance["discogs_release_barcode"] = disc.barcode
        return discogs_hit.discogs_release_id
    if linked_id is None and link_release:
        from cdda2img.mb_lookup import discogs_link_and_barcode

        linked_id, _mb_barcode = discogs_link_and_barcode(link_release)
    if linked_id is None:
        outcome = provenance.get("discogs_barcode_outcome", "not_searched")
        provenance["discogs_release"] = f"{outcome}+no_link"
        return None
    record = discogs_lookup.fetch_release(linked_id)
    if record is None:
        provenance["discogs_release"] = f"unfetched:{linked_id}"
        return None
    if not _albums_match(disc.album, record.album):
        provenance["discogs_release"] = f"album_mismatch:{linked_id}"
        return None
    provenance["discogs_release"] = str(linked_id)
    provenance["discogs_release_via"] = "mb_link"
    return linked_id


_MBID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _discogs_after_menu(
    disc: RBIDisc,
    provenance: dict[str, str],
    provisional: str | None,
    *,
    reopen: Callable[[RBIDisc, int | None], RBIDisc] | None = None,
) -> RBIDisc:
    """Settle the Discogs release for the pressing the menu settled on
    (trust_model_design.md §12). Returns the disc, which *reopen* may replace.

    - A release picked on the Discogs master screen
      (``discogs_release_via=master_versions``) or set from the menu's Discogs
      search (``menu``) is the user's and stands.
    - When the user picked another pressing, its MusicBrainz Discogs link is
      followed (ABBA *Gold*: 09cc6e86 and 159d12a8 share a disc ID and a barcode,
      ``mb_link_after_menu``); a release found by the provisional pressing's single
      barcode hit stands when the chosen pressing carries that barcode.
    - Otherwise *reopen* (an interactive rip) opens the Discogs master screen for
      the chosen pressing, its single barcode hit marked (§12 D6); unattended,
      that hit settles it (``barcode_after_menu``) and, failing that, the master
      MusicBrainz links the release group to is recorded instead of a release
      (``discogs_release=master_only:<id>``, §12 D7).

    Under ``--digital-media`` the screen lists the master's ``File`` versions,
    never its CDs (kgr, 2026-11-26: "anything else would be incorrect"); the
    reopener carries the format.
    """
    if provenance.get("discogs_release_via") == "master_versions":
        return disc
    auto = provenance.get("discogs_release")
    if disc.discogs_release_id is not None and auto != str(disc.discogs_release_id):
        provenance["discogs_release"] = str(disc.discogs_release_id)
        provenance["discogs_release_via"] = "menu"
        # Set by another route than the master screen: any master a screen pick
        # left behind belongs to that pick; _settle_discogs_master refills it.
        disc.discogs_master_id = None
        return disc
    chosen = disc.mb_release_id
    if chosen and chosen != provisional:
        return _discogs_for_new_pressing(disc, provenance, chosen, reopen)
    if disc.discogs_release_id is not None:
        return disc
    if reopen is not None:
        # The master screen was open in the menu (§12 D8) and nothing was taken.
        provenance["discogs_release"] = "not_chosen"
        return disc
    _record_discogs_master_only(disc, provenance)
    return disc


def _discogs_for_new_pressing(
    disc: RBIDisc,
    provenance: dict[str, str],
    chosen: str,
    reopen: Callable[[RBIDisc, int | None], RBIDisc] | None,
) -> RBIDisc:
    """The Discogs release for a pressing the user picked in the menu: its
    MusicBrainz link; or the provisional release, when it was found by the same
    barcode; or the master screen (interactive, its single barcode hit marked);
    or, unattended, that hit, else the master (:func:`_discogs_after_menu`)."""
    from cdda2img.mb_lookup import discogs_link_and_barcode

    linked, chosen_barcode = discogs_link_and_barcode(chosen)
    if (
        linked is None
        and provenance.get("discogs_release_via") == "barcode"
        and chosen_barcode
        and chosen_barcode == provenance.get("discogs_release_barcode")
    ):
        return disc
    provenance.pop("discogs_release_via", None)
    disc.discogs_release_id = None
    disc.discogs_master_id = None
    if linked is not None:
        disc.discogs_release_id = _discogs_release(
            disc, provenance, None, linked_id=linked
        )
        if disc.discogs_release_id is not None:
            provenance["discogs_release_via"] = "mb_link_after_menu"
        return disc
    # The chosen pressing's own barcode, as MusicBrainz holds it: never
    # disc.barcode, which still names the provisional pressing when the chosen
    # one has none.
    hit = _single_barcode_hit(disc, provenance, chosen_barcode)
    if reopen is not None:
        disc = reopen(disc, hit.discogs_release_id if hit else None)
        if provenance.get("discogs_release_via") != "master_versions":
            provenance["discogs_release"] = "not_chosen"
        return disc
    if hit is not None:
        disc.discogs_release_id = hit.discogs_release_id
        provenance["discogs_release"] = str(hit.discogs_release_id)
        provenance["discogs_release_via"] = "barcode_after_menu"
        return disc
    _record_discogs_master_only(disc, provenance)
    return disc


def _single_barcode_hit(
    disc: RBIDisc, provenance: dict[str, str], barcode: str | None
) -> DiscMeta | None:
    """The one Discogs release *barcode* finds naming the album, or ``None``.
    Records ``discogs_barcode_after_menu=<barcode>:<n>``."""
    from cdda2img import discogs_lookup
    from cdda2img.barcode import normalize_barcode

    if not barcode or not discogs_lookup.is_available():
        return None
    code = normalize_barcode(barcode, require_check_digit=False) or barcode
    hits = [
        h
        for h in discogs_lookup.search_by_barcode(code)
        if h.discogs_release_id and _albums_match(disc.album, h.album)
    ]
    provenance["discogs_barcode_after_menu"] = f"{code}:{len(hits)}"
    return hits[0] if len(hits) == 1 else None


def _record_discogs_master_only(disc: RBIDisc, provenance: dict[str, str]) -> None:
    """Unattended, with no single Discogs release: record the master MusicBrainz
    links the release group to (§12 D7). Discogs numbers masters and releases
    separately, so it goes in ``discogs_master_id`` and the release stays empty.
    """
    from cdda2img.mb_lookup import discogs_master_of_release_group

    group = disc.mb_release_group_id
    master = discogs_master_of_release_group(group) if group else None
    if master is None:
        provenance["discogs_release"] = "no_master"
        return
    disc.discogs_master_id = master
    provenance["discogs_release"] = f"master_only:{master}"


def _settle_discogs_master(disc: RBIDisc) -> None:
    """Fill ``discogs_master_id`` from the settled release when nothing set it:
    the release's own master, one Discogs request."""
    if disc.discogs_release_id is None or disc.discogs_master_id is not None:
        return
    from cdda2img import discogs_lookup

    record = discogs_lookup.fetch_release(disc.discogs_release_id)
    if record is not None:
        disc.discogs_master_id = record.discogs_master_id


def _discogs_reopener(
    *,
    archival: bool,
    auto: bool,
    tui: bool,
    provenance: dict[str, str],
    fmt: str = "CD",
    candidates: Sequence[DiscMeta] = (),
) -> Callable[[RBIDisc, int | None], RBIDisc] | None:
    """The post-menu Discogs master screen, for an interactive ``rip`` only:
    ``rip`` is the pipeline that requires a Discogs release, and a screen needs a
    terminal and no ``--auto``. ``import`` shares :func:`_finalize_import` and is
    never asked."""
    if not (archival and _interactive(auto)):
        return None
    from cdda2img.metadata_menu import run_discogs_menu

    def reopen(disc: RBIDisc, barcode_hit: int | None) -> RBIDisc:
        # The candidates let the screen hide versions that contradict the
        # pressing the user picked (§13.6): the Brothers in Arms path.
        return run_discogs_menu(
            disc,
            tui=tui,
            barcode_hit=barcode_hit,
            provenance=provenance,
            fmt=fmt,
            candidates=candidates,
        )

    return reopen


#: Pressings beyond which unattended pairing does not ask MusicBrainz for each one's
#: Discogs link (one request apiece, at its 1 a second). Pairing is then incomplete,
#: and an incomplete search cannot claim a unique pair.
_PAIR_LINK_MAX = 10


def _settle_identity_after_menu(
    disc: RBIDisc,
    provenance: dict[str, str],
    mb_result: MBPrepopResult,
    *,
    archival: bool,
    auto_apply: bool,
    tui: bool,
    discogs_fmt: str,
) -> RBIDisc:
    """Everything §12 and §13 of trust_model_design.md settle after the metadata
    menu, in order: the Discogs release (:func:`_discogs_after_menu`), then for a
    rip with several candidate pressings the pressing itself (unattended:
    :func:`_pair_unattended`; interactive: :func:`_pair_after_pick`), the Discogs
    master, the single-release gate, the disc's own year (asked for when nothing
    gives it, kgr 2026-11-26) and the identity gate. Returns the disc, which a
    reopened screen may replace."""
    candidates = list(mb_result.menu_candidates)
    reopen = _discogs_reopener(
        archival=archival,
        auto=auto_apply,
        tui=tui,
        provenance=provenance,
        fmt=discogs_fmt,
        candidates=candidates,
    )
    disc = _discogs_after_menu(
        disc, provenance, mb_result.selected_release_id, reopen=reopen
    )
    if archival and len(candidates) > 1:
        if reopen is None:
            disc = _pair_unattended(disc, provenance, candidates, discogs_fmt)
        elif provenance.get("release_selection") == "rejected":
            disc = _after_rejection(disc, provenance, candidates, reopen)
        else:
            disc = _pair_after_pick(disc, provenance, candidates, tui=tui)
    _settle_discogs_master(disc)
    _require_discogs_release(provenance, archival=archival)
    if archival:
        disc = _settle_release_year(disc, provenance, ask=reopen is not None)
        _require_identity_complete(disc, provenance)
    return disc


def _candidate(candidates: list[DiscMeta], mbid: str | None) -> DiscMeta | None:
    return next((c for c in candidates if mbid and c.mb_release_id == mbid), None)


def _describe_pressing(provenance: dict[str, str], meta: DiscMeta | None) -> None:
    """``release_disambiguation`` for the pressing the container now claims: its
    own MusicBrainz description, or none (the pinned pressing's would mislead)."""
    if meta is not None and meta.disambiguation:
        provenance["release_disambiguation"] = meta.disambiguation
    else:
        provenance.pop("release_disambiguation", None)


def _pair_unattended(
    disc: RBIDisc,
    provenance: dict[str, str],
    candidates: list[DiscMeta],
    fmt: str,
) -> RBIDisc:
    """§13.5, unattended with several candidate pressings: exactly one
    MusicBrainz/Discogs pair (a pressing's own Discogs link, or a sufficient field
    match against the master's versions) is taken; anything else records the
    release group only (kgr: "we have no way of corroborating the truth").
    PROV ``release_pairs`` counts the pairs found (``+`` = the search was cut
    short, so a single pair is not unique)."""
    from cdda2img import discogs_lookup, query_log
    from cdda2img import release_match as rm
    from cdda2img.mb_lookup import (
        discogs_link_and_barcode,
        discogs_master_of_release_group,
    )

    metas = {c.mb_release_id: c for c in candidates if c.mb_release_id}
    pairs: dict[tuple[str, int], str] = {}
    complete = len(metas) <= _PAIR_LINK_MAX
    if complete:
        start = query_log.mark()
        for mbid in metas:
            linked, _barcode = discogs_link_and_barcode(mbid)
            if linked is not None:
                pairs[(mbid, linked)] = "mb_link"
        # A failed lookup returns (None, None), the same as "no link": one lost to
        # a MusicBrainz 503 could hide the second pair that makes the first not
        # unique, so any failure here leaves the search incomplete.
        if query_log.errored_since("MusicBrainz", start):
            complete = False
    group = disc.mb_release_group_id
    master = disc.discogs_master_id or (
        discogs_master_of_release_group(group) if group else None
    )
    versions = discogs_lookup.master_versions(master, fmt=fmt) if master else None
    if master is not None and (versions is None or versions.truncated):
        complete = False
    if versions is not None:
        right = [rm.record_from_version(v) for v in versions.versions]
        left = [rm.record_from_meta(m) for m in metas.values()]
        for a, b, c in rm.matching_pairs(left, right):
            pairs.setdefault((a.ident, int(b.ident)), c.describe())
    provenance["release_pairs"] = f"{len(pairs)}{'' if complete else '+'}"
    if len(pairs) == 1 and complete:
        (mbid, discogs_id), evidence = next(iter(pairs.items()))
        return _take_pair(disc, provenance, metas[mbid], discogs_id, evidence)
    return _release_group_only(disc, provenance, candidates, master=master)


def _take_pair(
    disc: RBIDisc,
    provenance: dict[str, str],
    meta: DiscMeta,
    discogs_id: int,
    evidence: str,
) -> RBIDisc:
    """Record a unique pair: the pressing, the Discogs release, and each Discogs
    field the pressing lacks (§13.7; a field both state cannot differ, or the pair
    would not match). A barcode adopted this way never becomes the MCN."""
    from cdda2img import discogs_lookup
    from cdda2img.resolver_adapter import apply_menu_selection

    if meta.mb_release_id != disc.mb_release_id:
        disc = apply_menu_selection(disc, meta, overwrite=True)
    details = discogs_lookup.release_details(discogs_id)
    disc.discogs_release_id = discogs_id
    disc.discogs_master_id = details.master_id if details else None
    adopted = []
    for name, value in (details.catalogue_fields() if details else {}).items():
        if not getattr(disc, name):
            setattr(disc, name, value)
            adopted.append(name)
    provenance["release_selection"] = "paired"
    _describe_pressing(provenance, meta)
    provenance["release_pair"] = f"{meta.mb_release_id}|{discogs_id}:{evidence}"
    provenance["discogs_release"] = str(discogs_id)
    provenance["discogs_release_via"] = "paired"
    if adopted:
        provenance["release_adopted"] = ",".join(f"{n}:discogs" for n in adopted)
    if "barcode" in adopted:
        provenance["barcode_source"] = "adopted"
    return disc


#: The pressing-level fields a release-group-only record keeps only when every
#: candidate pressing states them alike (§13.5), with the key they are compared by.
def _pressing_field_keys() -> dict[str, Callable[[str], object]]:
    from cdda2img import release_match as rm

    return {
        "catalog_number": lambda v: rm.norm_catnos([v]),
        "label": lambda v: rm.norm_labels([v]),
        "country": rm.norm_country,
        "barcode": lambda v: rm.norm_barcodes([v]),
        "release_date": rm.norm_year,
    }


def _release_group_only(
    disc: RBIDisc,
    provenance: dict[str, str],
    candidates: list[DiscMeta],
    *,
    master: int | None,
    keep: frozenset[str] = frozenset(),
) -> RBIDisc:
    """No pressing could be settled: drop ``mb_release_id``, and every
    pressing-level field not stated alike by all candidates, except those in
    *keep* (fields a release the user picked stated). Without *keep* the Discogs
    side is the master only (§12 D7)."""
    from cdda2img import release_match as rm

    disc.mb_release_id = None
    # The description was the pinned pressing's; the container claims none now.
    provenance.pop("release_disambiguation", None)
    for name, key in _pressing_field_keys().items():
        if name in keep:
            continue
        value = rm.unanimous(
            [getattr(c, name) for c in candidates], key, require_all=True
        )
        if name == "release_date" and value:
            value = rm.norm_year(value)  # the pressings agree on the year only
        setattr(disc, name, value)
    provenance["release_selection"] = "release_group_only"
    if not keep:
        disc.discogs_release_id = None
        disc.discogs_master_id = master
        provenance["discogs_release"] = (
            f"master_only:{master}" if master else "no_master"
        )
        provenance.pop("discogs_release_via", None)
    if disc.barcode and "barcode" not in keep:
        provenance["barcode_source"] = "unanimous"
    return disc


def _pair_after_pick(
    disc: RBIDisc,
    provenance: dict[str, str],
    candidates: list[DiscMeta],
    *,
    tui: bool,
) -> RBIDisc:
    """§13.6, interactive: when the user picked the Discogs release on the master
    screen, settle the pressing from it. A pressing the user also picked is only
    checked against it (PROV ``release_pair``; a contradiction is printed). Else
    the reverse link (MusicBrainz releases linking that Discogs release), then a
    unique sufficient field match, derive it (``release_selection=derived``); else
    the user is asked among the pressings that do not contradict it, and a user
    who picks none gets the release group only, keeping the Discogs release."""
    from cdda2img import discogs_lookup
    from cdda2img import release_match as rm
    from cdda2img.mb_lookup import mb_releases_linking_discogs
    from cdda2img.metadata_menu import run_pressing_menu

    discogs_id = disc.discogs_release_id
    if provenance.get("discogs_release_via") != "master_versions" or discogs_id is None:
        return disc
    details = discogs_lookup.release_details(discogs_id)
    record = rm.record_from_details(details) if details else None
    if provenance.get("release_selection") in ("manual", "accepted"):
        pressing = _candidate(candidates, disc.mb_release_id)
        if pressing is not None and record is not None:
            c = rm.compare(rm.record_from_meta(pressing), record)
            provenance["release_pair"] = (
                f"{pressing.mb_release_id}|{discogs_id}:{c.describe()}"
            )
            if c.contradicted:
                print(
                    f"   ! The pressing and the Discogs release you picked disagree"
                    f" on {', '.join(c.contradicted)}."
                )
        return disc
    ids = {c.mb_release_id for c in candidates}
    hits = [m for m in (mb_releases_linking_discogs(discogs_id) or []) if m in ids]
    evidence = "reverse_link"
    if len(hits) != 1 and record is not None:
        hits = [
            c.mb_release_id
            for c in candidates
            if c.mb_release_id and rm.compare(rm.record_from_meta(c), record).sufficient
        ]
        evidence = "fields"
    if len(hits) == 1:
        meta = _candidate(candidates, hits[0])
        disc = _apply_pressing_under_discogs(disc, meta, details)
        provenance["release_selection"] = "derived"
        _describe_pressing(provenance, meta)
        provenance["release_pair"] = f"{hits[0]}|{discogs_id}:{evidence}"
        return disc
    compatible = [
        c
        for c in candidates
        if record is None or rm.compare(rm.record_from_meta(c), record).compatible
    ] or candidates
    disc = run_pressing_menu(disc, compatible, tui=tui, provenance=provenance)
    if provenance.get("release_selection") in ("manual", "accepted"):
        return _apply_pressing_under_discogs(disc, None, details)
    stated = frozenset(details.catalogue_fields()) if details else frozenset()
    return _release_group_only(
        disc, provenance, candidates, master=disc.discogs_master_id, keep=stated
    )


def _after_rejection(
    disc: RBIDisc,
    provenance: dict[str, str],
    candidates: list[DiscMeta],
    reopen: Callable[[RBIDisc, int | None], RBIDisc],
) -> RBIDisc:
    """The user answered "none of these" on the pressing screen (kgr, 2026-11-26:
    the negative answer must be on offer, and taken when given). The album is
    recorded without a pressing; no pressing is derived from a Discogs pick, since
    the user has said none of MusicBrainz's is theirs. A Discogs release the user
    picked on the master screen stands; one that came from the rejected
    pressing's link does not, and the master screen reopens for the user to pick
    (leaving it without one aborts, ``not_chosen``). ``release_selection`` stays
    ``rejected``."""
    picked = provenance.get("discogs_release_via") == "master_versions"
    stated: frozenset[str] = frozenset()
    if picked and disc.discogs_release_id is not None:
        from cdda2img import discogs_lookup

        details = discogs_lookup.release_details(disc.discogs_release_id)
        stated = frozenset(details.catalogue_fields()) if details else frozenset()
    disc = _release_group_only(
        disc, provenance, candidates, master=disc.discogs_master_id, keep=stated
    )
    provenance["release_selection"] = "rejected"
    if picked:
        return disc
    disc.discogs_release_id = None
    provenance.pop("discogs_release_via", None)
    disc = reopen(disc, None)
    if provenance.get("discogs_release_via") != "master_versions":
        provenance["discogs_release"] = "not_chosen"
    return disc


def _apply_pressing_under_discogs(
    disc: RBIDisc, meta: DiscMeta | None, details: object
) -> RBIDisc:
    """Apply pressing *meta* (when given), then lay the Discogs release the user
    picked back on top: its stated fields win (§12.5)."""
    from cdda2img.resolver_adapter import apply_menu_selection

    discogs_id, master = disc.discogs_release_id, disc.discogs_master_id
    if meta is not None:
        disc = apply_menu_selection(disc, meta, overwrite=True)
    fields = details.catalogue_fields() if details is not None else {}  # type: ignore[attr-defined]
    return replace(
        disc, discogs_release_id=discogs_id, discogs_master_id=master, **fields
    )


def _settle_release_year(
    disc: RBIDisc,
    provenance: dict[str, str],
    *,
    ask: bool,
    input_fn: Callable[[str], str] = input,
) -> RBIDisc:
    """The disc's own release year, when neither service gave one: the user is
    asked (kgr, 2026-11-26). Unattended there is nobody to ask, and
    :func:`_require_identity_complete` aborts. PROV ``release_date_source=user``."""
    import datetime

    from cdda2img.rbi_format import year_of

    if year_of(disc.release_date) is not None or not ask:
        return disc
    latest = datetime.date.today().year + 1
    print("   Neither MusicBrainz nor Discogs gives this disc's own release year.")
    while True:
        try:
            answer = input_fn(
                "   The year on the disc or its packaging (YYYY), Enter to abort: "
            ).strip()
        except EOFError:
            return disc
        if not answer:
            return disc
        if answer.isdigit() and len(answer) == 4 and 1982 <= int(answer) <= latest:
            provenance["release_date_source"] = "user"
            return replace(disc, release_date=answer)
        print(f"   A CD's year is between 1982 and {latest}.")


def _require_identity_complete(disc: RBIDisc, provenance: dict[str, str]) -> None:
    """A rip writes nothing without the disc's own year and a complete original
    release, title and year (kgr, 2026-11-26: "abort rather than have the
    'original' PROV blank or incomplete")."""
    from cdda2img import query_log
    from cdda2img.rbi_format import year_of

    details = []
    year_missing = year_of(disc.release_date) is None
    if year_missing:
        details.append(
            "Neither MusicBrainz nor Discogs gives this disc's own release year, and"
            " it was not entered (an unattended run cannot ask for it)."
        )
    if not (
        disc.original_release_found
        and disc.original_release_title
        and disc.original_release_year
    ):
        why = provenance.get("original_release_rejected")
        details.append(
            "The original release could not be determined"
            + (f" (MusicBrainz's answer was rejected: {why})" if why else "")
            + "; the menu's [r] can set it."
        )
    if not details:
        return
    # Only a missing original release can be an outage (its lookups failed); a
    # missing year is the data, and re-running unchanged will not supply it.
    outage = not year_missing and (
        query_log.errored("MusicBrainz") or query_log.errored("Discogs")
    )
    raise RipAborted(
        EXIT_RETRY_LATER if outage else EXIT_INVESTIGATE,
        "the release's identity is incomplete",
        details,
        provenance,
    )


def _require_discogs_release(provenance: dict[str, str], *, archival: bool) -> None:
    """After the menu, a rip must name exactly one Discogs release (kgr,
    2026-11-21: "require a single Discogs release").

    Not before the menu: a pressing the user picks there, or a Discogs release
    they set, can supply what the provisional pick lacked. Before it,
    :func:`_require_service_answers` asks only that Discogs answered.
    """
    from cdda2img import query_log

    if not archival:
        return
    if provenance.get("discogs_release", "").startswith("master_only:"):
        # Unattended (--auto, no terminal): the master stands in for the release
        # (trust_model_design.md §12 D7, kgr 2026-11-26). Only the unattended path
        # writes it; an interactive rip still needs a release.
        return
    why = _service_gap("Discogs", "discogs_release", provenance)
    if why is None:
        return
    raise RipAborted(
        EXIT_RETRY_LATER
        if "did not answer" in why or query_log.errored("Discogs")
        else EXIT_INVESTIGATE,
        "Discogs provided no data for the archive",
        [f"Discogs {why}; its queries and answers are in the report."],
        provenance,
    )


def _require_digital_identity(provenance: dict[str, str]) -> None:
    """``--digital-media``: abort unless the digital release was identified on
    MusicBrainz (kgr, 2026-11-21: "If the data corresponding to the media type is
    not available, then abort"). Its Discogs release is required for every rip,
    by :func:`_require_service_answers`.

    The disc-ID databases (AccurateRip, CDDB, CTDB, the MusicBrainz disc ID) are
    the wrong lookups for a digital release and are not asked, so they cannot
    abort it; this record is what it has instead. Exit 2 when MusicBrainz did not
    answer, else 3.
    """
    release = provenance.get("digital_release", "never identified")
    if _MBID_RE.fullmatch(release):
        return
    raise RipAborted(
        EXIT_RETRY_LATER if release.startswith("down:") else EXIT_INVESTIGATE,
        "the digital release this disc was made from could not be identified",
        [
            f"MusicBrainz identified no Digital Media release ({release});"
            " its queries and answers are in the report."
        ],
        provenance,
    )


def _archival_gates(
    disc: RBIDisc,
    pcm_file: Path,
    provenance: dict[str, str],
    ui: TerminalUI | None,
    *,
    archival: bool,
    digital_media: bool,
) -> None:
    """The pre-menu gates of an archival run: every service answered, and under
    ``--digital-media`` every track fingerprints to the identified release."""
    _require_service_answers(provenance, archival=archival, digital_media=digital_media)
    if archival and digital_media:
        _require_digital_identity(provenance)
        _require_acoustid_identity(disc, pcm_file, provenance, ui)


def _require_acoustid_identity(
    disc: RBIDisc, pcm_file: Path, provenance: dict[str, str], ui: TerminalUI | None
) -> None:
    """Under ``--digital-media``, every track's fingerprint must match the release.

    With no disc ID the identification rests on CD-Text, the ISRC tally or the
    duration match; AcoustID is the only evidence tying the audio itself to that
    release (kgr, 2026-11-21: a digital release "still has a fingerprint, a
    MusicBrainz entry, cover art ... everything but a disc-id"). A track matches
    when one of its fingerprint's recordings appears on the identified release
    group. Measured on Toca (20th Anniversary Edition): tracks 1 and 2 both list
    it among 211 and 87 candidate releases. Writes ``acoustid_tracks_matched``;
    any unmatched track aborts (exit 3, the per-track answers in the report).
    """
    import tempfile
    import wave

    from cdda2img import acoustid_lookup

    group = disc.mb_release_group_id
    unmatched: list[int] = []
    _ui_status(ui, "Digital media: fingerprinting every track…")
    with tempfile.TemporaryDirectory(dir=pcm_file.parent) as td:
        offset = 0
        for entry in disc.tracks:
            offset += entry.pregap_frames * _R6_BYTES_PER_FRAME
            size = entry.duration_frames * _R6_BYTES_PER_FRAME
            with open(pcm_file, "rb") as f:
                f.seek(offset)
                pcm = f.read(size)
            offset += size
            wav = Path(td) / f"track-{entry.track_number:02d}.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(pcm)
            hits = acoustid_lookup.fingerprint_and_lookup(wav)
            if not group or not any(h.mb_release_group_id == group for h in hits):
                unmatched.append(entry.track_number)
    n = len(disc.tracks)
    provenance["acoustid_tracks_matched"] = f"{n - len(unmatched)}/{n}"
    if unmatched:
        raise RipAborted(
            EXIT_INVESTIGATE,
            f"{len(unmatched)} of {n} track(s) did not fingerprint to the identified"
            " release",
            [
                f"Release group: {group or 'none identified'}.",
                f"Unmatched tracks: {', '.join(map(str, unmatched))}; every"
                " fingerprint query and its candidates are in the report.",
            ],
            provenance,
        )


def _require_cover_art(provenance: dict[str, str], *, archival: bool) -> None:
    """Abort a rip whose cover art could not be fetched: the ART block is part of
    the container (kgr, 2026-11-21). Same exit rule as
    :func:`_require_service_answers`."""
    from cdda2img import query_log

    if not archival or provenance.get("lookup_status_art") == "OK":
        return
    raise RipAborted(
        EXIT_RETRY_LATER
        if query_log.errored("Cover Art Archive")
        else EXIT_INVESTIGATE,
        "no cover art was found for the release",
        [
            "Every art URL tried and its answer are in the report; check the release"
            " and release-group IDs they were built from."
        ],
        provenance,
    )


def _total_miss_message(provenance: dict[str, str]) -> tuple[str, list[str]]:
    """``(reason, details)`` for a rip aborted on a total AccurateRip miss.

    The details carry the offset search's diagnosis, which with no container
    written survives only here and in the failure report. A confirmed candidate
    offset is named with what to do about it, but **not** as a verdict: a disc
    AccurateRip holds in several pressings verifies at several offsets, so the
    candidate is either a wrong drive offset or a pressing held only at that
    offset.
    """
    reason = "AccurateRip holds this disc and none of its tracks verified"
    details: list[str] = []
    miss = provenance.get("ar_total_miss")
    candidates = provenance.get("ar_offset_candidates")
    if miss == "offset_mismatch" and candidates:
        first = candidates.split(",")[0]
        delta = provenance.get("ar_offset_suggests", "?")
        details.append(
            f"A whole-track checksum matches at read offset {first}"
            f" (candidates: {candidates}; {delta} from the offset used). Either this"
            " drive's read_offset is wrong, so set it in its [[drives]] entry and"
            " re-rip, or AccurateRip holds this pressing only at that offset."
        )
    elif miss == "no_offset_verifies":
        details.append(
            "It verifies at no read offset within AccurateRip's search radius: the"
            " audio differs from every submitted copy (damage, a misread, or a"
            " pressing AccurateRip does not hold)."
        )
    elif miss == "offset_probe_failed":
        details.append("The offset search itself failed, so no cause could be named.")
    return reason, details


def _interactive(auto: bool) -> bool:
    """A prompt can be answered: a TTY, and not ``--auto``. Same rule as the pre-flight."""
    import sys

    return sys.stdin.isatty() and not auto


def _offer_service_retry(
    ui: TerminalUI | None,
    service: str,
    detail: str,
    *,
    interactive: bool,
    input_fn: Callable[[str], str] = input,
) -> bool:
    """Ask whether to try *service* again after it failed mid-run. True = retry.

    Unlike the pre-flight there is no abort: by the time this is asked the disc has
    been read or the image converted, and continuing costs nothing that re-running
    would not. Enter retries, since the likely reason for waiting at a prompt is to
    fix the network first. Continuing writes a container that records the service
    as ``down`` and makes the run exit with status 3. Never asked when *interactive*
    is false (no TTY, or ``--auto``): an unattended run continues.
    """
    if not interactive:
        return False
    if ui is not None:
        ui.pause()
    try:
        print(f"   {service} did not answer ({detail}).")
        while True:
            try:
                answer = input_fn(
                    f"   [R]etry {service}, or [c]ontinue without it"
                    " (exit status 3)? [R/c] "
                )
            except EOFError:
                return False
            answer = answer.strip().lower()
            if answer in ("", "r", "retry"):
                return True
            if answer in ("c", "continue"):
                return False
    finally:
        if ui is not None:
            ui.resume()


def _verify_ar_with_retry(
    verify: Callable[[], ARVerifyResult],
    ui: TerminalUI | None,
    *,
    interactive: bool,
) -> ARVerifyResult:
    """Run *verify*; while AccurateRip gives no answer, offer to try again.

    Only the first verification of a rip goes through here. The later ones (after a
    CTDB repair or a re-read) run only when this one reached AccurateRip, so a
    second prompt there would be for a service that just answered.
    """
    result = verify()
    while not result.reachable and _offer_service_retry(
        ui, "AccurateRip", "no response", interactive=interactive
    ):
        _ui_status(ui, "Retrying AccurateRip…")
        result = verify()
    return result


def _warn_ar_total_miss(ui: TerminalUI | None) -> None:
    """Say that no repair ran on a whole-disc miss, and why, before the offset search.

    Both repair exits are gated on a
    PARTIAL mismatch, so a total miss skips them by design; until 2026-09-24 it
    skipped them silently, and the offset search that follows ran with no output
    either. Printed first so it covers that wait, however long it turns out to be.
    """
    import sys

    if ui is not None:
        ui.pause()
    print(
        "   No track verified, so no repair was attempted: re-reading cannot fix a"
        " whole disc\n"
        "   that is wrong everywhere (a wrong drive offset, a pressing AccurateRip"
        " does not\n"
        "   hold, or a bad burn). Searching for an offset that does verify…",
        file=sys.stderr,
    )
    if ui is not None:
        ui.resume()


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
    Since 2026-11-21 the rip is aborted and nothing is written, so these keys
    reach the user only through :func:`_total_miss_message` on stderr.
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

    # Only a CONFIRMED match (a whole-track checksum agreed) is evidence of an
    # offset. `detect_offset` always appends offset 0 "for reference" and ranks
    # frame-450 probe hits, so an unfiltered list is never empty for a disc in the
    # database, and its head can match nothing at all. Until 2026-09-14 that
    # unconfirmed head was reported as `offset_mismatch`: the LITE-ON LH-20A1S
    # rip, misframed by libata's PIO fallback and verifying 0/11, was sealed with
    # `ar_offset_candidates=0` and `ar_offset_suggests=-6` on a match with no
    # checksum agreeing, which sent the user after the one parameter that was
    # correct.
    matches = [m for m in matches if m.confirmed]
    if not matches:
        # In the database, verifies at no offset in the swept radius. That is a
        # real result: the audio differs from every submitted copy, which is
        # damage, a misread or a different pressing — not a misconfiguration.
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


class _RawWindow(NamedTuple):
    """The raw sectors one track's offset-corrected audio is cut from.

    Shared by both re-read rungs, because they must agree to the byte: the speed
    ladder reads the whole window, the span rung rebuilds it from the PCM file and
    overlays re-read sectors, and both then slice the same corrected track out of
    it. ``first`` is the unclamped window start (``track_start - lead``); ``lo``/
    ``hi`` are that window clamped to the disc, and the difference is zero-padded.
    """

    first: int  # unclamped window start
    last: int  # unclamped window end (exclusive)
    lo: int  # clamped to the disc
    hi: int
    lead: int  # margin sectors before the track
    track_bytes: int


def _raw_track_window(
    track_lsns: list[int],
    disc_last_lsn: int,
    idx: int,
    read_offset: int,
    margin: int = 0,
) -> _RawWindow:
    """Track ``idx`` (0-based) plus ``ceil(|read_offset| / 588)`` margin sectors on
    the side the offset points to, clamped to ``[0, lead-out)``.

    *margin* adds that many sectors on **both** sides. It defaults to 0, which is
    the window both re-read rungs have always used and the span rung still does;
    the speed ladder passes :data:`_LADDER_MARGIN` so a re-read that came back
    displaced can be recognised (:func:`_ladder_attempt`)."""
    from math import ceil

    leadout = disc_last_lsn + 1
    s = track_lsns[idx]
    e = track_lsns[idx + 1] if idx + 1 < len(track_lsns) else leadout
    lead = margin + (ceil(-read_offset / 588) if read_offset < 0 else 0)
    tail = margin + (ceil(read_offset / 588) if read_offset > 0 else 0)
    return _RawWindow(
        first=s - lead,
        last=e + tail,
        lo=max(0, s - lead),
        hi=min(leadout, e + tail),
        lead=lead,
        track_bytes=(e - s) * _R6_BYTES_PER_FRAME,
    )


def _corrected_from_window(window: bytes, w: _RawWindow, read_offset: int) -> bytes:
    """Offset-correct the raw bytes of ``[w.lo, w.hi)`` into the track's audio.

    Where the window crossed a disc edge it is zero-padded back to its unclamped
    extent; the pad lands inside AccurateRip's first/last 2940-sample exclusion
    zone, the same invariant accuraterip.py relies on.
    """
    pad_front = (w.lo - w.first) * _R6_BYTES_PER_FRAME
    pad_back = (w.last - w.hi) * _R6_BYTES_PER_FRAME
    if pad_front or pad_back:
        window = bytes(pad_front) + window + bytes(pad_back)
    base = w.lead * _R6_BYTES_PER_FRAME + read_offset * 4
    corrected = window[base : base + w.track_bytes]
    if len(corrected) < w.track_bytes:
        msg = f"short window read: {len(corrected)} of {w.track_bytes} bytes"
        raise RuntimeError(msg)
    return corrected


def _splice_corrected(
    pcm_fh: BinaryIO,
    corrected: bytes,
    track_lsn: int,
    read_offset: int,
    file_size: int,
) -> None:
    """Write VERIFIED corrected bytes back at their raw-file position.

    Clamped to the file: samples beyond a disc edge were zero-pad inside AR's
    exclusion zone and have no file position. Callers invoke this only after
    ``match_track_pcm`` has matched *corrected*; it is the one write either
    re-read rung makes.
    """
    dst_lo = track_lsn * _R6_BYTES_PER_FRAME + read_offset * 4
    src = corrected
    if dst_lo < 0:
        src = src[-dst_lo:]
        dst_lo = 0
    if dst_lo + len(src) > file_size:
        src = src[: file_size - dst_lo]
    pcm_fh.seek(dst_lo)
    pcm_fh.write(src)


#: Sectors read either side of a track on a speed-ladder re-read, so a re-read
#: that came back displaced still holds the whole track. Five sectors is 2940
#: samples, one more than the displacement search radius.
_LADDER_MARGIN = 5


def _ladder_attempt(
    device: str,
    track_lsns: list[int],
    disc_last_lsn: int,
    idx: int,
    read_offset: int,
    speed: int,
    prog_cb: Callable[[int, int], None] | None,
    n_tracks: int,
    responses: list,
    cohort: list | None,
    c2: bool = False,
    readable: tuple[int, int] | None = None,
) -> tuple[tuple[bytes, str] | None, str, int | None]:
    """One targeted re-read of track ``idx`` (0-based) at *speed*.

    Returns ``(hit, attempt, c2_flagged)``; *c2_flagged* is the sectors a C2
    pointer fired in when *c2* asked for them (``recovery_speed = "auto"``), else
    ``None``.

    Returns ``(hit, attempt)``. *hit* is ``(corrected, outcome)`` when the re-read
    yields the track's AccurateRip-matching audio, else ``None``; *corrected* is
    offset-corrected and ready for :func:`_splice_corrected`. *attempt* is this
    read's entry in ``recovery_attempts_track_<n>`` (:func:`_attempt_record`),
    returned on a miss as well as a match. Geometry in :func:`_raw_track_window`.

    With a *cohort* (``displacement.cohort_blocks``) a re-read that does not match
    in place is also tested for a displaced copy of the track, and the outcome
    then says so: ``matched@40X,displaced@+12``. That record is the point as much
    as the repair. On 2026-10-10 seven tracks failed after a pass that had lost 12
    samples and each was recovered by a re-read at 4X, and nothing kept what the
    re-reads held, so whether the drive stays displaced across a seek could not be
    answered afterwards. ``matched@<N>X`` names the speed that matched, as
    requested; *attempt* records what every attempt asked for, what the drive ran
    at, and whether the cache defeat ran (kgr, 2026-10-25), so the walk itself is
    in PROV and not only its configuration.

    **Every attempt asks AccuDisc for a cache defeat** (kgr, 2026-10-22): one
    sector read 5000 away before the first transfer, so the attempt is not
    answered from what the previous one left in the drive's cache. A whole-track
    window starts away from where the last read ended, so the start misses the
    cache anyway. What the defeat protects is a short track re-read back to back:
    on the LITE-ON, spans of 49 to 196 sectors came back cached 15/15 with no read
    between (AccuDisc 2026-09-18s). A cached attempt can only repeat a miss,
    because AccurateRip gates the splice, so the option costs one seek and buys
    attempts that are really attempts. Measured on the LITE-ON only, not on the
    PX-716A; an engine without the option reads as before (the seam
    feature-detects it).
    """
    from cdda2img import displacement
    from cdda2img.accudisc_reader import read_span_attempt
    from cdda2img.accuraterip import match_track_pcm

    w = _raw_track_window(
        track_lsns,
        disc_last_lsn,
        idx,
        read_offset,
        margin=_LADDER_MARGIN if cohort else 0,
    )
    # Before the read, so a spent budget stops recovery without sending it.
    drive_work.charge(speed, far_read=True)
    read = read_span_attempt(
        device,
        w.lo,
        w.hi - w.lo,
        read_speed=speed,
        progress_cb=prog_cb,
        cache_defeat=True,
        c2=c2,
        readable=readable,
    )
    attempt = _attempt_record(speed, read.speed_honoured_x, read.cache_defeat)
    window = read.pcm
    corrected = _corrected_from_window(window, w, read_offset)
    _v1, _v2, conf_v1, conf_v2 = match_track_pcm(
        corrected, idx + 1, n_tracks, responses
    )
    if conf_v1 or conf_v2:
        return (corrected, f"matched@{speed}X"), attempt, read.c2_flagged
    if not cohort:
        return None, attempt, read.c2_flagged
    pad_front = (w.lo - w.first) * 588  # samples the window lacks at a disc edge
    pad_back = (w.last - w.hi) * 588
    found = displacement.displaced_in_window(
        bytes(pad_front * 4) + window + bytes(pad_back * 4),
        w.lead * 588 + read_offset,
        w.track_bytes // 4,
        (pad_front, pad_front + len(window) // 4),
        idx + 1,
        n_tracks,
        cohort,
    )
    if found is None:
        return None, attempt, read.c2_flagged
    shift, data = found
    return (
        (data, f"matched@{speed}X,{displacement.outcome(shift)}"),
        attempt,
        read.c2_flagged,
    )


def _attempt_record(speed: int, honoured: int | None, cache_defeat: str) -> str:
    """One entry of ``recovery_attempts_track_<n>`` (rbi_spec §6.3):
    ``<requested>><honoured>:<cache_defeat>``, e.g. ``32>24:done``.

    ``?`` stands for an honoured speed nobody reported, never ``0``: a zero reads
    as a speed somebody measured. A read that raised has no entry of this shape;
    the loop writes ``<requested>:error`` for it.
    """
    return f"{speed}>{honoured if honoured else '?'}:{cache_defeat}"


def _track_sector_window(
    track_lsns: list[int], idx: int, disc_last_lsn: int
) -> tuple[int, int]:
    """``[start, end)`` sectors of track *idx* (0-based), for the recovery map.

    The last track runs to the lead-out, so its end comes from *disc_last_lsn*
    rather than a following entry. Module level rather than a closure only
    because the loop it came from sits on the C901 limit.
    """
    lo = track_lsns[idx]
    hi = track_lsns[idx + 1] if idx + 1 < len(track_lsns) else disc_last_lsn + 1
    return lo, hi


class _RecoveryMap:
    """The whole-disc map kept on screen while the AR ladder repairs one track.

    ``progress-map-plan.md`` §2: the user should see a repair *in context* rather
    than a bar that restarts per attempt. So the rip's own damage map stays up,
    with the track under repair drawn as ``REREADING`` and everything either side
    of it still showing what the first pass found.

    Holds a mutable **copy** of the rip's damage map. This is the one place
    something other than the reader writes to a map, and copying keeps the rip's
    own record of what the drive reported intact for the CTDB report drawn later.

    Inert (every method a no-op) without a TUI or without a captured map, so the
    caller needs no branches — which is the other reason this is a class: the
    loop was at C901 14 with the branches inline.
    """

    def __init__(
        self, ui: TerminalUI | None, disc_damage: bytes | None, status_width: int
    ) -> None:
        self._ui = ui if disc_damage else None
        self._buf = bytearray(disc_damage) if disc_damage else None
        self._sw = status_width

    def arm(self, lo: int, hi: int) -> None:
        """Make ``[lo, hi)`` the live region. Re-armed per track."""
        if self._ui is None or self._buf is None:
            return
        # `status_width` is the widest banner across ALL tracks, not this one:
        # map geometry is pinned at the first frame, so a later two-digit track
        # number would be truncated for the rest of the ladder. Same trap the Q
        # lane hit in review.
        self._ui.set_map(self._buf, status_width=self._sw, active=(lo, hi))

    def clear(self, lo: int, hi: int) -> None:
        """Mark ``[lo, hi)`` undamaged — call only on an AccurateRip match.

        Last-write-wins over whatever earlier attempts left (§2's proposal), and
        here the last write is an AR match: positive evidence that the track's
        audio is now correct, whatever C2 said during the first pass. So the
        clear is a claim we can defend rather than "assume the reread was clean".

        A track that never matches keeps its damage, deliberately — the ladder
        exhausted every pass x speed and the original audio was kept, so drawing
        it clean would assert a repair that did not happen.
        """
        if self._buf is not None:
            self._buf[lo:hi] = bytes(hi - lo)

    def release(self) -> None:
        """Drop the renderer's reference once the ladder is over.

        A map with a live region and no reader behind it reads as a stalled
        repair — the same reason the first-pass map is released in a ``finally``.
        """
        if self._ui is not None:
            self._ui.set_map(None)


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
    disc_damage: bytes | None = None,
    cohort: list | None = None,
    c2: bool = False,
    readable: tuple[int, int] | None = None,
) -> tuple[dict[int, str], dict[str, str]]:
    """Re-read each AR-failed track across the drive's speed ladder until it matches.

    *readable* bounds every far read of every attempt to the session being read
    (:func:`_rip_disc_stage`).

    *c2* asks every re-read for C2 pointers and records how many sectors each
    flagged as ``recovery_c2_track_<n>`` (``40X:12,32X:3``), which is what
    ``recovery_speed = "auto"`` chooses its rung from (2026-11-21).

    For every failed track, walk *ladder* in the order given, ``n_passes`` times (total
    ``n_passes x len(ladder)`` attempts), re-reading the track's raw sector window
    through AccuDisc at each speed, AR-verifying the offset-corrected slice against the
    cached *responses*, and splicing the first match into the RAW *pcm_file*.

    **The order is decided where the ladder is bound, never here**:
    :func:`~cdda2img.recovery_profile.bind_ladder` returns the admitted ladder fastest
    first (kgr, 2026-10-22: slow reads were not necessarily more successful, so start
    fast), and ``--ad-ladder`` arrives as written. Until 2026-10-22 this loop reversed
    the list, a leftover from the ascending ``probe_speed_ladder`` it was written
    against. Once ``e5c37ff`` (2026-07-25) bound a fastest-first ladder instead, every
    rip walked slowest-first while PROV ``recovery_ladder`` still listed the speeds
    fastest first. Two components each applying an order to the same list is how that
    happened, so this loop applies none.

    The splice is sample-exact — the verified corrected bytes land at
    ``track_start*2352 + read_offset*4`` — so a neighbouring track's already-verified
    audio is never perturbed. A track that never matches keeps its original audio (no
    unverified splice). *pcm_file* stays in the raw offset domain throughout; the
    caller applies ``apply_offset`` once, at storage. Speed is set per read and NOT
    restored between attempts — the caller restores once after the loop.

    Validated live: 6/6 recoveries on the damaged reference disc across two days
    (tools/c2read_recovery_test.py + tools/c2timing.py baseline arm), every recovery
    byte-identical at AR confidence 200. The sweep across passes x speeds is the
    recovery mechanism — no paranoia engine involved.

    The recovery loop owns the TUI status line for its whole duration: a compact
    per-attempt status (``Recover track N (x/y)``) with the live read progress routed
    into the bar + detail only.

    *cohort* (optional) turns on the displaced-re-read test in
    :func:`_ladder_attempt`; without it every re-read is the track's own window
    and nothing else, as before.

    Returns ``(outcomes, prov)``. ``outcomes`` maps track number →
    ``"matched@<N>X"``, ``"matched@<N>X,displaced@<±S>"`` or ``"unrecovered"``.
    ``prov`` holds ``recovery_attempts_track_<n>`` for every track the ladder
    re-read: each attempt in the order it ran, including one whose read raised
    (``<N>:error``), since an attempt that left no record is how the walk order
    went unseen for three months.
    """
    outcomes: dict[int, str] = {}
    prov: dict[str, str] = {}
    # The ladder as bound, repeated n_passes times: fastest first, so an early
    # high-speed match exits sooner. Never reorder it here (see the docstring).
    speeds = [s for _ in range(n_passes) for s in ladder]
    total_attempts = len(speeds)
    file_size = (disc_last_lsn + 1) * _R6_BYTES_PER_FRAME

    status_line = [""]
    prog_cb = _recovery_status_cb(ui, status_line)

    # The whole-disc map stays on screen for the whole ladder, with only the
    # track under repair live (progress-map-plan.md §2).
    disc_map_view = _RecoveryMap(
        ui,
        disc_damage,
        max(
            (
                len(f"Recover track {r.track} ({total_attempts}/{total_attempts})")
                for r in failed_tracks
            ),
            default=0,
        ),
    )

    with pcm_file.open("r+b") as pcm_fh:
        for result in failed_tracks:
            t = result.track  # 1-indexed
            idx = t - 1
            lo, hi = _track_sector_window(track_lsns, idx, disc_last_lsn)

            if ui is None:
                print(f"  Recovering track {t}…")
            disc_map_view.arm(lo, hi)

            matched = False
            tried: list[str] = []
            c2_seen: list[str] = []
            for attempt, speed in enumerate(speeds, 1):
                status_line[0] = f"Recover track {t} ({attempt}/{total_attempts})"
                if ui is not None:
                    ui.set_status(status_line[0], 0.0)
                try:
                    hit, record, flagged = _ladder_attempt(
                        device,
                        track_lsns,
                        disc_last_lsn,
                        idx,
                        read_offset,
                        speed,
                        prog_cb,
                        n_tracks,
                        responses,
                        cohort,
                        c2,
                        readable=readable,
                    )
                except (RuntimeError, OSError) as exc:
                    log.warning("track %d re-read at %dX failed: %s", t, speed, exc)
                    tried.append(f"{speed}:error")
                    continue

                tried.append(record)
                if flagged is not None:
                    c2_seen.append(f"{speed}X:{flagged}")
                if hit is not None:
                    corrected, outcomes[t] = hit
                    _splice_corrected(
                        pcm_fh, corrected, track_lsns[idx], read_offset, file_size
                    )
                    matched = True
                    disc_map_view.clear(lo, hi)
                    break

            if not matched:
                outcomes[t] = "unrecovered"  # keep the original audio
            prov[f"recovery_attempts_track_{t}"] = ",".join(tried)
            if c2_seen:
                prov[f"recovery_c2_track_{t}"] = ",".join(c2_seen)
    disc_map_view.release()
    return outcomes, prov


#: Map states a re-read copy may be accepted from (span-recovery-plan.md §4.4).
#: ``SUSPECT``, ``C2``, ``HARD`` and ``PENDING`` never are.
_SPAN_ACCEPT_STATES = frozenset({"OK", "RECOVERED"})


def _span_copy_accepted(detail: SpanDetail, i: int) -> bool:
    """The §4.4 acceptance rule for sector *i* of one span re-read.

    Four conditions, all relative: the read had a position witness
    (``verify_passes >= 2``, checked here although the seam already refuses less,
    so the rule is visible where it is applied), the map state is ``OK`` or
    ``RECOVERED``, the sector's own C2 block is all zero, and it is outside the
    **widened** Q-position lane. On the LITE-ON every one of them held for wrong
    sectors (AccuDisc §18w), so this is a filter that cheapens the work and
    nothing more: the AccurateRip gate in :func:`_recover_flagged_spans` decides.
    """
    return (
        detail.verify_passes >= 2
        and detail.states[i] in _SPAN_ACCEPT_STATES
        and detail.c2_clean[i]
        and not detail.position_suspect[i]
    )


def _span_pass(
    device: str,
    spans: tuple,
    profile: Profile,
    speed: int | None,
    prog_cb: Callable[[int, int], None] | None,
    best: dict[int, bytes],
    track: int,
    readable: tuple[int, int] | None = None,
    records: list[str] | None = None,
) -> None:
    """Re-read every span once and record each accepted copy in *best*, replacing
    the previous one. A failed span read is logged and skipped, not fatal.

    Each read is appended to *records* as its ``span_reads_track_<n>`` entry
    (:func:`_span_read_record`)."""
    from cdda2img.accudisc_reader import read_span_detail

    for span in spans:
        # Before the try: a spent budget must stop the rung, not be skipped past.
        # The read policy (SPAN_RESCUE) asks for a cache defeat, counted as our
        # seek; the engine's own verify and rescue far reads are booked from its
        # report after the read (recovery_engine_work).
        drive_work.charge(speed, far_read=True)
        try:
            # The engine knobs (verify, overlap, C2 retries, rescue ladder) are
            # the read policy's since 2026-11-25, not the profile's.
            detail = read_span_detail(
                device,
                span.start,
                span.count,
                read_speed=speed,
                progress_cb=prog_cb,
                readable=readable,
            )
        except (RuntimeError, OSError) as exc:
            log.warning("track %d span re-read failed: %s", track, exc)
            if records is not None:
                records.append(f"{span.start}+{span.count}@{speed or 0}X:error")
            continue
        if records is not None:
            records.append(_span_read_record(detail, speed))
        for i in range(detail.count):
            if _span_copy_accepted(detail, i):
                off = i * _R6_BYTES_PER_FRAME
                best[detail.start_lba + i] = detail.pcm[off : off + _R6_BYTES_PER_FRAME]


def _span_read_record(detail: SpanDetail, speed: int | None) -> str:
    """One span read as PROV ``span_reads_track_<n>`` writes it (rbi_spec):
    ``<start>+<count>@<speed>X:<stage>=<n>/...:slips=<s>[/<off>]``."""
    if detail.stage_cmds is None:
        stages = "?"
    else:
        stages = "/".join(f"{k}={v}" for k, v in detail.stage_cmds.items() if v)
        stages = stages or "none"
    slips = f"slips={detail.slips}"
    if detail.slips_off_frame is not None:
        slips += f"/{detail.slips_off_frame}"
    return f"{detail.start_lba}+{detail.count}@{speed or 0}X:{stages}:{slips}"


def _lba_ranges(lbas: list[int]) -> str:
    """Ascending LBAs as ``a,b-c,...`` (PROV ``span_target_lbas_track_<n>``)."""
    runs: list[list[int]] = []
    for lba in sorted(set(lbas)):
        if runs and lba == runs[-1][1] + 1:
            runs[-1][1] = lba
        else:
            runs.append([lba, lba])
    return ",".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _span_gate(
    base: bytes,
    best: dict[int, bytes],
    w: _RawWindow,
    read_offset: int,
    track: int,
    n_tracks: int,
    responses: list,
) -> bytes | None:
    """Overlay *best* onto the window's raw bytes **in memory** and AR-verify.

    Returns the corrected track when it matches, else ``None``. Writes nothing:
    the only write is the caller's splice, made on a non-``None`` return.
    """
    from cdda2img.accuraterip import match_track_pcm

    if not best:
        return None
    candidate = bytearray(base)
    for lba, sector in best.items():
        off = (lba - w.lo) * _R6_BYTES_PER_FRAME
        candidate[off : off + _R6_BYTES_PER_FRAME] = sector
    corrected = _corrected_from_window(bytes(candidate), w, read_offset)
    _v1, _v2, conf_v1, conf_v2 = match_track_pcm(corrected, track, n_tracks, responses)
    return corrected if conf_v1 or conf_v2 else None


def _recover_flagged_spans(
    device: str,
    failed_tracks: list,
    track_lsns: list[int],
    disc_last_lsn: int,
    pcm_file: Path,
    responses: list,
    n_tracks: int,
    ladder: list[int],
    profile: Profile,
    read_offset: int,
    disc_damage: bytes,
    ui: TerminalUI | None,
    readable: tuple[int, int] | None = None,
    sweeps: int | None = None,
) -> tuple[dict[int, str], dict[str, str]]:
    """Re-read only the flagged sectors of each AR-failed track; commit on AR pass.

    *readable* bounds the engine's far reads to the session (:func:`_rip_disc_stage`).

    ``docs/reference/span-recovery-plan.md`` §4. Per track: take the capture
    pass's damage lane inside the track's raw window as targets (the disc's final
    sector excluded, a lead-out artefact), cluster and pad them into spans
    (``span_gap``/``span_pad``), then run passes, each re-reading every span once
    at the next speed of the ladder, in the order it was bound (fastest first,
    cycling). *sweeps* (default ``profile.passes``; the chain passes
    ``passes x recovery_passes``) counts sweeps of the ladder, one pass per
    rung each (kgr, 2026-11-24: a sweep is the least that tries every speed), so
    the passes run are ``sweeps x len(ladder)``. Accepted copies (:func:`_span_copy_accepted`) are
    overlaid onto the track's current raw audio **in memory**; after each pass the
    assembled track goes to ``match_track_pcm``, and only a match is written. A
    track that never matches keeps its audio byte-identical, the same invariant
    the speed ladder keeps: no unverified splice.

    **The newest accepted copy replaces the current best**, and every sector is
    re-read on every pass: nothing is retired before the AR gate passes. Keeping
    the first accepted copy would lock in an early wrong one, and on the LITE-ON
    agreement between reads was worth nothing (seven cache-defeated reads, majority
    correct for 0/49), so "being wrong early" has to stay survivable.

    ``profile.budget_s`` is seconds per span read: each track may take
    ``budget_s x spans x passes`` in wall time, so the bound grows with the work
    planned and is a guard against a stalled drive, not a cap on the plan.

    **Not yet run on hardware.** The H1/H2 bench runs in the plan were cancelled
    on 2026-09-24 under the replacement drive's minimal-testing terms; that ruling
    was rescinded on 2026-11-21 (kgr), but no run has happened since. This is
    reachable only through ``--profile span-flagged`` and is validated offline
    alone.

    Returns ``(outcomes, prov)``: ``outcomes`` has an entry ``span_matched@p<k>``
    **only for tracks it recovered**, so the caller hands the rest to the speed
    ladder; ``prov`` carries ``span_targets_track_<n>`` for every track tried and,
    for a track given up on, ``span_unresolved_track_<n>`` (targets never
    accepted) or ``span_accepted_unverified_track_<n>`` (every target accepted and
    AR still failed: the falsifier, rbi_spec §6.3.1).
    """
    import time

    from cdda2img.span_planner import cluster_spans, locate_targets

    outcomes: dict[int, str] = {}
    prov: dict[str, str] = {}
    # The ladder as bound, never reordered here (see _recover_failed_tracks).
    speeds: list[int | None] = [*ladder] or [None]
    n_sweeps = profile.passes if sweeps is None else sweeps
    file_size = (disc_last_lsn + 1) * _R6_BYTES_PER_FRAME
    status_line = [""]
    prog_cb = _recovery_status_cb(ui, status_line)

    with pcm_file.open("r+b") as pcm_fh:
        for result in failed_tracks:
            t = result.track
            idx = t - 1
            w = _raw_track_window(track_lsns, disc_last_lsn, idx, read_offset)
            targets = locate_targets(
                disc_damage, w.lo, w.hi - w.lo, exclude=(disc_last_lsn,)
            )
            spans = cluster_spans(
                targets, gap=profile.span_gap, pad=profile.span_pad, lo=w.lo, hi=w.hi
            )
            prov[f"span_targets_track_{t}"] = f"{len(targets)}/{len(spans)}"
            if not spans:
                continue  # nothing flagged: the ladder is the only remedy left
            prov[f"span_target_lbas_track_{t}"] = _lba_ranges(list(targets))
            records: list[str] = []

            if ui is None:
                print(f"  Re-reading {len(targets)} flagged sector(s) of track {t}…")
            pcm_fh.seek(w.lo * _R6_BYTES_PER_FRAME)
            base = pcm_fh.read((w.hi - w.lo) * _R6_BYTES_PER_FRAME)
            best: dict[int, bytes] = {}
            n_passes = n_sweeps * len(speeds)
            deadline = time.monotonic() + profile.budget_s * len(spans) * n_passes

            for k in range(1, n_passes + 1):
                if time.monotonic() > deadline:
                    break
                speed = speeds[(k - 1) % len(speeds)]
                status_line[0] = f"Span re-read track {t} ({k}/{n_passes})"
                if ui is not None:
                    ui.set_status(status_line[0], 0.0)
                _span_pass(
                    device,
                    spans,
                    profile,
                    speed,
                    prog_cb,
                    best,
                    t,
                    readable=readable,
                    records=records,
                )
                prov[f"span_reads_track_{t}"] = ",".join(records)
                corrected = _span_gate(
                    base, best, w, read_offset, t, n_tracks, responses
                )
                if corrected is not None:
                    _splice_corrected(
                        pcm_fh, corrected, track_lsns[idx], read_offset, file_size
                    )
                    outcomes[t] = f"span_matched@p{k}"
                    break

            if t not in outcomes:
                never = len(set(targets) - best.keys())
                if never:
                    prov[f"span_unresolved_track_{t}"] = str(never)
                else:
                    prov[f"span_accepted_unverified_track_{t}"] = str(len(targets))
    return outcomes, prov


def _strategy_attempt(
    device: str,
    req: ReadReq,
    windows: list[tuple[int, int]],
    prog_cb: Callable[[int, int], None] | None,
    *,
    cache_defeat: bool = True,
    readable: tuple[int, int] | None = None,
) -> tuple[list[bytes] | None, str]:
    """One strategy read, keeping *windows*. Returns ``(pieces, record)``.

    *pieces* is ``None`` when the read raised; *record* is its entry in
    ``strategy_reads_track_<n>`` either way: ``<start>+<count>@`` followed by the
    ladder's ``<requested>><honoured>:<cache_defeat>``, or ``<requested>:error``.

    A cache defeat is asked for on every read except the whole-disc one
    (AccuDisc 2026-11-21j, adopted by kgr): its far target for a read starting
    at LBA 0 lies past the lead-out, so it would fail on every pass, and the
    start of the disc cannot be in the cache after a pass that ended at the
    lead-out. Every other shape repeats or overlaps the previous read's window,
    which is the case the defeat exists for.
    """
    from cdda2img.accudisc_reader import read_span_windows

    where = f"{req.start}+{req.count}@"
    # Before the try: a spent budget must stop the strategy, not be skipped past.
    drive_work.charge(req.speed, far_read=cache_defeat)
    try:
        got = read_span_windows(
            device,
            req.start,
            req.count,
            windows,
            read_speed=req.speed,
            progress_cb=prog_cb,
            cache_defeat=cache_defeat,
            readable=readable,
        )
    except (RuntimeError, OSError) as exc:
        log.warning("strategy read %s%dX failed: %s", where, req.speed, exc)
        return None, f"{where}{req.speed}:error"
    record = where + _attempt_record(req.speed, got.speed_honoured_x, got.cache_defeat)
    return got.pieces, record


def _overlay_and_match(
    working: bytearray,
    w: _RawWindow,
    piece: bytes,
    read_start: int,
    read_offset: int,
    track: int,
    n_tracks: int,
    responses: list,
) -> bytes | None:
    """Splice one read's piece of the window into *working* and AR-verify it.

    *working* is the track's raw window **in memory**, carrying every earlier
    read of this track, exactly as the bench's working copy did. Returns the
    corrected track on a match, else ``None``; nothing is written to the file
    here.
    """
    from cdda2img.accuraterip import match_track_pcm

    if not piece:
        return None
    off = (max(w.lo, read_start) - w.lo) * _R6_BYTES_PER_FRAME
    working[off : off + len(piece)] = piece
    corrected = _corrected_from_window(bytes(working), w, read_offset)
    _v1, _v2, conf_v1, conf_v2 = match_track_pcm(corrected, track, n_tracks, responses)
    return corrected if conf_v1 or conf_v2 else None


class _StrategyTrack(NamedTuple):
    """One failed track under a recovery strategy: its window, the in-memory
    working copy of that window, and the reads made for it."""

    track: int
    window: _RawWindow
    working: bytearray
    reads: list[str]


def _recover_by_strategy(
    kind: str,
    profile: Profile,
    *,
    device: str,
    failed_tracks: list,
    track_lsns: list[int],
    disc_last_lsn: int,
    pcm_file: Path,
    responses: list,
    ladder: list[int],
    read_offset: int,
    disc_damage: bytes | None,
    ui: TerminalUI | None,
    seed: int,
    fixed_speed: int | None = None,
    readable: tuple[int, int] | None = None,
    sweeps: int | None = None,
) -> tuple[dict[int, str], dict[str, str]]:
    """Run one of the bench's recovery strategies **instead of** the speed ladder.

    *kind* comes from :func:`cdda2img.strategy_reads.strategy_kind`, i.e. from the
    profile's fields. The reads are the bench's own plan
    (:func:`~cdda2img.strategy_reads.strategy_reads`, shared with
    ``tools/strategy_bench.py``), and so is the rule around them: every read is
    spliced into an in-memory working copy of the track, the track is checked
    against AccurateRip after each read, and only a match is written to
    *pcm_file*. A track that never matches keeps its audio byte-identical, as on
    the ladder. What the bench lacked and this adds: a cache defeat on every read
    (kgr, 2026-11-21), and the record of every read in PROV.

    **It stands alone** (kgr, 2026-11-21): a track it does not recover is
    ``unrecovered``, not handed to the ladder.

    **Reads are planned in sweeps** (kgr, 2026-11-24): a sweep is the least that
    tries every target at every speed the profile reads at, ``targets x rungs``
    reads (:meth:`_StrategyRun.sweep_reads`; one rung when the profile holds
    ``recovery_speed``). *sweeps* (default ``profile.passes``; the chain passes
    ``passes x recovery_passes``) is how many run, so the reads per track are
    ``sweeps x targets x rungs``, and the first AccurateRip match ends them.
    ``profile.budget_s`` is seconds per read: a track may take ``budget_s`` x
    its planned reads, a guard against a stalled drive rather than a cap on the
    plan. ``whole-disc`` reads the disc once per rung of a sweep and tests every
    remaining track against that read, so there the plan and the time bound
    serve all the tracks together.

    *seed* drives ``max-variation``'s random choices and is recorded
    (``recovery_seed``) so its reads can be regenerated. Returns
    ``(outcomes, prov)``: ``strategy_matched@a<k>`` or ``unrecovered`` per track,
    and ``recovery_strategy``, ``strategy_targets_track_<n>`` and
    ``strategy_reads_track_<n>`` (rbi_spec §6.3).
    """
    import random

    prov: dict[str, str] = {"recovery_strategy": kind}
    if kind == "max-variation":
        prov["recovery_seed"] = str(seed)
    # Seeded and recorded on purpose: the point is that the reads can be regenerated.
    rng = random.Random(seed)  # noqa: S311
    status_line = [""]
    prog_cb = _recovery_status_cb(ui, status_line)
    outcomes: dict[int, str] = {}

    with pcm_file.open("r+b") as pcm_fh:
        tracks: list[_StrategyTrack] = []
        for result in failed_tracks:
            w = _raw_track_window(
                track_lsns, disc_last_lsn, result.track - 1, read_offset
            )
            pcm_fh.seek(w.lo * _R6_BYTES_PER_FRAME)
            working = bytearray(pcm_fh.read((w.hi - w.lo) * _R6_BYTES_PER_FRAME))
            tracks.append(_StrategyTrack(result.track, w, working, []))
        run = _StrategyRun(
            kind=kind,
            profile=profile,
            device=device,
            track_lsns=track_lsns,
            disc_last_lsn=disc_last_lsn,
            responses=responses,
            ladder=ladder,
            read_offset=read_offset,
            rng=rng,
            ui=ui,
            status_line=status_line,
            prog_cb=prog_cb,
            pcm_fh=pcm_fh,
            outcomes=outcomes,
            fixed_speed=fixed_speed,
            readable=readable,
            sweeps=profile.passes if sweeps is None else sweeps,
        )
        if kind == "whole-disc":
            run.whole_disc(tracks)
        else:
            for st in tracks:
                n = run.sector_track(st, disc_damage)
                prov[f"strategy_targets_track_{st.track}"] = str(n)
    for st in tracks:
        outcomes.setdefault(st.track, "unrecovered")  # keep the original audio
        prov[f"strategy_reads_track_{st.track}"] = ",".join(st.reads)
    return outcomes, prov


@dataclass
class _StrategyRun:
    """The loop state :func:`_recover_by_strategy` shares between its two shapes,
    held on one object so neither loop carries a dozen parameters."""

    kind: str
    profile: Profile
    device: str
    track_lsns: list[int]
    disc_last_lsn: int
    responses: list
    ladder: list[int]
    read_offset: int
    rng: random.Random
    ui: TerminalUI | None
    status_line: list[str]
    prog_cb: Callable[[int, int], None] | None
    pcm_fh: BinaryIO
    outcomes: dict[int, str]
    fixed_speed: int | None = None
    #: The session the audio was read from (:func:`_rip_disc_stage`).
    readable: tuple[int, int] | None = None
    #: Sweeps to run per track (``passes x recovery_passes`` in the chain).
    sweeps: int = 1

    def speed_for(self, i: int, per: int = 1) -> int:
        """The speed of read *i*: the next rung of the admitted ladder every *per*
        reads, cycling.

        kgr, 2026-11-21: a fixed speed repeats a speed that may itself be the cause
        of the failure, so every strategy's reads step through the ladder. A
        profile with ``ladder = "single"`` holds the one ``recovery_speed``
        instead: shipped, ``span-fixed``, the exception he kept, which is what
        makes it a fixed-span control. Decided from the field, never the name.
        The plan in ``strategy_reads`` chooses the sectors; this chooses the speed.

        *per* is the length of a round: a plan that takes n targets in turn
        (:data:`~cdda2img.strategy_reads.ROUND_ROBIN`) passes n, so the speed
        steps once per round and every target is read at every rung, and the
        targets of a round share one speed. Until 2026-11-24 the speed stepped on
        every read, and with the targets advancing too, target j only ever met
        rungs j, j + n, ...: L/gcd(n, L) of them (kgr asked for the fix that day).
        """
        if self.holds_speed() and self.fixed_speed is not None:
            return self.fixed_speed
        return self.ladder[(i // max(1, per)) % len(self.ladder)]

    def holds_speed(self) -> bool:
        """True when every read is at the one ``recovery_speed`` (``ladder =
        "single"`` with a speed to hold); the one rule both :meth:`speed_for` and
        :meth:`sweep_reads` read, so a sweep is always as long as the speeds it
        has to cover."""
        return self.profile.ladder == "single" and self.fixed_speed is not None

    def sweep_reads(self, targets: int) -> int:
        """Reads in one sweep: every one of *targets* at every rung read at
        (the whole ladder, or the one held speed). ``max-variation`` draws its
        targets at random, so its sweep has as many reads but no promise that
        every pairing occurs."""
        return max(1, targets) * (1 if self.holds_speed() else len(self.ladder))

    @property
    def leadout(self) -> int:
        return self.disc_last_lsn + 1

    @property
    def file_size(self) -> int:
        return self.leadout * _R6_BYTES_PER_FRAME

    @property
    def n_tracks(self) -> int:
        return len(self.track_lsns)

    def _reads(self, track: int, flagged: list[int], total: int) -> Iterator[ReadReq]:
        from cdda2img.strategy_reads import strategy_reads

        return strategy_reads(
            self.kind,
            track,
            flagged,
            self.track_lsns,
            self.leadout,
            self.ladder,
            self.ladder[0],
            self.profile.run_up,
            self.profile.span,
            self.rng,
            total,
        )

    def _status(self, text: str) -> None:
        self.status_line[0] = text
        if self.ui is not None:
            self.ui.set_status(text, 0.0)

    def _commit(self, st: _StrategyTrack, corrected: bytes, k: int) -> None:
        _splice_corrected(
            self.pcm_fh,
            corrected,
            self.track_lsns[st.track - 1],
            self.read_offset,
            self.file_size,
        )
        self.outcomes[st.track] = f"strategy_matched@a{k}"

    def sector_track(self, st: _StrategyTrack, disc_damage: bytes | None) -> int:
        """Run a sector strategy on one track; returns how many flagged sectors
        it had to aim at (``0``: every read aims at the track's first sector)."""
        import time

        from cdda2img.span_planner import locate_targets
        from cdda2img.strategy_reads import ROUND_ROBIN, targets_in_track, track_bounds

        w = st.window
        flagged = (
            list(
                locate_targets(
                    disc_damage,
                    w.lo,
                    w.hi - w.lo,
                    exclude=(self.disc_last_lsn,),
                )
            )
            if disc_damage
            else []
        )
        lo, hi = track_bounds(st.track, self.track_lsns, self.leadout)
        n_targets = sum(1 for lba in flagged if lo <= lba < hi)
        targets = targets_in_track(st.track, flagged, self.track_lsns, self.leadout)
        # One speed per round of targets for a plan that takes them in turn.
        per = len(targets) if self.kind in ROUND_ROBIN else 1
        total = self.sweeps * self.sweep_reads(len(targets))
        if self.ui is None:
            print(f"  Recovering track {st.track} ({self.kind})…")
        deadline = time.monotonic() + self.profile.budget_s * total
        for k, planned in enumerate(self._reads(st.track, flagged, total), 1):
            if time.monotonic() > deadline:
                break
            req = replace(planned, speed=self.speed_for(k - 1, per))
            self._status(f"Recover track {st.track} ({k}/{total})")
            pieces, record = _strategy_attempt(
                self.device, req, [(w.lo, w.hi)], self.prog_cb, readable=self.readable
            )
            st.reads.append(record)
            if pieces is None:
                continue
            corrected = _overlay_and_match(
                st.working,
                w,
                pieces[0],
                req.start,
                self.read_offset,
                st.track,
                self.n_tracks,
                self.responses,
            )
            if corrected is not None:
                self._commit(st, corrected, k)
                break
        return n_targets

    def whole_disc(self, tracks: list[_StrategyTrack]) -> None:
        """Read the whole disc once per rung of each sweep; test every remaining
        track against that one read. A match is written; the rest wait for the
        next read, and the reads stop once none is left."""
        import time

        if not tracks:
            return
        if self.ui is None:
            print(f"  Re-reading the whole disc for {len(tracks)} track(s)…")
        total = self.sweeps * self.sweep_reads(1)
        deadline = time.monotonic() + self.profile.budget_s * total
        remaining = list(tracks)
        for k, planned in enumerate(self._reads(remaining[0].track, [], total), 1):
            if not remaining or time.monotonic() > deadline:
                break
            req = replace(planned, speed=self.speed_for(k - 1))
            self._status(f"Whole-disc re-read ({k}/{total})")
            windows = [(st.window.lo, st.window.hi) for st in remaining]
            pieces, record = _strategy_attempt(
                self.device,
                req,
                windows,
                self.prog_cb,
                cache_defeat=False,
                readable=self.readable,
            )
            for st in remaining:
                st.reads.append(record)
            if pieces is None:
                continue
            for st, piece in zip(remaining, pieces, strict=True):
                corrected = _overlay_and_match(
                    st.working,
                    st.window,
                    piece,
                    req.start,
                    self.read_offset,
                    st.track,
                    self.n_tracks,
                    self.responses,
                )
                if corrected is not None:
                    self._commit(st, corrected, k)
            remaining = [st for st in remaining if st.track not in self.outcomes]


#: The chain's threshold between a lightly and a heavily damaged track, in
#: flagged sectors: the bench's (run3 aggregate: max-variation "must fall back to
#: the ladder above n~5"), adopted by kgr 2026-11-21. Recorded per track.
_LOW_N_MAX = 5

#: The automatic recovery chain's order per damage class (kgr, 2026-11-21). A
#: track with nothing flagged gets only the whole-track shapes: the sector shapes
#: would aim at its first sector and the span rung has nothing to read. Light
#: damage starts with the cheap targeted shapes the bench found fastest at low n;
#: heavy damage starts with the speed ladder, the only shape reliable at n = 47.
#: The bench's weakest shapes come last in both. One shared ``whole-disc`` stage
#: follows for every track still failing, since one read serves them all.
#:
#: Heavy damage leaves out the four per-target strategies (kgr, 2026-11-25:
#: protect the drive). Their reads scale as flagged sectors x speeds x passes, so
#: 300 flagged sectors on 10 tracks asked for ~189,000 re-reads and ~45,000 speed
#: changes, while the bench scored them weakest exactly there (max-variation 0/9
#: at high n). span-flagged stays: its reads scale with clustered spans, not
#: sectors. A profile named with --profile still runs, under the drive budget.
#:
#: Light damage reordered after the 0.59 hardware runs on Tracy (kgr, 2026-11-25:
#: "Go ahead with your proposed chain order"): the order is success per unit of
#: drive work. span-flagged and span-fixed (one span read, 515 sectors, each)
#: first; sector-runup (35 sectors a read); max-variation (hit on track 8, but at
#: the 12th of 15 reads); then the whole-track shapes, track-constant (3 reads at
#: 32X) before track-ladder (hit at 4X, the 5th read). sector-hammer left the chain:
#: it never recovered anything, 150 one-sector reads of 113044 included, and it is
#: now ``experimental``, i.e. disabled. "none" follows the same rule the same day
#: (kgr): track-constant before track-ladder. "high" is unchanged.
_CHAIN_ORDER = {
    "none": ("track-constant", "track-ladder"),
    "low": (
        "span-flagged",
        "span-fixed",
        "sector-runup",
        "max-variation",
        "track-constant",
        "track-ladder",
    ),
    "high": ("track-ladder", "track-constant", "span-flagged"),
}


#: The fewest sweeps any chain stage gets (kgr, 2026-11-25: "set minimum 3 passes
#: for chain stages"). On Tracy track 8, span-fixed and max-variation each matched
#: only on the last read of three sweeps; one sweep, the shipped default, would
#: have missed both. The first AccurateRip match still ends a stage early, so the
#: floor costs reads only where a stage is failing anyway, under the drive budget.
_CHAIN_MIN_SWEEPS = 3


def _ladder_first(order: tuple[str, ...]) -> tuple[str, ...]:
    """*order* with ``track-ladder`` moved to the front, for ``recovery_speed =
    "auto"`` (kgr, 2026-11-25: "run the ladder first when recovery_speed =
    auto"). auto holds, for a track's fixed-speed stages, the rung whose ladder
    re-read flagged the fewest C2 errors, so the ladder must run before them;
    the 2026-11-25 orders put track-constant and span-fixed ahead of it for
    tracks with 0-5 flagged sectors, which left auto with no evidence there."""
    if "track-ladder" not in order:
        return order
    return ("track-ladder", *(s for s in order if s != "track-ladder"))


def _snap_to_ladder(speed: int, ladder: list[int]) -> int:
    """The admitted rung nearest *speed* (ties go to the slower rung). A disc the
    drive throttles may admit nothing near the configured speed: in July the
    PX-716A admitted only ``[8, 4]`` on a degraded ABBA *Gold*."""
    return min(ladder, key=lambda r: (abs(r - speed), r))


def _damage_class(flagged: int) -> str:
    if flagged == 0:
        return "none"
    return "low" if flagged <= _LOW_N_MAX else "high"


class _ChainContext(NamedTuple):
    """What every chain stage needs, so each stage call stays one line."""

    device: str
    track_lsns: list[int]
    disc_last_lsn: int
    pcm_file: Path
    responses: list
    ladder: list[int]
    fixed_speed: int
    read_offset: int
    disc_damage: bytes | None
    ui: TerminalUI | None
    cohort: list | None
    #: recovery_speed = "auto": the ladder captures C2, and a track's fixed-speed
    #: stages hold the rung whose re-read flagged fewest (_fewest_c2).
    auto_speed: bool = False
    #: The session the audio was read from; bounds every far read (_rip_disc_stage).
    readable: tuple[int, int] | None = None
    #: recovery_passes (kgr, 2026-11-24 as recovery_attempts; merged into
    #: recovery_passes 2026-11-25): each stage runs its profile's ``passes``
    #: sweeps this many times over, in ONE call, so a strategy's working copy
    #: carries every read and the first match still ends it.
    recovery_passes: int = 1


def _initial_fixed_speed(speed: int | str, ladder: list[int]) -> int:
    """The fixed-speed stages' speed before any evidence: the configured one
    snapped to the ladder, or for ``auto`` the slowest admitted rung, used only
    until a track's ladder re-reads say which rung came closest."""
    if speed == "auto":
        return min(ladder)
    return _snap_to_ladder(int(speed), ladder)


def _fewest_c2(record: str) -> tuple[int, int]:
    """``(speed, flagged)`` of the rung whose re-read flagged fewest, from
    ``recovery_c2_track_<n>`` (``40X:12,32X:3``); ties go to the slower rung."""
    pairs = []
    for item in record.split(","):
        speed, flagged = item.split(":")
        pairs.append((int(speed.rstrip("X")), int(flagged)))
    return min(pairs, key=lambda p: (p[1], p[0]))


def _run_chain_stage(
    stage: str, results: list, ctx: _ChainContext
) -> tuple[dict[int, str], dict[str, str]]:
    """Run one shipped profile over *results*; return ``(outcomes, prov)``.

    *outcomes* maps a recovered track to how it matched; any other value
    (``unrecovered``) or absence is a miss. The stage reuses the code each
    profile already runs on its own: the span rung, the speed ladder (with its
    displaced-re-read test and per-attempt record), and the bench strategies.
    The stage's PROV gains ``recovery_sweeps`` (the sweeps it was given).
    """
    from cdda2img.recovery_profile import load_profile

    profile = load_profile(stage)
    # A pass is one sweep: every target at every speed the profile reads at (the
    # ladder's one sweep per pass, the span rung's one pass per rung, a strategy's
    # targets x rungs reads). recovery_passes repeats the profile's passes, and
    # no stage gets fewer than _CHAIN_MIN_SWEEPS. recovery_passes = 0 never gets
    # here: it turns recovery off before the chain is built.
    sweeps = max(_CHAIN_MIN_SWEEPS, profile.passes * ctx.recovery_passes)
    got, prov = _dispatch_chain_stage(stage, profile, results, ctx, sweeps)
    return got, {**prov, "recovery_sweeps": str(sweeps)}


def _dispatch_chain_stage(
    stage: str, profile: Any, results: list, ctx: _ChainContext, sweeps: int
) -> tuple[dict[int, str], dict[str, str]]:
    """Hand *results* to the code *profile*'s fields describe, *sweeps* times over."""
    import secrets

    from cdda2img.strategy_reads import strategy_kind

    n_tracks = len(ctx.track_lsns)
    # Dispatch on the profile's FIELDS, never its name: a user profile may shadow a
    # shipped one, or be new, and PROV must describe what ran.
    if profile.granularity == "span":
        reason = _span_rung_decline(ctx.disc_damage, ctx.responses, 1)
        if reason is not None or ctx.disc_damage is None:
            return {}, {"span_declined": reason or "no_damage_map"}
        return _recover_flagged_spans(
            ctx.device,
            results,
            ctx.track_lsns,
            ctx.disc_last_lsn,
            ctx.pcm_file,
            ctx.responses,
            n_tracks,
            ctx.ladder,
            profile,
            ctx.read_offset,
            ctx.disc_damage,
            ctx.ui,
            readable=ctx.readable,
            sweeps=sweeps,
        )
    if profile.granularity == "track":
        return _recover_failed_tracks(
            ctx.device,
            results,
            ctx.track_lsns,
            ctx.disc_last_lsn,
            ctx.pcm_file,
            ctx.responses,
            n_tracks,
            ctx.ladder if profile.ladder == "full" else [ctx.fixed_speed],
            sweeps,
            ctx.read_offset,
            ctx.ui,
            ctx.disc_damage,
            ctx.cohort,
            c2=ctx.auto_speed and profile.ladder == "full",
            readable=ctx.readable,
        )
    kind = strategy_kind(profile)
    if kind is None:  # unreachable for a validated profile; refuse, never guess
        msg = f"profile {stage!r} describes no recovery shape the chain can run"
        raise ValueError(msg)
    return _recover_by_strategy(
        kind,
        profile,
        device=ctx.device,
        failed_tracks=results,
        track_lsns=ctx.track_lsns,
        disc_last_lsn=ctx.disc_last_lsn,
        pcm_file=ctx.pcm_file,
        responses=ctx.responses,
        ladder=ctx.ladder,
        read_offset=ctx.read_offset,
        disc_damage=ctx.disc_damage,
        ui=ctx.ui,
        seed=secrets.randbits(32),
        fixed_speed=ctx.fixed_speed,
        readable=ctx.readable,
        sweeps=sweeps,
    )


def _stage_keys(prov: dict[str, str], stage: str) -> dict[str, str]:
    """A stage's PROV keys, suffixed with the stage so two stages that write the
    same key for one track (every strategy writes ``strategy_reads_track_<n>``)
    do not overwrite each other (rbi_spec ``recovery_chain_track_<n>``)."""
    tag = stage.replace("-", "_")
    return {f"{k}__{tag}": v for k, v in prov.items()}


def _flagged_lbas_in_track(ctx: _ChainContext, track: int) -> list[int]:
    """The raw LBAs the capture pass flagged in *track*'s raw window, the disc's
    final sector excluded (a lead-out artefact), as the span rung targets them."""
    from cdda2img.span_planner import locate_targets

    if not ctx.disc_damage:
        return []
    w = _raw_track_window(ctx.track_lsns, ctx.disc_last_lsn, track - 1, ctx.read_offset)
    return list(
        locate_targets(ctx.disc_damage, w.lo, w.hi - w.lo, exclude=(ctx.disc_last_lsn,))
    )


def _flagged_in_track(ctx: _ChainContext, track: int) -> int:
    """How many sectors :func:`_flagged_lbas_in_track` finds."""
    return len(_flagged_lbas_in_track(ctx, track))


def _stage_disabled(stage: str) -> bool:
    """Whether chain stage *stage* resolves to a profile marked ``experimental``,
    which disables it (kgr, 2026-11-25; ``recovery_profile.load_enabled_profile``).
    Asked of the LOADED profile, so a user file shadowing a shipped stage with
    ``experimental = true`` turns that stage off; the chain records it as
    ``<stage>:disabled`` rather than dropping it from the record."""
    from cdda2img.recovery_profile import load_profile

    return load_profile(stage).experimental


def _chain_one_track(
    result: object,
    order: tuple[str, ...],
    ctx: _ChainContext,
    expired: Callable[[], bool],
    prov: dict[str, str],
    outcomes: dict[int, str],
) -> list[str]:
    """Run *order* on one track until a stage matches; return the chain record."""
    t = result.track  # type: ignore[attr-defined]
    chain: list[str] = []
    if ctx.auto_speed:
        prov[f"recovery_speed_track_{t}"] = f"{ctx.fixed_speed}X:no_c2_data"
    for stage in order:
        if expired() or "recovery_budget_hit" in prov:
            break
        if _stage_disabled(stage):
            chain.append(f"{stage}:disabled")
            continue
        try:
            got, stage_prov = _run_chain_stage(stage, [result], ctx)
        except drive_work.DriveBudgetSpent as exc:
            # The stage's own record is lost with it; the reads it made are in the
            # drive-work totals. Recovery stops here for every track.
            prov["recovery_budget_hit"] = str(exc)
            chain.append(f"{stage}:budget")
            break
        prov.update(_stage_keys(stage_prov, stage))
        c2_record = stage_prov.get(f"recovery_c2_track_{t}")
        if ctx.auto_speed and c2_record:
            speed, flagged = _fewest_c2(c2_record)
            ctx = ctx._replace(fixed_speed=speed)
            prov[f"recovery_speed_track_{t}"] = f"{speed}X:c2={flagged}"
        hit = got.get(t, "unrecovered") != "unrecovered"
        chain.append(f"{stage}:{'hit' if hit else 'miss'}")
        if hit:
            outcomes[t] = got[t]
            break
    return chain


def _shared_whole_disc(
    remaining: list,
    ctx: _ChainContext,
    prov: dict[str, str],
    outcomes: dict[int, str],
    chains: dict[int, list[str]],
) -> None:
    """The chain's one ``whole-disc`` stage for every track still failing; a spent
    drive budget is recorded on every one of them (``whole-disc:budget``)."""
    if _stage_disabled("whole-disc"):
        for r in remaining:
            chains[r.track].append("whole-disc:disabled")
        return
    try:
        got, stage_prov = _run_chain_stage("whole-disc", remaining, ctx)
    except drive_work.DriveBudgetSpent as exc:
        prov["recovery_budget_hit"] = str(exc)
        for r in remaining:
            chains[r.track].append("whole-disc:budget")
        return
    prov.update(_stage_keys(stage_prov, "whole-disc"))
    for r in remaining:
        hit = got.get(r.track, "unrecovered") != "unrecovered"
        chains[r.track].append(f"whole-disc:{'hit' if hit else 'miss'}")
        if hit:
            outcomes[r.track] = got[r.track]


def _recovery_chain(
    ctx: _ChainContext,
    failed_tracks: list,
    recovery_speed: int | str,
    ceiling_s: int,
    only: str | None = None,
) -> tuple[dict[int, str], dict[str, str]]:
    """The automatic recovery chain (kgr, 2026-11-21): every shipped profile, in
    an order chosen from each track's damage, until every track verifies.

    Per track: count the sectors the capture pass flagged, pick the order for
    that class (:data:`_CHAIN_ORDER`), and run each stage on that track alone
    until one matches AccurateRip. Then one ``whole-disc`` stage for every track
    still failing. Every stage gates its own writes on AccurateRip, so a stage
    that misses leaves the track's audio exactly as it found it.

    *only* (``--profile`` or config ``default_profile``) restricts the chain to
    that one stage, for diagnosis and bench work (kgr, 2026-11-21): every track
    gets that stage alone, and ``whole-disc`` runs only when it is the one named.

    *ceiling_s* > 0 bounds the whole chain in wall time; when it stops the chain,
    ``recovery_ceiling_hit`` says so, so the abort can tell "raise the ceiling"
    from "the chain was exhausted". Returns ``(outcomes, prov)``: a recovered
    track maps to how it matched, an unrecovered one to ``unrecovered``.
    """
    import time

    deadline = time.monotonic() + ceiling_s if ceiling_s > 0 else None

    def expired() -> bool:
        return deadline is not None and time.monotonic() > deadline

    prov: dict[str, str] = {
        "recovery_speed": "auto"
        if ctx.auto_speed
        else f"{recovery_speed}X>{ctx.fixed_speed}X",
    }
    outcomes: dict[int, str] = {}
    chains: dict[int, list[str]] = {}
    for result in failed_tracks:
        lbas = _flagged_lbas_in_track(ctx, result.track)
        flagged = len(lbas)
        cls = _damage_class(flagged)
        prov[f"recovery_damage_track_{result.track}"] = f"{flagged}:{cls}"
        # kgr, 2026-11-25: which sectors, so any profile's run can be matched to
        # the damage it was handed.
        if lbas:
            prov[f"recovery_damage_lbas_track_{result.track}"] = _lba_ranges(lbas)
        order = _CHAIN_ORDER[cls] if only is None else (only,)
        if ctx.auto_speed:
            order = _ladder_first(order)
        if only == "whole-disc":
            order = ()
        if "recovery_budget_hit" in prov:
            order = ()
        chains[result.track] = _chain_one_track(
            result, order, ctx, expired, prov, outcomes
        ) or (["budget"] if "recovery_budget_hit" in prov else [])
    remaining = [r for r in failed_tracks if r.track not in outcomes]
    if (
        remaining
        and not expired()
        and "recovery_budget_hit" not in prov
        and only in (None, "whole-disc")
    ):
        _shared_whole_disc(remaining, ctx, prov, outcomes, chains)
    if expired() and any(r.track not in outcomes for r in failed_tracks):
        prov["recovery_ceiling_hit"] = str(ceiling_s)
    for t, chain in chains.items():
        prov[f"recovery_chain_track_{t}"] = ",".join(chain)
        outcomes.setdefault(t, "unrecovered")
    return outcomes, prov


def _span_rung_decline(
    disc_damage: bytes | None, responses: list, recovery_passes: int
) -> str | None:
    """Why the span rung cannot run although the profile asked for it, or ``None``.

    Each reason is recorded as ``span_declined`` rather than skipped silently,
    and the speed ladder still runs as usual on every one of them.
    """
    from cdda2img.accudisc_reader import span_detail_supported

    if recovery_passes <= 0:
        return "recovery_disabled"
    if disc_damage is None:
        return "no_damage_map"
    if not responses:
        return "no_ar_responses"
    if not span_detail_supported():
        return "no_engine_support"
    return None


def _track_ranges(tracks: list[int]) -> str:
    """``[6, 7, 8, 10]`` as ``6-8, 10``."""
    runs: list[list[int]] = []
    for t in sorted(tracks):
        if runs and t == runs[-1][1] + 1:
            runs[-1][1] = t
        else:
            runs.append([t, t])
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in runs)


def _print_displacement(outcomes: dict[int, str]) -> None:
    """Say which tracks were re-sliced and by how much, one line per displacement."""
    by_amount: dict[int, list[int]] = {}
    for track, value in outcomes.items():
        by_amount.setdefault(int(value.split("@")[1]), []).append(track)
    for amount, tracks in sorted(by_amount.items()):
        what = "early" if amount > 0 else "late"
        noun = "track" if len(tracks) == 1 else "tracks"
        print(
            f"   Displaced audio: {noun} {_track_ranges(tracks)} intact in the "
            f"capture, {abs(amount)} samples {what}"
        )
    print("   Re-sliced from the capture, no re-read.")


def _displacement_stage(
    pcm_file: Path,
    track_lsns: list[int],
    disc_last_lsn: int,
    read_offset: int,
    results: list,
    responses: list,
    scratch_dir: Path,
) -> tuple[dict[int, str], list]:
    """The zero-read displacement check, between CTDB and the re-read rungs.

    Returns ``(outcomes, cohort)``: ``outcomes`` maps each track that was
    re-sliced to ``displaced@<±N>``, and *cohort* is this disc's own dBAR blocks,
    which the speed ladder reuses to test its re-reads the same way. What it
    looks for, the three rules that keep a coincidence from being written, and
    the sign are in :mod:`cdda2img.displacement`.

    Runs on the raw PCM, like CTDB and the ladder, and writes a track only after
    its bytes matched AccurateRip. A track it cannot place (real damage, or the
    track the slip is inside) is left exactly as it was for the ladder.
    """
    from cdda2img import displacement

    cohort = displacement.cohort_blocks(responses, results)
    found = displacement.find_displaced(
        pcm_file,
        track_lsns,
        disc_last_lsn,
        cohort,
        displacement.failed_track_numbers(results),
        read_offset,
    )
    if not found:
        return {}, cohort
    written = displacement.reslice(
        pcm_file,
        found,
        track_lsns,
        disc_last_lsn,
        read_offset,
        cohort,
        scratch_dir / "resliced.pcm",
        _splice_corrected,
    )
    return {t: displacement.outcome(s) for t, s in written.items()}, cohort


def _run_span_rung(
    profile: Profile | None,
    failed_tracks: list,
    *,
    device: str,
    track_lsns: list[int],
    disc_last_lsn: int,
    pcm_file: Path,
    responses: list,
    ladder: list[int],
    recovery_passes: int,
    read_offset: int,
    disc_damage: bytes | None,
    ui: TerminalUI | None,
    readable: tuple[int, int] | None = None,
) -> tuple[dict[int, str], dict[str, str], list]:
    """Dispatch the span rung ahead of the speed ladder, when the profile asks.

    Returns ``(outcomes, prov, remaining)``. *remaining* is what the ladder gets:
    every failed track the span rung did not recover. Handing it the recovered
    ones too would re-read a verified track (grinding, under kgr's terms for the
    replacement drive) and overwrite ``span_matched@p<k>`` with the ladder's
    outcome. Any profile whose granularity is not ``"span"`` passes straight
    through untouched.
    """
    if profile is None or profile.granularity != "span":
        return {}, {}, failed_tracks
    reason = _span_rung_decline(disc_damage, responses, recovery_passes)
    if reason is not None or disc_damage is None:
        return {}, {"span_declined": reason or "no_damage_map"}, failed_tracks
    outcomes, prov = _recover_flagged_spans(
        device,
        failed_tracks,
        track_lsns,
        disc_last_lsn,
        pcm_file,
        responses,
        len(track_lsns),
        ladder,
        profile,
        read_offset,
        disc_damage,
        ui,
        readable=readable,
    )
    remaining = [r for r in failed_tracks if r.track not in outcomes]
    return outcomes, prov, remaining


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
    network_preflight: str | None = None,
    media_override: str | None = None,
    recovery_speed: int | str | None = None,
    profile_only: bool = False,
    recovery_passes: int | None = None,
) -> Path | None:
    """Rip *device* into an RBI container, or write nothing at all.

    *recovery_passes* (``--recovery-passes``, else config ``recovery_passes``,
    default 1; 0 = no recovery) is how many times each recovery stage runs its
    profile's ``passes`` sweeps, every target at every speed per sweep; on the
    ``--ad-ladder`` path, how many sweeps of that ladder. The first AccurateRip
    match ends a track's reads early. (kgr: ``recovery_attempts`` 2026-11-24,
    merged into ``recovery_passes`` 2026-11-25.)

    *profile_only* (``--profile-only``, a temporary test switch, kgr 2026-11-22)
    skips CTDB parity repair and the displacement check, so a failed track reaches
    the recovery profile itself. Nothing else changes: AccurateRip still has to
    verify every track or nothing is written. PROV ``zero_read_repairs=skipped``.

    Returns the container's path: ``None`` only when ``--extract --no-keep-rbi``
    removed it after extraction. A rip that cannot build a complete, fully
    AccurateRip-verified container raises :class:`~cdda2img.rip_abort.RipAborted`
    instead, from whichever gate found the problem (kgr, 2026-11-21). The
    ``finally`` below still restores the drive and removes the scratch.
    """
    import sys

    from cdda2img import drive_speed
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

    # NO mid-pipeline restore-to-max here. This used to un-throttle the drive
    # before the lead-in scan; read speed is drive state that persists across
    # handles and processes, so blasting it to maximum on the way in governs the
    # rip that follows. Settled as D1 in accudisc-migration-plan.md §6: delete it,
    # do not re-shape it.
    #
    # **The conclusion outlived its original reason, and the reason is now a
    # different one.** D1 was argued from the `subq_speed_cliff` model — raw-Q
    # yield collapsing at the top of the range while audio stayed clean. That
    # model is superseded (RECOVERY.md §12.3/§12.8: one run read clean at
    # 40/32/8x and dirty at 24/4x, non-monotone, so not a cliff). What keeps the
    # rule is plainer and no longer speculative: since 2026-08-09 the initial
    # pass has a REQUESTED speed (--ad-speed), and a restore sited here would
    # silently overwrite the rate the user asked for, between the request and the
    # read. A rule can be right for a reason that later turns out to be wrong;
    # what it must not do is keep citing that reason.
    #
    # The stated justification was that a drive left throttled by a previous run
    # keeps that speed, and that the lead-in scan and the album-art fetch should
    # run at full speed. Only the first clause is true. `album_art` is network
    # only and never touches the drive; and a throttled drive costs seconds on a
    # lead-in read, where the failure this caused costs the disc's structure.
    #
    # Restoring at the END of the rip is kept (after the recovery ladder, which
    # is the thing that actually throttles) — that is the "one restore at session
    # end" half of D1, and it is where an inherited ceiling belongs.

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
                # Disc-ID miss → stage 7. Without this the banner has no MBID at
                # all, so `fetch_cover` returns None *immediately* (it needs a
                # release or release-group id) and the preview silently renders
                # nothing — which reads as a broken/absent art fetch rather than
                # "MB does not know this disc ID". Measured 2026-08-18 on
                # `Toca (20th Anniversary Edition)`: disc ID 404s, yet the very
                # same rip embedded art seconds later from
                # `caa:release-group:39957eb7…`, found by the stage-7 duration
                # matcher in `_finalize_import`. The art was always reachable; only
                # the banner could not reach it.
                #
                # Gated on a miss so the normal path pays nothing, and on an
                # album/artist seed because that is stage 7's own precondition —
                # here it comes from CD-Text via `_fast_scan_disc`, so a disc
                # without CD-Text still shows no art, correctly.
                if not (best_rg or best_r) and (disc_p.album or disc_p.artist):  # type: ignore[union-attr]
                    from cdda2img.mb_lookup import duration_match_lookup

                    dm = duration_match_lookup(disc_p)  # type: ignore[arg-type]
                    if dm is not None:
                        best_rg = dm.mb_release_group_id
                        # C2: a duration match is NOT disc-ID-verified, so its
                        # release id must not be treated as this pressing. It is
                        # used here for art only, never merged into `disc` — the
                        # banner's RBIDisc is a throwaway (`replace` below) and is
                        # discarded when the worker returns.
                        best_r = dm.mb_release_id
                        if label == "(unknown)" and dm.album:
                            label = (
                                f"{dm.album} - {dm.artist}" if dm.artist else dm.album
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
    from cdda2img import query_log

    query_log.start()
    # Captured BEFORE anything asks the drive for a rate, and handed back in the
    # `finally` below. AccuDisc documents `read_req.speed_x` as deliberately NOT
    # restored on exit (accudisc.h:1414) — a recovery loop steps rungs without
    # re-spinning and restores once itself — so the drive is left wherever the last
    # request put it, and this is the "once itself". It has to be a `finally`: the
    # only restore before this lived after the recovery loop, so a rip that never
    # entered recovery (i.e. a clean one) left the drive throttled with no restore
    # anywhere, and a rip that raised left it throttled too.
    entry_speed_x = drive_speed.current_speed_x(device)
    try:
        # What is in the drive, before anything else reads it (kgr, 2026-11-21): a
        # recordable disc is refused unless the user says what it is.
        from cdda2img.accudisc_reader import media_check

        media = media_check(device)
        _require_media_decision(media.kind, media.evidence, media_override)
        digital_media = media_override == "digital_media"

        # Grab track 1 first (drive is single-use), then play it on a loop in
        # the background while the rest of the rip runs.
        track_preview = _start_track_preview(device, temp.base, ui, enabled=preview)

        c2_file = temp.pcm_file.with_suffix(".c2")
        info, rip_type, c2_path, disc_damage, readable = _rip_disc_stage(
            device,
            temp.pcm_file,
            c2_file,
            read_offset,
            cfg,
            ui=ui,
            read_speed=strategy.read_speed if strategy is not None else None,
        )

        _require_usable_q(info.prov or {})

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
        span_prov: dict[str, str] = {}  # flagged-span rung keys, merged below
        ladder_prov: dict[str, str] = {}  # recovery_attempts_track_<n>, likewise
        recovery_ladder: list[int] = []
        # The one recovery-effort setting (kgr, 2026-11-25; merged from
        # recovery_attempts): --recovery-passes, else config. 0 = no recovery.
        passes = _effective_recovery_passes(recovery_passes, cfg.recovery_passes)
        # CTDB attempt outcome, kept whether it succeeded or declined — a declined
        # repair is the interesting case and used to leave no trace at all.
        ctdb_result: CtdbRepairResult | None = None
        # Displacement check outcome ("found" | "none"), None when it did not run.
        displacement_check: str | None = None
        ar_responses: list = []
        ar_cohort: list = []
        _ar_transport: str | None = None

        digital_prov: dict[str, str] = {}
        if digital_media:
            # No disc-ID database can hold this disc: the audio is proved by
            # byte-identical reads at different speeds instead. An empty result
            # leaves every AccurateRip-keyed stage below (repair, recovery, the
            # end gates) with nothing to act on.
            from cdda2img.accuraterip import ARVerifyResult

            ar_verify = ARVerifyResult()
            digital_prov = _digital_media_consensus(
                device,
                temp.pcm_file,
                disc_damage,
                ui,
                capture_speed=strategy.read_speed if strategy is not None else None,
                capture_honoured=_prov_int(info.prov, "capture_speed_honoured"),
                readable=readable,
            )
        else:
            _ui_status(ui, "Verifying AccurateRip…")
            ar_verify = _verify_ar_with_retry(
                lambda: verify_rip(
                    temp.pcm_file,
                    final_track_lsns,
                    final_disc_last_lsn,
                    read_offset=ar_offset,
                    cddb_id=cddb_id,
                ),
                ui,
                interactive=_interactive(auto),
            )
            if ui is not None:
                ui.pause()
            print_ar_report(
                ar_verify.tracks, read_offset=read_offset, reachable=ar_verify.reachable
            )
            if ui is not None:
                ui.resume()
            # kgr, 2026-11-21: without AccurateRip's answer nothing can be
            # verified, and without a verified rip nothing is written. Both gates
            # come before any repair read, so a doomed rip spends no drive time.
            _require_accuraterip_answer(ar_verify.tracks, ar_verify.reachable, cddb_id)

        # CTDB parity repair FIRST (above the re-read ladder): error-only ctanalyse on the
        # raw PCM (network parity, zero extra reads), with C2 erasures if the C2 path
        # captured them. Skipped on a cd-paranoia read fallback (already corrected — no
        # raw domain for ctanalyse's offset detection). On success the PCM is repaired in
        # place and the ladder is skipped; on failure we fall through, still raw — the
        # c2read ladder works in the raw domain too.
        if (
            raw_domain
            and not profile_only
            and _ar_has_partial_mismatch(ar_verify.tracks)
        ):
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
                # The repair map goes ABOVE the AR report, because it is the
                # evidence and the report is the verdict: the reader wants to see
                # what parity rewrote before being told whether the result now
                # verifies. Both are inside the one pause, so the TUI is torn
                # down and rebuilt once rather than twice (N8's stray-write
                # mechanism — anything printed outside a pause strands a frame).
                _print_ctdb_repair_map(
                    _ctdb.repaired_sectors,
                    disc_damage,
                    # `ar_verify` was recomputed just above, so the "after" row
                    # rests on the post-repair verdict rather than on the gate
                    # that admitted the repair. The two can disagree: AR and
                    # CTDB are different reference populations, and a track can
                    # fail one while passing the other.
                    resolved=not _ar_has_partial_mismatch(ar_verify.tracks),
                )
                print_ar_report(
                    ar_verify.tracks,
                    read_offset=read_offset,
                    reachable=ar_verify.reachable,
                )
                if ui is not None:
                    ui.resume()
        # Displacement check, also zero reads: a drive that drops or repeats a few
        # samples mid-stream leaves every later track intact and out of place, which
        # AccurateRip reports as a mismatch and CTDB as damage beyond its capacity.
        # Tracks it can place are re-sliced from the capture; the rest fall through.
        if (
            raw_domain
            and not profile_only
            and _ar_has_partial_mismatch(ar_verify.tracks)
        ):
            from cdda2img.accuraterip import fetch_ar_responses

            ar_responses, _ar_transport, _ar_b3 = fetch_ar_responses(
                final_track_lsns, final_disc_last_lsn, cddb_id
            )
            if ar_responses:
                _ui_status(ui, "Checking for displaced audio…")
                _displaced, ar_cohort = _displacement_stage(
                    temp.pcm_file,
                    final_track_lsns,
                    final_disc_last_lsn,
                    read_offset,
                    ar_verify.tracks,
                    ar_responses,
                    temp.base,
                )
                displacement_check = "found" if _displaced else "none"
                if _displaced:
                    rip_type = f"{rip_type}+reslice"
                    recovery_outcomes.update(_displaced)
                    _ui_status(ui, "Re-verifying AccurateRip (re-sliced)…")
                    ar_verify = verify_rip(
                        temp.pcm_file,
                        final_track_lsns,
                        final_disc_last_lsn,
                        read_offset=read_offset,
                        cddb_id=cddb_id,
                    )
                    if ui is not None:
                        ui.pause()
                    _print_displacement(_displaced)
                    print_ar_report(
                        ar_verify.tracks,
                        read_offset=read_offset,
                        reachable=ar_verify.reachable,
                    )
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
                f"{n_bad} track(s) failed AccurateRip — re-reading with AccuDisc…",
            )

            # Speed-laddered recovery: re-read each failed track across the drive's own
            # speed ladder (fastest→slowest, `passes` sweeps), AR-verifying
            # each attempt and splicing the first match; a track that never matches
            # keeps its original audio. The sweep across passes x speeds is the recovery
            # mechanism (validated 6/6 on the damaged reference disc).
            from cdda2img import recovery_profile
            from cdda2img.accuraterip import fetch_ar_responses

            # Fetched once already for the displacement check; asked again only
            # when that fetch came back empty.
            if not ar_responses:
                ar_responses, _ar_transport, _ar_b3 = fetch_ar_responses(
                    final_track_lsns, final_disc_last_lsn, cddb_id
                )
            if not ar_responses:
                log.warning(
                    "AccurateRip dBAR re-fetch returned nothing (transport=%s); "
                    "re-read recovery cannot verify a splice and will not run",
                    _ar_transport,
                )
            # The ladder comes from the resolved profile bound to THIS drive and
            # THIS disc (§9.3) — a self-throttling governor caps a degraded disc
            # regardless of drive capability, so it must be probed per rip.
            # `recovery_passes = 0` (config or --recovery-passes) remains the global
            # kill switch; otherwise each stage runs its profile's sweeps that many
            # times (kgr, 2026-11-25).
            #
            # The gate is `strategy is not None`, NOT `strategy.profile is not
            # None`. The narrower test was a perfectly good check for "can I bind a
            # profile's ladder" and quite the wrong one for "should recovery run": the
            # ad-flags rung carries no profile by construction, so any --ad-* flag
            # silently turned recovery off — including --ad-ladder, whose entire
            # purpose is to supply the ladder that was being discarded. bind_ladder
            # now sources rungs for that rung too and returns an empty ladder when
            # there genuinely are none.
            bound = (
                recovery_profile.bind_ladder(strategy, device)
                if strategy is not None
                else None
            )
            recovery_ladder = (
                list(bound.ladder)
                if bound is not None and ar_responses and passes > 0
                else []
            )
            # kgr, 2026-11-21: with no profile named, every recovery profile is used,
            # in an order chosen from each track's damage, until every track
            # verifies (_recovery_chain). A named profile restricts the chain to
            # that one stage. Only --ad-* flags (no profile) take the old path.
            # kgr, 2026-11-25: every recovery read is charged to one per-rip drive
            # budget before it is sent (drive_work), so a heavily damaged disc
            # cannot turn recovery into a command storm on the drive.
            # kgr, 2026-11-25: "I would rather do my tests against 0.59.0 with all
            # new features enabled". Every recovery read is filled by AccuDisc's
            # read policy (GATED_REREAD for the ladder and strategy reads,
            # SPAN_RESCUE for the span rung, shift_search on by our override), and
            # its engine gets what is left of the rip's budget, enforced
            # (recovery_max_engine_cmds; engine speed changes share
            # recovery_max_speed_changes).
            from cdda2img.accudisc_reader import drive_supports_c2, recovery_policy

            with (
                drive_work.budget(
                    cfg.recovery_max_reads,
                    cfg.recovery_max_speed_changes,
                    cfg.recovery_max_engine_cmds,
                ) as work,
                recovery_policy(
                    recovery_ladder,
                    c2_usable=bool(recovery_ladder) and drive_supports_c2(device),
                    readable=readable,
                ) as read_policy,
            ):
                try:
                    if (
                        bound is not None
                        and bound.profile is not None
                        and recovery_ladder
                        and failed_tracks
                    ):
                        chain_outcomes, chain_prov = _recovery_chain(
                            _ChainContext(
                                device=device,
                                track_lsns=final_track_lsns,
                                disc_last_lsn=final_disc_last_lsn,
                                pcm_file=temp.pcm_file,
                                responses=ar_responses,
                                ladder=recovery_ladder,
                                fixed_speed=_initial_fixed_speed(
                                    recovery_speed or cfg.recovery_speed,
                                    recovery_ladder,
                                ),
                                auto_speed=(recovery_speed or cfg.recovery_speed)
                                == "auto",
                                read_offset=read_offset,
                                disc_damage=disc_damage,
                                ui=ui,
                                cohort=ar_cohort or None,
                                readable=readable,
                                recovery_passes=passes,
                            ),
                            failed_tracks,
                            recovery_speed or cfg.recovery_speed,
                            cfg.recovery_ceiling_s,
                            only=None
                            if bound.source == "builtin"
                            else bound.profile.name,
                        )
                        recovery_outcomes.update(chain_outcomes)
                        ladder_prov.update(chain_prov)
                    else:
                        span_outcomes, span_keys, failed_tracks = _run_span_rung(
                            bound.profile if bound is not None else None,
                            failed_tracks,
                            device=device,
                            track_lsns=final_track_lsns,
                            disc_last_lsn=final_disc_last_lsn,
                            pcm_file=temp.pcm_file,
                            responses=ar_responses,
                            ladder=list(bound.ladder) if bound is not None else [],
                            recovery_passes=passes,
                            read_offset=read_offset,
                            disc_damage=disc_damage,
                            ui=ui,
                            readable=readable,
                        )
                        recovery_outcomes.update(span_outcomes)
                        span_prov.update(span_keys)
                        # A bench strategy (sector-* / span-fixed / max-variation / whole-disc,
                        # picked from the profile's FIELDS) runs instead of the speed ladder and
                        # stands alone (kgr, 2026-11-21). It needs the same preconditions the
                        # ladder does: AR responses to gate with, recovery enabled, and a bound
                        # ladder to take its speeds from.
                        from cdda2img.strategy_reads import (
                            strategy_kind as _strategy_kind,
                        )

                        bound_profile = bound.profile if bound is not None else None
                        strategy_kind = (
                            _strategy_kind(bound_profile)
                            if bound_profile is not None
                            else None
                        )
                        if (
                            strategy_kind is not None
                            and bound_profile is not None
                            and recovery_ladder
                            and failed_tracks
                        ):
                            import secrets

                            outcomes, strategy_keys = _recover_by_strategy(
                                strategy_kind,
                                bound_profile,
                                device=device,
                                failed_tracks=failed_tracks,
                                track_lsns=final_track_lsns,
                                disc_last_lsn=final_disc_last_lsn,
                                pcm_file=temp.pcm_file,
                                responses=ar_responses,
                                ladder=recovery_ladder,
                                read_offset=read_offset,
                                disc_damage=disc_damage,
                                ui=ui,
                                seed=secrets.randbits(32),
                                readable=readable,
                            )
                            recovery_outcomes.update(outcomes)
                            ladder_prov.update(strategy_keys)
                        elif recovery_ladder and failed_tracks:
                            outcomes, attempts_prov = _recover_failed_tracks(
                                device,
                                failed_tracks,
                                final_track_lsns,
                                final_disc_last_lsn,
                                temp.pcm_file,
                                ar_responses,
                                len(final_track_lsns),
                                recovery_ladder,
                                passes,
                                read_offset,
                                ui,
                                disc_damage,
                                ar_cohort or None,
                                readable=readable,
                            )
                            recovery_outcomes.update(outcomes)
                            ladder_prov.update(attempts_prov)
                            # No restore here any more. The ladder leaves the drive at its last
                            # rung, and the `finally` at the end of this function puts it back to
                            # the rate captured at entry — which covers this path *and* the clean
                            # rip that never reaches it. Restoring twice was not harmful, but the
                            # old call restored to MAX while the finally restores as-found, so
                            # leaving both would have made the drive's final state depend on
                            # whether recovery happened to run.
                except drive_work.DriveBudgetSpent as exc:
                    # The chain handles its own; this is the --ad-ladder path.
                    ladder_prov["recovery_budget_hit"] = str(exc)
                finally:
                    ladder_prov["recovery_drive_work"] = work.record()
                    engine = work.engine_record()
                    if engine is not None:
                        ladder_prov["recovery_engine_work"] = engine
                    for key, value in (
                        ("recovery_policy", read_policy.record()),
                        ("recovery_policy_overrides", read_policy.overrides_record()),
                    ):
                        if value is not None:
                            ladder_prov[key] = value

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
            print_ar_report(
                ar_verify.tracks, read_offset=read_offset, reachable=ar_verify.reachable
            )
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

        # An ARIP block of all-NOT_IN_DB statuses is a sealed claim that AccurateRip
        # answered "this disc is unknown". When it gave no answer at all, write no
        # block: absence plus lookup_status_accuraterip=down is the honest record
        # (rbi_spec §6.5).
        arip_block = (
            pack_arip_block(
                ar_verify.tracks, final_track_lsns, final_disc_last_lsn, cddb_id
            )
            if ar_verify.reachable
            else None
        )

        from cdda2img.rip_log import RipLogBuilder

        rlog_builder = RipLogBuilder(
            rip_type=rip_type,
            drive_name=drive_name,
            read_offset=read_offset,
        )
        if digital_media:
            rlog_builder.digital_media_consensus = digital_prov.get(
                "digital_media_consensus"
            )
        else:
            rlog_builder.ar_results = ar_verify.tracks
            rlog_builder.ar_reachable = ar_verify.reachable
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
        provenance["lookup_status_accuraterip"] = (
            "disabled" if digital_media else ar_verify.lookup_status
        )
        provenance["media_type"] = media.kind
        provenance["media_evidence"] = media.evidence
        if media_override is not None:
            provenance["media_override"] = media_override
        provenance.update(digital_prov)
        if network_preflight is not None:
            provenance["network_preflight"] = network_preflight
        provenance.update(span_prov)
        if displacement_check is not None:
            provenance["displacement_check"] = displacement_check
        if profile_only:
            provenance["zero_read_repairs"] = "skipped"
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
            provenance["recovery_passes"] = str(passes)
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
        provenance.update(ladder_prov)
        # A whole-disc AR miss runs no recovery — the ladder is gated on a PARTIAL
        # mismatch, correctly, since re-reading every track is not a read-error
        # remedy. Without this the rip just fails to verify and says nothing about
        # why. Diagnostic only: the audio is untouched, because an offset that
        # verifies is not automatically the right one to store (a widely-pressed
        # disc verifies at several at once).
        total_miss = _ar_has_total_mismatch(ar_verify.tracks)
        if total_miss:
            _warn_ar_total_miss(ui)
            _ui_status(ui, "Searching for a verifying offset…")
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
        ar_summary = format_ar_report(
            ar_verify.tracks, read_offset=read_offset, reachable=ar_verify.reachable
        )
        # kgr, 2026-11-21: a container is written only when every track verified.
        # Both gates sit before _finalize_import, the first thing that touches the
        # output path or the catalogue, so an existing container of the same name
        # survives; main() reports the abort once the TUI has stopped.
        if total_miss:
            reason, details = _total_miss_message(provenance)
            raise RipAborted(EXIT_INVESTIGATE, reason, details, provenance)
        _require_every_track_verified(ar_verify.tracks, recovery_outcomes, provenance)
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
            cddb_track_lsns=None if digital_media else info.track_lsns,
            cddb_disc_last_lsn=None if digital_media else info.disc_last_lsn,
            cddb_server=cfg.cddb_server,
            ar_summary=ar_summary,
            tui=tui,
            duplicate_policy=duplicate_policy,
            auto=auto,
            preferred_country=cfg.preferred_country,
            archival=True,
            digital_media=digital_media,
        )
    finally:
        _stop_preview(track_preview)
        if ui is not None:
            ui.stop()
        # Put the drive back where we found it, on every exit path including the
        # failure one. `entry_speed_x` is None when the drive would not report at
        # entry, and restoring to a rate nobody measured is worse than leaving it —
        # so that case deliberately does nothing rather than guessing at max.
        if entry_speed_x is not None:
            drive_speed.restore_drive_speed(device, entry_speed_x)
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
        # Only a complete, fully verified container reaches this point, so
        # --no-keep-rbi can always be honoured.
        if not keep_rbi:
            rbi_path.unlink()
            return None
    return rbi_path


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
    speed: int = DEFAULT_BURN_SPEED,
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
                # No config entry. There is no table to fall back to: the EAC
                # OffsetBase suggestion that used to sit here was retired with
                # the offsets database on 2026-08-27, published write-offset
                # data being too sparse to be worth keeping. A write offset is
                # MEASURED per drive and then lives in config, so the remedy is
                # a one-off run rather than another lookup.
                log.info(
                    "No write offset configured for %s; burning uncorrected."
                    " Measure it once with `cdda2img setup --write-offset"
                    " --device %s`, which writes it to [[drives]] in"
                    " ~/.config/cdda2img/cdda2img.toml",
                    drive_name,
                    device,
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


def _require_rip_services_configured() -> None:
    """Refuse a rip, before the drive is touched, when Discogs or AcoustID is not
    configured (kgr, 2026-11-21: mandatory).

    Without Discogs the container loses the label, catalogue number and country
    wherever MusicBrainz lacks them, plus the cross-database barcode check and the
    original-year check. Without AcoustID it loses the evidence that the audio
    belongs to the selected release, and the gate that stops ``--auto`` committing
    a wrong one. Either way the archive could not be complete, and both keys are
    free. Raises ``RuntimeError``, which ``main`` reports with exit 1 ("fix the
    configuration").
    """
    from cdda2img import acoustid_lookup, discogs_lookup

    missing = []
    if not discogs_lookup.is_available():
        missing.append(
            "Discogs: DISCOGS_TOKEN not set (free at discogs.com/settings/developers)"
        )
    if not acoustid_lookup.is_available():
        missing.append(f"AcoustID: {acoustid_lookup.unavailability_reason()}")
    if missing:
        msg = (
            "a rip needs Discogs and AcoustID to build a complete archive:\n  "
            + "\n  ".join(missing)
        )
        raise RuntimeError(msg)


def _require_engine_features() -> None:
    """Refuse a rip, before the drive is touched, on an AccuDisc without every
    0.53-0.59 feature the rip turns on (kgr, 2026-11-25: "I would rather do my
    tests against 0.59.0 with all new features enabled").

    Feature-detected, never version-checked. Refusing rather than running what
    the engine has is the point: on an older install a rip would otherwise get
    half the features while its user believes every one is on. Raises
    ``RuntimeError``, which ``main`` reports with exit 1 ("fix the
    configuration": install AccuDisc 0.59.0 or later)."""
    from cdda2img.accudisc_reader import engine_version, missing_rip_features

    missing = missing_rip_features()
    if missing:
        msg = (
            f"a rip needs AccuDisc 0.59.0 or later (found {engine_version()}); "
            f"this engine lacks: {', '.join(missing)}"
        )
        raise RuntimeError(msg)


def _network_preflight(
    pipeline: str, *, auto: bool, allow_offline: bool, cddb_server: str | None
) -> str:
    """Check the services *pipeline* depends on; return the PROV value.

    Runs from dispatch so it precedes everything the pipeline invests in — drive
    offset prompts, spin-up, transcoding. Raises NetworkUnavailable (a RuntimeError,
    so ``main`` reports it and exits 1) when the run must not start. A prompt is
    offered only on a TTY without ``--auto``: an unattended run cannot answer one.
    """
    import sys

    from cdda2img.network_preflight import run_preflight, services_for

    outcome = run_preflight(
        services_for(pipeline, cddb_server=cddb_server),
        interactive=sys.stdin.isatty() and not auto,
        allow_offline=allow_offline,
        archival=pipeline.startswith("rip"),
    )
    return outcome.prov_value


def _report_unanswered(unanswered: list[str]) -> int:
    """Say which required services a written container lacks; return the exit code."""
    if not unanswered:
        return 0
    print(
        f"  Written without an answer from: {', '.join(unanswered)}."
        f" Exit status {EXIT_RETRY_LATER}; re-run once"
        f" {'it is' if len(unanswered) == 1 else 'they are'} reachable."
    )
    return EXIT_RETRY_LATER


def _profile_listing(rows: list[tuple[str, Profile, Path]]) -> list[str]:
    """``rip --list-profiles``: two lines per profile, the columns sized to the
    longest entry so a user profile's longer name (``track-constant-x20``) keeps
    them aligned. The path goes on the second line, under the granularity, and a
    third line says what damage the profile is for when it says (``best_for``,
    kgr 2026-11-25)."""
    if not rows:
        return []
    name_w = max(len(name) for name, _p, _path in rows)
    gran_w = max(len(p.granularity) for _n, p, _path in rows)
    ladder_w = max(len(p.ladder) for _n, p, _path in rows)
    lines = []
    for name, prof, path in rows:
        mark = "  (experimental: disabled)" if prof.experimental else ""
        head = (
            f"{name:<{name_w}}  {prof.granularity:<{gran_w}}  "
            f"ladder={prof.ladder:<{ladder_w}}{mark}"
        )
        lines.append(head.rstrip())
        lines.append(f"{'':<{name_w}}  {path}")
        if prof.best_for:
            lines.append(f"  best for: {prof.best_for}")
    return lines


def _dispatch(args: argparse.Namespace) -> int:
    """Run the subcommand; return the exit code (``rip_abort``: 0, or 2 when
    ``import`` wrote a container without a required service's answer). A rip that
    cannot write a complete container raises ``RipAborted`` instead."""
    if args.cmd == "create":
        from cdda2img.config import load_config

        cfg = load_config()
        auto = args.auto if args.auto is not None else cfg.auto
        preflight = _network_preflight(
            "create",
            auto=auto,
            allow_offline=args.allow_offline,
            cddb_server=cfg.cddb_server,
        )
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
            auto=auto,
            network_preflight=preflight,
            preferred_country=cfg.preferred_country,
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
            rows = [
                (name, load_profile(name), path)
                for name, path in sorted(list_profiles().items())
            ]
            for line in _profile_listing(rows):
                print(line)
            return 0

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
        auto = args.auto if args.auto is not None else cfg.auto
        _require_rip_services_configured()
        _require_engine_features()
        preflight = _network_preflight(
            "rip-digital" if args.media_override == "digital_media" else "rip",
            auto=auto,
            allow_offline=False,
            cddb_server=cfg.cddb_server,
        )
        # A rip either writes a complete container or raises RipAborted, which
        # main() turns into a failure report and exit 2 or 3.
        rip_image(
            args.device,
            loudness=args.loudness,
            output=args.output,
            preview=args.preview if args.preview is not None else cfg.preview,
            tui=args.tui if args.tui is not None else cfg.tui,
            low_dr_threshold=cfg.low_dr_threshold,
            duplicate_policy=args.duplicate,
            auto=auto,
            extract=args.extract,
            keep_rbi=not args.no_keep_rbi,
            strategy=strategy,
            network_preflight=preflight,
            media_override=args.media_override,
            recovery_speed=args.recovery_speed,
            profile_only=args.profile_only,
            recovery_passes=args.recovery_passes,
        )
    elif args.cmd == "import":
        if args.info:
            info_image(args.source)
        else:
            from cdda2img.config import load_config

            cfg = load_config()
            auto = args.auto if args.auto is not None else cfg.auto
            preflight = _network_preflight(
                "import",
                auto=auto,
                allow_offline=args.allow_offline,
                cddb_server=cfg.cddb_server,
            )
            return _report_unanswered(
                import_image(
                    args.source,
                    loudness=args.loudness,
                    output=args.output,
                    low_dr_threshold=cfg.low_dr_threshold,
                    tui=args.tui if args.tui is not None else cfg.tui,
                    duplicate_policy=args.duplicate,
                    auto=auto,
                    network_preflight=preflight,
                )
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
    return 0


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
            speed=getattr(args, "speed", DEFAULT_BURN_SPEED),
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


_R = TypeVar("_R")


def _in_daemon_thread(fn: Callable[..., _R], *args: Any, **kwargs: Any) -> Future[_R]:
    """Run *fn* on a daemon thread; its result or exception arrives on the Future.

    Not a ``ThreadPoolExecutor``: its workers are joined when its block is left
    and again at interpreter exit, so a Ctrl-C had to wait for a stalled lookup.
    A daemon thread is abandoned at exit instead, and ``Future.result()`` is an
    interruptible wait.
    """
    future: Future[_R] = Future()

    def run() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # handed to the caller, never swallowed
            future.set_exception(exc)

    threading.Thread(target=run, daemon=True, name=getattr(fn, "__name__", "")).start()
    return future


def _install_interrupt_watcher() -> None:
    """Let Ctrl-C reach an AccuDisc read in flight (kgr, 2026-11-22).

    Python acts on SIGINT only in the main thread between bytecodes, and during a
    read the main thread is inside libaccudisc. ``signal.set_wakeup_fd`` is
    written by CPython's C-level handler at once, whatever the main thread is
    doing, so a thread blocked on the pipe wakes and sets every live read's
    AccuDisc ``Cancel`` flag (``accudisc_reader.cancel_reads``). The usual
    ``KeyboardInterrupt`` still follows in the main thread, so the rip's
    ``finally`` blocks restore the drive and remove the scratch as before.

    The second of the entry-point-only global mutations beside the log handler
    and the socket timeout: it owns the process's one wakeup fd. Only bytes for
    SIGINT act; ``menu_state``'s SIGWINCH handler writes here too.
    """
    import os
    import signal

    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        signal.set_wakeup_fd(write_fd, warn_on_full_buffer=False)
    except ValueError:  # not the main thread (an embedding caller)
        os.close(read_fd)
        os.close(write_fd)
        return

    def watch() -> None:
        from cdda2img.accudisc_reader import cancel_reads

        while True:
            try:
                data = os.read(read_fd, 64)
            except OSError:
                return
            if not data:
                return
            if signal.SIGINT in data:
                cancel_reads()

    threading.Thread(target=watch, daemon=True, name="interrupt-watcher").start()


def _install_log_handler(*, verbose: bool) -> None:
    """Route every log record through one handler, at the CLI entry point (N8).

    **This is the only place in the tree that mutates global logging state**, per
    CLAUDE.md. It runs here rather than in ``TerminalUI.start()`` because the TUI
    is constructed deep inside ``rip_image``/``import_image``; the handler finds
    it later through :func:`terminal_ui.active_ui`, so the mutation stays at the
    entry point while the route stays live for the TUI's whole lifetime.

    It replaces ``basicConfig``, and the replacement is the fix rather than a
    tidy-up. ``basicConfig`` installs a ``StreamHandler`` on stderr; at default
    verbosity it installs nothing at all and ``logging.lastResort`` — itself a
    WARNING-level stderr handler — takes over. Either way a record reaches the
    terminal behind the TUI's back and strands a frame. One handler that decides
    per record where the output goes cannot double up, and installing *any*
    handler retires ``lastResort``, which only fires on an empty handler list.

    ``--verbose`` needs no special case here, which is the point of doing it this
    way. It sets the root **level** and the format; where records go is still the
    handler's single decision, so the firehose lands in the TUI's output region
    when one is running and on stderr when one is not. (``args.verbose`` never
    reaches the pipelines — it is consumed here — so making the TUI conditional
    on it would have meant threading a new parameter through the call chain.)
    """
    import logging

    from cdda2img.terminal_ui import TuiLogHandler

    handler = TuiLogHandler()
    root = logging.getLogger()
    if verbose:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-5s %(name)s: %(message)s", datefmt="%H:%M:%S"
            )
        )
        root.setLevel(logging.DEBUG)
    else:
        # Two spaces, to sit with the pipeline's own status lines. No timestamp:
        # at default verbosity these are notices to an operator watching a rip,
        # not a trace to correlate after the fact.
        handler.setFormatter(logging.Formatter("  %(levelname)s: %(message)s"))
        # The root level alone no longer filters everything: the musicbrainzngs
        # logger is lowered to INFO below, and propagation to root handlers ignores
        # the root logger's level. Without this its retry chatter would print.
        handler.setLevel(logging.WARNING)
    root.addHandler(handler)

    # MusicBrainz retry notice. musicbrainzngs retries a stalled request 8 times
    # (56 s against fast 503s, ~5 min when the connection stalls) with no public
    # knob, and says so only at INFO. The notice
    # turns those records into a short status line (mb_lookup.RetryNotice). The
    # logger is lowered to INFO only if nothing already set it lower (--verbose).
    from cdda2img.mb_lookup import RetryNotice

    mb_log = logging.getLogger("musicbrainzngs")
    if mb_log.getEffectiveLevel() > logging.INFO:
        mb_log.setLevel(logging.INFO)
    mb_log.handlers[:] = [h for h in mb_log.handlers if not isinstance(h, RetryNotice)]
    mb_log.addHandler(RetryNotice())


def _dispatch_reporting_aborts(args: argparse.Namespace) -> int:
    """:func:`_dispatch`, with the two "nothing was written" outcomes turned into
    their exit codes: a rip abort (failure report, then 2 or 3) and a network
    preflight refusal (2, re-run when the service answers)."""
    from cdda2img.network_preflight import NetworkUnavailable
    from cdda2img.rip_abort import report_to_stderr, write_report

    try:
        return _dispatch(args)
    except RipAborted as e:
        try:
            report = write_report(e)
        except OSError as exc:
            print(f"Could not write the failure report: {exc}", file=sys.stderr)
            report = None
        return report_to_stderr(e, report)
    except NetworkUnavailable as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_RETRY_LATER


def main() -> None:
    from cdda2img.recovery_profile import ProfileError

    args = parse_args()
    _install_log_handler(verbose=args.verbose)
    _install_interrupt_watcher()
    # The other entry-point-only global mutation: bound every urllib socket, the
    # only lever musicbrainzngs offers. requests-based lookups (AcoustID, Discogs)
    # ignore this default and pass their own timeout — see net.py for why one line
    # cannot cover them.
    from cdda2img.net import install_default_socket_timeout

    install_default_socket_timeout()
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
        rc = _dispatch_reporting_aborts(args)
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
    if rc:
        raise SystemExit(rc)


if __name__ == "__main__":
    main()
