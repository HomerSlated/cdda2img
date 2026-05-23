"""Tests for disc_writer.py TOC sanitization helpers."""

from cdda2img.disc_writer import _sanitize_toc_for_burn


def _toc(
    catalog: str | None = None,
    tracks: list[tuple[str, str]] | None = None,
    disc_title: str = "Test Album",
    disc_performer: str = "Test Artist",
) -> str:
    lines = ["CD_DA\n"]
    if catalog is not None:
        lines.append(f'CATALOG "{catalog}"\n')
    # Always emit the disc-level block as-is (including empty-string fields) so
    # _sanitize_toc_for_burn can be tested against old-RBI-style empty shells.
    lines += [
        "CD_TEXT {",
        "  LANGUAGE_MAP { 0: 9 }",
        "  LANGUAGE 0 {",
        f'    TITLE "{disc_title}"',
        f'    PERFORMER "{disc_performer}"',
        "  }",
        "}\n",
    ]
    for title, performer in tracks or [("Track 1", "Test Artist")]:
        lines += [
            "TRACK AUDIO",
            "NO COPY",
            "CD_TEXT {",
            "  LANGUAGE 0 {",
            f'    TITLE "{title}"',
            f'    PERFORMER "{performer}"',
            "  }",
            "}",
            'FILE "disc.wav" 0 1:00:00',
            "",
        ]
    return "\n".join(lines)


# ── CATALOG sanitization ──────────────────────────────────────────────────────


def test_valid_13_digit_catalog_is_kept():
    toc = _toc(catalog="1234567890123")
    result = _sanitize_toc_for_burn(toc)
    assert 'CATALOG "1234567890123"' in result


def test_11_digit_catalog_is_stripped():
    toc = _toc(catalog="07599237742")
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result


def test_12_digit_catalog_is_stripped():
    toc = _toc(catalog="123456789012")
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result


def test_14_digit_catalog_is_stripped():
    toc = _toc(catalog="12345678901234")
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result


def test_non_digit_catalog_is_stripped():
    toc = _toc(catalog="ABCDEF1234567")
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result


def test_no_catalog_line_unchanged():
    toc = _toc(catalog=None)
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result
    assert "CD_DA" in result


# ── Empty CD-Text field sanitization ─────────────────────────────────────────


def test_empty_performer_is_stripped():
    toc = _toc(tracks=[("Sharp Dressed Man", "")])
    result = _sanitize_toc_for_burn(toc)
    assert 'PERFORMER ""' not in result
    assert "Sharp Dressed Man" in result


def test_empty_title_is_stripped():
    toc = _toc(tracks=[("", "ZZ Top")])
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE ""' not in result
    assert "ZZ Top" in result


def test_non_empty_fields_are_kept():
    toc = _toc(tracks=[("Gimme All Your Lovin", "ZZ Top")])
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE "Gimme All Your Lovin"' in result
    assert 'PERFORMER "ZZ Top"' in result


def test_disc_level_fields_are_not_affected_by_empty_regex():
    """Disc-level TITLE/PERFORMER at column 4 should not be touched."""
    toc = _toc(catalog=None)
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE "Test Album"' in result
    assert 'PERFORMER "Test Artist"' in result


def test_both_sanitizations_applied_together():
    toc = _toc(catalog="07599237742", tracks=[("Track 1", ""), ("Track 2", "Artist")])
    result = _sanitize_toc_for_burn(toc)
    assert "CATALOG" not in result
    assert 'PERFORMER ""' not in result
    assert 'PERFORMER "Artist"' in result
    assert "Track 1" in result
    assert "Track 2" in result


# ── Empty CD_TEXT shell stripping ─────────────────────────────────────────────


def test_track_cdtext_block_stripped_when_both_fields_empty():
    """A track-level CD_TEXT block with both TITLE "" and PERFORMER "" stripped."""
    toc = _toc(tracks=[("", "")])
    result = _sanitize_toc_for_burn(toc)
    # Disc-level block is preserved (has default "Test Album" / "Test Artist").
    assert 'TITLE "Test Album"' in result
    # Track-level block is entirely removed — only the disc-level CD_TEXT remains.
    assert result.count("CD_TEXT {") == 1


def test_track_cdtext_block_preserved_when_title_present():
    """Block kept when TITLE has content even if PERFORMER is empty."""
    toc = _toc(tracks=[("Sharp Dressed Man", "")])
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE "Sharp Dressed Man"' in result
    assert result.count("CD_TEXT {") == 2  # disc-level + track-level both present


def test_disc_level_empty_language_block_stripped():
    """Disc-level CD_TEXT with empty TITLE and PERFORMER stripped entirely."""
    toc = _toc(disc_title="", disc_performer="")
    result = _sanitize_toc_for_burn(toc)
    # The entire disc-level CD_TEXT block (including LANGUAGE_MAP shell) is gone.
    assert "LANGUAGE_MAP" not in result
    assert "CD_DA" in result


def test_disc_level_nonempty_preserved_when_tracks_empty():
    """Disc-level CD_TEXT with content is kept even when track blocks are stripped."""
    toc = _toc(disc_title="The Eliminator", disc_performer="ZZ Top", tracks=[("", "")])
    result = _sanitize_toc_for_burn(toc)
    assert 'TITLE "The Eliminator"' in result
    assert 'PERFORMER "ZZ Top"' in result
    # Disc-level block preserved (has LANGUAGE_MAP); track-level block is gone.
    assert "LANGUAGE_MAP" in result
    assert result.count("CD_TEXT {") == 1
