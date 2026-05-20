import math
from pathlib import Path

import av
import mutagen
from ortools.sat.python import cp_model

MAX_RUNTIME_MINUTES = 80
MAX_TRACKS = 99
SCALE = 100  # preserves two decimal places


def get_audio_duration_minutes(path: Path) -> float:
    try:
        with av.open(str(path)) as container:
            stream = next(s for s in container.streams if s.type == "audio")
            duration = float(stream.duration * stream.time_base)
            return duration / 60.0
    except Exception:
        return 0.0


def batch_fcfs(files: list[Path], durations: list[float]) -> list[list[Path]]:
    batch = []
    total_runtime = 0.0

    for f, d in zip(files, durations):
        if len(batch) >= MAX_TRACKS or total_runtime + d > MAX_RUNTIME_MINUTES:
            break
        batch.append(f)
        total_runtime += d

    return [batch]


def batch_aatc(files: list[Path], durations: list[float]) -> list[list[Path]]:
    batches = []
    batch = []
    total_runtime = 0.0

    for f, d in zip(files, durations):
        if len(batch) >= MAX_TRACKS or total_runtime + d > MAX_RUNTIME_MINUTES:
            batches.append(batch)
            batch = []
            total_runtime = 0.0

        batch.append(f)
        total_runtime += d

    if batch:
        batches.append(batch)

    return batches


def batch_best(files: list[Path], durations: list[float]) -> list[list[Path]]:
    """Best: global bin-packing to minimise total number of discs (track order not preserved)."""
    # Use aatc as the upper bound — best can only match or beat it
    upper = batch_aatc(files, durations)
    if len(upper) <= 1:
        return upper

    n = len(files)
    max_discs = len(upper)
    int_durations = [math.ceil(d * SCALE) for d in durations]
    int_capacity = int(MAX_RUNTIME_MINUTES * SCALE)

    model = cp_model.CpModel()
    y = [model.NewBoolVar(f"y{j}") for j in range(max_discs)]  # type: ignore[attr-defined]  # LINT-001
    x = [[model.NewBoolVar(f"x{i}_{j}") for j in range(max_discs)] for i in range(n)]  # type: ignore[attr-defined]  # LINT-001

    for i in range(n):
        model.Add(sum(x[i][j] for j in range(max_discs)) == 1)  # type: ignore[attr-defined]  # LINT-001

    for j in range(max_discs):
        model.Add(sum(x[i][j] * int_durations[i] for i in range(n)) <= int_capacity)  # type: ignore[attr-defined]  # LINT-001
        model.Add(sum(x[i][j] for i in range(n)) <= MAX_TRACKS)  # type: ignore[attr-defined]  # LINT-001
        for i in range(n):
            model.Add(x[i][j] <= y[j])  # type: ignore[attr-defined]  # LINT-001

    # Symmetry breaking: used discs come first
    for j in range(max_discs - 1):
        model.Add(y[j] >= y[j + 1])  # type: ignore[attr-defined]  # LINT-001

    model.Minimize(sum(y))  # type: ignore[attr-defined]  # LINT-001

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 30.0
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return upper

    batches = []
    for j in range(max_discs):
        if solver.Value(y[j]):
            batch = [files[i] for i in range(n) if solver.Value(x[i][j])]
            if batch:
                batches.append(batch)

    return batches


def _read_disc_number(path: Path) -> int | None:
    """Return the disc number from embedded audio metadata, or None if absent/unreadable.

    Uses mutagen for reliable cross-format support: ID3v2 TPOS (MP3), Vorbis
    DISCNUMBER (FLAC/OGG), and MP4 disk (M4A/AAC).  Values like "1/2" are split
    on '/' and the first token is used.
    """
    try:
        tags = mutagen.File(path)
        if not tags:
            return None
        for key in ("TPOS", "discnumber", "DISCNUMBER", "disk", "disc"):
            val = tags.get(key)
            if val is None:
                continue
            if hasattr(val, "text"):  # ID3 TextFrame (TPOS)
                raw = val.text[0] if val.text else ""
            elif isinstance(val, list) and val:
                item = val[0]
                raw = str(item[0]) if isinstance(item, tuple) else str(item)
            else:
                raw = str(val)
            part = raw.split("/")[0].strip()
            if part.isdigit() and int(part) > 0:
                return int(part)
    except Exception:
        return None
    return None


def batch_meta(files: list[Path]) -> list[list[Path]]:
    """Meta: group tracks by their embedded disc-number tag; untagged tracks form a final group."""
    groups: dict[int, list[Path]] = {}
    untagged: list[Path] = []

    for f in files:
        disc_num = _read_disc_number(f)
        if disc_num is not None:
            groups.setdefault(disc_num, []).append(f)
        else:
            untagged.append(f)

    batches = [groups[k] for k in sorted(groups)]
    if untagged:
        batches.append(untagged)
    return batches


def select_batches(files: list[Path], strategy: str) -> list[list[Path]]:
    durations = [get_audio_duration_minutes(f) for f in files]
    files_and_durations = [(f, d) for f, d in zip(files, durations) if d > 0.0]
    if not files_and_durations:
        return []

    files, durations = zip(*files_and_durations)

    if strategy == "fcfs":
        return batch_fcfs(list(files), list(durations))
    elif strategy == "aatc":
        return batch_aatc(list(files), list(durations))
    elif strategy == "best":
        return batch_best(list(files), list(durations))
    elif strategy == "meta":
        return batch_meta(list(files))
    else:
        msg = f"Unknown strategy: {strategy!r}"
        raise ValueError(msg)
