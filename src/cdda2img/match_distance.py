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


def _release_contributor(
    disc: RBIDisc, prov: dict[str, str]
) -> tuple[str, float] | None:
    """How the release was identified, and its weight; None without a release."""
    if not disc.mb_release_id:
        return None
    if prov.get("mb_identified_via") in ("file_tags", "file_match"):
        # create's file match (trust_model_design.md §14): a tier-A release every
        # file was found on. No disc ID was looked up, and naming it mb_disc_id
        # said one was (kgr's run, 2026-11-28). Same weight.
        return "file_match", 0.50
    if "duration_match_release" in prov:
        # Came from text+duration fuzzy — lowest-confidence MB path
        return "mb_duration_match", 0.20
    if prov.get("release_selected_via"):
        # §10.3: the disc-ID fingerprint matched MB but several pressings
        # tied; the lexicographic rung picked one by preference/date. The
        # album/master is identified, but the *specific pressing* is a best
        # guess — so this must NOT reach STRONG auto-apply on its own; the
        # user should see the menu to confirm the pressing. Weaker than a
        # unique disc-ID hit (0.50), stronger than a duration fuzzy (0.20).
        return "mb_disc_id_multi", 0.30
    # Came from a unique disc-ID fingerprint — deterministic, strong
    return "mb_disc_id", 0.50


def build_match_distance(disc: RBIDisc, prov: dict[str, str]) -> MatchDistance:
    """Compute a confidence score from the post-lookup disc and provenance.

    Contributors (additive, then clamped to [0, 1]):

    +0.50  mb_disc_id          — MB release_id from a unique disc-ID fingerprint (strong)
    +0.50  file_match          — create: the release the file match chose (§14)
    +0.30  mb_disc_id_multi    — disc-ID matched several pressings; rung pinned one (§10.3)
    +0.20  mb_duration_match   — MB release_id from text+duration fuzzy (weaker signal)
    +0.25  acoustid            — AcoustID corroborates the MB release (R6)
    +0.15  isrc_disambiguated  — ISRCs resolved a multi-match disc-ID (R1)
    -0.10  cddb_mb_disagreement — CDDB and MB disagree on album / artist (R9)
    """
    contributors: dict[str, float] = {}
    release = _release_contributor(disc, prov)
    if release is not None:
        contributors[release[0]] = release[1]

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


def _user_confirmed(prov: dict[str, str]) -> bool:
    """The user, holding the disc, settled which release it is (kgr: "logically
    that is always the strongest signal").

    Either they picked the pressing on the MusicBrainz pressing screen
    (``release_selection=manual``), or they picked the Discogs release on the
    master screen (``discogs_release_via=master_versions``, 2026-11-26) where
    MusicBrainz had only one pressing to offer (``release_selection=unique``) or
    pinned none (absent: a fallback identified the album, and the user's Discogs
    pick is the only pressing identity there is), so nothing about the pressing
    was left to the automatic tiebreak. A Discogs pick
    beside an un-reviewed ``auto_tiebreak`` pressing does NOT count: the
    MusicBrainz release in the container would still be a guess.
    """
    selection = prov.get("release_selection")
    # `derived` (§13.6): the user picked the Discogs release and the pressing
    # follows from it uniquely, by MusicBrainz's link or a field match.
    # `accepted` (kgr, 2026-11-26): the user left the marked pressing in place
    # with "none of these" on offer, which is a yes.
    if selection in ("manual", "derived", "accepted"):
        return True
    # A Discogs release picked on the master screen is the only pressing identity
    # when MusicBrainz offered one pressing, none, or the user rejected or
    # declined every one it offered.
    return prov.get("discogs_release_via") == "master_versions" and selection in (
        "unique",
        None,
        "rejected",
        "release_group_only",
    )


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

    ``release_selection`` and what it scores here (see :func:`_user_confirmed`):

    ``manual`` / ``derived`` / ``accepted``
        1.000. The user held the disc and picked, or picked the Discogs release the
        pressing follows from, or left the marked pressing in place with "none of
        these" on offer (kgr, 2026-11-26: that is a yes).
    ``rejected`` / ``release_group_only`` / ``unique`` / absent
        1.000 when the user picked the Discogs release on the master screen (the
        only pressing identity there is); otherwise falls through to
        :func:`build_match_distance`. ``rejected`` no longer keeps the automatic
        pick: the rip records the album only (2026-11-26), which closes the gap
        this docstring used to flag (TODO N6).
    ``auto_tiebreak`` / ``paired``
        Fall through: the pressing was settled without a human.
    """
    if _user_confirmed(prov):
        return MatchDistance(
            score=1.0,
            contributors={"user_confirmed": 1.0},
            recommendation=MatchRecommendation.STRONG,
        )
    return build_match_distance(disc, prov)
