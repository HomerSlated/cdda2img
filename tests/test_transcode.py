import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cdda2img.transcode import transcode_audio


@pytest.mark.skipif(not Path("example/Koiduuni.mp3").exists(), reason="example audio file not present")
def test_transcode_roundtrip(tmp_path):
    input_mp3 = Path("example/Koiduuni.mp3")
    output_wav = tmp_path / "Koiduuni.wav"

    transcode_audio(input_mp3, output_wav)

    assert output_wav.exists()
    assert output_wav.stat().st_size > 0
