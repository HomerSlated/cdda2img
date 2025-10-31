from pathlib import Path

import av

MAX_RUNTIME_MINUTES = 80
MAX_FILE_COUNT = 99


def get_audio_duration_minutes(path: Path) -> float:
    try:
        with av.open(str(path)) as container:
            stream = next(s for s in container.streams if s.type == "audio")
            duration = float(stream.duration * stream.time_base)
            return duration / 60.0
    except Exception:
        return 0.0  # Invalid or unsupported file


def select_audio_files(directory: Path) -> list[Path]:
    selected = []
    total_runtime = 0.0

    for item in sorted(directory.iterdir()):
        if not item.is_file():
            continue

        duration = get_audio_duration_minutes(item)
        if duration <= 0.0:
            continue

        if total_runtime + duration > MAX_RUNTIME_MINUTES:
            break

        selected.append(item)
        total_runtime += duration

        if len(selected) >= MAX_FILE_COUNT:
            break

    return selected


if __name__ == "__main__":
    from sys import argv

    directory = Path(argv[1])
    files = select_audio_files(directory)
    total_runtime = sum(get_audio_duration_minutes(f) for f in files)
    print(f"🎧 Selected {len(files)} audio files ({total_runtime:.1f} min total):")
    for f in files:
        print(f" - {f.name}")
