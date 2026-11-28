"""Matching a folder of files against MusicBrainz releases (trust_model_design.md §14).

``create`` identifies an *album* from loose files, never a disc: no TOC or disc ID is
rebuilt from them (§14.1, kgr 2026-11-27). Each candidate release (one medium of it) is
checked against the folder:

- **Every track the release lists must be in the folder**, or the release is rejected:
  the archive cannot be built with missing audio. A release track is found by its
  recording (a file's fingerprint, or its ``musicbrainz_trackid`` tag), else by title.
- **Files the release does not list** are left out of the archive, and the user is
  told (tier B).
- **Absence is silent; a contradiction falsifies** (§13.1), but a contradicted release
  is still shown, as tier C, never taken automatically.

Tiers (§14.4): **A** exactly the folder's files, in their order, nothing contradicting;
**B** files left out, or the release's order taken; **C** something contradicts. A
release in another order than the files' is a candidate only when no release holds the
tracks in the files' order.

Pure: no network, no I/O. The caller gathers candidates and fingerprints.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from cdda2img import release_match as rm
from cdda2img.lookup_result import DiscMeta

#: A file title matching a release track's title, when no recording links them.
TITLE_MATCH = 88  # rapidfuzz token_set_ratio, as the original-release search
#: The titles of the matched tracks, averaged, below which they contradict (R3's).
TITLES_AGREE = 80
#: Per-track runtimes all within this agree (supporting only).
RUNTIME_AGREE_MS = 3_000
#: The matched tracks' total runtime off by more than this contradicts. A source
#: trimmed before it reached us shortens tracks, so this is loose; to be tuned in
#: kgr's tests (§14.8).
RUNTIME_CONTRADICT_MS = 15_000

TIERS = ("A", "B", "C")

#: Formats with "CD" in the name that hold no CD audio.
_NOT_CD_AUDIO = frozenset({"VCD", "SVCD", "CDV", "CD-ROM", "CD-i", "CDi", "Mini-CD"})


def is_cd_or_digital(fmt: str) -> bool:
    """kgr, 2026-11-28: *"CD and digital media releases only. Never include vinyl
    or cassette."* A medium MusicBrainz records no format for is absent data, not a
    contradiction, so it is kept. A hybrid SACD has a CD layer; an SACD alone has
    none."""
    if not fmt or fmt == "Digital Media":
        return True
    if fmt in _NOT_CD_AUDIO or fmt.startswith("SACD"):
        return False
    return "CD" in fmt


@dataclass(frozen=True)
class FileTrack:
    """One file, in the files' own order (``position`` is 0-based in that order)."""

    position: int
    name: str
    title: str | None = None  # the title TAG; never the file name
    duration_ms: int | None = None
    tag_recording: str | None = None
    #: Recordings the fingerprint matched, and their titles (AcoustID's).
    fp_recordings: frozenset[str] = frozenset()
    fp_titles: tuple[str, ...] = ()
    #: AcoustID's best recording, the one candidate releases are gathered from.
    fp_best: str | None = None
    #: The file name's number and text: corroboration only, checked last, never
    #: required and never a contradiction (kgr, 2026-11-28).
    name_number: int | None = None
    name_title: str | None = None


@dataclass(frozen=True)
class FolderEvidence:
    """The folder: its files in order, and what its album-level tags say."""

    files: tuple[FileTrack, ...]
    tags: rm.ReleaseRecord | None = None  # barcode, catno, label, country
    tag_year: str | None = None
    tagged_release: str | None = None
    album: str | None = None  # the tags' album and artist, for a text search
    artist: str | None = None
    #: Whether ``files`` is in an order the files themselves state (track-number
    #: tags). When not, order is no evidence and the release's order is taken.
    order_known: bool = True


@dataclass
class Classified:
    """One candidate release (one medium) against the folder."""

    meta: DiscMeta
    tier: str
    #: For each release track in release order, the position of its file.
    mapping: tuple[int, ...]
    excluded: tuple[str, ...]
    order: str  # "files" | "release"
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def contradicted(self) -> tuple[str, ...]:
        return tuple(k for k, v in self.evidence.items() if v == "contradict")

    @property
    def score(self) -> int:
        return sum(1 for v in self.evidence.values() if v == "match")

    def describe(self) -> str:
        """``tracks:recording,fingerprint:match,…`` for PROV ``file_match``."""
        return ",".join(f"{k}:{v}" for k, v in self.evidence.items())


