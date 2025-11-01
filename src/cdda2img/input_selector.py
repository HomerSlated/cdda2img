import math
from pathlib import Path

import av
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


def best_fit_knapsack(values: list[float], capacity: float) -> list[int]:
    if not values:
        return []

    int_values = [math.ceil(v * SCALE) for v in values]
    int_capacity = int(capacity * SCALE)

    model = cp_model.CpModel()
    n = len(int_values)
    x = [model.NewBoolVar(f"x{i}") for i in range(n)]

    model.Add(sum(x[i] * int_values[i] for i in range(n)) <= int_capacity)
    model.Add(sum(x) <= MAX_TRACKS)
    model.Maximize(sum(x[i] * int_values[i] for i in range(n)))

    solver = cp_model.CpSolver()
    status = solver.Solve(model)

    return [i for i in range(n) if solver.Value(x[i])] if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else []


def batch_best(files: list[Path], durations: list[float]) -> list[list[Path]]:
    remaining = list(enumerate(zip(files, durations)))
    batches = []

    while remaining:
        idx_map, file_and_durations = zip(*remaining)
        _, batch_durations = zip(*file_and_durations)

        selected = best_fit_knapsack(list(batch_durations), MAX_RUNTIME_MINUTES)

        int_durations = [math.ceil(d * SCALE) for d in batch_durations]
        int_limit = int(MAX_RUNTIME_MINUTES * SCALE)

        selected_batch_local = []
        selected_batch_global = []
        total_runtime = 0
        for i in selected:
            if len(selected_batch_local) < MAX_TRACKS and total_runtime + int_durations[i] <= int_limit:
                selected_batch_local.append(i)
                selected_batch_global.append(idx_map[i])
                total_runtime += int_durations[i]

        if not selected_batch_local:
            break

        batches.append([files[i] for i in selected_batch_global])
        remaining = [item for j, item in enumerate(remaining) if j not in selected_batch_local]

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
    else:
        raise ValueError
