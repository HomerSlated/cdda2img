"""match_distance — confidence score and recommendation for the current MB match.

``build_match_distance(disc, prov)`` inspects the post-lookup disc and
provenance dict and returns a ``MatchDistance`` with a float score in [0, 1]
and a ``MatchRecommendation`` (STRONG / MEDIUM / LOW / NONE).

Adapted from beets' ``Distance`` accumulator and ``Recommendation`` enum
(``beets/autotag/match.py``, MIT licence), but inverted: our score is
higher-is-better (confidence in the committed MB match) rather than
lower-is-better (ranking candidates against each other).

STRONG matches auto-apply metadata without prompting the user.
MEDIUM / LOW / NONE proceed to the interactive metadata menu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cdda2img.rbi_format import RBIDisc

# Score thresholds.
_STRONG_THRESH = 0.70
_MEDIUM_THRESH = 0.40
_LOW_THRESH = 0.10


class MatchRecommendation(Enum):
    """Strength of the match — drives auto-apply vs. interactive prompt."""

    STRONG = "strong"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


@dataclass
class MatchDistance:
    """Confidence score for the current post-lookup MB match.

    ``score``        — float in [0, 1]; higher = more confident
    ``contributors`` — named addends/penalties that composed the score
    ``recommendation`` — STRONG / MEDIUM / LOW / NONE
    """

    score: float
    contributors: dict[str, float] = field(default_factory=dict)
    recommendation: MatchRecommendation = MatchRecommendation.NONE

    def summary(self) -> str:
        """One-line summary suitable for the auto-confirm message."""
        parts = [k for k, v in self.contributors.items() if v > 0]
        sources = " + ".join(parts) if parts else "no positive signals"
        return f"{self.recommendation.value} match, confidence {self.score:.2f} ({sources})"


def build_match_distance(disc: RBIDisc, prov: dict[str, str]) -> MatchDistance:
    """Compute a confidence score from the post-lookup disc and provenance.

    Contributors (additive, then clamped to [0, 1]):

    +0.50  mb_disc_id          — MB release_id from disc-ID fingerprint (strong signal)
    +0.20  mb_duration_match   — MB release_id from text+duration fuzzy (weaker signal)
    +0.25  acoustid            — AcoustID corroborates the MB release (R6)
    +0.15  isrc_disambiguated  — ISRCs resolved a multi-match disc-ID (R1)
    -0.10  cddb_mb_disagreement — CDDB and MB disagree on album / artist (R9)
    """
    contributors: dict[str, float] = {}

    if disc.mb_release_id:
        if "duration_match_release" in prov:
            # Came from text+duration fuzzy — lowest-confidence MB path
            contributors["mb_duration_match"] = 0.20
        else:
            # Came from disc-ID fingerprint — deterministic, strong signal
            contributors["mb_disc_id"] = 0.50

    if prov.get("acoustid_corroborates") == "YES":
        contributors["acoustid"] = 0.25

    if prov.get("multi_match_isrc_disambiguated") == "YES":
        contributors["isrc_disambiguated"] = 0.15

    if "disagreement_cddb_mb" in prov:
        contributors["cddb_mb_disagreement"] = -0.10

    raw = sum(contributors.values())
    score = max(0.0, min(1.0, raw))

    if score >= _STRONG_THRESH:
        rec = MatchRecommendation.STRONG
    elif score >= _MEDIUM_THRESH:
        rec = MatchRecommendation.MEDIUM
    elif score >= _LOW_THRESH:
        rec = MatchRecommendation.LOW
    else:
        rec = MatchRecommendation.NONE

    return MatchDistance(score=score, contributors=contributors, recommendation=rec)