def _ratio(a: str | None, b: str | None) -> int | None:
    if not a or not b:
        return None
    from rapidfuzz import fuzz

    return int(fuzz.token_set_ratio(a.casefold(), b.casefold()))


def _strong(rec: str | None, f: FileTrack) -> bool:
    return bool(rec) and (rec in f.fp_recordings or rec == f.tag_recording)


def _weak(title: str | None, f: FileTrack) -> bool:
    r = _ratio(title, f.title)
    return r is not None and r >= TITLE_MATCH


def _assign(meta: DiscMeta, files: Sequence[FileTrack]) -> list[int] | None:
    """Each release track to a distinct file: recordings first, then titles, the
    same position preferred. ``None`` when some track has no file."""
    n = len(meta.tracks)
    owner: dict[int, int] = {}  # file position -> release track index

    def edges(i: int, strong: bool) -> list[int]:
        t = meta.tracks[i]
        hits = [
            f.position
            for f in files
            if (_strong(t.recording_id, f) if strong else _weak(t.title, f))
        ]
        return sorted(hits, key=lambda p: (p != i, p))

    def augment(i: int, strong: bool, seen: set[int]) -> bool:
        for p in edges(i, strong):
            if p in seen:
                continue
            seen.add(p)
            if p not in owner or augment(owner[p], strong, seen):
                owner[p] = i
                return True
        return False

    for strong in (True, False):
        for i in range(n):
            if i not in owner.values():
                augment(i, strong, set())
    by_track = {i: p for p, i in owner.items()}
    if len(by_track) != n:
        return None
    return [by_track[i] for i in range(n)]


def _fingerprints(meta: DiscMeta, mapping: Sequence[int], files) -> str:
    seen = "absent"
    for t, p in zip(meta.tracks, mapping):
        f = files[p]
        if not f.fp_recordings or not t.recording_id:
            continue
        if t.recording_id in f.fp_recordings:
            seen = "match" if seen == "absent" else seen
            continue
        # MusicBrainz holds duplicate recordings of one performance; a fingerprint
        # naming another recording of the same title is an unlinked id, not a
        # different song (absent, not a contradiction).
        if any((_ratio(t.title, x) or 0) >= TITLE_MATCH for x in f.fp_titles):
            continue
        return "contradict"
    return seen


def _titles(meta: DiscMeta, mapping: Sequence[int], files) -> str:
    """The title TAGS against the release's titles."""
    scores = [
        r
        for t, p in zip(meta.tracks, mapping)
        if (r := _ratio(t.title, files[p].title)) is not None
    ]
    if len(scores) < 2:
        return "absent"
    return "match" if sum(scores) / len(scores) >= TITLES_AGREE else "contradict"


def _names(meta: DiscMeta, mapping: Sequence[int], files) -> str:
    """The file names, checked last (kgr, 2026-11-28): corroboration only. Their
    numbers all equal the release's positions, or their text matches the titles:
    ``match``; anything else is ``absent``, never a contradiction, since a name
    can mean nothing ("01.flac" against *Smells Like Teen Spirit*, live)."""
    numbered = [
        (k, files[p].name_number)
        for k, p in enumerate(mapping, start=1)
        if files[p].name_number is not None
    ]
    if len(numbered) >= 2 and all(k == n for k, n in numbered):
        return "match"
    scores = [
        r
        for t, p in zip(meta.tracks, mapping)
        if (r := _ratio(t.title, files[p].name_title)) is not None
    ]
    if len(scores) >= 2 and sum(scores) / len(scores) >= TITLES_AGREE:
        return "match"
    return "absent"


