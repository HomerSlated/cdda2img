"""Compare catalogue metadata for the same release on MusicBrainz vs Discogs.

Diagnostic for the disambiguation design debate (docs/reference/DISAMBIGUATION.md §4,
decision (a)): how often do MB and Discogs *disagree* on the catalogue fields we want to
use for release-selection — country, label, catalogue number, barcode, year?

The two services are joined by MusicBrainz's own ``url-relation`` to a specific Discogs
release (the bridge found in the interrogation), so this compares *the release MB itself
points at* — not a fuzzy re-search.

Usage:
    uv run python tools/compare_mb_discogs.py <mb_release_id> [<mb_release_id> ...]
    uv run python tools/compare_mb_discogs.py            # runs the built-in sample

Requires DISCOGS_TOKEN in the environment (same as the live pipeline).

Output: per-release side-by-side of raw values + a normalised match/mismatch verdict per
field, then an aggregate mismatch rate per field across all releases compared.
"""

from __future__ import annotations

import re
import sys

import musicbrainzngs as mb

from cdda2img import discogs_lookup
from cdda2img.mb_lookup import _setup_useragent

# Built-in sample: the 5 finalist 1987 Joshua Tree releases from the interrogation.
_SAMPLE = [
    "19fb4543-45ee-4ded-a07b-32568f6214b0",  # #1 US 075679058126
    "e08f21bf-e63e-31f7-9cc3-6aac550a382a",  # #2 AU 9399084229829
    "9d990576-a20a-3faf-88db-73d6b6c9364e",  # #3 GB 042284229821
    "fb8f25c1-b149-383e-a206-ad9d24a32487",  # #4 US 042284229821
    "aba9be96-5800-436c-a617-4899b3648159",  # #10 XE 042284229821
]

_FIELDS = ("country", "label", "catalog_number", "barcode", "year")

