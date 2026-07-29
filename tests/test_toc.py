"""
test_toc.py — Round-trip and canonical-format tests for generate_toc / parse_toc.
"""

import pytest

from cdda2img import toc_parser
from cdda2img.barcode import normalize_barcode
from cdda2img.cdrdao_reader import parsed_to_rbi_disc
from cdda2img.rbi_format import RBIDisc, RBITocEntry
from cdda2img.toc import (
    escape_toc_string,
    fold_cdtext,
    generate_toc,
    sanitize_title,
)
from cdda2img.toc_parser import parse_toc

# ---------------------------------------------------------------------------
# Shared fixture — covers the full optional-field surface:
#   catalog set, cdtext_catalog_ref set,
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
        cdtext_catalog_ref="CAT-001",
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
            pre_emphasis=True,  # spec §6.1.10 flags + INDEX >= 02 points
            copy_permitted=True,
            index_points=[750, 3000],
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
    assert parsed.disc_id == disc.cdtext_catalog_ref
    assert len(parsed.tracks) == len(disc.tracks)

    for pt, rt in zip(parsed.tracks, disc.tracks):
        assert pt.track_number == rt.track_number
        assert pt.title == rt.title
        assert pt.performer == sanitize_title(rt.performer)
        assert pt.isrc == rt.isrc
        assert pt.duration_frames == rt.duration_frames
        assert pt.pregap_frames == rt.pregap_frames
        assert pt.start_frame == rt.start_frame
        assert pt.pre_emphasis == rt.pre_emphasis
        assert pt.copy_permitted == rt.copy_permitted
        assert pt.index_points == rt.index_points

    # R14 aggregate: track 3 carries PRE_EMPHASIS, so the disc-level flag is set.
    assert parsed.pre_emphasis is True


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


def test_toc_emits_burnable_invalid_check_digit_catalog() -> None:
    """A 13-digit on-disc MCN with a bad GS1 check digit is still burned.

    cdrdao only requires 13 numeric digits; the check digit is our integrity
    preference, applied at selection (not the burn). A gospel MCN that reached
    disc.catalog must not be dropped here — cdrdao would happily burn it.
    """
    disc = RBIDisc(album="X", artist="Y", catalog="1234567890123")  # bad check digit
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
    assert 'CATALOG "1234567890123"' in toc_text


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


def test_normalize_barcode_rejects_bad_check_digit_by_default() -> None:
    """Default (strict) rejects a 13-digit value with a wrong GS1 check digit."""
    assert normalize_barcode("1234567890123") is None


def test_normalize_barcode_require_check_digit_false_keeps_burnable() -> None:
    """require_check_digit=False returns any 13-digit numeric value (cdrdao-burnable)."""
    assert (
        normalize_barcode("1234567890123", require_check_digit=False) == "1234567890123"
    )


def test_normalize_barcode_require_check_digit_false_still_needs_13_digits() -> None:
    """The burnable form still enforces cdrdao's 13-digit rule (drops 11-digit)."""
    assert normalize_barcode("12345678901", require_check_digit=False) is None


def test_normalize_barcode_require_check_digit_false_pads_upc_a() -> None:
    """UPC-A padding still applies in burnable mode (12 → 13)."""
    assert (
        normalize_barcode("075992377423", require_check_digit=False) == "0075992377423"
    )


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


def test_track_title_folded_in_cdtext_but_preserved_in_comment() -> None:
    """The CD-Text TITLE line is folded to charset-safe ASCII (cdrdao drops the
    whole lead-in on one un-encodable char), while the pristine Unicode is
    preserved in the TRACK_TITLE_UNICODE comment for FLAC fidelity — even with
    no raw_titles supplied, because folding is now the lossy step."""
    toc_bytes = generate_toc(_disc_with_track_title("Café"))
    toc_text = toc_bytes.decode("utf-8")
    assert 'TITLE "Cafe"' in toc_text  # folded in the CD-Text block
    assert 'TITLE "Café"' not in toc_text  # raw Unicode never reaches cdrdao
    assert '// TRACK_TITLE_UNICODE: "Caf\\u00e9"' in toc_text  # archived
    assert parse_toc(toc_bytes).tracks[0].title == "Café"  # round-trips


