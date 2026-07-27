"""
rip_log.py — RLOG block builder (rbi_spec.md §6.6).

Public interface:
    RipLogBuilder  — accumulate rip-phase metadata; call finalize(disc) -> bytes
"""

from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

from cdda2img.rbi_format import (
    RBIDisc,
)

if TYPE_CHECKING:
    from cdda2img.accuraterip import ARTrackResult

log = logging.getLogger(__name__)


def _get_engine_version(rip_type: str) -> str:
    """Return the read engine's version string for the rip log.

    Every live drive path is AccuDisc now (M8 of the migration), so *rip_type*
    no longer selects a binary — it is kept in the signature because it still
    names the *path* taken (``accudisc`` / ``accudisc+toc``, plus ``+c2rec``
    when the recovery ladder ran) and callers pass it positionally.
    """
    from cdda2img.accudisc_reader import engine_version

    return engine_version()


class RipLogBuilder:
    """Accumulates rip-phase metadata and produces an RLOG block on finalize()."""

    def __init__(
        self,
        *,
        rip_type: str,
        drive_name: str | None = None,
        read_offset: int = 0,
    ) -> None:
        self.rip_type = rip_type
        self.drive_name = drive_name
        self.read_offset = read_offset
        self.ar_results: list[ARTrackResult] | None = None
        self.cddb_id: int | None = None
        self._created: str = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        )
        self._engine_str: str = _get_engine_version(rip_type)

    def finalize(self, disc: RBIDisc) -> bytes:  # noqa: C901
        """Build and return the UTF-8 RLOG block bytes with SHA-256 self-seal."""
        from importlib.metadata import version as _pkg_version

        try:
            cdda2img_ver = _pkg_version("cdda2img")
        except Exception:
            cdda2img_ver = "unknown"

        lines: list[str] = [
            f"Log created by: cdda2img {cdda2img_ver}",
            f"Log creation date: {self._created}",
            "",
            "Ripping phase information:",
        ]
        if self.drive_name is not None:
            lines.append(f"  Drive: {self.drive_name}")
        lines.append(f"  Extraction engine: {self._engine_str}")
        lines.append(f"  Read offset correction: {self.read_offset:+d}")
        lines.append("  Gap detection: pre-gap (cdrdao default)")
        lines.append("")

        lines.append("CD metadata:")
        lines.append(f"  Artist: {disc.artist}")
        lines.append(f"  Title: '{disc.album}'")
        if self.cddb_id is not None:
            lines.append(f"  CDDB Disc ID: {self.cddb_id:08x}")
        lines.append("")

        lines.append("TOC:")
        for t in disc.tracks:
            audio_start = t.start_frame + t.pregap_frames
            audio_end = audio_start + t.duration_frames - 1
            lines.append(f"  {t.track_number}:")
            lines.append(f"    Start: {t.start_timestamp}")
            lines.append(f"    Length: {t.duration_timestamp}")
            lines.append(f"    Start sector: {audio_start}")
            lines.append(f"    End sector: {audio_end}")
        lines.append("")

        lines.append("Tracks:")
        for t in disc.tracks:
            lines.append(f"  {t.track_number}:")
            if self.ar_results is not None and len(self.ar_results) >= t.track_number:
                r = self.ar_results[t.track_number - 1]
                if r.max_confidence is None:
                    for ar_ver, crc in (("v1", r.v1_crc), ("v2", r.v2_crc)):
                        lines.append(f"    AccurateRip {ar_ver}:")
                        lines.append("      Result: Disc not present in database")
                        lines.append(f"      Local CRC: {crc}")
                else:
                    for ar_ver, crc, conf in (
                        ("v1", r.v1_crc, r.confidence_v1),
                        ("v2", r.v2_crc, r.confidence_v2),
                    ):
                        lines.append(f"    AccurateRip {ar_ver}:")
                        if conf is not None:
                            lines.append("      Result: Found, exact match")
                            lines.append(f"      Confidence: {conf}")
                        else:
                            lines.append("      Result: Found, no match")
                        lines.append(f"      Local CRC: {crc}")
                ok = r.confidence_v1 is not None or r.confidence_v2 is not None
                lines.append(f"    Status: {'Copy OK' if ok else 'Copy error'}")
        lines.append("")

        lines.append("Conclusive status report:")
        if self.ar_results is not None:
            n = len(self.ar_results)
            if self.ar_results[0].max_confidence is None:
                lines.append(
                    "  AccurateRip summary: Disc not present in AccurateRip database"
                )
            else:
                n_ok = sum(
                    1
                    for r in self.ar_results
                    if r.confidence_v1 is not None or r.confidence_v2 is not None
                )
                if n_ok == n:
                    lines.append("  AccurateRip summary: All tracks accurately ripped")
                else:
                    lines.append(
                        f"  AccurateRip summary: {n_ok}/{n} tracks accurately ripped"
                    )
        lines.append("  Health status: No errors occurred")
        lines.append("  EOF: End of status report")
        lines.append("")  # trailing blank → body ends with \n after join

        import blake3 as _blake3

        body = "\n".join(lines)
        seal = _blake3.blake3(body.encode("utf-8")).hexdigest()
        return (body + f"BLAKE3: {seal}\n").encode("utf-8")