# Diverse seed albums (era/genre/label/country spread) for `--sample`. Resolved to one MB
# release ID each at run time, so the sample is reproducible without hard-coding MBIDs.
_SEED_ALBUMS = [
    ("Pink Floyd", "The Dark Side of the Moon"),
    ("The Beatles", "Abbey Road"),
    ("Led Zeppelin", "Led Zeppelin IV"),
    ("Fleetwood Mac", "Rumours"),
    ("Nirvana", "Nevermind"),
    ("Radiohead", "OK Computer"),
    ("The Clash", "London Calling"),
    ("David Bowie", "The Rise and Fall of Ziggy Stardust and the Spiders From Mars"),
    ("Queen", "A Night at the Opera"),
    ("Bruce Springsteen", "Born to Run"),
    ("Michael Jackson", "Thriller"),
    ("Madonna", "Like a Prayer"),
    ("Prince", "Purple Rain"),
    ("Taylor Swift", "1989"),
    ("Adele", "21"),
    ("Daft Punk", "Discovery"),
    ("The Prodigy", "The Fat of the Land"),
    ("Massive Attack", "Mezzanine"),
    ("Kraftwerk", "Trans-Europe Express"),
    ("Aphex Twin", "Selected Ambient Works 85-92"),
    ("Dr. Dre", "The Chronic"),
    ("Nas", "Illmatic"),
    ("Kanye West", "The College Dropout"),
    ("Wu-Tang Clan", "Enter the Wu-Tang (36 Chambers)"),
    ("Metallica", "Master of Puppets"),
    ("Iron Maiden", "The Number of the Beast"),
    ("Black Sabbath", "Paranoid"),
    ("Miles Davis", "Kind of Blue"),
    ("John Coltrane", "A Love Supreme"),
    ("The Dave Brubeck Quartet", "Time Out"),
    ("Marvin Gaye", "What's Going On"),
    ("Stevie Wonder", "Songs in the Key of Life"),
    ("Amy Winehouse", "Back to Black"),
    ("Bob Marley & The Wailers", "Exodus"),
    ("Bob Dylan", "Highway 61 Revisited"),
    ("Joni Mitchell", "Blue"),
    ("Simon & Garfunkel", "Bridge Over Troubled Water"),
    ("Arcade Fire", "Funeral"),
    ("The Smiths", "The Queen Is Dead"),
    ("Joy Division", "Unknown Pleasures"),
    ("Pixies", "Doolittle"),
    ("Portishead", "Dummy"),
    ("R.E.M.", "Automatic for the People"),
    ("Beastie Boys", "Paul's Boutique"),
    ("Talking Heads", "Remain in Light"),
    # --- second batch (2026-06-19): widen genre / era / region coverage ---
    ("Aretha Franklin", "I Never Loved a Man the Way I Love You"),
    ("Otis Redding", "Otis Blue"),
    ("Sly and the Family Stone", "There's a Riot Goin' On"),
    ("Curtis Mayfield", "Super Fly"),
    ("Al Green", "Let's Stay Together"),
    ("Earth, Wind & Fire", "That's the Way of the World"),
    ("Johnny Cash", "At Folsom Prison"),
    ("Willie Nelson", "Red Headed Stranger"),
    ("Dolly Parton", "Jolene"),
    ("Burning Spear", "Marcus Garvey"),
    ("Ramones", "Ramones"),
    ("Sex Pistols", "Never Mind the Bollocks, Here's the Sex Pistols"),
    ("Television", "Marquee Moon"),
    ("Patti Smith", "Horses"),
    ("The Velvet Underground", "The Velvet Underground & Nico"),
    ("Slayer", "Reign in Blood"),
    ("Megadeth", "Rust in Peace"),
    ("Pantera", "Vulgar Display of Power"),
    ("Judas Priest", "British Steel"),
    ("Motörhead", "Ace of Spades"),
    ("The Chemical Brothers", "Dig Your Own Hole"),
    ("Underworld", "Dubnobasswithmyheadman"),
    ("Orbital", "Orbital 2"),
    ("Boards of Canada", "Music Has the Right to Children"),
    ("Burial", "Untrue"),
    ("A Tribe Called Quest", "The Low End Theory"),
    ("Public Enemy", "It Takes a Nation of Millions to Hold Us Back"),
    ("OutKast", "Stankonia"),
    ("Eminem", "The Marshall Mathers LP"),
    ("The Notorious B.I.G.", "Ready to Die"),
    ("Lauryn Hill", "The Miseducation of Lauryn Hill"),
    ("Sonic Youth", "Daydream Nation"),
    ("My Bloody Valentine", "Loveless"),
    ("Neutral Milk Hotel", "In the Aeroplane Over the Sea"),
    ("The Strokes", "Is This It"),
    ("The White Stripes", "Elephant"),
    ("LCD Soundsystem", "Sound of Silver"),
    ("Sufjan Stevens", "Illinois"),
    ("Bon Iver", "For Emma, Forever Ago"),
    ("The Police", "Synchronicity"),
    ("Dire Straits", "Brothers in Arms"),
    ("Peter Gabriel", "So"),
    ("Kate Bush", "Hounds of Love"),
    ("Tom Waits", "Rain Dogs"),
    ("Leonard Cohen", "Songs of Leonard Cohen"),
    ("Carole King", "Tapestry"),
    ("Steely Dan", "Aja"),
    ("Fela Kuti", "Zombie"),
    ("Charles Mingus", "Mingus Ah Um"),
    ("Herbie Hancock", "Head Hunters"),
]


def _resolve_sample() -> list[str]:
    """Resolve seed albums to one MB CD-release ID each (skips unresolved)."""
    ids: list[str] = []
    total = len(_SEED_ALBUMS)
    for i, (artist, album) in enumerate(_SEED_ALBUMS, 1):
        try:
            res = mb.search_releases(artist=artist, release=album, format="CD", limit=3)
        except Exception as exc:
            print(f"  [{i}/{total}] search failed for {artist} — {album}: {exc}")
            continue
        rl = res.get("release-list") or []
        if rl:
            ids.append(rl[0]["id"])
            print(f"  [{i}/{total}] {artist} — {album} -> {rl[0]['id'][:8]}")
        else:
            print(f"  [{i}/{total}] {artist} — {album} -> (no CD release found)")
    return ids