def test_u2010_hyphen_folded_not_dropped() -> None:
    """The exact bug: U+2010 HYPHEN (from MusicBrainz) folds to '-'. Previously
    cdrdao silently dropped ALL CD-Text on it; the sanitiser deleted it."""
    toc_text = generate_toc(_disc_with_track_title("Voulez‐Vous")).decode("utf-8")  # noqa: RUF001
    assert 'TITLE "Voulez-Vous"' in toc_text


def test_fold_cdtext_subsumes_the_old_replacement_table() -> None:
    """Curly quotes, dashes and ellipsis fold without a hand-kept table."""
    assert fold_cdtext("‘’") == "''"  # noqa: RUF001  # curly single quotes
    assert fold_cdtext("“”") == '""'  # curly double quotes
    assert fold_cdtext("–") == "-"  # noqa: RUF001  # en dash
    assert fold_cdtext("…") == "..."  # ellipsis


def test_fold_cdtext_leaves_ascii_untouched() -> None:
    """ASCII (incl. control chars, handled downstream) passes through."""
    assert fold_cdtext("Plain ASCII 123!") == "Plain ASCII 123!"


def test_fold_cdtext_reports_untranslatable_drop(caplog) -> None:
    """A codepoint with no transliteration is dropped from that one field and
    logged at WARNING — never silently, never taking down the lead-in."""
    with caplog.at_level("WARNING"):
        result = fold_cdtext("A\U0001f600B", field="track 1 title")
    assert result == "AB"  # emoji dropped, rest survives
    assert "dropped" in caplog.text
    assert "U+1F600" in caplog.text
    assert "track 1 title" in caplog.text


def test_sanitize_title_transliterates_instead_of_deleting() -> None:
    """Accented Latin folds to ASCII rather than being deleted (é -> e)."""
    assert sanitize_title("Beyoncé") == "Beyonce"


def test_isrc_with_injection_payload_is_escaped() -> None:
    """Defence-in-depth: even an unvalidated ISRC sink cannot inject."""
    disc = _disc_with_track_title("T")
    disc.tracks[0].isrc = 'X"\n  PERFORMER "Z'
    toc_text = generate_toc(disc).decode("utf-8")
    assert '"Z' not in toc_text


# ── TOC injection: quote-aware parsing (SECURITY, 2026-07-29) ────────────────
#
# The exposure is the `import` subcommand, which parses foreign cdrdao TOC+BIN
# images that nothing of ours ever escaped. `escape_toc_string` is on the WRITE
# side and gives zero protection here. Found while cross-checking AccuDisc's
# equivalent parser bug, which produced a phantom track, a shifted lead-out and
# an attacker-chosen ISRC returned as OK.


def _one_track_toc(title: str) -> bytes:
    return (
        f'CD_DA\n\nCD_TEXT {{ LANGUAGE 0 {{ TITLE "{title}" }} }}\n\n'
        f"// Track 1\nTRACK AUDIO\nNO COPY\nNO PRE_EMPHASIS\n"
        f'CD_TEXT {{ LANGUAGE 0 {{ TITLE "{title}" }} }}\n'
        f'FILE "a.bin" 0 00:04:00\n'
    ).encode()


def test_a_newline_inside_a_quoted_value_is_refused_not_parsed() -> None:
    """The demonstrated exploit: a payload in a CD_TEXT block changed the parsed
    title. The line-anchored patterns match inside a quoted string because they
    have no idea they are in one.

    Refusing beats sanitising: cdrdao strings do not span lines, so an
    unterminated one means malformed or hostile, and guessing where it was meant
    to close is how a parser ends up with two readings of the same bytes.
    """
    payload = b'CD_DA\n\nCD_TEXT { LANGUAGE 0 { TITLE "Normal\nx" } }\n\n// Track 1\n'
    with pytest.raises(toc_parser.TocParseError, match="unterminated string"):
        toc_parser.parse_toc(payload)


