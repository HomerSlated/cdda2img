import argparse
import textwrap
from pathlib import Path

from ffmpeg_normalize import FFmpegNormalize

from cdda2img.concat import concat_wav
from cdda2img.container import TempFiles, build_container, extract_data, resolve_temp_dir, wav_to_raw_pcm
from cdda2img.input_selector import select_batches
from cdda2img.metadata import derive_album_info
from cdda2img.rbi_format import PCM_CHANNELS, PCM_SAMPLE_RATE, RBIDisc
from cdda2img.silence import trim_silence_cd_da
from cdda2img.toc import build_toc_entries, generate_toc, get_track_durations
from cdda2img.transcode import transcode_audio

DEFAULT_STRATEGY = "aatc"
SILENCE_PAD_DUR = "1"  # seconds of post-track silence (Red Book inter-track gap)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="cdda2img",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            create options:
              --normalize           Apply EBU R128 loudness normalisation
              --strategy {fcfs,aatc,bech,ball}
                                    Disc batching strategy (default: aatc)
                fcfs  first-come-first-served: fill one disc in input order, stop
                aatc  all-as-they-come: fill discs in input order, as many as needed
                bech  best-each: pack each disc as full as possible in turn (order not preserved)
                ball  best-all: global bin-packing to minimise total disc count (order not preserved)

            extract options:
              --tracks              Write per-track FLAC files and CUE sheet (default)
              --raw                 Write raw PCM (.s16le) and TOC
              (both flags may be combined)

            examples:
              cdda2img c /music/album
              cdda2img c /music/album --normalize --strategy ball
              cdda2img x album.rbi
              cdda2img x album.rbi --raw
              cdda2img x album.rbi --tracks --raw
        """),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("c", help="Create an RBI image from a directory of audio files")
    c.add_argument("input_dir", type=Path, help="Directory containing audio files")
    c.add_argument("--normalize", action="store_true", help="Apply EBU R128 loudness normalisation")
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


def _normalize(pre_pcm: Path, norm_wav: Path) -> None:
    norm = FFmpegNormalize(
        normalization_type="ebu",
        target_level=-5.0,
        auto_lower_loudness_target=True,
        keep_loudness_range_target=True,
        audio_codec="pcm_s16le",
        sample_rate=PCM_SAMPLE_RATE,
        audio_channels=PCM_CHANNELS,
        progress=True,
    )
    norm.add_media_file(str(pre_pcm), str(norm_wav))
    norm.run_normalization()


def create_image(input_dir: Path, normalize: bool = False, strategy: str = DEFAULT_STRATEGY) -> None:
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
        trimmed: list[Path] = []

        for i, track in enumerate(batch, start=1):
            print(f"  Transcoding   {i:2}: {track.stem}")
            trans = temp.temp_track(i, "_trans.wav")
            transcode_audio(track, trans)

            print(f"  Trimming      {i:2}: {track.stem}")
            trim = temp.temp_track(i, "_trim.wav")
            trim_silence_cd_da(str(trans), str(trim), SILENCE_PAD_DUR)
            trimmed.append(trim)

        print("  Concatenating tracks")
        concat_wav(trimmed, temp.pcm_pre)

        if normalize:
            print("  Normalising")
            _normalize(temp.pcm_pre, temp.pcm_norm)
            wav_to_raw_pcm(temp.pcm_norm, temp.pcm_file)
        else:
            wav_to_raw_pcm(temp.pcm_pre, temp.pcm_file)

        durations = get_track_durations(trimmed)
        disc = RBIDisc(album=album, artist=artist, disc_number=disc_num, disc_total=disc_total)
        disc.tracks = build_toc_entries(batch, durations, disc)
        toc_data = generate_toc(disc)

        stem = album if disc_total == 1 else f"{album}_disc{disc_num}"
        output_file = _unique_path(stem, "rbi")
        build_container(temp.pcm_file, toc_data, disc, output_file)
        temp.cleanup()


def extract_image(rbi_file: Path, raw: bool, tracks: bool) -> None:
    from cdda2img.container import read_header
    from cdda2img.toc_parser import parse_toc
    from cdda2img.track_extract import collect_tracks_output_paths

    if not raw and not tracks:
        tracks = True  # default

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

    existing = [p for p in output_paths if p.exists()]
    if existing:
        print(f"\n{len(existing)} output file(s) already exist and would be overwritten:")
        for p in existing[:5]:
            print(f"  {p}")
        if len(existing) > 5:
            print(f"  ... and {len(existing) - 5} more")
        answer = input("Overwrite? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    extract_data(rbi_file, raw_dir, tracks, base_dir)


def main() -> None:
    args = parse_args()
    try:
        if args.cmd == "c":
            create_image(args.input_dir, normalize=args.normalize, strategy=args.strategy)
        else:
            extract_image(args.rbi_file, raw=args.raw, tracks=args.tracks)
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
