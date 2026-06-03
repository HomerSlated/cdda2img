"""
test_toc.py — Round-trip and canonical-format tests for generate_toc / parse_toc.
"""

from cdda2img.barcode import normalize_barcode
from cdda2img.cdrdao_reader import parsed_to_rbi_disc
from cdda2img.rbi_format import RBIDisc, RBITocEntry
from cdda2img.toc import escape_toc_string, generate_toc, sanitize_title
from cdda2img.toc_parser import parse_toc

# ---------------------------------------------------------------------------
# Shared fixture — covers the full optional-field surface:
#   catalog set, disc_id set,
#   track 1: ISRC + no pregap
#   track 2: pregap only
#   track 3: ISRC + pregap
#   track 4: bare (neither)
# ---------------------------------------------------------------------------

_FRAMES_PER_MIN = 75 * 60


def _make_disc() -> RBIDisc:
    disc = RBIDisc(
        album="Test Album",
        artist="Test Artist",
        catalog="0724383697724",
        disc_id="CAT-001",
    )
    disc.tracks = [
        RBITocEntry(
            track_number=1,
            title="Track One",
            performer="Test Artist",
            start_frame=0,
            duration_frames=_FRAMES_PER_MIN * 3,
            pregap_frames=0,
            isrc="GBAYE9300001",
        ),
        RBITocEntry(
            track_number=2,
            title="Track Two",
            performer="Test Artist",
            start_frame=_FRAMES_PER_MIN * 3,
            duration_frames=_FRAMES_PER_MIN * 4,
            pregap_frames=150,  # 2-second pregap
            isrc=None,
        ),
        RBITocEntry(
            track_number=3,
            title="Track Three",
            performer="Test Artist",
            start_frame=_FRAMES_PER_MIN * 7 + 150,
            duration_frames=_FRAMES_PER_MIN * 3,
            pregap_frames=75,  # 1-second pregap
            isrc="GBAYE9300003",
        ),
        RBITocEntry(
            track_number=4,
            title="Track Four",
            performer="Test Artist",
            start_frame=_FRAMES_PER_MIN * 10 + 225,
            duration_frames=_FRAMES_PER_MIN * 5,
            pregap_frames=0,
            isrc=None,
        ),
    ]
    return disc


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_toc_generate_is_deterministic() -> None:
    disc = _make_disc()
    assert generate_toc(disc) == generate_toc(disc)


# ---------------------------------------------------------------------------
# Field round-trip: parse(generate(disc)) preserves every field
# ---------------------------------------------------------------------------


def test_toc_fields_round_trip() -> None:
    disc = _make_disc()
    toc_bytes = generate_toc(disc)
    parsed = parse_toc(toc_bytes)

    assert parsed.title == sanitize_title(disc.album)
    assert parsed.performer == sanitize_title(disc.artist)
    assert parsed.catalog == disc.catalog
    assert parsed.disc_id == disc.disc_id
    assert len(parsed.tracks) == len(disc.tracks)

    for pt, rt in zip(parsed.tracks, disc.tracks):
        assert pt.track_number == rt.track_number
        assert pt.title == rt.title
        assert pt.performer == sanitize_title(rt.performer)
        assert pt.isrc == rt.isrc
        assert pt.duration_frames == rt.duration_frames
        assert pt.pregap_frames == rt.pregap_frames
        assert pt.start_frame == rt.start_frame


# ---------------------------------------------------------------------------
# Byte-identical round-trip: parse → parsed_to_rbi_disc → generate
# Only valid for pure-ASCII titles (sanitisation is idempotent on ASCII).
# ---------------------------------------------------------------------------


def test_toc_bytes_round_trip() -> None:
    disc1 = _make_disc()
    toc1 = generate_toc(disc1)
    parsed = parse_toc(toc1)
    disc2 = parsed_to_rbi_disc(parsed)
    toc2 = generate_toc(disc2)
    assert toc1 == toc2


# ---------------------------------------------------------------------------
# TRACK_TITLE_UNICODE comment: written when needed, absent otherwise
# ---------------------------------------------------------------------------


def test_toc_unicode_comment_present() -> None:
    disc = _make_disc()
    raw_titles = ["Träck Öne", "Track Two", "Track Three", "Track Four"]
    toc_bytes = generate_toc(disc, raw_titles=raw_titles)
    toc_text = toc_bytes.decode("utf-8")

    # Comment must be present for track 1 (title differs after sanitisation)
    assert "// TRACK_TITLE_UNICODE:" in toc_text
    parsed = parse_toc(toc_bytes)
    assert parsed.tracks[0].title == "Träck Öne"


