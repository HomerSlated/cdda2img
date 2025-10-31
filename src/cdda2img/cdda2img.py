import sys
from pathlib import Path

from cdda2img.transcode import transcode_audio
from cdda2img.unique_name import generate_unique_name


def parse_args() -> str:
    """
    Parse CLI arguments of the form 'c|x <dir>'.
    Returns the input directory name.
    """
    if len(sys.argv) != 3:
        raise RuntimeError
    return sys.argv[2]


def prepare_input_output(input_dir: str, suffix: str) -> tuple[Path, Path]:
    """
    Validate input file and generate unique output directory name.
    Returns (input_file_path, output_dir_path).
    """
    input_path = Path(input_dir) / "Koiduuni.mp3"
    if not input_path.is_file():
        raise RuntimeError

    output_dir = Path(generate_unique_name(input_dir, suffix))
    return input_path, output_dir


def main() -> None:
    input_dir = parse_args()
    input_file, output_dir = prepare_input_output(input_dir, "trans")
    transcode_audio(input_file, output_dir)


if __name__ == "__main__":
    main()
