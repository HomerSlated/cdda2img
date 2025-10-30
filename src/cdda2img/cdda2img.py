import sys
from pathlib import Path

import av


def transcode_audio(input_path: Path, output_path: Path) -> None:
    if not input_path.is_file():
        raise FileNotFoundError

    if not output_path.parent.exists():
        raise FileNotFoundError

    print(f"Transcoding: {input_path.name} → {output_path.name}")

    with av.open(str(input_path)) as container:
        input_stream = container.streams.audio[0]
        input_codec = input_stream.codec_context

        with av.open(str(output_path), mode="w", format="wav") as output:
            output_stream = output.add_stream(
                "pcm_s16le",
                rate=input_codec.sample_rate,
                options={
                    "channels": str(input_codec.channels),
                    "channel_layout": input_codec.layout.name,
                },
            )

            for packet in container.demux(input_stream):
                for frame in packet.decode():
                    frame.pts = None
                    output.mux(output_stream.encode(frame))

            output.mux(output_stream.encode(None))  # flush


def main():
    if len(sys.argv) != 3:
        print("Usage: python hello.py <input.mp3> <output.wav>")
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
