import argparse
import re
import textwrap
from pathlib import Path

from cdda2img.concat import concat_wav
from cdda2img.container import TempFiles, build_container, extract_data, resolve_temp_dir, wav_to_raw_pcm
from cdda2img.input_selector import select_batches
from cdda2img.metadata import derive_album_info, read_source_rg_tags
from cdda2img.rbi_format import FLAG_MASTER_MODE, RBIDisc
from cdda2img.silence import trim_silence_cd_da
from cdda2img.toc import build_toc_entries, generate_toc, get_track_durations
from cdda2img.transcode import transcode_audio

DEFAULT_STRATEGY = "aatc"
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

            extract options:
              --tracks              Write per-track FLAC files and CUE sheet (default)
              --raw                 Write raw PCM (.s16le) and TOC; .rg.json sidecar if RG block present
              --normalize           Normalise extracted FLACs to -18 LUFS (EBU R128);
                                    mutually exclusive with RG tag embedding
              (--tracks and --raw may be combined; --normalize requires --tracks)

            examples:
              cdda2img c /music/album
              cdda2img c /music/album --mode master --loudness none
              cdda2img c /music/album --strategy ball
              cdda2img x album.rbi
              cdda2img x album.rbi --raw
              cdda2img x album.rbi --tracks --raw
              cdda2img x album.rbi --normalize
              cdda2img l album.rbi
              cdda2img t album.rbi
        """),
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

    x = sub.add_parser("x", help="Extract TOC and audio from an RBI image")
    x.add_argument("rbi_file", type=Path, help="RBI file to extract")
    x.add_argument("--raw", action="store_true", help="Write raw PCM (.s16le) and TOC")
    x.add_argument("--tracks", action="store_true", help="Write per-track FLAC files and CUE")
    x.add_argument(
        "--normalize",
        action="store_true",
        help="Apply EBU R128 normalisation to extracted FLACs (mutually exclusive with RG tag embedding)",
    )

    l_cmd = sub.add_parser("l", help="List the contents of an RBI image")
    l_cmd.add_argument("rbi_file", type=Path, help="RBI file to list")

    t_cmd = sub.add_parser("t", help="Test/validate an RBI image against the spec")
    t_cmd.add_argument("rbi_file", type=Path, help="RBI file to validate")

    return parser.parse_args()


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
    input_dir: Path, mode: str = "remaster", loudness: str = "rg", strategy: str = DEFAULT_STRATEGY
) -> None:
    files = sorted(p for p in input_dir.iterdir() if p.is_file() and not p.name.startswith("."))
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

            if mode == "remaster":
                print(f"  Trimming      {i:2}: {track.stem}")
                trim = temp.temp_track(i, "_trim.wav")
                trim_silence_cd_da(str(trans), str(trim), SILENCE_PAD_DUR)
                source_wavs.append(trim)
            else:
                print(f"  Skipping silence trim (master mode): {track.stem}")
                source_wavs.append(trans)

        print("  Concatenating tracks")
        concat_wav(source_wavs, temp.pcm_pre)
        wav_to_raw_pcm(temp.pcm_pre, temp.pcm_file)

        durations = get_track_durations(source_wavs)
        disc = RBIDisc(album=album, artist=artist, disc_number=disc_num, disc_total=disc_total)
        disc.tracks = build_toc_entries(batch, durations, disc)
        source_rg = [read_source_rg_tags(p) for p in batch]
        raw_titles = [re.sub(r"^\d{2} ", "", p.stem) for p in batch]
        toc_data = generate_toc(disc, source_rg=source_rg, raw_titles=raw_titles)

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
        build_container(temp.pcm_file, toc_data, disc, output_file, rg_block=rg_block, extra_flags=container_flags)
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


def extract_image(rbi_file: Path, raw: bool, tracks: bool, normalize: bool = False) -> None:
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
        output_paths += [raw_dir / f"{stem}.toc", raw_dir / f"{stem}.s16le"]
    if tracks:
        output_paths += collect_tracks_output_paths(disc, header.disc_number, header.disc_total, base_dir)

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
        create_image(args.input_dir, mode=args.mode, loudness=args.loudness, strategy=args.strategy)
    elif args.cmd == "x":
        extract_image(args.rbi_file, raw=args.raw, tracks=args.tracks, normalize=args.normalize)
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
