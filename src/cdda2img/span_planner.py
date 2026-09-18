"""Turn a per-sector damage map into the spans a recovery rung should re-read.

The locate-and-cluster half of ``docs/reference/span-recovery-plan.md`` §4.1-4.2,
kept out of ``cdda2img.py`` because it is the part with no device, no I/O and no
engine in it: everything here is a pure function of a damage map and two integers,
which is what lets the clustering be tuned offline against saved maps rather than
against a drive.

**This is a cost optimiser, not a correctness mechanism** (AccuDisc 2026-09-18g,
and they spec to the same rule). Correctness comes from the witness applied to
each re-read copy — ``verify_passes >= 2``, the map state, the sector's own C2 and
the Q position lane — not from choosing the right sectors to re-read. The planner
only decides *where to spend a witness*. Two consequences, both easy to get
backwards:

* **Over-triggering costs time; under-triggering costs correctness.** A sector
  that is never re-read is never witnessed, so it keeps whatever the capture pass
  delivered. Err wide.
* **Never carve apparently-good sectors out of a span.** The damage measured at
  raw 113068 contains a four-sector island that is byte-correct in the middle of
  a wrong run, with nothing marking either edge, and 31 of the 44 wrong sectors
  were reported ``OK`` by the map. So the map locates the *neighbourhood* of a
  fault, not its extent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = ["Span", "cluster_spans", "locate_targets"]


class Span(NamedTuple):
    """A half-open run of sectors to re-read, ``[start, start + count)``."""

    start: int
    count: int

    @property
    def stop(self) -> int:
        return self.start + self.count


def locate_targets(
    damage: Sequence[int] | bytes,
    start: int,
    count: int,
    *,
    exclude: Iterable[int] = (),
) -> tuple[int, ...]:
    """Sectors in ``[start, start + count)`` the capture pass flagged as damaged.

    *damage* is the whole-disc lane: one byte per sector, non-zero meaning "not
    intact here" (``accudisc_reader.ReadLanes.damage``, projected from the
    engine's status map). It is indexed by absolute sector, so this slices it
    rather than expecting a pre-cut window — a caller that sliced first would have
    to remember to add *start* back to every result, and forgetting is silent.

    *exclude* drops known artefacts. The disc's final sector belongs there: it is
    flagged on every rip of the reference disc and is a lead-out boundary effect,
    not damage, so re-reading it spends a witness on a sector that will never
    verify. It is a parameter rather than a hard-coded ``disc_last_lsn`` because
    "which sectors are artefacts" is a property of the disc and the drive, and a
    rule baked in here could not be turned off for a bench run that wanted to
    measure the artefact itself.

    A map shorter than the requested window is not padded: the sectors it does not
    cover are simply not targets. Treating absent map bytes as damage would turn a
    truncated map into a whole-track re-read, and treating them as clean is the
    honest reading — we have no evidence about them either way, and the AR gate
    still judges the track.
    """
    skip = set(exclude)
    hi = min(start + count, len(damage))
    return tuple(
        lba for lba in range(max(start, 0), hi) if damage[lba] and lba not in skip
    )


def cluster_spans(
    targets: Sequence[int],
    *,
    gap: int,
    pad: int,
    lo: int,
    hi: int,
) -> tuple[Span, ...]:
    """Group *targets* into padded, clamped spans.

    Two targets join the same span when at most *gap* clean sectors separate them,
    so ``gap=0`` merges only adjacent sectors and ``gap=3`` tolerates a three-sector
    hole. Each span is then grown by *pad* sectors at both ends and clamped to
    ``[lo, hi)`` — the track's window plus whatever margin the caller allows for
    the read offset.

    **Padding happens before the second merge, and that order is the whole
    subtlety.** Two spans that do not overlap can overlap *after* padding, and
    emitting both would re-read the shared sectors twice in one pass — which costs
    time and, worse, makes an accepted-copy count meaningless because one sector
    contributed two observations. So the merge runs again on the padded spans.

    *pad* is not decoration. The engine settles a positional disagreement by
    anchoring against neighbours, so a span with no clean margin gives it nothing
    to anchor to; the padding is what supplies the alignment signal. It is also
    why a target set clustered with ``pad=0`` is a legitimate control rather than
    a broken configuration — the bench needs to be able to ask what the padding
    bought.

    Returns spans in ascending order, non-overlapping, none empty. An empty
    *targets* yields no spans, which the caller must treat as "nothing to do"
    rather than "nothing is wrong": the capture pass cannot see a displacement, so
    a track can fail AccurateRip with an empty target set, and that is the
    falsifier in §4.5 rather than a success.
    """
    if not targets or hi <= lo:
        return ()
    if gap < 0 or pad < 0:
        msg = f"cluster_spans: gap and pad must not be negative (gap={gap}, pad={pad})"
        raise ValueError(msg)

    ordered = sorted(set(targets))
    runs: list[list[int]] = [[ordered[0], ordered[0]]]
    for lba in ordered[1:]:
        # `lba - previous_end - 1` is the number of clean sectors between them.
        if lba - runs[-1][1] - 1 <= gap:
            runs[-1][1] = lba
        else:
            runs.append([lba, lba])

    padded = [(max(lo, first - pad), min(hi, last + pad + 1)) for first, last in runs]

    merged: list[list[int]] = []
    for first, stop in padded:
        if stop <= first:
            continue
        if merged and first <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([first, stop])
    return tuple(Span(first, stop - first) for first, stop in merged)
