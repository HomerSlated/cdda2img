import argparse
import importlib.metadata
import logging
import re
import tempfile
import textwrap
import wave
from pathlib import Path

from cdda2img.concat import concat_wav
from cdda2img.container import (
    TempFiles,
    build_container,
    extract_data,
    resolve_temp_dir,
    wav_to_raw_pcm,
)
from cdda2img.input_selector import select_batches
from cdda2img.metadata import derive_album_info, read_source_rg_tags
from cdda2img.rbi_format import (
    CD_FRAMES_PER_SECOND,
    FLAG_MASTER_MODE,
    PCM_BIT_DEPTH,
    PCM_CHANNELS,
    PCM_SAMPLE_RATE,
    RBIDisc,
)
from cdda2img.silence import trim_silence_cd_da
from cdda2img.toc import build_toc_entries, generate_toc, get_track_durations
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
              --strategy {fcfs,aatc,bech,ball}
                                    Disc batching strategy (default: aatc)
                fcfs  first-come-first-served: fill one disc in input order, stop
                aatc  all-as-they-come: fill discs in input order, as many as needed
                bech  best-each: pack each disc as full as possible in turn (order not preserved)
                ball  best-all: global bin-packing to minimise total disc count (order not preserved)
              --no-trim-silence     Skip silence trimming in remaster mode
              --preserve-pregaps    Preserve pre-gaps in remaster mode (no-op for audio-file sources)

            extract options:
              --tracks              Write per-track FLAC files and CUE sheet (default)
              --raw                 Write raw PCM (.s16le) and TOC; .rg.json sidecar if RG block present
              --normalize           Normalise extracted FLACs to -18 LUFS (EBU R128);
                                    mutually exclusive with RG tag embedding
              (--tracks and --raw may be combined; --normalize requires --tracks)

            rip options:
              --loudness {rg|none}  rg: embed EBU R128 ReplayGain block (default); none: skip
              --output <path>       Output .rbi path (default: derived from album title)
              Note: rip always uses master mode (1:1 capture via cdrdao; falls back to cd-paranoia)

            import options:
              --loudness {rg|none}  rg: embed EBU R128 ReplayGain block (default); none: skip
              --output <path>       Output .rbi path (default: derived from album title)
              Note: import always uses master mode (1:1 conversion; s16be→s16le only)
              Accepts: cdrdao .toc file, or a DDP 2.0 image directory (must contain DDPID)

            examples:
              cdda2img r
              cdda2img r /dev/sr0 --loudness none --output mydisc.rbi
              cdda2img c /music/album
              cdda2img c /music/album --mode master --loudness none
              cdda2img c /music/album --strategy ball
              cdda2img c /music/album --no-trim-silence
              cdda2img x album.rbi
              cdda2img x album.rbi --raw
              cdda2img x album.rbi --tracks --raw
              cdda2img x album.rbi --normalize
              cdda2img i disc.toc
              cdda2img i disc.toc --loudness none --output mydisc.rbi
              cdda2img i /path/to/ddp_dir
              cdda2img i /path/to/ddp_dir --output mydisc.rbi
              cdda2img l album.rbi
              cdda2img t album.rbi
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
        choices=["fcfs", "aatc", "bech", "ball"],
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

    x = sub.add_parser("x", help="Extract TOC and audio from an RBI image")
    x.add_argument("rbi_file", type=Path, help="RBI file to extract")
    x.add_argument("--raw", action="store_true", help="Write raw PCM (.s16le) and TOC")
    x.add_argument(
        "--tracks", action="store_true", help="Write per-track FLAC files and CUE"
    )
    x.add_argument(
        "--normalize",
        action="store_true",
        help="Apply EBU R128 normalisation to extracted FLACs (mutually exclusive with RG tag embedding)",
    )

    l_cmd = sub.add_parser("l", help="List the contents of an RBI image")
    l_cmd.add_argument("rbi_file", type=Path, help="RBI file to list")

    t_cmd = sub.add_parser("t", help="Test/validate an RBI image against the spec")
    t_cmd.add_argument("rbi_file", type=Path, help="RBI file to validate")

    r_cmd = sub.add_parser("r", help="Rip a physical CD-DA disc to an RBI container")
    r_cmd.add_argument(
        "device",
        nargs="?",
        default="/dev/sr0",
        help="Optical drive device (default: /dev/sr0)",
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
        help="Import a cdrdao TOC+BIN or DDP 2.0 image as an RBI container (master mode)",
    )
    i_cmd.add_argument(
        "source", type=Path, help="cdrdao .toc file or DDP 2.0 image directory"
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

    return parser.parse_args()


def _add_release_provenance(provenance: dict, disc: RBIDisc) -> None:
    """Append release-intelligence fields to *provenance* if populated on *disc*."""
    if disc.remastered_source != "UNKNOWN":
        provenance["REMASTERED_SOURCE"] = disc.remastered_source
    if disc.release_date:
        provenance["RELEASE_DATE"] = disc.release_date
    if disc.original_release_date:
        provenance["ORIGINAL_RELEASE_DATE"] = disc.original_release_date
    if disc.mb_release_id:
        provenance["MB_RELEASE_ID"] = disc.mb_release_id


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


def create_image(
    input_dir: Path,
    mode: str = "remaster",
    loudness: str = "rg",
    strategy: str = DEFAULT_STRATEGY,
    trim_silence: bool = True,
) -> None:
    files = sorted(
        p for p in input_dir.iterdir() if p.is_file() and not p.name.startswith(".")
    )
    batches = select_batches(files, strategy)
    if not batches:
        print("No audio files found.")
        return

    meta = derive_album_info(files)
    album = meta["album"]
    artist = meta["artist"]
    disc_total = len(batches)

    temp_base = resolve_temp_dir()

    for disc_num, batch in enumerate(batches, start=1):
        print(f"\nDisc {disc_num}/{disc_total}: {len(batch)} tracks")
        temp = TempFiles(temp_base)
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
                    print(f"  Skipping silence trim (--no-trim-silence): {track.stem}")
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

        source_rg = [read_source_rg_tags(p) for p in batch]
        raw_titles = [re.sub(r"^\d{2} ", "", p.stem) for p in batch]
        provenance = {
            "MODE": "c",
            "SOURCE": str(input_dir.resolve()),
            "TYPE": "audio files",
        }
        _add_release_provenance(provenance, disc)
        toc_data = generate_toc(
            disc, source_rg=source_rg, raw_titles=raw_titles, provenance=provenance
        )

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

        stem = album if disc_total == 1 else f"{album}_disc{disc_num}"
        output_file = _unique_path(stem, "rbi")
        container_flags = FLAG_MASTER_MODE if mode == "master" else 0
        build_container(
            temp.pcm_file,
            toc_data,
            disc,
            output_file,
            rg_block=rg_block,
            extra_flags=container_flags,
        )
        temp.cleanup()


def _per_track_wavs(disc: RBIDisc, pcm_path: Path, out_dir: Path) -> list[Path]:
    """Slice raw s16le PCM into per-track WAV files for loudness analysis."""
    bytes_per_frame = (
        (PCM_SAMPLE_RATE // CD_FRAMES_PER_SECOND) * PCM_CHANNELS * (PCM_BIT_DEPTH // 8)
    )
    paths: list[Path] = []
    with open(pcm_path, "rb") as f:
        for track in disc.tracks:
            audio_start = track.start_frame + track.pregap_frames
            f.seek(audio_start * bytes_per_frame)
            pcm_data = f.read(track.duration_frames * bytes_per_frame)
            tw = out_dir / f"track{track.track_number:02d}.wav"
            with wave.open(str(tw), "wb") as w:
                w.setnchannels(PCM_CHANNELS)
                w.setsampwidth(PCM_BIT_DEPTH // 8)
                w.setframerate(PCM_SAMPLE_RATE)
                w.writeframes(pcm_data)
            paths.append(tw)
    return paths


def import_image(
    source: Path, loudness: str = "rg", output: Path | None = None
) -> None:
    from cdda2img.toc import sanitize_title

    if not source.exists():
        msg = f"{source}: no such file or directory"
        raise FileNotFoundError(msg)

    temp_base = resolve_temp_dir()
    temp = TempFiles(temp_base)

    try:
        if source.is_dir():
            from cdda2img.ddp_reader import import_ddp

            print(f"Importing DDP image {source.name} (master mode) ...")
            disc, _ = import_ddp(source, temp.pcm_file)
            output_stem = sanitize_title(disc.album) or source.name
            provenance = {
                "MODE": "i",
                "SOURCE": str(source.resolve()),
                "TYPE": "Gear Pro DDP 2.0",
            }
        elif source.suffix.lower() == ".toc":
            from cdda2img.cdrdao_reader import (
                _find_bin_filename,
                convert_cdrdao_bin_to_wav,
                parsed_to_rbi_disc,
            )
            from cdda2img.toc_parser import parse_toc

            print(f"Importing {source.name} (master mode) ...")
            toc_text = source.read_text(encoding="utf-8")
            bin_name = _find_bin_filename(toc_text)
            bin_path = source.parent / bin_name
            if not bin_path.exists():
                msg = f"BIN file not found: {bin_path}"
                raise FileNotFoundError(msg)

            disc = parsed_to_rbi_disc(parse_toc(toc_text.encode("utf-8")))

            print(f"  Converting {bin_name} (s16be → s16le) ...")
            convert_cdrdao_bin_to_wav(bin_path, temp.pcm_pre)
            wav_to_raw_pcm(temp.pcm_pre, temp.pcm_file)
            output_stem = sanitize_title(disc.album) or source.stem
            provenance = {
                "MODE": "i",
                "SOURCE": str(source.resolve()),
                "TYPE": "cdrdao TOC/BIN",
            }
        else:
            msg = (
                f"{source.name}: expected a cdrdao .toc file or DDP 2.0 image directory"
            )
            raise ValueError(msg)

        _finalize_import(disc, temp.pcm_file, provenance, output_stem, loudness, output)
    finally:
        temp.cleanup()


def _finalize_import(
    disc: RBIDisc,
    pcm_file: Path,
    provenance: dict[str, str],
    output_stem: str,
    loudness: str,
    output: Path | None,
) -> None:
    """Shared post-rip/import pipeline: MB lookup → metadata menu → TOC → RG → container."""
    import sys

    from cdda2img.mb_lookup import prepopulate_from_mb
    from cdda2img.metadata_menu import run_metadata_menu

    disc = prepopulate_from_mb(disc, verbose=sys.stdin.isatty())
    disc = run_metadata_menu(disc, source_pcm=pcm_file)

    _add_release_provenance(provenance, disc)
    toc_data = generate_toc(disc, provenance=provenance)

    rg_block: bytes | None = None
    if loudness == "rg":
        from cdda2img.replaygain import analyse, pack_rg_block

        print("  Measuring loudness (EBU R128)...")
        with tempfile.TemporaryDirectory() as td:
            track_wavs = _per_track_wavs(disc, pcm_file, Path(td))
            rg_result = analyse(track_wavs)
        for warning in rg_result.warnings:
            print(f"  Warning: {warning}")
        print(
            f"  Album gain: {rg_result.album_gain:+.2f} dB  "
            f"peak: {rg_result.album_peak:.4f}  "
            f"LRA: {rg_result.album_lra:.1f} LU"
        )
        rg_block = pack_rg_block(rg_result)

    if output is None:
        output = _unique_path(output_stem, "rbi")

    build_container(
        pcm_file,
        toc_data,
        disc,
        output,
        rg_block=rg_block,
        extra_flags=FLAG_MASTER_MODE,
    )


def _resolve_drive_offset(
    device: str, cfg
) -> tuple[int, str | None]:  # cfg: Config (inline import avoids top-level dep)
    """Return ``(offset, drive_name)`` for *device*.

    *drive_name* is the normalised sysfs name (e.g. ``"PLEXTOR DVDR PX-716A"``),
    or ``None`` when the sysfs probe fails.

    Resolution order for *offset*:
      1. Per-drive entry in cfg.drives (user-confirmed; always takes precedence).
      2. AccurateRip catalog (auto-applied when submissions >= _MIN_AR_CONFIDENCE;
         prompts the user when confidence is lower).
      3. cfg.drive_offset (global fallback).

    Confirmed offsets are persisted to [[drives]] in cdda2img.toml so that
    subsequent rips skip the catalog lookup entirely.
    """
    import sys

    from cdda2img.config import DriveConfig, save_drive
    from cdda2img.db import open_drive_offsets_db
    from cdda2img.drive_info import (
        ensure_drive_offsets,
        find_drive_offset,
        probe_drive_name,
    )

    drive_name = probe_drive_name(device)
    if drive_name is None:
        print(
            f"  Drive: unknown (sysfs probe failed); using offset {cfg.drive_offset:+d}"
        )
        return cfg.drive_offset, None

    print(f"  Drive: {drive_name}")

    # 1. User-confirmed per-drive entry in config takes precedence over AR catalog.
    for d in cfg.drives:
        if d.name == drive_name:
            print(f"  Offset: {d.offset:+d} samples (from config)")
            return d.offset, drive_name

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
                f"  Offset: {offset:+d} samples (AccurateRip, {submissions} submission(s))"
            )
            try:
                save_drive(DriveConfig(name=drive_name, offset=offset))
            except OSError as exc:
                log.warning("Could not persist drive offset to config: %s", exc)
            return offset, drive_name

    # 3. Global config fallback.
    if ar is None:
        print(f"  Drive not in AccurateRip catalog; using offset {cfg.drive_offset:+d}")
    else:
        print(f"  AccurateRip match not applied; using offset {cfg.drive_offset:+d}")
    return cfg.drive_offset, drive_name


def _rip_with_fallback(device: str, output_pcm: Path):
    """Try cdrdao read-cd first; fall back to cd-paranoia (full) on failure."""
    from cdda2img.cdrdao_ripper import rip_cdrdao
    from cdda2img.disc_reader import rip_disc

    print(f"Ripping {device} via cdrdao ...")
    try:
        return rip_cdrdao(device, output_pcm), "cdrdao"
    except RuntimeError as exc:
        print(f"  cdrdao failed: {exc}")
        print("  Falling back to cd-paranoia (paranoia=full) ...")
        return rip_disc(device, output_pcm, paranoia="full"), "cd-paranoia"


def rip_image(
    device: str,
    loudness: str = "rg",
    output: Path | None = None,
) -> None:
    from cdda2img.accuraterip import print_ar_report, verify_rip
    from cdda2img.cddb import compute_cddb_disc_id, prepopulate_from_cddb
    from cdda2img.config import load_config
    from cdda2img.toc import sanitize_title

    cfg = load_config()
    drive_offset, drive_name = _resolve_drive_offset(device, cfg)

    temp_base = resolve_temp_dir()
    temp = TempFiles(temp_base)
    try:
        info, rip_type = _rip_with_fallback(device, temp.pcm_file)

        track_count = len(info.disc.tracks)
        total_s = info.disc.total_seconds
        print(
            f"  {track_count} track(s), "
            f"{int(total_s) // 60}:{int(total_s) % 60:02d} total"
        )

        disc = prepopulate_from_cddb(
            info.disc, info.track_lsns, info.disc_last_lsn, server=cfg.cddb_server
        )

        cddb_id = int(compute_cddb_disc_id(info.track_lsns, info.disc_last_lsn), 16)
        ar_results = verify_rip(
            temp.pcm_file,
            info.track_lsns,
            info.disc_last_lsn,
            drive_offset=drive_offset,
            cddb_id=cddb_id,
        )
        print_ar_report(ar_results, drive_offset=drive_offset)

        output_stem = sanitize_title(disc.album) or device.lstrip("/").replace("/", "_")
        provenance: dict[str, str] = {
            "MODE": "r",
            "SOURCE": device,
            "TYPE": rip_type,
        }
        if drive_name is not None:
            provenance["DRIVE_NAME"] = drive_name
            provenance["DRIVE_OFFSET"] = f"{drive_offset:+d}"
        _finalize_import(disc, temp.pcm_file, provenance, output_stem, loudness, output)
    finally:
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


def _normalize_flac(path: Path) -> None:
    """Normalize a FLAC file in-place to -18 LUFS (EBU R128 / ReplayGain 2.0 reference)."""
    import logging

    from ffmpeg_normalize import FFmpegNormalize

    logging.getLogger("ffmpeg_normalize").setLevel(logging.ERROR)
    tmp = path.with_suffix(".normalizing.flac")
    try:
        norm = FFmpegNormalize(
            normalization_type="ebu",
            target_level=-18.0,
            auto_lower_loudness_target=True,
            keep_loudness_range_target=True,
            audio_codec="flac",
            sample_rate=44100,
            audio_channels=2,
            progress=False,
        )
        norm.add_media_file(str(path), str(tmp))
        norm.run_normalization()
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def extract_image(
    rbi_file: Path, raw: bool, tracks: bool, normalize: bool = False
) -> None:
    from cdda2img.container import read_header
    from cdda2img.toc_parser import parse_toc
    from cdda2img.track_extract import collect_tracks_output_paths

    if not raw and not tracks:
        tracks = True  # default

    if normalize and not tracks:
        print("Note: --normalize has no effect without --tracks.")
        normalize = False

    header = read_header(rbi_file)
    with open(rbi_file, "rb") as f:
        f.seek(header.toc_start)
        toc_data = f.read(header.toc_length)
    disc = parse_toc(toc_data)

    stem = rbi_file.stem
    base_dir = Path.cwd()
    raw_dir = Path(f"{stem}.extracted") if raw else None

    # Collect all paths that will be written and check for overwrites
    output_paths: list[Path] = []
    if raw_dir is not None:
        output_paths += [raw_dir / f"{stem}.toc", raw_dir / f"{stem}.bin"]
    if tracks:
        output_paths += collect_tracks_output_paths(
            disc, header.disc_number, header.disc_total, base_dir
        )

    if not _confirm_overwrite(output_paths):
        print("Aborted.")
        return

    extract_data(rbi_file, raw_dir, tracks, base_dir, embed_rg=not normalize)

    if normalize and tracks:
        flac_paths = [p for p in output_paths if p.suffix == ".flac"]
        print(f"\nNormalising {len(flac_paths)} tracks to -18 LUFS...")
        for p in flac_paths:
            print(f"  {p.name}", end="", flush=True)
            _normalize_flac(p)
            print(" done")


def _dispatch(args: argparse.Namespace) -> None:
    if args.cmd == "c":
        create_image(
            args.input_dir,
            mode=args.mode,
            loudness=args.loudness,
            strategy=args.strategy,
            trim_silence=args.trim_silence,
        )
    elif args.cmd == "r":
        rip_image(
            args.device,
            loudness=args.loudness,
            output=args.output,
        )
    elif args.cmd == "i":
        import_image(args.source, loudness=args.loudness, output=args.output)
    elif args.cmd == "x":
        extract_image(
            args.rbi_file, raw=args.raw, tracks=args.tracks, normalize=args.normalize
        )
    elif args.cmd == "l":
        from cdda2img.container import list_container

        list_container(args.rbi_file)
    elif args.cmd == "t":
        from cdda2img.container import verify_container

        if not verify_container(args.rbi_file):
            raise SystemExit(1)


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
