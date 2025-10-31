import sys
from pathlib import Path

from cdda2img.transcode import transcode_audio


def main():
    if len(sys.argv) != 3:
        print("Usage: cdda2img <input_audio_file> <output.wav>")
        sys.exit(1)

    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])

    try:
        transcode_audio(input_path, output_path)
        print("✅ Transcoding complete.")
    except FileNotFoundError as e:
        print(f"❌ Missing file or directory: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