def test_toc_unicode_comment_absent_for_ascii() -> None:
    disc = _make_disc()
    # raw_titles identical to sanitised titles — no comment needed
    raw_titles = [t.title for t in disc.tracks]
    toc_bytes = generate_toc(disc, raw_titles=raw_titles)
    toc_text = toc_bytes.decode("utf-8")

    assert "// TRACK_TITLE_UNICODE:" not in toc_text


def test_toc_unicode_comment_recovery() -> None:
    disc = _make_disc()
    # Track 2 gets a curly-quote title; sanitiser converts it to ASCII
    disc.tracks[1].title = sanitize_title("It’s Alive")  # noqa: RUF001
    raw_titles = [t.title for t in disc.tracks]
    raw_titles[1] = "It’s Alive"  # noqa: RUF001  # Unicode original
    toc_bytes = generate_toc(disc, raw_titles=raw_titles)
    parsed = parse_toc(toc_bytes)
    assert parsed.tracks[1].title == "It’s Alive"  # noqa: RUF001
    assert parsed.tracks[0].title == disc.tracks[0].title  # unchanged


# ---------------------------------------------------------------------------
# Canonical formatting spot-checks
# ---------------------------------------------------------------------------


def test_toc_file_extension_is_bin() -> None:
    disc = _make_disc()
    toc_text = generate_toc(disc).decode("utf-8")
    assert '.bin"' in toc_text
    assert '.pcm"' not in toc_text


def test_toc_file_start_uses_timestamp_not_zero() -> None:
    """Track 1 start should be 00:00:00, not bare 0 (canonical rule §6.1.9.6)."""
    disc = _make_disc()
    toc_text = generate_toc(disc).decode("utf-8")
    # The FILE line for track 1 starts at 00:00:00
    assert 'FILE "' in toc_text
    assert '" 00:00:00 ' in toc_text


def test_toc_unix_line_endings() -> None:
    disc = _make_disc()
    toc_bytes = generate_toc(disc)
    assert b"\r\n" not in toc_bytes
    assert toc_bytes.endswith(b"\n")


def test_toc_optional_fields_absent_when_none() -> None:
    disc = RBIDisc(album="Bare Album", artist="Bare Artist")
    disc.tracks = [
        RBITocEntry(
            track_number=1,
            title="Only Track",
            performer="Bare Artist",
            start_frame=0,
            duration_frames=_FRAMES_PER_MIN * 3,
        )
    ]
    toc_text = generate_toc(disc).decode("utf-8")
    assert "CATALOG" not in toc_text
    assert "DISC_ID" not in toc_text
    assert "ISRC" not in toc_text
    assert "START" not in toc_text
    assert "TRACK_TITLE_UNICODE" not in toc_text


def test_toc_drops_invalid_catalog() -> None:
    """Safety net: invalid disc.catalog (not 12/13 digits) is omitted, not emitted raw."""
    disc = RBIDisc(album="X", artist="Y", catalog="0 7599-23774-2")
    disc.tracks = [
        RBITocEntry(
            track_number=1,
            title="T",
            performer="Y",
            start_frame=0,
            duration_frames=_FRAMES_PER_MIN,
        )
    ]
    toc_text = generate_toc(disc).decode("utf-8")
    assert "CATALOG" not in toc_text


def test_toc_normalises_upc_to_gtin13() -> None:
    """A 12-digit UPC-A in disc.catalog is padded to 13-digit GTIN-13 in the TOC."""
    disc = RBIDisc(album="X", artist="Y", catalog="075992377423")
    disc.tracks = [
        RBITocEntry(
            track_number=1,
            title="T",
            performer="Y",
            start_frame=0,
            duration_frames=_FRAMES_PER_MIN,
        )
    ]
    toc_text = generate_toc(disc).decode("utf-8")
    assert 'CATALOG "0075992377423"' in toc_text


# ---------------------------------------------------------------------------
# PERFORMER fallback
# ---------------------------------------------------------------------------


def test_performer_fallback_from_disc_artist() -> None:
    """Empty track.performer falls back to disc.artist in the TOC."""
    disc = RBIDisc(album="Test Album", artist="Album Artist")
    disc.tracks = [
        RBITocEntry(
            track_number=1,
            title="Track One",
            performer="",
            start_frame=0,
            duration_frames=_FRAMES_PER_MIN * 3,
        )
    ]
    toc_text = generate_toc(disc).decode("utf-8")
    assert 'PERFORMER "Album Artist"' in toc_text


