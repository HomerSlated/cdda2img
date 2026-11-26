"""Field-by-field matching of MusicBrainz and Discogs releases (trust_model_design.md §13).

kgr, 2026-11-26: submissions on both services are incomplete and barely audited, so a
pressing is matched across them on whatever fields each side actually states.

- **A contradiction rejects.** Two present, normalised values that disagree mean the
  two records are not the same product.
- **Absence is silent.** An empty field, or one claiming no data exists ("no barcode",
  which on a CD always means "none submitted"), neither confirms nor refutes.
- **Sufficient** (§13.3): a strong identifier, barcode or catalogue number, present on
  both sides and agreeing, and no field contradicting. The evidence classes are a table
  (:data:`FIELDS`) so a fallback kgr expects to need (album title, track count, artist)
  is an added row, not a rewrite.

Pure: no network, no I/O. Callers build :class:`ReleaseRecord` from whatever they hold.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from cdda2img.barcode import normalize_barcode
from cdda2img.countries import discogs_country_code


@dataclass(frozen=True)
class ReleaseRecord:
    """One release as a matcher sees it: every field optional, every value raw."""

    source: str  # "mb" | "discogs"
    ident: str  # MBID or Discogs release id, as text
    barcodes: tuple[str, ...] = ()
    catnos: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    country: str | None = None  # MusicBrainz code, or Discogs' own name
    year: str | None = None
    extra: dict[str, str] = field(default_factory=dict, compare=False, hash=False)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

_ABSENT = {"", "none", "n/a", "na", "-", "unknown", "not on label"}
_LABEL_SUFFIXES = {
    "records",
    "recordings",
    "record",
    "ltd",
    "limited",
    "inc",
    "gmbh",
    "co",
    "company",
    "corporation",
    "corp",
    "llc",
    "bv",
    "sa",
    "ag",
}


def _present(value: str | None) -> str | None:
    text = (value or "").strip()
    return None if text.casefold() in _ABSENT else text


def norm_barcodes(values: Iterable[str | None]) -> frozenset[str]:
    """GTIN-13 forms of the values that normalise (check digit not required:
    a printed barcode with a wrong check digit is still that barcode)."""
    out = set()
    for v in values:
        b = normalize_barcode(_present(v) or "", require_check_digit=False)
        if b:
            out.add(b)
    return frozenset(out)


def norm_catnos(values: Iterable[str | None]) -> frozenset[str]:
    """Catalogue numbers case-folded with spaces, hyphens, dots and slashes removed
    (``824 499-2`` = ``8244992``). A value Discogs lists as several (``CDV 2345, 0777
    7 86402 2 6``) is split on commas."""
    out = set()
    for v in values:
        for part in (_present(v) or "").split(","):
            folded = re.sub(r"[\s\-./_\u2010\u2012\u2013\u2014]", "", part.casefold())
            if _present(folded):
                out.add(folded)
    return frozenset(out)


def norm_labels(values: Iterable[str | None]) -> frozenset[str]:
    """Label names case-folded, punctuation removed, and corporate suffixes
    ("Records", "Ltd", ...) dropped from the end."""
    out = set()
    for v in values:
        words = re.sub(r"[^\w\s]", " ", (_present(v) or "").casefold()).split()
        while words and words[-1] in _LABEL_SUFFIXES:
            words.pop()
        if words:
            out.add(" ".join(words))
    return frozenset(out)


def norm_year(value: str | None) -> str | None:
    text = (_present(value) or "")[:4]
    return text if len(text) == 4 and text.isdigit() and text != "0000" else None


#: Countries a MusicBrainz "XE" (Europe) or a Discogs European region contains.
_EUROPE = frozenset([
    "AD",
    "AL",
    "AT",
    "BA",
    "BE",
    "BG",
    "BY",
    "CH",
    "CY",
    "CZ",
    "DE",
    "DK",
    "EE",
    "ES",
    "FI",
    "FO",
    "FR",
    "GB",
    "GG",
    "GI",
    "GR",
    "HR",
    "HU",
    "IE",
    "IM",
    "IS",
    "IT",
    "JE",
    "LI",
    "LT",
    "LU",
    "LV",
    "MC",
    "MD",
    "ME",
    "MK",
    "MT",
    "NL",
    "NO",
    "PL",
    "PT",
    "RO",
    "RS",
    "RU",
    "SE",
    "SI",
    "SK",
    "SM",
    "UA",
    "VA",
    "XK",
    "XC",
    "XG",
    "SU",
    "YU",
    "CS",
])


def norm_country(value: str | None) -> frozenset[str] | None:
    """The places a country field can mean, or ``None`` when it states none.

    A MusicBrainz code stands for itself (``XE`` = Europe); a Discogs name is mapped
    with :func:`countries.discogs_country_code`, and a joint name ("UK, Europe & US")
    is split into its parts. ``XW`` (worldwide) and "Unknown" state no place.
    """
    text = _present(value)
    if text is None:
        return None
    places: set[str] = set()
    parts = re.split(r"\s*(?:,|&|\band\b)\s*", text)
    for part in parts:
        if not part:
            continue
        # A Discogs name first ("UK" is GB, not an ISO code), then a MusicBrainz
        # code ("GB", "XE"), which the Discogs mapping does not know.
        code = discogs_country_code(part)
        if code is None and len(part) == 2 and part.isalpha():
            code = part.upper()
        if code and code != "XW":
            places.add(code)
    return frozenset(places) or None


def _contains(a: str, b: str) -> bool:
    if a == b:
        return True
    return (a == "XE" and b in _EUROPE) or (b == "XE" and a in _EUROPE)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


Outcome = str  # "agree" | "contradict" | "absent"


def _sets(a: frozenset[str], b: frozenset[str]) -> Outcome:
    if not a or not b:
        return "absent"
    return "agree" if a & b else "contradict"


def _cmp_barcode(a: ReleaseRecord, b: ReleaseRecord) -> Outcome:
    return _sets(norm_barcodes(a.barcodes), norm_barcodes(b.barcodes))


def _cmp_catno(a: ReleaseRecord, b: ReleaseRecord) -> Outcome:
    return _sets(norm_catnos(a.catnos), norm_catnos(b.catnos))


def _cmp_label(a: ReleaseRecord, b: ReleaseRecord) -> Outcome:
    """Supporting only: an agreement counts, a difference is ``absent``, never a
    contradiction. Measured 2026-11-26 on *Brothers in Arms*: MusicBrainz lists
    "Phonogram" and "Vertigo" on 5a5330cd and the disc-ID response keeps the first,
    so a Discogs "Vertigo" looked contradicted; and the two services fill the field
    with label, imprint or distributor interchangeably. A difference here is not
    evidence of a different product."""
    la, lb = norm_labels(a.labels), norm_labels(b.labels)
    if not la or not lb:
        return "absent"
    for x in la:
        for y in lb:
            # "warner bros" agrees with "warner bros records" once folded, and a
            # name that is the other's leading words ("phonogram" / "phonogram
            # france") is the same label written more or less fully.
            if x == y or y.startswith(x + " ") or x.startswith(y + " "):
                return "agree"
    return "absent"


def _cmp_country(a: ReleaseRecord, b: ReleaseRecord) -> Outcome:
    ca, cb = norm_country(a.country), norm_country(b.country)
    if ca is None or cb is None:
        return "absent"
    return "agree" if any(_contains(x, y) for x in ca for y in cb) else "contradict"


def _cmp_year(a: ReleaseRecord, b: ReleaseRecord) -> Outcome:
    ya, yb = norm_year(a.year), norm_year(b.year)
    if ya is None or yb is None:
        return "absent"
    return "agree" if ya == yb else "contradict"


#: The evidence classes: (name, comparator, strong). A strong field can make a match
#: on its own (§13.3); every field can break one. Add fallbacks here.
FIELDS: tuple[
    tuple[str, Callable[[ReleaseRecord, ReleaseRecord], Outcome], bool], ...
] = (
    ("barcode", _cmp_barcode, True),
    ("catno", _cmp_catno, True),
    ("label", _cmp_label, False),
    ("country", _cmp_country, False),
    ("year", _cmp_year, False),
)


@dataclass(frozen=True)
class Comparison:
    """What two records said about each other, field by field."""

    agreed: tuple[str, ...]
    contradicted: tuple[str, ...]

    @property
    def compatible(self) -> bool:
        """No field contradicts."""
        return not self.contradicted

    @property
    def sufficient(self) -> bool:
        """Compatible, and a strong field agrees (§13.3)."""
        strong = {name for name, _cmp, is_strong in FIELDS if is_strong}
        return self.compatible and bool(strong & set(self.agreed))

    def describe(self) -> str:
        """``agreed:barcode,catno`` / ``contradicted:year`` for PROV."""
        if self.contradicted:
            return "contradicted:" + ",".join(self.contradicted)
        return "agreed:" + (",".join(self.agreed) or "none")


def compare(a: ReleaseRecord, b: ReleaseRecord) -> Comparison:
    agreed: list[str] = []
    contradicted: list[str] = []
    for name, cmp, _strong in FIELDS:
        outcome = cmp(a, b)
        if outcome == "agree":
            agreed.append(name)
        elif outcome == "contradict":
            contradicted.append(name)
    return Comparison(tuple(agreed), tuple(contradicted))


def matching_pairs(
    left: Sequence[ReleaseRecord], right: Sequence[ReleaseRecord]
) -> list[tuple[ReleaseRecord, ReleaseRecord, Comparison]]:
    """Every (left, right) pair whose comparison is :attr:`Comparison.sufficient`."""
    out = []
    for a in left:
        for b in right:
            c = compare(a, b)
            if c.sufficient:
                out.append((a, b, c))
    return out


def rank_compatible(
    anchor: ReleaseRecord, others: Sequence[ReleaseRecord]
) -> list[tuple[ReleaseRecord, Comparison]]:
    """*others* that do not contradict *anchor*, most agreeing fields first; ties keep
    their order. For the Discogs list beneath a chosen pressing (§13.6)."""
    kept = [(o, compare(anchor, o)) for o in others]
    kept = [(o, c) for o, c in kept if c.compatible]
    return sorted(kept, key=lambda oc: -len(oc[1].agreed))


def unanimous(
    values: Iterable[str | None],
    key: Callable[[str], object],
    *,
    require_all: bool = False,
) -> str | None:
    """The value every record that states one agrees on (compared by *key*), or
    ``None`` when none states one or two disagree.

    *require_all*: every record must state it. That is the rule for the
    pressing-level fields of a release-group-only record (§13.5): with no pressing
    matched, a value one pressing alone states would be adopted without the matched
    fields principle 3 asks for (Tracy Chapman: one of seven pressings is dated)."""
    found: str | None = None
    seen: object = None
    for v in values:
        text = _present(v)
        if text is None:
            if require_all:
                return None
            continue
        k = key(text)
        if found is None:
            found, seen = text, k
        elif k != seen:
            return None
    return found


# ---------------------------------------------------------------------------
# Records from what the lookups return (duck-typed: no imports, no I/O)
# ---------------------------------------------------------------------------


def record_from_meta(meta) -> ReleaseRecord:
    """A MusicBrainz pressing (``DiscMeta`` from the disc-ID response). Never the
    on-disc MCN: kgr holds a disc whose MCN differs from its barcode, and the MCN
    is not a disambiguation key (identifier_trust_model.md §1a)."""
    return ReleaseRecord(
        "mb",
        meta.mb_release_id or "",
        barcodes=tuple(b for b in (meta.barcode,) if b),
        catnos=tuple(c for c in (meta.catalog_number,) if c),
        labels=tuple(lab for lab in (meta.label,) if lab),
        country=meta.country,
        year=meta.release_date,
    )


def record_from_version(v) -> ReleaseRecord:
    """A row of a Discogs master's versions list: no barcode there."""
    return ReleaseRecord(
        "discogs",
        str(v.release_id),
        catnos=tuple(c for c in (v.catno,) if c),
        labels=tuple(lab for lab in (v.label,) if lab),
        country=v.country or None,
        year=v.year,
    )


def record_from_details(d) -> ReleaseRecord:
    """A fetched Discogs release: barcodes and every label's catalogue number."""
    return ReleaseRecord(
        "discogs",
        str(d.release_id),
        barcodes=tuple(d.barcodes),
        catnos=tuple(c for _n, c in d.labels if c),
        labels=tuple(n for n, _c in d.labels if n),
        country=d.country or None,
        year=d.released or None,
    )
