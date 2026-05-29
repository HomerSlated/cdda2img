from __future__ import annotations

import argparse
import importlib.metadata
import logging
import re
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
              --raw                 Write TOC + BIN (s16be) to extracted/raw/
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
              Note: rip captures audio verbatim (1:1 via cdrdao; falls back to cd-paranoia); no silence trim or gap insertion

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
    # lookup (CDDB, MB, Discogs, AcoustID, AccurateRip) short-circuits to
    # "unavailable". Combine with R7's SQLite cache to reproduce a prior
    # rip's metadata without network access.
    parser.add_argument(
        "--no-network-services",
        action="store_true",
        help="Disable all remote metadata lookups (CDDB/MB/Discogs/AcoustID/AR).",
    )
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

    x = sub.add_parser("extract", help="Extract blocks from an RBI image")
    x.add_argument("rbi_file", type=Path, help="RBI file to extract")
    x.add_argument(
        "--raw", action="store_true", help="Extract TOC + BIN (s16be) to output dir"
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

    t_cmd = sub.add_parser("test", help="Test/validate an RBI image against the spec")
    t_cmd.add_argument("rbi_file", type=Path, help="RBI file to validate")

    r_cmd = sub.add_parser("rip", help="Rip a physical CD-DA disc to an RBI container")
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
        help="Play track 1 on a loop in the background during rip (default: from config, true)",
    )
    r_cmd.add_argument(
        "--tui",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Use terminal UI for live progress rendering (default: from config, true; no-op when stdin is not a TTY)",
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

    d_cmd = sub.add_parser("catalogue", help="Browse disc catalogue")
    d_cmd.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="catalogue database path (default: from config or XDG data dir)",
    )

    w_cmd = sub.add_parser(
        "burn", help="Burn an RBI image to a blank CD-DA disc via cdrdao"
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

            # Identify the original release before the menu so the user
            # sees "Original: <title> (<year>)" in the initial summary.
            from cdda2img.original_release import populate_original_release

            populate_original_release(disc)

            from cdda2img.metadata_menu import run_metadata_menu

            disc = run_metadata_menu(disc, source_wavs=source_wavs)

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

            provenance = {
                "mode": "create",
                "source": str(input_dir.resolve()),
                "ripper": "file",
            }
            _add_release_provenance(provenance, disc)
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
            )
            from cdda2img.catalogue import register_rbi

            register_rbi(output_file)
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
) -> None:
    import sys

    if not source.exists():
        msg = f"{source}: no such file or directory"
        raise FileNotFoundError(msg)

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

        _finalize_import(
            disc,
            temp.pcm_file,
            provenance,
            output_stem,
            loudness,
            output,
            ui=ui,
            low_dr_threshold=low_dr_threshold,
        )
    finally:
        if ui is not None:
            ui.stop()
        temp.cleanup()


def _collect_barcode_candidates(
    disc: RBIDisc,
    barcode_hints: list[tuple[str, str]] | None,
) -> list[str]:
    """Build the de-duplicated 13-digit candidate list (disc.catalog first, then hints).

    Only the *normalised* form of disc.catalog enters the list. A non-normalising
    raw value (e.g. 11-digit printed barcode) is *not* skipped silently — the
    caller uses `_pick_canonical_mcn` to substring-match raw digits against this
    candidate list as a separate, deductive step. *barcode_hints* is the R16
    tuple form ``(mb_release_id, barcode)``; the MBID is unused here.
    """
    from cdda2img.barcode import normalize_barcode

    candidates: list[str] = []
    seen: set[str] = set()
    if disc.catalog:
        norm = normalize_barcode(disc.catalog)
        if norm:
            candidates.append(norm)
            seen.add(norm)
    for _mbid, hint in barcode_hints or []:
        if hint and hint not in seen:
            candidates.append(hint)
            seen.add(hint)
    return candidates