def test_performer_no_fallback_when_both_empty() -> None:
    """No PERFORMER line emitted when both track.performer and disc.artist are empty."""
    disc = RBIDisc(album="Test Album", artist="")
    disc.tracks = [
        RBITocEntry(
            track_number=1,
            title="Track One",
            performer="",
            start_frame=0,
            duration_frames=_FRAMES_PER_MIN * 3,
        )
    ]
    toc_text = generate_toc(disc).decode("utf-8")
    assert "PERFORMER" not in toc_text


# ---------------------------------------------------------------------------
# normalize_barcode
# ---------------------------------------------------------------------------


def test_normalize_barcode_gtin13_passthrough() -> None:
    assert normalize_barcode("0724383697724") == "0724383697724"


def test_normalize_barcode_upc_a_padded_to_gtin13() -> None:
    assert normalize_barcode("075992377423") == "0075992377423"


def test_normalize_barcode_strips_non_digits() -> None:
    assert normalize_barcode("0 75992 37742 3") == "0075992377423"


def test_normalize_barcode_rejects_short() -> None:
    assert normalize_barcode("12345") is None


def test_normalize_barcode_rejects_long() -> None:
    assert normalize_barcode("12345678901234") is None


def test_normalize_barcode_none_input() -> None:
    assert normalize_barcode(None) is None


def test_normalize_barcode_empty_string() -> None:
    assert normalize_barcode("") is None


# ---------------------------------------------------------------------------
# Injection safety (GRD-2026-0531-01 / -03) — escape_toc_string + TITLE/ISRC
# ---------------------------------------------------------------------------


def test_escape_toc_string_strips_control_chars() -> None:
    """Newline/CR/tab/DEL are removed so a value can't break onto a new line."""
    assert escape_toc_string("a\nb\rc\td\x7fe") == "abcde"


def test_escape_toc_string_doubles_backslash() -> None:
    """cdrdao treats \\ as an escape introducer; a lone trailing \\ would escape
    the closing quote, so every backslash is doubled to a literal."""
    assert escape_toc_string("foo\\") == "foo\\\\"
    assert escape_toc_string('bar\\"baz') == "bar\\\\'baz"


def test_escape_toc_string_quote_to_apostrophe() -> None:
    assert escape_toc_string('a"b') == "a'b"


def test_escape_toc_string_preserves_non_ascii() -> None:
    """Unlike sanitize_title, escape_toc_string keeps non-ASCII (track titles)."""
    assert escape_toc_string("Café déjà vu") == "Café déjà vu"


def _disc_with_track_title(title: str) -> RBIDisc:
    disc = RBIDisc(album="Album", artist="Artist")
    disc.tracks = [
        RBITocEntry(
            track_number=1,
            title=title,
            performer="Artist",
            start_frame=0,
            duration_frames=_FRAMES_PER_MIN,
        )
    ]
    return disc


def test_track_title_newline_cannot_inject_toc_directive() -> None:
    """A track title with a quote + newline must not forge a TOC directive."""
    evil = 'Real Title"\n  PERFORMER "Hacker'
    toc_text = generate_toc(_disc_with_track_title(evil)).decode("utf-8")
    # The malicious payload must be confined to a single TITLE line — no forged
    # PERFORMER carrying the injected value, and no stray double-quote anywhere.
    assert '"Hacker' not in toc_text
    assert 'PERFORMER "Hacker' not in toc_text
    # The TITLE line itself stays a single, well-formed line.
    title_lines = [ln for ln in toc_text.splitlines() if ln.strip().startswith("TITLE")]
    assert any("Real Title'" in ln for ln in title_lines)
    # Round-trips through the parser as an ordinary title.
    parsed = parse_toc(generate_toc(_disc_with_track_title(evil)))
    assert parsed.tracks[0].title is not None


def test_track_title_trailing_backslash_cannot_escape_quote() -> None:
    """A title ending in a backslash must not escape the TITLE closing quote."""
    toc_text = generate_toc(_disc_with_track_title("Title\\")).decode("utf-8")
    assert '    TITLE "Title\\\\"' in toc_text
    # The next track-block directive is still on its own line, not swallowed.
    parsed = parse_toc(generate_toc(_disc_with_track_title("Title\\")))
    assert len(parsed.tracks) == 1


def test_track_title_preserves_non_ascii_in_toc() -> None:
    """Non-ASCII track titles survive into the TOC (FLAC fidelity)."""
    toc_text = generate_toc(_disc_with_track_title("Café")).decode("utf-8")
    assert 'TITLE "Café"' in toc_text


def test_isrc_with_injection_payload_is_escaped() -> None:
    """Defence-in-depth: even an unvalidated ISRC sink cannot inject."""
    disc = _disc_with_track_title("T")
    disc.tracks[0].isrc = 'X"\n  PERFORMER "Z'
    toc_text = generate_toc(disc).decode("utf-8")
    assert '"Z' not in toc_text
