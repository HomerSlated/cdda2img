from pathlib import Path

import av
from av.audio.frame import AudioFrame


def transcode_audio(input_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)

    output_path = output_dir / input_path.with_suffix(".wav").name

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
                    if not isinstance(frame, AudioFrame):
                        continue
                    frame.pts = 0
                    for resampled in resampler.resample(frame):
                        output.mux(output_stream.encode(resampled))

            output.mux(output_stream.encode(None))