# Discogs uses display country names; MB uses ISO-3166 (+ XE Europe, XW Worldwide).
_COUNTRY_MAP = {
    "UK": "GB",
    "USA": "US",
    "US": "US",
    "EUROPE": "XE",
    "WORLDWIDE": "XW",
    "AUSTRALIA": "AU",
    "GERMANY": "DE",
    "SOUTH AFRICA": "ZA",
    "UK & EUROPE": "XE",
    "UK, EUROPE & US": "XE",
}


def _norm_country(v: str | None) -> str | None:
    if not v:
        return None
    return _COUNTRY_MAP.get(v.strip().upper(), v.strip().upper())


def _norm_label(v: str | None) -> str | None:
    if not v:
        return None
    v = v.lower()
    v = re.sub(r"\b(records|recordings|ltd\.?|inc\.?|gmbh)\b", "", v)
    return re.sub(r"[^a-z0-9]", "", v) or None


def _norm_catno(v: str | None) -> str | None:
    if not v:
        return None
    return re.sub(r"[^a-z0-9]", "", v.lower()) or None


def _norm_barcode(v: str | None) -> str | None:
    if not v:
        return None
    digits = re.sub(r"\D", "", v)
    return digits.lstrip("0") or None  # ignore leading-zero / EAN-13 vs UPC-12 padding


def _year(date: str | None) -> str | None:
    if not date:
        return None
    m = re.match(r"(\d{4})", str(date))
    return m.group(1) if m else None


_NORM = {
    "country": _norm_country,
    "label": _norm_label,
    "catalog_number": _norm_catno,
    "barcode": _norm_barcode,
    "year": lambda v: v,
}


def _mb_fields(rid: str) -> tuple[dict, int | None]:
    r = mb.get_release_by_id(rid, includes=["labels", "url-rels"])["release"]
    li = (r.get("label-info-list") or [{}])[0]
    out = {
        "country": r.get("country"),
        "label": (li.get("label") or {}).get("name"),
        "catalog_number": li.get("catalog-number"),
        "barcode": r.get("barcode"),
        "year": _year(r.get("date")),
    }
    discogs_id = None
    for u in r.get("url-relation-list") or []:
        if u.get("type") == "discogs":
            m = re.search(r"/release/(\d+)", u.get("target", ""))
            if m:
                discogs_id = int(m.group(1))
    return out, discogs_id


def _discogs_fields(discogs_id: int) -> dict:
    dm = discogs_lookup.fetch_release(discogs_id)
    if dm is None:
        return dict.fromkeys(_FIELDS)
    return {
        "country": dm.country,
        "label": dm.label,
        "catalog_number": dm.catalog_number,
        "barcode": dm.barcode,
        "year": _year(dm.release_date),
    }


def main() -> None:
    if not discogs_lookup.is_available():
        msg = "DISCOGS_TOKEN not set"
        raise SystemExit(msg)
    _setup_useragent()
    args = sys.argv[1:]
    if args == ["--sample"]:
        print("resolving diverse seed albums to MB release IDs…")
        ids = _resolve_sample()
        print(f"resolved {len(ids)} releases")
    else:
        ids = args or _SAMPLE

    tally = {f: [0, 0] for f in _FIELDS}  # field -> [compared, mismatched]
    for rid in ids:
        mbf, dgid = _mb_fields(rid)
        print(f"\n=== MB {rid[:8]}  ->  Discogs {dgid} ===")
        if dgid is None:
            print("  no Discogs url-relation on this MB release; skipped")
            continue
        dgf = _discogs_fields(dgid)
        for f in _FIELDS:
            mv, dv = mbf.get(f), dgf.get(f)
            nm, nd = _NORM[f](mv), _NORM[f](dv)
            if mv is None or dv is None:
                verdict = "—(missing)"
            else:
                tally[f][0] += 1
                if nm == nd:
                    verdict = "match"
                else:
                    verdict = "MISMATCH"
                    tally[f][1] += 1
            print(f"  {f:15s} MB={mv!s:28.28} Discogs={dv!s:24.24} {verdict}")

    print("\n=== aggregate mismatch rate (normalised) ===")
    for f in _FIELDS:
        comp, mis = tally[f]
        rate = f"{mis}/{comp}" if comp else "0/0"
        print(f"  {f:15s} {rate} mismatched")


if __name__ == "__main__":
    main()