def _runtime(meta: DiscMeta, mapping: Sequence[int], files) -> str:
    pairs = [
        (t.duration_ms, files[p].duration_ms)
        for t, p in zip(meta.tracks, mapping)
        if t.duration_ms and files[p].duration_ms
    ]
    if not pairs:
        return "absent"
    total = abs(sum(a for a, _ in pairs) - sum(b for _, b in pairs))
    if total > RUNTIME_CONTRADICT_MS:
        return "contradict"
    if all(abs(a - b) <= RUNTIME_AGREE_MS for a, b in pairs):
        return "match"
    return "absent"


def _year(meta: DiscMeta, tag_year: str | None) -> str:
    """The tag year against the release's date AND its group's first release: a
    tagger may write either (beets puts the release date in ``date``; many put the
    original year there), so only a mismatch with both contradicts."""
    ty = rm.norm_year(tag_year)
    years = {rm.norm_year(meta.release_date), rm.norm_year(meta.original_release_date)}
    years.discard(None)
    if ty is None or not years:
        return "absent"
    return "match" if ty in years else "contradict"


_FIELD_RESULT = {"agree": "match", "contradict": "contradict", "absent": "absent"}


def classify(folder: FolderEvidence, meta: DiscMeta) -> Classified | None:
    """*meta* (one medium) against the folder; ``None`` when the release lists a
    track the folder lacks (§14.4: rejected, never shown)."""
    files = folder.files
    if not meta.tracks:
        return None
    mapping = _assign(meta, files)
    if mapping is None:
        return None
    by_pos = {f.position: f for f in files}
    used = set(mapping)
    excluded = tuple(f.name for f in files if f.position not in used)
    in_order = folder.order_known and list(mapping) == sorted(mapping)
    order = "files" if in_order else "release"
    strong = [_strong(t.recording_id, by_pos[p]) for t, p in zip(meta.tracks, mapping)]
    evidence = {
        "tracks": "recording"
        if all(strong)
        else ("title" if not any(strong) else "mixed"),
        "fingerprint": _fingerprints(meta, mapping, by_pos),
        "titles": _titles(meta, mapping, by_pos),
        "runtime": _runtime(meta, mapping, by_pos),
        "year": _year(meta, folder.tag_year),
    }
    if folder.tags is not None:
        release = rm.record_from_meta(meta)
        for name, cmp, _strong_field in rm.FIELDS:
            if name == "year":
                continue  # the tag year is judged above, against both dates
            evidence[name] = _FIELD_RESULT[cmp(folder.tags, release)]
    evidence["tagged"] = (
        "match"
        if folder.tagged_release and meta.mb_release_id == folder.tagged_release
        else "absent"
    )
    evidence["names"] = _names(meta, mapping, by_pos)  # last: corroboration only
    if any(v == "contradict" for v in evidence.values()):
        tier = "C"
    elif excluded or (order == "release" and folder.order_known):
        tier = "B"
    else:
        tier = "A"
    return Classified(meta, tier, tuple(mapping), excluded, order, evidence)


@dataclass
class Ranked:
    """The candidates to offer, best first, and the analysis cases (§14.4)."""

    candidates: list[Classified]
    #: Releases in the files' order a contradiction held back.
    contradicted: list[Classified]
    rejected: int  # candidates listing a track the folder lacks


def _country_rank(meta: DiscMeta, preferred: Sequence[str]) -> int:
    c = (meta.country or "").upper()
    return preferred.index(c) if c in preferred else len(preferred)


def _whole_release_present(
    meta: DiscMeta, metas: Sequence[DiscMeta], whole: FolderEvidence | None
) -> bool:
    """kgr, 2026-11-28: a multi-disc release of which the input holds one disc's
    tracks is not a match, as with missing tracks: the archive cannot be built
    with missing files. Every OTHER medium of the release must be found, whole,
    among all the input's files (*whole*; the batch matched *meta* itself)."""
    others = [
        m
        for m in metas
        if m.mb_release_id == meta.mb_release_id and m.disc_number != meta.disc_number
    ]
    if not others:
        return True
    if whole is None:
        return False
    return all(_assign(m, whole.files) is not None for m in others)


