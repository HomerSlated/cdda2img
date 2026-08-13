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

    +0.50  mb_disc_id          — MB release_id from a unique disc-ID fingerprint (strong)
    +0.30  mb_disc_id_multi    — disc-ID matched several pressings; rung pinned one (§10.3)
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
        elif prov.get("release_selected_via"):
            # §10.3: the disc-ID fingerprint matched MB but several pressings
            # tied; the lexicographic rung picked one by preference/date. The
            # album/master is identified, but the *specific pressing* is a best
            # guess — so this must NOT reach STRONG auto-apply on its own; the
            # user should see the menu to confirm the pressing. Weaker than a
            # unique disc-ID hit (0.50), stronger than a duration fuzzy (0.20).
            contributors["mb_disc_id_multi"] = 0.30
        else:
            # Came from a unique disc-ID fingerprint — deterministic, strong
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


def final_match_distance(disc: RBIDisc, prov: dict[str, str]) -> MatchDistance:
    """The score to STORE, computed after the metadata menu has closed (N6).

    :func:`build_match_distance` scores *the automatic guess*. This scores *what
    the container ended up believing*, and the two are different numbers taken at
    different moments. Before N6 they were the same call: the score was computed
    at ``cdda2img.py:2321`` and the menu ran at ``:2349``, so every container from
    the N5 alternatives menu carried ``match_confidence=0.550`` beside
    ``release_selection=manual`` — two keys describing two moments with nothing
    saying which was which.

    **A manual selection short-circuits to 1.000.** kgr's ruling, 2026-08-13:

        the purpose of ``match_confidence`` is to say how confident the automatic
        *guess* is, and a manual selection is not a guess — it is a certainty.

    So the key keeps its 2026-06-20 meaning and is merely recorded at the moment
    that meaning is finally determined. Note this **replaces** the scorer rather
    than adding to it: the "how MB found it" axis never runs on a manual pick, so
    a user-confirmed duration match cannot read 0.20.

    ``release_selection`` takes four values and only two are handled here:

    ``manual``
        1.000. The user held the disc and picked.
    ``unique`` / ``auto_tiebreak`` / absent
        Fall through to :func:`build_match_distance` unchanged.
    ``rejected``
        **Also falls through, and this is a known gap, not an oversight.** The
        user said none of the listed pressings match; ``PressingScreen`` keeps the
        automatic pick anyway ("The automatic pick is kept, but flagged as
        unconfirmed") and does *not* clear ``mb_release_id``. So a rejected disc
        still scores ``mb_disc_id_multi`` at 0.30 — identical to an *un-reviewed*
        ``auto_tiebreak``, which loses the fact that a human looked and said no.
        ``rejected`` is negative evidence about the candidate *list* and nothing
        reads it. kgr's ruling covers ``manual`` and deliberately leaves this
        open: flag it rather than invent a value (TODO N6).
    """
    if prov.get("release_selection") == "manual":
        return MatchDistance(
            score=1.0,
            contributors={"user_confirmed": 1.0},
            recommendation=MatchRecommendation.STRONG,
        )
    return build_match_distance(disc, prov)
