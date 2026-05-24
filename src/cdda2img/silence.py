import sys

import av
from av import error
from av.audio import AudioFrame
from av.filter import Graph


def open_containers(input_path, output_path):
    in_container = av.open(input_path)
    out_container = av.open(output_path, mode="w")
    return in_container, out_container


def setup_streams(in_container, out_container):
    in_stream = in_container.streams.audio[0]
    out_stream = out_container.add_stream("pcm_s16le", rate=44100)
    out_stream.layout = "stereo"
    return in_stream, out_stream


def build_filter_graph(in_stream, pad_dur, threshold_db=55):
    graph = Graph()
    abuffer = graph.add_abuffer(
        template=in_stream,
        sample_rate=in_stream.codec_context.sample_rate,
        format=in_stream.codec_context.format.name,
        layout=in_stream.codec_context.layout.name,
        channels=in_stream.codec_context.channels,
        time_base=in_stream.time_base,
    )
    spec = (
        f"start_periods=1:start_duration=0:"
        f"start_threshold=-{threshold_db}dB:detection=peak"
    )
    silenceremove1 = graph.add("silenceremove", spec)
    reverse1 = graph.add("areverse")
    silenceremove2 = graph.add("silenceremove", spec)
    reverse2 = graph.add("areverse")
    apad = graph.add("apad", f"pad_dur={pad_dur}")
    sink = graph.add("abuffersink")
    graph.link_nodes(
        abuffer, silenceremove1, reverse1, silenceremove2, reverse2, apad, sink
    )
    graph.configure()
    return graph


def drain_graph(graph, out_stream, out_container):
    while True:
        try:
            frame = graph.pull()
            if isinstance(frame, AudioFrame):
                for packet in out_stream.encode(frame):
                    out_container.mux(packet)
        except error.FFmpegError:
            break


def process_frames(graph, in_container, in_stream, out_stream, out_container):
    for frame in in_container.decode(in_stream):
        graph.push(frame)
        drain_graph(graph, out_stream, out_container)
    graph.push(None)
    drain_graph(graph, out_stream, out_container)
    for packet in out_stream.encode(None):
        out_container.mux(packet)


def trim_silence_cd_da(input_path, output_path, pad_dur, threshold_db=55):
    in_container, out_container = open_containers(input_path, output_path)
    in_stream, out_stream = setup_streams(in_container, out_container)
    graph = build_filter_graph(in_stream, pad_dur, threshold_db=threshold_db)
    process_frames(graph, in_container, in_stream, out_stream, out_container)
    out_container.close()
    in_container.close()


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        print("Usage: python silence.py <infile> <outfile> <pad> [threshold_db]")
        sys.exit(1)
    infile = sys.argv[1]
    outfile = sys.argv[2]
    pad = sys.argv[3]
    threshold = int(sys.argv[4]) if len(sys.argv) == 5 else 55
    trim_silence_cd_da(infile, outfile, pad, threshold_db=threshold)
