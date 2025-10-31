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

        resampler = av.AudioResampler(format="s16", layout="stereo", rate=44100)

        with av.open(str(output_path), mode="w", format="wav") as output:
            output_stream = output.add_stream("pcm_s16le", rate=44100)
            output_stream.options = {
                "channels": "2",
                "channel_layout": "stereo",
            }

            for packet in container.demux(input_stream):
                for frame in packet.decode():
                    frame.pts = None
                    for resampled in resampler.resample(frame):
                        output.mux(output_stream.encode(resampled))

            output.mux(output_stream.encode(None))  # flush
