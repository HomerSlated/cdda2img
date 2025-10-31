import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img.transcode import transcode_audio


def test_transcode_roundtrip(tmp_path):
    input_mp3 = Path("example/Koiduuni.mp3")
    output_dir = tmp_path / "outdir"

    transcode_audio(input_mp3, output_dir)

    output_wav = output_dir / "Koiduuni.wav"
    assert output_wav.exists()
    assert output_wav.stat().st_size > 0
