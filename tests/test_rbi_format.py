"""
test_rbi_format.py — RBI format helpers (year_of, format_original).

The four canonical ``format_original`` strings are pinned here as golden
outputs so the format contract (capitalisation, gating, unknown defaults)
can't drift silently.
"""

from __future__ import annotations

from cdda2img.rbi_format import (
    RBIDisc,
    format_disc_metadata,
    format_original,
    format_original_fields,
    year_of,
)


def _disc(**kw) -> RBIDisc:
    return RBIDisc(
        album=kw.pop("album", "Album"), artist=kw.pop("artist", "Artist"), **kw
    )


# ---------------------------------------------------------------------------
# year_of
# ---------------------------------------------------------------------------


def test_year_of_variants():
    assert year_of("1983") == 1983
    assert year_of("1983-11-18") == 1983
    assert year_of("") is None
    assert year_of(None) is None
    assert year_of("not-a-date") is None


# ---------------------------------------------------------------------------
# format_original — the four golden strings
# ---------------------------------------------------------------------------


def test_format_original_yes_this_release():
    disc = _disc(
        release_date="2024", original_release_found=True, original_release_year=2024
    )
    assert format_original(disc) == "Original:  Yes, this release (2024)"


def test_format_original_no_names_the_earlier_release():
    disc = _disc(
        release_date="2008",
        original_release_found=True,
        original_release_title="Thriller",
        original_release_year=1982,
    )
    assert format_original(disc) == "Original:  No, Thriller (1982)"


def test_format_original_no_with_unknown_earlier_fields():
    disc = _disc(
        release_date="2008",
        original_release_found=True,
        original_release_title=None,
        original_release_year=None,
    )
    assert format_original(disc) == "Original:  No, unknown release (unknown year)"


def test_format_original_unknown_when_disc_year_unknown():
    # No disc year → cannot claim anything predates it → Unknown, gated fields.
    disc = _disc(
        release_date=None,
        original_release_found=True,
        original_release_title="Thriller",
        original_release_year=1982,
    )
    assert format_original(disc) == "Original:  Unknown, unknown release (unknown year)"


def test_format_original_unknown_when_not_found():
    disc = _disc(release_date="1983", original_release_found=False)
    assert format_original(disc) == "Original:  Unknown, unknown release (unknown year)"


def test_format_original_year_granularity():
    # A 1983 pressing is the original even when the RG first date is 1983-03-23
    # (year-granularity comparison: disc year 1983 == original year 1983).
    disc = _disc(
        release_date="1983-11-18",
        original_release_found=True,
        original_release_title="Eliminator",
        original_release_year=1983,
    )
    assert format_original(disc) == "Original:  Yes, this release (1983)"


# ---------------------------------------------------------------------------
# format_original_fields — the shared core (RBI list + catalogue route here)
# ---------------------------------------------------------------------------


def test_format_original_fields_golden_strings():
    # Same four states as format_original, pinned at the field level so the
    # core can't drift independently of the RBIDisc adapter.
    assert (
        format_original_fields(2024, True, None, 2024)
        == "Original:  Yes, this release (2024)"
    )
    assert (
        format_original_fields(2008, True, "Thriller", 1982)
        == "Original:  No, Thriller (1982)"
    )
    assert (
        format_original_fields(2008, True, None, None)
        == "Original:  No, unknown release (unknown year)"
    )
    assert (
        format_original_fields(None, True, "Thriller", 1982)
        == "Original:  Unknown, unknown release (unknown year)"
    )
    assert (
        format_original_fields(1983, False, None, None)
        == "Original:  Unknown, unknown release (unknown year)"
    )


def test_format_original_delegates_to_core():
    # The RBIDisc adapter must produce byte-identical output to the core fed the
    # same extracted values — this is what guarantees "identical everywhere".
    disc = _disc(
        release_date="2008-06-01",
        original_release_found=True,
        original_release_title="Thriller",
        original_release_year=1982,
    )
    assert format_original(disc) == format_original_fields(
        year_of(disc.release_date),
        disc.original_release_found,
        disc.original_release_title,
        disc.original_release_year,
    )


# ---------------------------------------------------------------------------
# format_disc_metadata — the canonical disc-level header (menu/list/catalogue)
# ---------------------------------------------------------------------------


def test_format_disc_metadata_full_golden():
    # Every value column starts at index 11 (== format_original_fields), and the
    # order is fixed: Album, Artist, Label, Country, Cat. no., MCN, Original,
    # Tracks. Pinned so the three surfaces cannot drift.
    assert format_disc_metadata(
        album="The Joshua Tree",
        artist="U2",
        release_date="1987-03-09",
        label="Island Records",
        country="GB",
        catalog_number="CID U2 6",
        mcn="0042284229821",
        original_release_found=True,
        original_release_title="The Joshua Tree",
        original_release_year=1987,
        track_count=11,
    ) == [
        "Album:     The Joshua Tree (1987)",
        "Artist:    U2",
        "Label:     Island Records",
        "Country:   GB",
        "Cat. no.:  CID U2 6",
        "MCN:       0042284229821",
        "Original:  Yes, this release (1987)",
        "Tracks:    11",
    ]


def test_format_disc_metadata_omits_absent_optionals():
    # Optional catalogue fields are dropped when None (no data gap); Album,
    # Artist, Original, Tracks always render. No duration -> bare track count.
    assert format_disc_metadata(
        album="X", artist="Y", release_date=None, track_count=3
    ) == [
        "Album:     X (unknown)",
        "Artist:    Y",
        "Original:  Unknown, unknown release (unknown year)",
        "Tracks:    3",
    ]


def test_format_disc_metadata_value_column_aligned():
    # The cross-surface guarantee: every rendered line places its value at the
    # same column, so the menu, list, and catalogue are visually interchangeable.
    lines = format_disc_metadata(
        album="A",
        artist="B",
        release_date="2000",
        label="L",
        country="US",
        catalog_number="C",
        mcn="M",
        track_count=1,
    )
    for line in lines:
        assert line[11] != " ", line  # value char present at the fixed column
        assert ":" in line[:11], line  # label (with colon) occupies the prefix