def test_a_directive_inside_a_quoted_value_is_not_a_directive() -> None:
    """Structure must come from the masked view, or a value becomes a field.

    START inside a title would have added a phantom pre-gap and silently shifted
    every downstream offset — the same class as AccuDisc's shifted lead-out.
    """
    masked = toc_parser.mask_quoted('TITLE "a START 00:02:00 b"\nSTART 00:01:00\n')
    assert masked.count("START") == 1  # the real one only
    assert len(masked) == len('TITLE "a START 00:02:00 b"\nSTART 00:01:00\n')


def test_masking_preserves_length_exactly() -> None:
    """Offsets are load-bearing: parse_toc slices per-track blocks by the byte
    positions of the `// Track` markers. A mask that changed length would trade an
    injection bug for a silent mis-slicing bug, which is worse."""
    for src in (
        'TITLE "abc"\n',
        'TITLE ""\n',
        'ISRC "GBAYE0000123"\nPERFORMER "x"\n',
        'TITLE "with \\" an escaped quote"\n',
        "no strings here at all\n",
    ):
        assert len(toc_parser.mask_quoted(src)) == len(src)


def test_escaped_quotes_do_not_end_the_string() -> None:
    """cdrdao's lexer treats \\" as a literal quote. Toggling on it would leave
    the parser's idea of "inside a string" inverted for the rest of the file —
    every subsequent directive misread, in whichever direction is worse."""
    masked = toc_parser.mask_quoted('TITLE "a \\" b"\nSTART 00:01:00\n')
    assert "START" in masked  # the real directive still visible
    assert masked.count('"') == 2  # both delimiters kept, the escaped one blanked


def test_values_still_round_trip_through_the_mask() -> None:
    """The mask must not eat the data. Values are read from the raw text at the
    offsets the masked view located, so both halves have to line up."""
    disc = toc_parser.parse_toc(_one_track_toc("Regular Title"))
    assert disc.title == "Regular Title"
    assert disc.tracks[0].title == "Regular Title"


def test_an_unterminated_string_at_eof_is_refused() -> None:
    """Same condition as mid-file: a cdrdao string cannot span a line, so the
    last line's unterminated quote is not a special case."""
    with pytest.raises(toc_parser.TocParseError, match="unterminated string"):
        toc_parser.mask_quoted('TITLE "never closed')


def test_a_comment_is_not_string_context() -> None:
    """`//` runs to end-of-line and cannot contain a TOC string.

    Our own `// TRACK_TITLE_UNICODE:` lines carry JSON, which is full of quotes.
    Treating those as delimiters would flip the scanner's idea of "inside a
    string" and refuse a file we wrote — and parse_toc is on the extract / list /
    test path, not just import, so that is a regression on our own archives.
    """
    src = '// a lone " quote in a comment\nTITLE "real"\nSTART 00:01:00\n'
    masked = toc_parser.mask_quoted(src)
    assert "START" in masked  # the real directive survived
    assert len(masked) == len(src)


def test_a_unicode_title_comment_with_embedded_quotes_round_trips() -> None:
    """The concrete case: json.dumps of a title containing quotes.

    Four raw quote characters on the line, two of them escaped. It balances — but
    it only balances because the escape handling is right, so this pins both.
    """
    from cdda2img.rbi_format import RBIDisc as _D
    from cdda2img.rbi_format import RBITocEntry as _T

    disc = _D(
        album="A",
        artist="B",
        tracks=[
            _T(
                track_number=1,
                title="She said Ubermensch",
                performer="B",
                start_frame=0,
                duration_frames=7500,
                pregap_frames=0,
            )
        ],
    )
    toc = generate_toc(disc, ['She said "Übermensch"'])
    assert b"TRACK_TITLE_UNICODE" in toc
    parsed = toc_parser.parse_toc(toc)
    assert parsed.tracks[0].title == 'She said "Übermensch"'
