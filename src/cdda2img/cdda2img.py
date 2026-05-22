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
              --mode {master|remaster}
                                    master: preserve source as-is (no silence trim)
                                    remaster: trim silence and add inter-track gap (default)
              --loudness {rg|none}  rg: measure EBU R128 and embed RG block (default)
                                    none: skip loudness analysis
              --strategy {fcfs,aatc,best,meta}
                                    Disc batching strategy (default: aatc)
                fcfs  first-come-first-served: fill one disc in input order, stop
                aatc  all-as-they-come: fill discs in input order, as many as needed
                best  global bin-packing to minimise total disc count (order not preserved)
                meta  group tracks by embedded disc-number tag; untagged tracks form a final group
              --no-trim-silence     Skip silence trimming in remaster mode
              --preserve-pregaps    Preserve pre-gaps in remaster mode (no-op for audio-file sources)

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
              --output <path>       Output .rbi path (default: derived from album title)
              Note: rip always uses master mode (1:1 capture via cdrdao; falls back to cd-paranoia)

            import options:
              --loudness {rg|none}  rg: embed EBU R128 ReplayGain block (default); none: skip
              --output <path>       Output .rbi path (default: derived from album title)
              --info                Dry-run: parse and display image metadata; do not import
              Note: import always uses master mode (1:1 conversion; s16be→s16le only)
              Accepts: cdrdao .toc file, or a DDP 2.0 image directory (must contain DDPID)

            burn options:
              --speed N             Burn speed in CD-DA drive units (default: 4)
              --write-offset N      Write offset override in samples (default: from config)
              --yes                 Skip confirmation prompt (non-interactive burn)

            mount options:
              --slot N              cdemu slot to load into (default: first free)
              --mnt-dir PATH        Directory for extracted TOC+BIN (default: ./mnt)

            examples:
              cdda2img r
              cdda2img r /dev/sr0 --loudness none --output mydisc.rbi
              cdda2img c /music/album
              cdda2img c /music/album --mode master --loudness none
              cdda2img c /music/album --strategy best
              cdda2img c /music/album --no-trim-silence
              cdda2img x album.rbi
              cdda2img x album.rbi --tracks
              cdda2img x album.rbi --raw
              cdda2img x album.rbi --tracks --raw --rg
              cdda2img x album.rbi --normalize
              cdda2img i disc.toc
              cdda2img i disc.toc --loudness none --output mydisc.rbi
              cdda2img i /path/to/ddp_dir
              cdda2img i /path/to/ddp_dir --output mydisc.rbi
              cdda2img w album.rbi
              cdda2img w album.rbi /dev/sr0 --speed 8
              cdda2img w album.rbi --write-offset -30 --yes
              cdda2img l album.rbi
              cdda2img l album.rbi --ar
              cdda2img t album.rbi
              cdda2img m album.rbi
              cdda2img m album.rbi --slot 1 --mnt-dir /tmp/mnt
        """),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"cdda2img {importlib.metadata.version('cdda2img')}",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("c", help="Create an RBI image from a directory of audio files")
    c.add_argument("input_dir", type=Path, help="Directory containing audio files")
    c.add_argument(
        "--mode",
        default="remaster",
        choices=["master", "remaster"],
        help="master: no silence trim; remaster: trim silence and add gap (default: remaster)",
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
        "--trim-silence",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Trim leading/trailing silence in remaster mode (default: enabled; no-op in master mode)",
    )
    c.add_argument(
        "--preserve-pregaps",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Preserve pre-gaps in remaster mode (default: disabled; no-op for audio-file sources)",
    )
    c.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output .rbi file path (default: derived from album title); multi-disc: used as base name",
    )

    x = sub.add_parser("x", help="Extract blocks from an RBI image")
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

    l_cmd = sub.add_parser("l", help="List the contents of an RBI image")
    l_cmd.add_argument("rbi_file", type=Path, help="RBI file to list")
    l_cmd.add_argument(
        "--info",
        action="store_true",
        help="Show disc/block/track summary (default if no flags)",
    )
    l_cmd.add_argument("--rg", action="store_true", help="Show ReplayGain data")
    l_cmd.add_argument("--ar", action="store_true", help="Show AccurateRip report")
    l_cmd.add_argument("--log", action="store_true", help="Show rip log")

    t_cmd = sub.add_parser("t", help="Test/validate an RBI image against the spec")
    t_cmd.add_argument("rbi_file", type=Path, help="RBI file to validate")

    r_cmd = sub.add_parser("r", help="Rip a physical CD-DA disc to an RBI container")
    r_cmd.add_argument(
        "device",
        nargs="?",
        default=None,
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
        help="Output .rbi file path (default: derived from album title)",
    )
    i_cmd = sub.add_parser(
        "i",
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
        help="Output .rbi file path (default: derived from album title)",
    )
    i_cmd.add_argument(
        "--info",
        action="store_true",
        help="Dry-run: parse and display image metadata without importing",
    )

    d_cmd = sub.add_parser("d", help="Browse disc catalogue")
    d_cmd.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="catalogue database path (default: from config or XDG data dir)",
    )

    w_cmd = sub.add_parser(
        "w", help="Burn an RBI image to a blank CD-DA disc via cdrdao"
    )
    w_cmd.add_argument("rbi_file", type=Path, help="RBI file to burn")
    w_cmd.add_argument(
        "device",
        nargs="?",
        default=None,
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

    m_cmd = sub.add_parser("m", help="Mount an RBI image as a virtual disc via cdemu")
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


def _add_release_provenance(provenance: dict, disc: RBIDisc) -> None:
    """Append release-intelligence fields to *provenance* if populated on *disc*."""
    if disc.remastered_source != "UNKNOWN":
        provenance["remastered"] = disc.remastered_source
    if disc.release_date:
        provenance["release_date"] = disc.release_date
    if disc.original_release_date:
        provenance["original_release_date"] = disc.original_release_date
    if disc.mb_release_id:
        provenance["mb_release_id"] = disc.mb_release_id
    if disc.set_title:
        provenance["set_title"] = disc.set_title


def _unique_path(stem: str, ext: str) -> Path:
    """Return a non-colliding Path for {stem}.{ext}, appending _1, _2... if needed."""
    p = Path(f"{stem}.{ext}")
    if not p.exists():
        return p
    for i in range(1, 10000):
        p = Path(f"{stem}_{i}.{ext}")
        if not p.exists():
            return p
    msg = f"Cannot find unique path for {stem}.{ext}"
    raise RuntimeError(msg)


def _check_batch_limits(batches: list[list[Path]]) -> None:
    """Raise ValueError if any batch exceeds Red Book track or duration limits."""
    for disc_idx, batch in enumerate(batches, start=1):
        n = len(batch)
        total_min = sum(get_audio_duration_minutes(f) for f in batch)
        if n > MAX_TRACKS or total_min > MAX_RUNTIME_MINUTES:
            msg = (
                f"Disc {disc_idx} would have {n} tracks and {total_min:.1f} min"
                f" (Red Book limit: {MAX_TRACKS} tracks / {MAX_RUNTIME_MINUTES} min)."
                f" Re-tag files with correct disc numbers or choose a different strategy."
            )
            raise ValueError(msg)


def create_image(
    input_dir: Path,
    mode: str = "remaster",
    loudness: str = "rg",
    strategy: str = DEFAULT_STRATEGY,
    trim_silence: bool = True,
    output: Path | None = None,
) -> None:
    files = sorted(
        p for p in input_dir.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    batches = select_batches(files, strategy)
    if not batches:
        print("No audio files found.")
        return

    _check_batch_limits(batches)

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

                if mode == "remaster" and trim_silence:
                    print(f"  Trimming      {i:2}: {track.stem}")
                    trim = temp.temp_track(i, "_trim.wav")
                    trim_silence_cd_da(str(trans), str(trim), SILENCE_PAD_DUR)
                    source_wavs.append(trim)
                else:
                    if mode == "remaster":
                        print(
                            f"  Skipping silence trim (--no-trim-silence): {track.stem}"
                        )
                    else:
                        print(f"  Skipping silence trim (master mode): {track.stem}")
                    source_wavs.append(trans)

            print("  Concatenating tracks")
            concat_wav(source_wavs, temp.pcm_pre)
            wav_to_raw_pcm(temp.pcm_pre, temp.pcm_file)

            durations = get_track_durations(source_wavs)
            disc = RBIDisc(
                album=album, artist=artist, disc_number=disc_num, disc_total=disc_total
            )
            disc.tracks = build_toc_entries(batch, durations, disc)

            from cdda2img.metadata_menu import run_metadata_menu

            disc = run_metadata_menu(disc, source_wavs=source_wavs)

            raw_titles = [re.sub(r"^\d{1,2}[-. ]+", "", p.stem) for p in batch]
            provenance = {
                "mode": "c",
                "source": str(input_dir.resolve()),
                "ripper": "file",
            }
            _add_release_provenance(provenance, disc)
            toc_data = generate_toc(disc, raw_titles=raw_titles)

            rg_block: bytes | None = None
            if loudness == "rg":
                from cdda2img.replaygain import analyse, pack_rg_block

                print("  Measuring loudness (EBU R128)...")
                rg_result = analyse(source_wavs)
                for warning in rg_result.warnings:
                    print(f"  Warning: {warning}")
                print(
                    f"  Album gain: {rg_result.album_gain:+.2f} dB  "
                    f"peak: {rg_result.album_peak:.4f}  "
                    f"LRA: {rg_result.album_lra:.1f} LU"
                )
                rg_block = pack_rg_block(rg_result)

            if output is not None:
                if disc_total == 1:
                    output_file = output
                else:
                    output_file = (
                        output.parent
                        / f"{output.stem}_disc{disc_num}{output.suffix or '.rbi'}"
                    )
            else:
                stem = album if disc_total == 1 else f"{album}_disc{disc_num}"
                output_file = _unique_path(stem, "rbi")
            container_flags = FLAG_MASTER_MODE if mode == "master" else 0
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
    source: Path, loudness: str = "rg", output: Path | None = None
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
                "mode": "i",
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
                "mode": "i",
                "source": str(source.resolve()),
                "ripper": "toc",
            }
        elif source.suffix.lower() == ".nrg":
            from cdda2img.nrg_reader import import_nrg

            _ui_status(ui, f"Importing {source.name}…")
            disc, _ = import_nrg(source, temp.pcm_file)
            output_stem = sanitize_title(disc.album) or source.stem
            provenance = {
                "mode": "i",
                "source": str(source.resolve()),
                "ripper": "nrg",
            }
        elif source.suffix.lower() == ".ccd":
            from cdda2img.ccd_reader import import_ccd

            _ui_status(ui, f"Importing {source.name}…")
            disc, _ = import_ccd(source, temp.pcm_file)
            output_stem = sanitize_title(disc.album) or source.stem
            provenance = {
                "mode": "i",
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
            disc, temp.pcm_file, provenance, output_stem, loudness, output, ui=ui
        )
    finally:
        if ui is not None:
            ui.stop()
        temp.cleanup()


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
) -> None:
    """Shared post-rip/import pipeline: MB lookup → metadata menu → TOC → RG → container."""
    import sys

    from cdda2img.mb_lookup import prepopulate_from_mb
    from cdda2img.metadata_menu import run_metadata_menu

    _ui_status(ui, "Querying MusicBrainz…")
    # Suppress verbose MB output when TUI is active — status line serves that role.
    disc = prepopulate_from_mb(disc, verbose=(ui is None) and sys.stdin.isatty())

    # Hand the terminal over to the interactive metadata menu.
    if ui is not None:
        ui.pause()
    disc = run_metadata_menu(disc, source_pcm=pcm_file)
    if ui is not None:
        ui.resume()

    if output is None:
        new_stem = sanitize_title(disc.album)
        if new_stem:
            output_stem = new_stem

    rlog_block: bytes | None = None
    if rlog_builder is not None:
        rlog_block = rlog_builder.finalize(disc)

    _add_release_provenance(provenance, disc)
    toc_data = generate_toc(disc)

    rg_block: bytes | None = None
    if loudness == "rg":
        from cdda2img.replaygain import analyse_raw, pack_rg_block

        _ui_status(ui, "Measuring loudness (EBU R128)…")
        rg_result = analyse_raw(
            disc,
            pcm_file,
            progress_cb=_phase_progress_cb(ui, "Measuring loudness (EBU R128)…"),
        )
        for warning in rg_result.warnings:
            _ui_print(ui, f"  Warning: {warning}")
        rg_summary = (
            f"Album gain: {rg_result.album_gain:+.2f} dB  "
            f"peak: {rg_result.album_peak:.4f}  "
            f"LRA: {rg_result.album_lra:.1f} LU"
        )
        _ui_status(ui, rg_summary)
        rg_block = pack_rg_block(rg_result)

    _ui_status(ui, "Building container…")
    if output is None:
        output = _unique_path(output_stem, "rbi")

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
        ui.add_output(f"  Container: {output}")
        ui.pause()
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
    device: str, work_dir: Path, ui: TerminalUI | None
) -> TrackPreview | None:
    """Grab track 1 and start looping background playback (TTY sessions only).

    Cosmetic only — start_preview() swallows every failure and returns None,
    so the rip is never affected.
    """
    if ui is None:
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


def rip_image(
    device: str | None = None,
    loudness: str = "rg",
    output: Path | None = None,
) -> None:
    import sys

    from cdda2img.accuraterip import pack_arip_block, print_ar_report, verify_rip
    from cdda2img.cddb import compute_cddb_disc_id, prepopulate_from_cddb
    from cdda2img.config import load_config

    cfg = load_config()
    device = device or cfg.default_device
    # Drive offset resolution may prompt the user (input()) — must happen before TUI.
    read_offset, _write_offset, drive_name = _resolve_drive_offsets(device, cfg)

    temp_base = resolve_temp_dir()
    temp = TempFiles(temp_base)

    ui: TerminalUI | None = None
    if sys.stdin.isatty():
        from cdda2img.terminal_ui import TerminalUI as _TUI

        ui = _TUI().start()

    preview: TrackPreview | None = None
    try:
        # Grab track 1 first (drive is single-use), then play it on a loop in
        # the background while the rest of the rip runs.
        preview = _start_track_preview(device, temp_base, ui)

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

        _ui_status(ui, "Querying CDDB…")
        disc = prepopulate_from_cddb(
            info.disc,
            info.track_lsns,
            info.disc_last_lsn,
            server=cfg.cddb_server,
            verbose=ui is None,
        )

        cddb_id = int(compute_cddb_disc_id(info.track_lsns, info.disc_last_lsn), 16)
        # Track which LSNs fed the final verify_rip call — may change if paranoia fallback fires.
        final_track_lsns = info.track_lsns
        final_disc_last_lsn = info.disc_last_lsn

        # PCM is now offset-corrected for both paths; verify_rip reads from correct positions.
        _ui_status(ui, "Verifying AccurateRip…")
        ar_results = verify_rip(
            temp.pcm_file,
            final_track_lsns,
            final_disc_last_lsn,
            read_offset=0,
            cddb_id=cddb_id,
        )
        if ui is not None:
            ui.pause()
        print_ar_report(ar_results, read_offset=read_offset)
        if ui is not None:
            ui.resume()

        # AR-triggered fallback: partial mismatch → read error on specific tracks.
        # Re-rip with cd-paranoia full paranoia; keep disc metadata from cdrdao scan
        # (cdrdao captures ISRC/MCN/CD-Text from subchannel; cd-paranoia -Q does not).
        if rip_type == "cdrdao" and _ar_has_partial_mismatch(ar_results):
            from cdda2img.disc_reader import rip_disc

            n_bad = sum(
                1
                for r in ar_results
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
            ar_results = verify_rip(
                temp.pcm_file,
                final_track_lsns,
                final_disc_last_lsn,
                read_offset=0,
                cddb_id=cddb_id,
            )
            if ui is not None:
                ui.pause()
            print_ar_report(ar_results, read_offset=read_offset)
            if ui is not None:
                ui.resume()

        arip_block = pack_arip_block(
            ar_results, final_track_lsns, final_disc_last_lsn, cddb_id
        )

        from cdda2img.rip_log import RipLogBuilder

        rlog_builder = RipLogBuilder(
            rip_type=rip_type,
            drive_name=drive_name,
            read_offset=read_offset,
        )
        rlog_builder.ar_results = ar_results
        rlog_builder.cddb_id = cddb_id

        output_stem = sanitize_title(disc.album) or device.lstrip("/").replace("/", "_")
        provenance: dict[str, str] = {
            "mode": "r",
            "source": device,
            "ripper": rip_type,
        }
        if drive_name is not None:
            provenance["drive_name"] = drive_name
            provenance["drive_read_offset"] = f"{read_offset:+d}"
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
        )
    finally:
        _stop_preview(preview)
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
    burn_disc(rbi_file, device=device, write_offset=write_offset, speed=speed, yes=yes)


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
    if args.cmd == "c":
        create_image(
            args.input_dir,
            mode=args.mode,
            loudness=args.loudness,
            strategy=args.strategy,
            trim_silence=args.trim_silence,
            output=args.output,
        )
    elif args.cmd == "r":
        rip_image(
            args.device,
            loudness=args.loudness,
            output=args.output,
        )
    elif args.cmd == "i":
        if args.info:
            info_image(args.source)
        else:
            import_image(args.source, loudness=args.loudness, output=args.output)
    elif args.cmd == "x":
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
    if args.cmd == "l":
        from cdda2img.container import list_container

        show_info = args.info or not (args.rg or args.ar or args.log)
        list_container(
            args.rbi_file, info=show_info, rg=args.rg, ar=args.ar, log=args.log
        )
    elif args.cmd == "t":
        from cdda2img.container import verify_container

        if not verify_container(args.rbi_file):
            raise SystemExit(1)
    elif args.cmd == "d":
        from cdda2img.catalogue_menu import run_catalogue_menu

        run_catalogue_menu(Path(args.db) if args.db else None)
    elif args.cmd == "w":
        burn_image(
            args.rbi_file,
            device=args.device,
            write_offset_override=args.write_offset,
            speed=args.speed,
            yes=args.yes,
        )
    elif args.cmd == "m":
        mount_image(args.rbi_file, slot=args.slot, mnt_dir=args.mnt_dir)


def main() -> None:
    args = parse_args()
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
