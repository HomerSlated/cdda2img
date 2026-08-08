"""Tests for cdda2img._discogs_barcode_corroborate (§10.3.1 cross-source check).

The MB url-rels fetch and the Discogs release fetch are both mocked — these
tests pin the PROV-only corroboration/conflict logic and the clean-skip guards,
not the network shape (that lives in test_mb_lookup.discogs_link_and_barcode).
"""

from __future__ import annotations

from unittest.mock import patch

from cdda2img.cdda2img import _discogs_barcode_corroborate
from cdda2img.lookup_result import DiscMeta
from cdda2img.rbi_format import RBIDisc


def _disc() -> RBIDisc:
    return RBIDisc(album="A", artist="B", mb_release_id="sel-mbid")


def _run(*, link, mb_barcode, discogs_meta, available=True):
    """Invoke the corroborator with all three network seams mocked."""
    disc = _disc()
    prov: dict[str, str] = {}
    with (
        patch("cdda2img.discogs_lookup.is_available", return_value=available),
        patch(
            "cdda2img.mb_lookup.discogs_link_and_barcode",
            return_value=(link, mb_barcode),
        ) as link_fn,
        patch(
            "cdda2img.discogs_lookup.fetch_release", return_value=discogs_meta
        ) as fetch_fn,
    ):
        _discogs_barcode_corroborate(disc, prov, selected_release_id=disc.mb_release_id)
    return prov, link_fn, fetch_fn


def test_agreement_sets_corroborates():
    prov, _, _ = _run(
        link=1198146,
        mb_barcode="042284229821",
        discogs_meta=DiscMeta(barcode="042284229821", source="discogs"),
    )
    assert prov["discogs_corroborates"] == "YES"
    assert "discogs_barcode_conflict" not in prov


def test_conflict_sets_conflict_with_both_barcodes():
    prov, _, _ = _run(
        link=1198146,
        mb_barcode="042284229821",
        discogs_meta=DiscMeta(barcode="999999999999", source="discogs"),
    )
    assert prov["discogs_barcode_conflict"] == "mb:042284229821|discogs:999999999999"
    # NO, not absent. The key mirrors `acoustid_corroborates`, which reports both
    # verdicts — and a fail-only key cannot be told apart from a check that never
    # ran, which is the distinction PROV exists to preserve.
    assert prov["discogs_corroborates"] == "NO"


def test_skips_without_mb_release_id():
    disc = RBIDisc(album="A", artist="B")  # no mb_release_id
    prov: dict[str, str] = {}
    with patch("cdda2img.discogs_lookup.is_available", return_value=True) as avail:
        _discogs_barcode_corroborate(disc, prov, selected_release_id=disc.mb_release_id)
    assert prov == {}
    avail.assert_not_called()  # short-circuits before touching Discogs


def test_skips_when_discogs_unavailable_no_mb_fetch():
    prov, link_fn, fetch_fn = _run(
        link=1198146,
        mb_barcode="042284229821",
        discogs_meta=DiscMeta(barcode="042284229821"),
        available=False,
    )
    assert prov == {}
    link_fn.assert_not_called()  # no MB url-rels fetch when we can't use Discogs
    fetch_fn.assert_not_called()


def test_skips_when_no_discogs_link():
    prov, _, fetch_fn = _run(link=None, mb_barcode="042284229821", discogs_meta=None)
    assert prov == {}
    fetch_fn.assert_not_called()  # no link -> no Discogs fetch


def test_skips_when_no_mb_barcode():
    prov, _, fetch_fn = _run(link=1198146, mb_barcode=None, discogs_meta=None)
    assert prov == {}
    fetch_fn.assert_not_called()


def test_skips_when_discogs_fetch_returns_none():
    prov, _, _ = _run(link=1198146, mb_barcode="042284229821", discogs_meta=None)
    assert prov == {}


def test_skips_when_discogs_release_has_no_barcode():
    prov, _, _ = _run(
        link=1198146,
        mb_barcode="042284229821",
        discogs_meta=DiscMeta(barcode=None, source="discogs"),
    )
    assert prov == {}


# ── lookup_status_discogs answers ONE question: did Discogs reply ─────────────


def test_a_release_with_no_barcode_still_counts_as_an_answer():
    """ "Answered" and "agreed" are separate facts.

    A Discogs release came back; it simply carries no barcode, so the comparison
    cannot run. Reporting that as "Discogs returned nothing" is the exact
    conflation this key has now got wrong twice.
    """
    prov, _, _ = _run(
        link=1198146,
        mb_barcode="042284229821",
        discogs_meta=DiscMeta(barcode=None, source="discogs"),
    )
    assert "discogs_corroborates" not in prov  # nothing to compare
    # …but the caller is told Discogs replied, which is what the status reads.


def test_the_corroboration_reports_whether_discogs_answered():
    """The return value, not the PROV key, is what feeds lookup_status_discogs —
    so agreement, disagreement and a barcode-less release must all report True,
    and only a genuine non-answer False."""
    from cdda2img.cdda2img import _discogs_barcode_corroborate

    disc = RBIDisc(album="A", artist="B")
    prov: dict[str, str] = {}
    # No selected release: nothing was asked, so nothing answered.
    assert _discogs_barcode_corroborate(disc, prov, selected_release_id=None) is False
    assert prov == {}