def rank(
    folder: FolderEvidence,
    metas: Sequence[DiscMeta],
    preferred_country: Sequence[str] = (),
    whole: FolderEvidence | None = None,
) -> Ranked:
    """Classify every candidate and order them: tier, fewest files left out (so a
    single holding two of the album's songs never outranks the album), most
    matching classes, then §10.3's ladder (preferred country, date, MBID). A
    release in another order is dropped while any release holds the tracks in the
    files' order (§14.4). One row per release: its best medium. A medium of a
    multi-disc release counts only when *whole* (every file of the input) holds
    the release's other discs too."""
    classified: list[Classified] = []
    rejected = 0
    for meta in metas:
        c = classify(folder, meta)
        if c is None or not _whole_release_present(meta, metas, whole):
            rejected += 1
        else:
            classified.append(c)
    if folder.order_known and any(c.order == "files" for c in classified):
        classified = [c for c in classified if c.order == "files"]
    preferred = [p.upper() for p in preferred_country]

    def key(c: Classified) -> tuple:
        return (
            TIERS.index(c.tier),
            len(c.excluded),
            -c.score,
            _country_rank(c.meta, preferred),
            c.meta.release_date or "9999",
            c.meta.mb_release_id or "~",
        )

    best: dict[str, Classified] = {}
    for c in sorted(classified, key=key):
        rid = c.meta.mb_release_id or f"#{id(c)}"
        best.setdefault(rid, c)
    ordered = sorted(best.values(), key=key)
    contradicted = [c for c in ordered if c.tier == "C" and c.order == "files"]
    return Ranked(ordered, contradicted, rejected)


@dataclass(frozen=True)
class Decision:
    """What ``--auto`` (or a run without a terminal) takes (§14.4)."""

    outcome: str  # "unique" | "release_group_only" | "rejected"
    chosen: Classified | None = None
    group: tuple[Classified, ...] = ()
    reason: str | None = None  # file_match_rejected


def decide_unattended(ranked: Ranked, tagged_release: str | None) -> Decision:
    """Tier A only. The release the tags name, in tier A, is the pressing; one
    tier-A release is kept; several record the album only (the files cannot choose
    between pressings), the album being the release group most of them belong to;
    none, or a tie between albums, apply nothing."""
    tier_a = [c for c in ranked.candidates if c.tier == "A"]
    if not ranked.candidates:
        reason = "missing_tracks" if ranked.rejected else "no_candidates"
        return Decision("rejected", reason=reason)
    if not tier_a:
        return Decision("rejected", reason="no_tier_a")
    for c in tier_a:
        if tagged_release and c.meta.mb_release_id == tagged_release:
            return Decision("unique", chosen=c)
    if len(tier_a) == 1:
        return Decision("unique", chosen=tier_a[0])
    # The album is the release group most tier-A releases belong to, when one
    # strictly outnumbers the rest (the rip's plurality rule): a medium of a
    # "2 for 1" set holding the same nine tracks must not make the album
    # undecidable (live, Night and Day, 2026-11-27). A tie applies nothing.
    counts: dict[str | None, int] = {}
    for c in tier_a:
        counts[c.meta.mb_release_group_id] = (
            counts.get(c.meta.mb_release_group_id, 0) + 1
        )
    top = sorted(counts.items(), key=lambda kv: -kv[1])
    if top[0][0] is None or (len(top) > 1 and top[1][1] == top[0][1]):
        return Decision("rejected", reason="ambiguous_album")
    album = [c for c in tier_a if c.meta.mb_release_group_id == top[0][0]]
    if len(album) == 1:
        return Decision("unique", chosen=album[0])
    return Decision("release_group_only", chosen=album[0], group=tuple(album))


def note(c: Classified) -> str:
    """The pressing screen's line for a candidate: its tier, and what it costs."""
    parts = {"A": "match", "B": "match", "C": "contradicted"}[c.tier]
    bits = [parts]
    if c.excluded:
        bits.append(f"leaves out {len(c.excluded)} file(s)")
    if c.order == "release":
        bits.append("release's track order")
    if c.contradicted:
        bits.append("by " + "+".join(c.contradicted))
    return f"{c.tier}: " + ", ".join(bits)