def _pick_canonical_mcn(disc: RBIDisc, candidates: list[str]) -> str | None:
    """Choose the canonical 13-digit MCN for *disc* from *candidates*.

    Strategy (in priority order):
      1. **Substring match (deductive).** If disc.catalog contains raw digits
         (>= 7) that appear as a substring of any candidate, that candidate IS
         the MCN — printed barcodes are GTIN-12 without check digit, a prefix
         of GTIN-12, which is a suffix of EAN-13. Substring bridges all three.
      2. **First candidate (best-guess fallback).** No substring match but
         candidates exist → first one wins. Blank is worse than a guess the
         user can correct via [c] in the menu.
      3. **None.** No candidates at all → nothing to work with.
    """
    import re

    _MIN_SUBSTRING_DIGITS = 7  # below this, false positives across hints

    raw_digits = re.sub(r"\D", "", disc.catalog) if disc.catalog else ""
    if len(raw_digits) >= _MIN_SUBSTRING_DIGITS:
        for c in candidates:
            if raw_digits in c:
                return c
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
) -> RBIDisc:
    """Pre-populate disc.catalog and optionally enrich via Discogs barcode lookup.

    Two distinct phases:

    A. **Canonical MCN.** Build a candidate list from disc.catalog (when it
       normalises) and any MB barcode hints; pick one via `_pick_canonical_mcn`.
       The chosen MCN is *always* written to disc.catalog — provenance trumps
       a blank field, and the menu's [c] flow lets the user correct a wrong
       guess. This is the cornerstone of metadata handling.

    B. **Metadata enrichment.** Query Discogs by the chosen MCN. If exactly one
       result comes back and its album matches disc.album, merge full metadata
       (label, country, year, track listing). Otherwise, leave the additional
       fields alone — the MCN is still set from phase A.
    """
    from cdda2img import discogs_lookup
    from cdda2img.mb_lookup import _merge_into_disc

    candidates = _collect_barcode_candidates(disc, barcode_hints)
    chosen = _pick_canonical_mcn(disc, candidates)
    if chosen and disc.catalog != chosen:
        disc.catalog = chosen
    if not chosen or not discogs_lookup.is_available():
        return disc

    _ui_status(ui, f"Querying Discogs by barcode {chosen}…")
    results = discogs_lookup.search_by_barcode(chosen)
    if len(results) != 1:
        return disc
    hit = results[0]
    if not _albums_match(disc.album, hit.album):
        return disc
    return _merge_into_disc(hit, disc)


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
    the merge. Disagreement is computed after NFC + casefold + reissue
    suffix stripping. Value is a comma-separated list of fields that
    disagreed (``album``, ``artist``, or ``album,artist``). Absent when
    one side is blank or both agree.
    """
    fields: list[str] = []
    if (
        pre_mb_album
        and mb_album
        and _r9_normalise_for_compare(pre_mb_album)
        != _r9_normalise_for_compare(mb_album)
    ):
        fields.append("album")
    if (
        pre_mb_artist
        and mb_artist
        and pre_mb_artist != "Unknown Artist"  # raw default; not from CDDB
        and _r9_normalise_for_compare(pre_mb_artist)
        != _r9_normalise_for_compare(mb_artist)
    ):
        fields.append("artist")
    if fields:
        provenance["disagreement_cddb_mb"] = ",".join(fields)


def _r6_acoustid_corroborate(  # noqa: C901
    disc: RBIDisc,
    pcm_file: Path,
    provenance: dict[str, str],
    ui: TerminalUI | None,
) -> RBIDisc:
    """R6: pre-menu AcoustID fingerprint of tracks 1 and ceil(N/2).

    Emits ``provenance["acoustid_corroborates"]`` as YES (best AcoustID hit
    agrees with the disc's existing MB release MBID) or NO (disagrees).
    When the disc has no MB release MBID yet AND AcoustID converges
    consistently across all fingerprinted tracks on a single MBID, merge
    that release into the disc via ``_merge_into_disc``.

    No-op when ``acoustid_lookup.is_available()`` is False. Failures from
    AcoustID propagate as empty results; the function never raises.
    """
    import tempfile
    import wave

    from cdda2img import acoustid_lookup
    from cdda2img.mb_lookup import _merge_into_disc

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

    # Tally release MBIDs across all fingerprinted tracks. A consistent
    # winner is one that appears in every per-track result set.
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
    top_rid = consistent_rids[0] if consistent_rids else None

    if disc.mb_release_id:
        provenance["acoustid_corroborates"] = (
            "YES" if top_rid == disc.mb_release_id else "NO"
        )
        return disc

    # Prepop missed; merge AcoustID's consistent winner.
    if top_rid is not None:
        merged_meta = next(
            (h for hits in per_track_hits for h in hits if h.mb_release_id == top_rid),
            None,
        )
        if merged_meta is not None:
            disc = _merge_into_disc(merged_meta, disc)
            provenance["acoustid_corroborates"] = "YES"
    return disc


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
) -> None:
    """Shared post-rip/import pipeline: MB lookup → metadata menu → TOC → RG → container.

    R8: when *cddb_track_lsns*, *cddb_disc_last_lsn*, and *cddb_server* are
    provided (rip path only), CDDB and MB pre-pop run concurrently in a
    2-worker ``ThreadPoolExecutor``. Merge order is CDDB-first → MB-second
    with non-blank-wins semantics — identical to the pre-R8 serial path
    when both services return data, with the bonus that a slow CDDB no
    longer blocks MB latency. When CDDB params are None (import path)
    only MB runs.
    """
    import sys

    from cdda2img.cddb import prepopulate_from_cddb
    from cdda2img.mb_lookup import _merge_into_disc, prepopulate_from_mb
    from cdda2img.metadata_menu import run_metadata_menu

    diag = _Notes(ui)
    do_cddb = cddb_track_lsns is not None and cddb_disc_last_lsn is not None
    cddb_verbose = ui is None
    mb_verbose = (ui is None) and sys.stdin.isatty()

    _ui_status(
        ui, "Querying CDDB + MusicBrainz…" if do_cddb else "Querying MusicBrainz…"
    )
    pre_cddb_album = disc.album

    if do_cddb:
        # R8: launch both prepops concurrently. Each operates on a copy of
        # the original disc, so their results are independent and can be
        # merged serially after both return.
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as ex:
            cddb_future = ex.submit(
                prepopulate_from_cddb,
                disc,
                cddb_track_lsns,
                cddb_disc_last_lsn,
                server=cddb_server,
                verbose=cddb_verbose,
            )
            mb_future = ex.submit(prepopulate_from_mb, disc, verbose=mb_verbose)
            cddb_disc = cddb_future.result()
            mb_result = mb_future.result()

        # Serial merge: CDDB first (already applied in cddb_disc), then re-apply
        # MB's winning meta on top with non-blank-wins.
        disc = cddb_disc
        if mb_result.meta is not None:
            disc = _merge_into_disc(mb_result.meta, disc)
        # R12: CDDB status.
        provenance["lookup_status_cddb"] = _r12_status(
            attempted=True,
            has_data=bool(disc.album) and disc.album != pre_cddb_album,
            errored=False,
        )
    else:
        # Import path: MB only, sequential.
        mb_result = prepopulate_from_mb(disc, verbose=mb_verbose)
        disc = mb_result.disc

    # R9: snapshot the pre-MB album/artist (= post-CDDB or original)
    # so we can detect CDDB↔MB disagreement when both ran.
    pre_mb_album = pre_cddb_album if not do_cddb else cddb_disc.album
    pre_mb_artist = (
        disc.artist if not do_cddb else cddb_disc.artist  # post-CDDB pre-MB
    )

    if mb_result.isrc_disambiguated:
        provenance["multi_match_isrc_disambiguated"] = "YES"
    _emit_r9_disagreement(
        provenance,
        pre_mb_album,
        pre_mb_artist,
        mb_result.mb_candidate_album,
        mb_result.mb_candidate_artist,
    )
    # R12: MB status follows match_count (0 = empty, ≥1 = OK; network
    # errors inside lookup_disc_id yield 0 too — undistinguishable here).
    provenance["lookup_status_mb"] = _r12_status(
        attempted=True, has_data=mb_result.match_count > 0, errored=False
    )
    # R12: Discogs status — checked before the call.
    from cdda2img import discogs_lookup as _discogs

    discogs_attempted = _discogs.is_available()
    pre_discogs_catalog = disc.catalog
    disc = _prepopulate_from_discogs(disc, ui, barcode_hints=mb_result.barcode_hints)
    provenance["lookup_status_discogs"] = _r12_status(
        attempted=discogs_attempted,
        has_data=bool(disc.catalog) and disc.catalog != pre_discogs_catalog,
        errored=False,
    )
    # R6: pre-menu AcoustID auto-fingerprint. Gated on availability; never
    # blocks the menu. Tracks 1 and ceil(N/2) are the canonical sample.
    from cdda2img import acoustid_lookup as _acoustid

    acoustid_attempted = _acoustid.is_available()
    disc = _r6_acoustid_corroborate(disc, pcm_file, provenance, ui)
    provenance["lookup_status_acoustid"] = _r12_status(
        attempted=acoustid_attempted,
        has_data="acoustid_corroborates" in provenance,
        errored=False,
    )

    # Identify the original release BEFORE the menu so the user sees
    # "Original: <title> (<year>)" in the initial summary. The menu's
    # [r] flow lets the user override; populate_original_release's
    # internal gate (skip when original_release_found is already True)
    # makes the call here idempotent with any later override.
    from cdda2img.original_release import populate_original_release

    _ui_status(ui, "Identifying original release…")
    populate_original_release(disc)
    # R11: corroborate with Discogs master if both sources are present.
    _r11_corroborate_with_discogs_master(disc, provenance)

    # Hand the terminal over to the interactive metadata menu. The
    # ar_summary kwarg drives the AR_PAUSE state (rip pipeline only).
    if ui is not None:
        ui.pause()
    disc = run_metadata_menu(disc, source_pcm=pcm_file, ar_summary=ar_summary)
    if ui is not None:
        ui.resume()

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
    )
    # register_rbi() has interactive input() prompts — pause TUI before handing
    # the terminal over so they're visible. No resume needed; this is the last step.
    if ui is not None:
        # pause() clears the TUI output region, so any post-menu notes (RG
        # warnings) must be printed *after* pause.
        ui.pause()
        diag.flush()
        print(f"  Container: {output}")
    from cdda2img.catalogue import register_rbi

    register_rbi(output)


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
        print("  Drive: unknown (sysfs probe failed); using read_offset=0")
        return 0, None, None

    print(f"  Drive: {drive_name}")

    # 1. User-confirmed per-drive entry in config takes precedence over AR catalog.
    for d in cfg.drives:
        if d.name == drive_name:
            print(f"  Read offset: {d.read_offset:+d} samples (from config)")
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
                f"  Read offset: {offset:+d} samples (AccurateRip, {submissions} submission(s))"
            )
            try:
                save_drive_read_offset(drive_name, offset)
            except OSError as exc:
                log.warning("Could not persist drive read offset to config: %s", exc)
            return offset, None, drive_name

    # 3. Drive not configured — warn and use 0.
    if ar is None:
        print("  Drive not in AccurateRip catalog; using read_offset=0")
    else:
        print("  AccurateRip match not applied; using read_offset=0")
    return 0, None, drive_name


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


def _start_track_preview(
    device: str, work_dir: Path, ui: TerminalUI | None, enabled: bool = True
) -> TrackPreview | None:
    """Grab track 1 and start looping background playback (TTY sessions only).

    Cosmetic only — start_preview() swallows every failure and returns None,
    so the rip is never affected. Returns None when *enabled* is False (--no-preview)
    or when there is no TUI session to host the progress display.
    """
    if not enabled or ui is None:
        return None
    from cdda2img.track_preview import start_preview

    _ui_status(ui, "Grabbing track 1…")
    return start_preview(device, work_dir, _phase_progress_cb(ui, "Grabbing track 1…"))


def _stop_preview(preview: TrackPreview | None) -> None:
    """Stop background track-1 playback, if a preview is running."""
    if preview is not None:
        preview.stop()


def _rip_with_fallback(
    device: str,
    output_pcm: Path,
    read_offset: int = 0,
    ui: TerminalUI | None = None,
):
    """Try cdrdao read-cd first; fall back to cd-paranoia (full) on failure.

    *read_offset* is passed through to cd-paranoia via ``-O`` so that the
    fallback path stores offset-corrected PCM.  The cdrdao path returns raw
    PCM; the caller applies ``apply_drive_offset`` after this function returns.

    When *ui* is provided, cdrdao stdout is captured and fed to the TUI
    progress bar. Without *ui*, behaviour is unchanged.
    """
    from cdda2img.cdrdao_progress import ProgressUpdate
    from cdda2img.cdrdao_ripper import rip_cdrdao
    from cdda2img.disc_reader import rip_disc

    _ui_status(ui, f"Ripping {device} via cdrdao…")

    def _cb(update: ProgressUpdate) -> None:
        if ui is not None:
            ui.set_status(
                update.status,
                update.fraction,
                detail=f"({update.elapsed_frames}/{update.total_frames})",
            )

    progress_cb = _cb if ui is not None else None

    try:
        return rip_cdrdao(device, output_pcm, progress_cb=progress_cb), "cdrdao"
    except RuntimeError as exc:
        if ui is not None:
            ui.pause()
            print(f"  cdrdao failed: {exc}")
            print("  Falling back to cd-paranoia (paranoia=full) …")
            ui.resume()
        else:
            print(f"  cdrdao failed: {exc}")
            print("  Falling back to cd-paranoia (paranoia=full) ...")
        _ui_status(ui, "cd-paranoia (full paranoia)…")
        return rip_disc(
            device, output_pcm, paranoia="full", read_offset=read_offset
        ), "cd-paranoia"


def _ar_has_partial_mismatch(results: list) -> bool:
    """True when some (but not all) disc-in-database tracks have AR mismatches.

    All-tracks mismatch means offset misconfiguration; partial mismatch means
    sector read errors — only the latter benefits from a cd-paranoia re-rip.
    """
    in_db = [r for r in results if r.max_confidence is not None]
    if not in_db:
        return False
    n_ok = sum(
        1 for r in in_db if r.confidence_v1 is not None or r.confidence_v2 is not None
    )
    return 0 < n_ok < len(in_db)


def rip_image(  # noqa: C901
    device: str | None = None,
    loudness: str = "rg",
    output: Path | None = None,
    preview: bool = True,
    tui: bool = True,
    low_dr_threshold: float = 5.0,
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

    temp_base = resolve_temp_dir()
    temp = TempFiles(temp_base)

    ui: TerminalUI | None = None
    if tui and sys.stdin.isatty():
        from cdda2img.terminal_ui import TerminalUI as _TUI

        ui = _TUI().start()

    track_preview: TrackPreview | None = None
    try:
        # Grab track 1 first (drive is single-use), then play it on a loop in
        # the background while the rest of the rip runs.
        track_preview = _start_track_preview(device, temp_base, ui, enabled=preview)

        info, rip_type = _rip_with_fallback(device, temp.pcm_file, read_offset, ui=ui)

        track_count = len(info.disc.tracks)
        total_s = info.disc.total_seconds
        _ui_status(
            ui,
            f"{track_count} track(s), {int(total_s) // 60}:{int(total_s) % 60:02d} total",
        )

        # cdrdao has no native offset flag; correct the PCM after ripping.
        # cd-paranoia applied -O at rip time, so its PCM is already corrected.
        if rip_type == "cdrdao" and read_offset != 0:
            from cdda2img.offset_correct import apply_offset

            _ui_status(ui, "Applying drive offset correction…")
            apply_offset(temp.pcm_file, read_offset)

        # R8: CDDB query now happens inside _finalize_import in parallel
        # with the MB disc-ID lookup. The standalone call is gone; we just
        # capture the disc as-is and pass the LSN data through.
        disc = info.disc
        cddb_id = int(compute_cddb_disc_id(info.track_lsns, info.disc_last_lsn), 16)
        # Track which LSNs fed the final verify_rip call — may change if paranoia fallback fires.
        final_track_lsns = info.track_lsns
        final_disc_last_lsn = info.disc_last_lsn

        # PCM is now offset-corrected for both paths; verify_rip reads from correct positions.
        _ui_status(ui, "Verifying AccurateRip…")
        ar_verify = verify_rip(
            temp.pcm_file,
            final_track_lsns,
            final_disc_last_lsn,
            read_offset=0,
            cddb_id=cddb_id,
        )
        if ui is not None:
            ui.pause()
        print_ar_report(ar_verify.tracks, read_offset=read_offset)
        if ui is not None:
            ui.resume()

        # AR-triggered fallback: partial mismatch → read error on specific tracks.
        # Re-rip with cd-paranoia full paranoia; keep disc metadata from cdrdao scan
        # (cdrdao captures ISRC/MCN/CD-Text from subchannel; cd-paranoia -Q does not).
        if rip_type == "cdrdao" and _ar_has_partial_mismatch(ar_verify.tracks):
            from cdda2img.disc_reader import rip_disc

            n_bad = sum(
                1
                for r in ar_verify.tracks
                if r.max_confidence is not None
                and r.confidence_v1 is None
                and r.confidence_v2 is None
            )
            _ui_status(
                ui,
                f"{n_bad} track(s) failed AccurateRip — re-ripping with cd-paranoia…",
            )
            paranoia_info = rip_disc(
                device, temp.pcm_file, paranoia="full", read_offset=read_offset
            )
            rip_type = "cd-paranoia"
            final_track_lsns = paranoia_info.track_lsns
            final_disc_last_lsn = paranoia_info.disc_last_lsn
            _ui_status(ui, "Verifying AccurateRip (re-rip)…")
            ar_verify = verify_rip(
                temp.pcm_file,
                final_track_lsns,
                final_disc_last_lsn,
                read_offset=0,
                cddb_id=cddb_id,
            )
            if ui is not None:
                ui.pause()
            print_ar_report(ar_verify.tracks, read_offset=read_offset)
            if ui is not None:
                ui.resume()

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
        if drive_name is not None:
            provenance["drive_name"] = drive_name
            provenance["drive_read_offset"] = f"{read_offset:+d}"
        # R2: surface the AccurateRip transport choice + dBAR body hash so
        # later re-fetches can detect AR-side changes / mirror tampering.
        if ar_verify.transport is not None:
            provenance["arip_transport"] = ar_verify.transport
        if ar_verify.dbar_sha256 is not None:
            provenance["arip_dbar_sha256"] = ar_verify.dbar_sha256
        ar_summary = format_ar_report(ar_verify.tracks, read_offset=read_offset)
        _finalize_import(
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
        )
    finally:
        _stop_preview(track_preview)
        if ui is not None:
            ui.stop()
        temp.cleanup()


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

    use_all = all_blocks or not (raw or tracks or rg or ar or log)
    if use_all:
        opts = ExtractOptions(
            raw=True,
            tracks=True,
            rg=True,
            ar=True,
            log=True,
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
            normalize=normalize,
            warn_missing=True,
        )

    extract_data(rbi_file, opts, base_dir=base_dir)


def burn_image(
    rbi_file: Path,
    device: str | None = None,
    write_offset_override: int | None = None,
    speed: int = 4,
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
    # R10: apply the CLI offline-mode override before any subcommand
    # imports a lookup module. None lets the TOML config value stand;
    # True forces offline; False forces online (overrides config).
    from cdda2img.config import set_no_network_override

    if args.no_network_services:
        set_no_network_override(True)
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
        )
    elif args.cmd == "rip":
        from cdda2img.config import load_config

        cfg = load_config()
        rip_image(
            args.device,
            loudness=args.loudness,
            output=args.output,
            preview=args.preview if args.preview is not None else cfg.preview,
            tui=args.tui if args.tui is not None else cfg.tui,
            low_dr_threshold=cfg.low_dr_threshold,
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
            )
    elif args.cmd == "extract":
        extract_image(
            args.rbi_file,
            raw=args.raw,
            tracks=args.tracks,
            rg=args.rg,
            ar=args.ar,
            log=args.log,
            all_blocks=args.all_blocks,
            normalize=args.normalize,
            output=args.output,
        )
    else:
        _dispatch_utility(args)


def _dispatch_utility(args: argparse.Namespace) -> None:
    if args.cmd == "list":
        from cdda2img.container import list_container

        show_info = args.info or not (args.rg or args.ar or args.log)
        list_container(
            args.rbi_file, info=show_info, rg=args.rg, ar=args.ar, log=args.log
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
            yes=args.yes,
        )
    elif args.cmd == "mount":
        mount_image(args.rbi_file, slot=args.slot, mnt_dir=args.mnt_dir)


def main() -> None:
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
    except EOFError:
        print("\nAborted (no input).")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\nInterrupted.")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
