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


def _knapsack_single_disc(values: list[float], capacity: float) -> list[int]:
    """Return indices of items that maximise total value within capacity (single-disc knapsack)."""
    if not values:
        return []

    int_values = [math.ceil(v * SCALE) for v in values]
    int_capacity = int(capacity * SCALE)
    n = len(int_values)

    model = cp_model.CpModel()
    x = [model.NewBoolVar(f"x{i}") for i in range(n)]  # type: ignore[attr-defined]  # LINT-001
    model.Add(sum(x[i] * int_values[i] for i in range(n)) <= int_capacity)  # type: ignore[attr-defined]  # LINT-001
    model.Add(sum(x) <= MAX_TRACKS)  # type: ignore[attr-defined]  # LINT-001
    model.Maximize(sum(x[i] * int_values[i] for i in range(n)))  # type: ignore[attr-defined]  # LINT-001

    solver = cp_model.CpSolver()
    status = solver.Solve(model)

    return (
        [i for i in range(n) if solver.Value(x[i])]
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        else []
    )


def batch_bech(files: list[Path], durations: list[float]) -> list[list[Path]]:
    """Best-each: greedily pack each disc as full as possible in turn (track order not preserved)."""
    remaining = list(enumerate(zip(files, durations)))
    batches = []

    while remaining:
        idx_map, file_and_durations = zip(*remaining)
        _, batch_durations = zip(*file_and_durations)

        selected = _knapsack_single_disc(list(batch_durations), MAX_RUNTIME_MINUTES)

        int_durations = [math.ceil(d * SCALE) for d in batch_durations]
        int_limit = int(MAX_RUNTIME_MINUTES * SCALE)

        selected_local = []
        selected_global = []
        total_runtime = 0
        for i in selected:
            if (
                len(selected_local) < MAX_TRACKS
                and total_runtime + int_durations[i] <= int_limit
            ):
                selected_local.append(i)
                selected_global.append(idx_map[i])
                total_runtime += int_durations[i]

        if not selected_local:
            break

        batches.append([files[i] for i in selected_global])
        remaining = [
            item for j, item in enumerate(remaining) if j not in selected_local
        ]

    return batches


def batch_ball(files: list[Path], durations: list[float]) -> list[list[Path]]:
    """Best-all: global bin-packing to minimise total number of discs (track order not preserved)."""
    # Use aatc as the upper bound — ball can only match or beat it
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
    elif strategy == "bech":
        return batch_bech(list(files), list(durations))
    elif strategy == "ball":
        return batch_ball(list(files), list(durations))
    else:
        msg = f"Unknown strategy: {strategy!r}"
        raise ValueError(msg)
