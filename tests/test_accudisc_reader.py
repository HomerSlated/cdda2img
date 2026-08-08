"""accudisc_reader: the AccuDisc API seam.

Every disc operation in this project goes through `accudisc_reader`, and since
the CLI was retired on 2026-08-01 every one of those goes through AccuDisc's
Python binding. These tests therefore assert against a **fake binding module**
installed via `_install`, never against a subprocess.

The whole first half of this file used to assert argv construction (`--c2f`, no
`--count` for a whole disc, `--progress-fd 1`) and stdout parsing (`track … lba`,
`page2A max …`, `speed req=… page2a=…`). Those tests were not ported when the CLI
went — they were deleted, because what they pinned no longer exists. Losing them
is not a loss of coverage: a test for a text format nobody emits is a test that
can only ever pass.

What survived the cut, and why:

* `TocGeometry.session_safe` — a policy this project owns, not AccuDisc. Only the
  way the fixtures are *built* changed (structs now, not parsed text).
* The seam invariant — now "no module outside this one imports `accudisc`".
* Everything below the binding heading, which was already carrier-correct.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import cdda2img.accudisc_reader as ar

# ---------------------------------------------------------------------------
# TOC geometry — the 0x02 -> 0x00 degrade and the session-safety rule
# ---------------------------------------------------------------------------


def _geom(**kw: Any) -> ar.TocGeometry:
    """A `TocGeometry` with healthy defaults, overridden per case.

    Built directly rather than through a parser. The values are the same ones the
    old text fixtures encoded — `source=fulltoc degrade=none sessions=1..1` and a
    degraded `source=toc degrade=leadin_unreadable` — so each case still says what
    it is testing rather than carrying an inert wall of track lines.
    """
    base: dict[str, Any] = {
        "track_lsns": [0, 25705],
        "disc_last_lsn": 253936,
        "source": "fulltoc",
        "degrade": "none",
        "sessions": "1..1",
    }
    base.update(kw)
    return ar.TocGeometry(**base)


_DEGRADED_KW: dict[str, Any] = {
    "source": "toc",
    "degrade": "leadin_unreadable",
    "sessions": None,
}


def test_session_safe_full_toc_is_always_safe() -> None:
    safe, _why = _geom().session_safe
    assert safe


def test_session_safe_degraded_all_audio_is_inferred_not_measured() -> None:
    """Accepted, but the reason must say it is an inference — a multi-session
    all-audio disc would pass this test and still be unsafe."""
    safe, why = _geom(**_DEGRADED_KW).session_safe
    assert safe
    assert "NOT measured" in why


def test_session_safe_degraded_with_a_data_track_refuses() -> None:
    safe, why = _geom(**_DEGRADED_KW, data_tracks=[2]).session_safe
    assert not safe
    assert "data track" in why


def test_session_safe_measured_single_session_beats_the_inference() -> None:
    """A measured session count outranks the all-audio guess in both directions."""
    safe, why = _geom(**_DEGRADED_KW, session_count=1).session_safe
    assert safe
    assert "NOT measured" not in why


def test_session_safe_measured_multisession_refuses_even_if_all_audio() -> None:
    """The case the inference cannot see: every track is audio and the disc is
    still multi-session, so session 2 would be read as if it were session 1."""
    safe, why = _geom(**_DEGRADED_KW, session_count=2).session_safe
    assert not safe
    assert "2" in why


def test_session_count_is_ignored_when_the_toc_is_healthy() -> None:
    """A healthy full TOC already carries the session range; the fallback count
    must not be able to override it."""
    safe, _why = _geom(session_count=2).session_safe
    assert safe


def test_session_count_zero_on_degrade_falls_back_to_the_inference() -> None:
    """0 is "not measured", not "zero sessions"."""
    safe, why = _geom(**_DEGRADED_KW, session_count=0).session_safe
    assert safe
    assert "NOT measured" in why


def test_untrusted_toc_geometry_refuses_regardless_of_sessions() -> None:
    """`toc_trusted=0` outranks everything: the track map contradicts itself, so
    a session count derived from it is not evidence of anything."""
    safe, why = _geom(
        session_count=1, anomalies=["lba_order"], toc_trusted=False
    ).session_safe
    assert not safe
    assert "untrusted" in why
    assert "lba_order" in why


def test_clean_disc_reports_trusted_geometry_and_no_anomalies() -> None:
    geom = _geom()
    assert geom.toc_trusted
    assert geom.anomalies == []


def test_report_only_anomalies_do_not_make_the_toc_untrusted() -> None:
    """The six report-only slugs (e.g. empty_track) are recorded but the disc
    still rips — only lba_order/overlap/leadout_before clear `toc_trusted`, and
    AccuDisc signals that separately. We key on the flag, not the slugs."""
    geom = _geom(session_count=1, anomalies=["empty_track"])
    assert geom.anomalies == ["empty_track"]
    assert geom.toc_trusted
    safe, _why = geom.session_safe
    assert safe


# ── the seam invariant ───────────────────────────────────────────────────────


def test_no_module_outside_the_seam_imports_accudisc() -> None:
    """Every AccuDisc call in `src/` lives in `accudisc_reader`.

    This used to grep for `_ACCUDISC`, the resolved binary path, because building
    an argv was how a module talked to the engine. With the CLI gone the same
    invariant has a new spelling: `import accudisc`. Both the old and the new
    checks are one line, and the invariant is what made *this* change a
    one-module edit — it has now paid for itself twice, which is the argument for
    keeping a guard that has become trivially true rather than deleting it.
    """
    src = Path(__file__).resolve().parent.parent / "src" / "cdda2img"
    seam = src / "accudisc_reader.py"
    offenders: list[str] = []
    for path in sorted(src.glob("*.py")):
        if path == seam:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if "import accudisc" in code and "cdda2img" not in code:
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert offenders == [], "AccuDisc imported outside the seam:\n" + "\n".join(
        offenders
    )


# ── the binding: import identity and error translation ───────────────────────
#
# Every test here clears the _import_binding cache: it is a functools.cache on a
# module global, so one test's fake binding would otherwise be the next test's
# environment.


class _FakeBindingError(Exception):
    pass


class _FakeAbiMismatch(_FakeBindingError):
    pass


class _FakeAnomaly(enum.IntFlag):
    """A real IntFlag, because the REAL one is and that is where the bug lived.

    The previous fake handed `anomalies` a plain tuple. Tuples iterate on every
    Python; an ``IntFlag`` *instance* only became iterable in 3.11. So the
    production code iterated the value directly, 1423 tests passed, and the call
    raised "'Anomaly' object is not iterable" on the first real 3.10 read the
    moment the binding became importable. A fake that is easier to iterate than
    the type it stands in for is not a simplification, it is a hole.
    """

    A = 1
    B = 2


class _FakeC2Verdict(enum.IntEnum):
    """Mirrors the real `C2Verdict`. UNVERIFIED is present on purpose: it is the
    member most easily mistaken for a weaker yes, and it must map to False."""

    UNSUPPORTED = 0
    SUPPORTED = 1
    UNVERIFIED = 2


class _FakeBinding:
    """The minimum surface accudisc_reader calls, so _BINDING_SURFACE is satisfied."""

    AccuDiscError = _FakeBindingError
    AbiMismatch = _FakeAbiMismatch
    Anomaly = _FakeAnomaly
    C2Verdict = _FakeC2Verdict

    def __init__(self) -> None:
        self.opened: list[str] = []

    @staticmethod
    def anomaly_token(bit: object) -> str:
        return getattr(bit, "name", str(bit)).lower()

    @staticmethod
    def version_string() -> str:
        return "0.0.0-fake"

    def Device(self, path: str) -> object:
        raise NotImplementedError


def _install(
    monkeypatch: pytest.MonkeyPatch, module: object | None, why: str = "x"
) -> None:
    ar._import_binding.cache_clear()
    monkeypatch.setattr(ar, "_import_binding", lambda: (module, "" if module else why))


@pytest.fixture(autouse=True)
def _reset_binding_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_import_binding` is a `functools.cache` on a module global.

    Without this, one test's fake binding is the next test's environment — and
    the failure is order-dependent, so it shows up as a test that passes alone
    and fails in the suite. The warn-once flags this fixture also used to reset
    are gone: they belonged to the silent-fallback warning, which had nothing to
    be silent about once there was one carrier.
    """
    ar._import_binding.cache_clear()


def test_a_namespace_package_is_not_the_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The trap this project actually fell into, kept as a regression.

    With tools/ on sys.path, ``import accudisc`` SUCCEEDS and binds
    tools/accudisc/ — the git-ignored *binary snapshot* directory — as an empty
    PEP 420 namespace package. No ImportError is raised, because nothing failed:
    a module was found. It just has no ``Device``, and the failure surfaces far
    from the import.

    The condition is built here rather than relied on: whether the real tools/
    directory is on sys.path depends on which test files ran first, so a test
    that waited for it would pass vacuously in isolation — which is how it
    escaped notice in ``tools/binding_ab.py`` in the first place.
    """
    import importlib
    import sys

    (tmp_path / "accudisc").mkdir()  # a directory, no __init__.py — the snapshot shape
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "accudisc", raising=False)
    importlib.invalidate_caches()

    # The trap itself: the import succeeds and yields an attribute-less module.
    # Both accesses go through getattr, matching what _import_binding does — a
    # plain `phantom.__file__` makes ty resolve the name to tools/accudisc/ and
    # reject the attribute, which is ty being right about the thing under test.
    phantom = importlib.import_module("accudisc")

    assert getattr(phantom, "__file__", None) is None
    assert not hasattr(phantom, "Device")

    ar._import_binding.cache_clear()
    module, why = ar._import_binding()
    assert module is None, (
        "an attribute-less namespace package was accepted as the binding"
    )
    assert "namespace directory" in why

    # Evict the phantom. `monkeypatch` restores sys.path and undoes the delitem,
    # but the INSERTION `import_module` made into sys.modules is untracked, so
    # the phantom outlives this test and every later `import accudisc` gets it
    # from cache — no sys.path involved, nothing to restore. Measured: it made
    # the real-binding shape tests skip with "binding unavailable" on a machine
    # where it is installed and working, i.e. a guard silently not running.
    sys.modules.pop("accudisc", None)
    importlib.invalidate_caches()
    ar._import_binding.cache_clear()


def test_import_rejects_a_module_missing_part_of_the_surface() -> None:
    class Partial:
        Device = object  # has Device, lacks the error types

    ar._import_binding.cache_clear()
    assert [n for n in ar._BINDING_SURFACE if not hasattr(Partial, n)]


def test_a_missing_binding_is_fatal_and_names_the_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is no second carrier, so this can only raise.

    Four tests used to live here covering the `CDDA2IMG_ACCUDISC_TRANSPORT`
    policy — auto/binding/subprocess, the warn-exactly-once fallback, and an
    unknown value degrading to auto. They are deleted rather than ported: the
    policy they described was a choice between two carriers, and there is one.

    The operation name is asserted because the message arrives at the top of
    whatever needed the engine, and "the binding is missing" is far more useful
    attached to "disc read" than floating free.
    """
    _install(monkeypatch, None, why="No module named 'accudisc'")
    with pytest.raises(RuntimeError, match="required for disc read"):
        ar._binding("disc read")


def test_the_fatal_message_carries_the_import_reason_and_the_remedy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user hitting this has an unrunnable install and needs both halves: what
    failed to import, and the command that fixes it."""
    _install(monkeypatch, None, why="No module named 'accudisc'")
    with pytest.raises(RuntimeError) as exc:
        ar._binding("toc")
    assert "No module named 'accudisc'" in str(exc.value)
    assert "pipx inject" in str(exc.value)


def test_an_abi_mismatch_raises_and_says_to_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It used to degrade to the subprocess, and that reasoning was sound: a
    skewed extension is broken while the binary is fine. What changed is not the
    reasoning but the availability of the thing it fell back to.

    Kept as its own arm rather than folded into the generic error, because the
    remedy differs — a rebuild fixes this and would be wasted effort on a device
    failure.
    """
    fake = _FakeBinding()

    def _skew() -> None:
        msg = "compiled against 0.2 but loaded 0.3"
        raise _FakeAbiMismatch(msg)

    with pytest.raises(RuntimeError, match="Rebuild the binding"):
        ar._call(fake, "toc", _skew)


def test_a_device_error_says_what_failed_without_mentioning_a_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other arm. A rebuild will not fix unrecovered read errors, and
    suggesting one sends the user to recompile a library over a scratched disc."""
    fake = _FakeBinding()

    def _sense() -> None:
        msg = "sense 3/11/00 unrecovered read error"
        raise _FakeBindingError(msg)

    with pytest.raises(RuntimeError, match="span read failed") as exc:
        ar._call(fake, "span read", _sense)
    assert "Rebuild" not in str(exc.value)


# ── the flipped paths, exercised device-free ─────────────────────────────────
#
# tools/binding_ab.py is the acceptance test and needs a drive. These are the
# cheap half: that the struct→dataclass assembly and the sink reassembly are
# wired correctly at all, so a typo fails here rather than on the shelf.


class _FakeTrack:
    def __init__(self, number: int, lba: int, *, audio: bool = True) -> None:
        self.number = number
        self.lba = lba
        self.is_audio = audio
        self.is_data = not audio


class _FakeSession:
    def __init__(self, number: int) -> None:
        self.number = number


class _FakeToken:
    def __init__(self, token: str) -> None:
        self.token = token


class _FakeToc:
    def __init__(self, tracks, sessions, leadout, anomalies=None, trusted=True) -> None:
        self.tracks = tracks
        self.sessions = sessions
        self.leadout_lba = leadout
        # Default to an empty FLAG, not a tuple: the whole point of this fake
        # being an IntFlag is that it iterates the way the real type does.
        self.anomalies = _FakeAnomaly(0) if anomalies is None else anomalies
        self.trusted = trusted

    @property
    def audio_tracks(self):
        return tuple(t for t in self.tracks if t.is_audio)

    @property
    def data_tracks(self):
        return tuple(t for t in self.tracks if t.is_data)


class _FakeInfo:
    def __init__(self, source: str, degrade: str, session_count: int) -> None:
        self.source = _FakeToken(source)
        self.degrade = _FakeToken(degrade)
        self.session_count = session_count


class _FakeDevice:
    """Context-manager device returning canned structs; records the reads it served."""

    def __init__(self, toc_src=None, chunks=()) -> None:
        self._toc_src = toc_src
        self._chunks = chunks
        self.read_kwargs: dict[str, object] = {}

    def __enter__(self) -> _FakeDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read_toc_src(self):
        return self._toc_src

    def read(self, lba: int, count: int, **kwargs: Any):
        self.read_kwargs = {"lba": lba, "count": count, **kwargs}
        sink = kwargs["sink"]
        for chunk in self._chunks:
            sink(chunk)


class _FakeChunk:
    def __init__(self, nsec: int, data: bytes, sector_len: int = 2352) -> None:
        self.nsec = nsec
        self.data = data
        self.sector_len = sector_len


def _binding_with(device: _FakeDevice) -> _FakeBinding:
    module = _FakeBinding()
    module.Device = lambda _path: device  # type: ignore[assignment]
    return module


def test_toc_geometry_from_binding_maps_every_field() -> None:
    device = _FakeDevice(
        toc_src=(
            _FakeToc(
                tracks=[
                    _FakeTrack(1, 0),
                    _FakeTrack(2, 15000),
                    _FakeTrack(3, 30000, audio=False),
                ],
                sessions=[_FakeSession(1)],
                leadout=162892,
                anomalies=_FakeAnomaly.A | _FakeAnomaly.B,
            ),
            _FakeInfo("fulltoc", "none", 1),
        )
    )
    geom = ar._toc_geometry_from_binding(_binding_with(device), "/dev/sr0")

    assert geom.track_lsns == [0, 15000]  # audio only
    assert geom.disc_last_lsn == 162891  # leadout - 1
    assert geom.data_tracks == [3]
    assert geom.source == "fulltoc"
    assert geom.session_count == 1
    assert geom.sessions == "1..1"
    assert geom.anomalies == ["a", "b"]  # sorted: the shape the A/B found agreeing
    assert geom.toc_trusted is True


def test_toc_geometry_sessions_is_none_on_the_format_0_degrade() -> None:
    """No session structure means no range to report — not "1..1" invented from a
    count. The CLI omits the token here too."""
    device = _FakeDevice(
        toc_src=(
            _FakeToc(tracks=[_FakeTrack(1, 0)], sessions=[], leadout=1000),
            _FakeInfo("toc", "leadin_unreadable", 0),
        )
    )
    geom = ar._toc_geometry_from_binding(_binding_with(device), "/dev/sr0")
    assert geom.sessions is None
    assert geom.degraded is True


def test_read_span_binding_reassembles_chunks_in_order() -> None:
    chunks = [_FakeChunk(2, b"\xaa" * 4704), _FakeChunk(1, b"\xbb" * 2352)]
    device = _FakeDevice(chunks=chunks)
    seen: list[tuple[int, int]] = []

    data = ar._read_span_binding(
        _binding_with(device),
        "/dev/sr0",
        100,
        3,
        8,
        lambda done, total: seen.append((done, total)),
    )

    assert data == b"\xaa" * 4704 + b"\xbb" * 2352
    assert device.read_kwargs["lba"] == 100
    assert device.read_kwargs["count"] == 3
    assert device.read_kwargs["speed_x"] == 8
    assert seen == [(2, 3), (3, 3)]  # progress is cumulative sectors, not per chunk


def test_read_span_binding_leaves_speed_unset_when_not_asked() -> None:
    device = _FakeDevice(chunks=[_FakeChunk(1, b"\x00" * 2352)])
    ar._read_span_binding(_binding_with(device), "/dev/sr0", 0, 1, None, None)
    assert device.read_kwargs["speed_x"] == 0  # 0 = leave the drive alone


def test_read_span_binding_refuses_an_unexpected_sector_length() -> None:
    """2352 is our PREDICTION of a number the library REPORTS. Slice assignment
    into a bytearray silently resizes it, so an unchecked wrong prediction yields
    a plausible buffer of the wrong length instead of an error."""
    device = _FakeDevice(chunks=[_FakeChunk(1, b"\x00" * 2646, sector_len=2646)])
    with pytest.raises(RuntimeError, match="2646-byte sectors"):
        ar._read_span_binding(_binding_with(device), "/dev/sr0", 0, 1, None, None)


def test_read_toc_builds_geometry_from_the_structs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _FakeDevice(
        toc_src=(
            _FakeToc(
                tracks=[_FakeTrack(1, 0)], sessions=[_FakeSession(1)], leadout=500
            ),
            _FakeInfo("fulltoc", "none", 1),
        )
    )
    _install(monkeypatch, _binding_with(device))
    assert ar.read_toc("/dev/sr0").track_lsns == [0]


def test_read_span_bytes_raises_on_abi_skew_rather_than_degrading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end, at a public entry point rather than at `_call`.

    This test asserted the opposite until 2026-08-01: a skewed binding used to
    fall through to a perfectly good CLI binary sitting right there. The binary
    is still there and is still good; it is simply not something this project
    executes any more, so the honest outcome is a raise that names the rebuild.
    """
    module = _FakeBinding()

    def _skewed(_path: str) -> None:
        msg = "compiled against 0.2 but loaded 0.3"
        raise _FakeAbiMismatch(msg)

    module.Device = _skewed  # type: ignore[assignment]
    _install(monkeypatch, module)

    with pytest.raises(RuntimeError, match="Rebuild the binding"):
        ar.read_span_bytes("/dev/sr0", 0, 1)


def test_read_span_binding_refuses_a_short_span() -> None:
    """The length guarantee has to be re-established, not inherited.

    On the subprocess path AccuDisc zero-fills hard-unreadable sectors, so the
    file is always exactly `count` sectors. Here the length is whatever the sink
    accumulated. A short return is invisible downstream: the AR recovery ladder
    splices it at a sample-exact byte offset, so it corrupts audio rather than
    failing.
    """
    device = _FakeDevice(chunks=[_FakeChunk(2, b"\x00" * 4704)])  # asked for 3
    with pytest.raises(RuntimeError, match="delivered 2 of 3 sectors"):
        ar._read_span_binding(_binding_with(device), "/dev/sr0", 500, 3, None, None)


# ── read_disc_c2 over the binding ────────────────────────────────────────────
#
# conftest pins the whole suite to the subprocess transport, so nothing above
# reaches this path: `make check` can be green while the binding carrier is
# broken. Every test here therefore drives _read_disc_binding directly, with a
# fake module. That keeps them device-free AND build-tree-free — AccuDisc's
# extension links libaccudisc from their build tree, so a test that imported the
# real binding would fail whenever they are mid-rebuild.


class _FakeMapState(enum.IntEnum):
    PENDING = 0
    OK = 1
    C2 = 2
    HARD = 3
    RECOVERED = 4
    SUSPECT = 5


class _FakeReadChunk:
    """One delivered chunk, with the per-stream lengths the real Chunk carries."""

    def __init__(
        self, nsec: int, audio_len: int = 2352, c2_len: int = 294, sub_len: int = 96
    ) -> None:
        self.nsec = nsec
        self.audio_len = audio_len
        self.c2_len = c2_len
        self.sub_len = sub_len
        self.sector_len = audio_len + c2_len + sub_len
        # Distinct byte per stream so a mis-sliced sink shows up as wrong
        # content, not merely wrong length.
        sector = b"A" * audio_len + b"C" * c2_len + b"S" * sub_len
        self.data = sector * nsec


class _FakeStats:
    def __init__(
        self,
        sectors_read: int = 100,
        hard_errors: int = 0,
        sectors_suspect: int = 0,
        sectors_flagged: int = 0,
        subq_total: int = 0,
        subq_ok: int = 0,
    ) -> None:
        self.sectors_read = sectors_read
        self.hard_errors = hard_errors
        self.sectors_suspect = sectors_suspect
        self.sectors_flagged = sectors_flagged
        self.subq_total = subq_total
        self.subq_ok = subq_ok

    @property
    def subq_bad(self) -> int:
        return self.subq_total - self.subq_ok


class _FakeReadResult:
    def __init__(self, stats: _FakeStats) -> None:
        self.stats = stats


class _FakeDiscDevice:
    """Records the lead-in call order and the read request it was given."""

    def __init__(
        self,
        leadout: int = 10,
        # Any chunk-shaped object: _FakeReadChunk for slicing tests, _C2Chunk
        # for census ones, which need per-sector control of the C2 block.
        chunks: tuple[Any, ...] = (),
        cdtext: bytes | None = b"CDTEXT",
        stats: _FakeStats | None = None,
    ) -> None:
        self._leadout = leadout
        self._chunks = chunks
        self._cdtext = cdtext
        self._stats = stats or _FakeStats()
        self.calls: list[str] = []
        self.read_kwargs: dict[str, Any] = {}
        self.closed = False

    def __enter__(self) -> _FakeDiscDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        self.closed = True

    def read_full_toc_raw(self) -> bytes:
        self.calls.append("fulltoc")
        return b"FULLTOC"

    def read_cdtext_raw(self) -> bytes | None:
        self.calls.append("cdtext")
        return self._cdtext

    def read_toc(self):
        self.calls.append("toc")
        return SimpleNamespace(leadout_lba=self._leadout)

    def read(self, lba: int, count: int, *, status_map: bool = False, **kwargs: Any):
        self.calls.append("read")
        self.read_kwargs = {
            "lba": lba,
            "count": count,
            "status_map": status_map,
            **kwargs,
        }
        # Stand in for the engine writing states as sectors complete. The real
        # one fills as it goes; filling up-front is equivalent here because the
        # sink only ever projects the range it has been told is finished.
        if isinstance(status_map, bytearray):
            for i in range(len(status_map)):
                status_map[i] = self._map_fill(i)
        for chunk in self._chunks:
            kwargs["sink"](chunk)
        return _FakeReadResult(self._stats)

    def _map_fill(self, i: int) -> int:
        return _FakeMapState.OK


class _ModernDiscDevice(_FakeDiscDevice):
    """A 0.5.0+ device: ``status_map`` accepts a buffer."""

    def read(self, lba: int, count: int, *, status_map: bool | Any = False, **kw: Any):
        return _FakeDiscDevice.read(self, lba, count, status_map=status_map, **kw)


class _FakeC2(enum.IntEnum):
    NONE = 0
    PTRS = 1
    PTRS_BEB = 2


class _FakeSub(enum.IntEnum):
    NONE = 0
    RAW = 1
    Q = 2


def _disc_binding(device: _FakeDiscDevice) -> _FakeBinding:
    module = _binding_with(device)  # type: ignore[arg-type]
    module.C2 = _FakeC2  # type: ignore[attr-defined]
    module.Sub = _FakeSub  # type: ignore[attr-defined]
    module.MapState = _FakeMapState  # type: ignore[attr-defined]
    module.map_state = lambda b: _FakeMapState(b & 0x0F)  # type: ignore[attr-defined]
    return module


def _run_disc(device: _FakeDiscDevice, tmp_path: Path, **kw: Any) -> None:
    defaults: dict[str, Any] = {
        "output_pcm": None,
        "output_c2": None,
        "output_sub": None,
        "output_cdtext": None,
        "output_fulltoc": None,
        "read_speed": None,
        "progress_cb": None,
    }
    defaults.update(kw)
    ar._read_disc_binding(_disc_binding(device), "/dev/sr0", **defaults)


def test_disc_binding_reads_lead_in_before_audio_on_one_device(tmp_path: Path) -> None:
    """Order is the contract, not an implementation detail.

    Both lead-in reads must land on the same spin-up as the audio pass — that is
    the entire reason the CLI captures them inline. A reordering that moved them
    after the read would still pass a content assertion.
    """
    device = _FakeDiscDevice(chunks=(_FakeReadChunk(2),))
    _run_disc(
        device,
        tmp_path,
        output_pcm=tmp_path / "a.pcm",
        output_cdtext=tmp_path / "a.cdtext",
        output_fulltoc=tmp_path / "a.toc",
    )

    assert device.calls == ["fulltoc", "cdtext", "toc", "read"]
    assert device.closed
    assert (tmp_path / "a.toc").read_bytes() == b"FULLTOC"
    assert (tmp_path / "a.cdtext").read_bytes() == b"CDTEXT"


def test_disc_binding_always_requests_c2_even_when_not_writing_it(
    tmp_path: Path,
) -> None:
    """cli/main.c:1176 sets req.c2 = ACCUDISC_C2_PTRS independent of --c2f.

    Requesting C2 only when a --c2f path is given would drop the sector length
    from 2646 to 2352 on this arm alone, which makes an A/B against the
    subprocess a comparison of two different reads rather than two carriers.
    """
    device = _FakeDiscDevice(chunks=(_FakeReadChunk(1),))
    _run_disc(device, tmp_path, output_pcm=tmp_path / "a.pcm")

    assert device.read_kwargs["c2"] is _FakeC2.PTRS
    assert device.read_kwargs["sub"] is _FakeSub.NONE


def test_disc_binding_requests_raw_sub_only_when_capturing_it(
    tmp_path: Path,
) -> None:
    device = _FakeDiscDevice(chunks=(_FakeReadChunk(1),))
    _run_disc(device, tmp_path, output_sub=tmp_path / "a.sub")

    assert device.read_kwargs["sub"] is _FakeSub.RAW


def test_disc_binding_reads_the_whole_disc_from_lba_zero(tmp_path: Path) -> None:
    """[0, leadout) — including any track-1 program-area pregap.

    Starting at the first track's INDEX 01 instead would drop ABBA Gold's 33
    head frames and shift every boundary and the disc ID (fixed 2026-07-12).
    """
    device = _FakeDiscDevice(leadout=162892, chunks=(_FakeReadChunk(1),))
    _run_disc(device, tmp_path, output_pcm=tmp_path / "a.pcm")

    assert device.read_kwargs["lba"] == 0
    assert device.read_kwargs["count"] == 162892
    assert device.read_kwargs["copy"] is False


def test_disc_binding_splits_the_streams_by_offset(tmp_path: Path) -> None:
    """Each stream gets its own bytes — a mis-sliced sink writes the wrong ones."""
    device = _FakeDiscDevice(chunks=(_FakeReadChunk(3),))
    _run_disc(
        device,
        tmp_path,
        output_pcm=tmp_path / "a.pcm",
        output_c2=tmp_path / "a.c2",
        output_sub=tmp_path / "a.sub",
    )

    assert (tmp_path / "a.pcm").read_bytes() == b"A" * 2352 * 3
    assert (tmp_path / "a.c2").read_bytes() == b"C" * 294 * 3
    assert (tmp_path / "a.sub").read_bytes() == b"S" * 96 * 3


def test_disc_binding_progress_is_cumulative_against_the_disc_total(
    tmp_path: Path,
) -> None:
    """Matches the subprocess `progress <done> <total>` tokens the TUI consumes."""
    seen: list[tuple[int, int]] = []
    device = _FakeDiscDevice(
        leadout=6, chunks=(_FakeReadChunk(2), _FakeReadChunk(2), _FakeReadChunk(2))
    )
    _run_disc(
        device,
        tmp_path,
        output_pcm=tmp_path / "a.pcm",
        progress_cb=lambda d, t: seen.append((d, t)),
    )

    assert seen == [(2, 6), (4, 6), (6, 6)]


def test_disc_binding_writes_no_cdtext_file_when_the_disc_has_none(
    tmp_path: Path,
) -> None:
    """None is absence, not failure — and absence must leave no file behind.

    A zero-byte .cdtext would be read downstream as a present-but-empty block
    rather than as "this disc has no CD-Text".
    """
    device = _FakeDiscDevice(chunks=(_FakeReadChunk(1),), cdtext=None)
    _run_disc(device, tmp_path, output_cdtext=tmp_path / "a.cdtext")

    assert not (tmp_path / "a.cdtext").exists()


def test_disc_binding_reads_with_no_outputs_at_all(tmp_path: Path) -> None:
    """The metadata-only pass: no PCM, no C2, no sub, but still a whole-disc read.

    read_to_file raises ValueError on an empty file set, which is why this path
    does its own sink rather than calling it.
    """
    device = _FakeDiscDevice(chunks=(_FakeReadChunk(1),))
    _run_disc(device, tmp_path)

    assert device.calls == ["toc", "read"]


def test_disc_binding_refuses_a_nonsense_leadout(tmp_path: Path) -> None:
    """count <= 0 reaches Device.read as ValueError('count must be > 0').

    Caught here so the message names the disc geometry rather than the argument.
    """
    device = _FakeDiscDevice(leadout=0)
    with pytest.raises(RuntimeError, match="lead-out reported at LBA 0"):
        _run_disc(device, tmp_path)


def test_disc_binding_passes_the_requested_speed(tmp_path: Path) -> None:
    device = _FakeDiscDevice(chunks=(_FakeReadChunk(1),))
    _run_disc(device, tmp_path, read_speed=8)
    assert device.read_kwargs["speed_x"] == 8

    device = _FakeDiscDevice(chunks=(_FakeReadChunk(1),))
    _run_disc(device, tmp_path)
    assert device.read_kwargs["speed_x"] == 0  # 0 = leave the drive alone


def test_read_disc_c2_reads_the_lead_in_then_the_audio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The public entry point reads the lead-in before the audio, on one device."""
    device = _FakeDiscDevice(chunks=(_FakeReadChunk(1),))
    _install(monkeypatch, _disc_binding(device))
    ar.read_disc_c2("/dev/sr0", output_pcm=tmp_path / "a.pcm")

    assert device.calls == ["toc", "read"]


def test_read_disc_c2_raises_on_abi_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole-disc read is where a silent degrade would have cost the most —
    it is the longest operation here, and it used to be the one most likely to
    quietly run on the other carrier. Now it stops before the drive spins."""
    module = _disc_binding(_FakeDiscDevice())

    def _boom(_path: str) -> object:
        msg = "built against a different header"
        raise _FakeAbiMismatch(msg)

    module.Device = _boom  # type: ignore[assignment]
    _install(monkeypatch, module)

    with pytest.raises(RuntimeError, match="Rebuild the binding"):
        ar.read_disc_c2("/dev/sr0", output_pcm=tmp_path / "a.pcm")

    assert not (tmp_path / "a.pcm").exists(), "a refused read must leave no output"


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        (_FakeStats(), "clean"),
        (_FakeStats(hard_errors=1), "completed with caveats"),
        (_FakeStats(sectors_suspect=1), "completed with caveats"),
        (_FakeStats(sectors_flagged=1), "completed with caveats"),
    ],
)
def test_exit_three_is_reconstructed_from_stats(
    caplog: pytest.LogCaptureFixture, stats: _FakeStats, expected: str
) -> None:
    """Exit 3 is a CLI projection, not a library return.

    cli/main.c computes it as (hard_errors || sectors_suspect || sectors_flagged)
    after the read; Device.read discards its rc. Without this reconstruction the
    binding transport would report clean on precisely the discs where the
    subprocess said "delivered, but gate it".
    """
    with caplog.at_level(logging.DEBUG, logger=ar.log.name):
        ar._log_read_caveats(stats, "read")
    assert expected in caplog.text


def test_subchannel_yield_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """The counter the exit code cannot carry, and the one the Q speed cliff moves.

    Q yield collapses at high speed (98% at 24x, 47% at 32x on the PX-716A) while
    the audio stays clean, so a rip can pass every audio gate having lost the
    disc's pre-gaps and INDEX points.
    """
    with caplog.at_level(logging.DEBUG, logger=ar.log.name):
        ar._log_read_caveats(_FakeStats(subq_total=100, subq_ok=47), "read")
    assert "47/100 Q frames good (53 bad)" in caplog.text


# ── write_disc + speed_ladder_rows over the binding ──────────────────────────
#
# The write mapping carries more test weight than usual because it CANNOT be
# hardware-tested here: burning needs blank media and --simulate needs it too.
# These fakes are the only thing standing between the four-token contract and a
# burn nobody can undo.


class _FakeUnsupported(_FakeBindingError):
    pass


class _FakeNotBlank(_FakeBindingError):
    """AccuDisc 0.4.0's ``NotBlank`` — a SIBLING of ``Unsupported``, not a subclass.

    The relationship is the fixture. AccuDisc declined to subclass on purpose
    (§ck.3): subclassing would keep ``except Unsupported`` catching a not-blank
    disc, which is the ambiguity ``ACCUDISC_ERR_NOT_BLANK`` exists to end. A fake
    that made this a subclass — or reused one class for both — would pass whether
    the seam caught the right type or the wrong one, and the burn path would be
    untested in the only respect that changed.
    """


class _FakeWriteResult(enum.Enum):
    OK = "ok"
    CAVEATS = "caveats"

    @property
    def token(self) -> str:
        return self.value


class _FakeWriteDevice:
    def __init__(
        self, outcome: object = _FakeWriteResult.OK, logs: tuple[str, ...] = ()
    ) -> None:
        self._outcome = outcome
        self._logs = logs
        self.rdwr: bool | None = None
        self.kwargs: dict[str, Any] = {}
        self._sink: Any = None

    def __call__(self, path: str, *, rdwr: bool = False) -> _FakeWriteDevice:
        self.rdwr = rdwr
        return self

    def __enter__(self) -> _FakeWriteDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def set_log(self, fn: Any) -> None:
        self._sink = fn

    def write(self, toc: str, binp: str, **kwargs: Any) -> object:
        self.kwargs = {"toc": toc, "bin": binp, **kwargs}
        for line in self._logs:
            self._sink(line)
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _write_binding(device: _FakeWriteDevice) -> _FakeBinding:
    module = _FakeBinding()
    module.Device = device  # type: ignore[assignment]
    module.Unsupported = _FakeUnsupported  # type: ignore[attr-defined]
    module.NotBlank = _FakeNotBlank  # type: ignore[attr-defined]
    module.WriteResult = _FakeWriteResult  # type: ignore[attr-defined]
    return module


def _do_write(device: _FakeWriteDevice, **kw: Any) -> tuple[int, str, str | None]:
    return ar._write_disc_binding(
        _write_binding(device),
        "/dev/sr0",
        Path("/x/a.toc"),
        Path("/x/a.bin"),
        kw.pop("speed", 8),
        kw.pop("simulate", False),
        kw.pop("progress_cb", None),
        kw.pop("cdtext_path", None),
    )


def test_write_ok_maps_to_exit_zero() -> None:
    rc, _err, token = _do_write(_FakeWriteDevice(_FakeWriteResult.OK))
    assert (rc, token) == (0, "ok")


def test_write_caveats_means_the_disc_WAS_written() -> None:
    """Exit 3, not a failure. Telling the user their disc is blank when it is not
    is the single mistake this whole mapping exists to prevent."""
    dev = _FakeWriteDevice(_FakeWriteResult.CAVEATS, logs=("cdtext size_info odd",))
    rc, err, token = _do_write(dev)
    assert (rc, token) == (3, "caveats")
    # CAVEATS without the log is a boolean with no cause, so the sink must be
    # installed BEFORE the burn or the reason is unrecoverable.
    assert "cdtext size_info odd" in err


def test_write_not_blank_raises_not_blank_and_maps_to_not_blank() -> None:
    dev = _FakeWriteDevice(_FakeNotBlank("disc is not blank"))
    rc, err, token = _do_write(dev)
    assert (rc, token) == (2, "not_blank")
    assert "not blank" in err


def test_write_unsupported_is_an_error_now_not_not_blank() -> None:
    """The discriminating case for AccuDisc 0.4.0's `-13`, and the reason for it.

    Before 0.4.0, "not blank" was the only place `ERR_UNSUPPORTED` was reachable
    under the write path — exact **by census, not by construction**. Any future
    unsupported operation would have silently joined it, and the user would be
    told to insert a blank disc they had already inserted. Neither side's tests
    would have noticed, because both sides' behaviour is well-formed.

    So this asserts the half that has no observable consequence *today*: a
    genuine `Unsupported` must reach the generic arm and report `error`. Pair it
    with the test above and the two types are told apart; drop it and the seam
    could catch `(NotBlank, Unsupported)` — the compatible-looking spelling that
    buys compatibility by preserving the bug — and nothing would fail.
    """
    dev = _FakeWriteDevice(_FakeUnsupported("simulate is not supported here"))
    rc, _err, token = _do_write(dev)
    assert (rc, token) == (2, "error")


def test_write_other_errors_map_to_error_and_do_not_fall_back() -> None:
    """A failed burn must NOT reach _try_binding's subprocess fallback.

    Falling back would attempt a second burn of a disc whose state is now
    unknown. Returning rc=2 keeps the decision with the caller, which is the same
    place the subprocess path left it.
    """
    dev = _FakeWriteDevice(_FakeBindingError("laser said no"))
    rc, _err, token = _do_write(dev)
    assert (rc, token) == (2, "error")


def test_write_abi_mismatch_is_re_raised_so_it_can_degrade() -> None:
    """AbiMismatch subclasses AccuDiscError, so arm order is load-bearing.

    It surfaces on Device() — before any laser fires — and means the extension is
    broken while the binary is fine. Swallowing it into result=error would turn a
    perfectly good subprocess burn into a refusal.
    """
    dev = _FakeWriteDevice(_FakeAbiMismatch("header drift"))
    with pytest.raises(_FakeAbiMismatch):
        _do_write(dev)


def test_write_opens_the_device_read_write() -> None:
    dev = _FakeWriteDevice()
    _do_write(dev)
    assert dev.rdwr is True


def test_write_passes_simulate_and_speed_through() -> None:
    dev = _FakeWriteDevice()
    _do_write(dev, speed=16, simulate=True)
    assert dev.kwargs["simulate"] is True
    assert dev.kwargs["speed"] == 16


class _FakeRung:
    def __init__(
        self, requested: int, reported: int, measured: float, verdict: object = None
    ) -> None:
        self.requested_x = requested
        self.reported_x = reported
        self.measured_x = measured
        self.verdict = verdict


class _FakeLadderDevice:
    def __init__(self, rungs: tuple[_FakeRung, ...]) -> None:
        self._rungs = rungs
        self.kwargs: dict[str, Any] = {}

    def __enter__(self) -> _FakeLadderDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def probe_speed_ladder(self, **kwargs: Any) -> tuple[_FakeRung, ...]:
        self.kwargs = kwargs
        return self._rungs


def test_speed_ladder_binding_returns_the_same_triple() -> None:
    """The transport swap must be invisible to drive_speed.admitted_ladder.

    That consumer unpacks 3-tuples and applies the req == page2a rule. The
    binding offers verdicts, min_x/max_x and its own admission rule, all of which
    would CHANGE the ladder — a policy question that does not belong in a carrier
    swap.
    """
    dev = _FakeLadderDevice((
        _FakeRung(48, 48, 22.96),
        _FakeRung(40, 40, 23.69),
        _FakeRung(16, 8, 8.0),
    ))
    rows = ar._speed_ladder_binding(_binding_with(dev), "/dev/sr0")  # type: ignore[arg-type]
    assert rows == [
        ar.SpeedRow(48, 48, 22.96, None),
        ar.SpeedRow(40, 40, 23.69, None),
        ar.SpeedRow(16, 8, 8.0, None),
    ]


def test_speed_ladder_binding_asks_for_three_points_and_no_span() -> None:
    """points=3 is what makes verdicts possible at all; the span is AccuDisc's.

    Our plan had recorded the CLI's span as leadout/4 + leadout/2 — the NON-sweep
    span. At points=3 the CLI opens out to the whole disc, because three bands of
    the middle half sample much the same neighbourhood. Passing no span gets
    their computation rather than our copy of it.
    """
    dev = _FakeLadderDevice(())
    ar._speed_ladder_binding(_binding_with(dev), "/dev/sr0")  # type: ignore[arg-type]
    assert dev.kwargs == {"points": 3}


# ── the speeds verdict, on both transports ───────────────────────────────────


class _FakeVerdict:
    """Stands in for AccuDisc's Verdict enum, which exposes `.token`."""

    def __init__(self, token: str, name: str = "X") -> None:
        self.token = token
        self.name = name


def test_the_verdict_enum_is_normalised_to_a_bare_class() -> None:
    """`DUPLICATE:40` in, `duplicate` out.

    This test was wrong once, in a way worth keeping the note for: it asserted
    the binding emits "duplicate:40", which is the CLI's spelling. The enum
    yields `duplicate` and carries the collapsed-onto rung separately, and live
    hardware is what said so. The error was invisible because `admitted` — the
    only verdict the ladder policy compares against — had no suffix on either
    side, so a future branch on `duplicate` would have been the first casualty.
    """
    dev = _FakeLadderDevice((
        _FakeRung(48, 48, 22.96),
        _FakeRung(40, 40, 23.68),
    ))
    dev._rungs[0].verdict = _FakeVerdict("DUPLICATE:40")  # type: ignore[attr-defined]
    dev._rungs[1].verdict = _FakeVerdict("ADMITTED")  # type: ignore[attr-defined]
    rows = ar._speed_ladder_binding(_binding_with(dev), "/dev/sr0")  # type: ignore[arg-type]
    assert [r.verdict for r in rows] == ["duplicate", "admitted"]


def test_verdict_class_normalises_every_shape_to_one_string() -> None:
    """The four inputs this has to survive.

    The suffixed form is kept even though the CLI that printed it is gone: it is
    AccuDisc's own spelling for the same verdict and costs one branch to accept,
    where guessing wrong costs a silently unmatched policy comparison.
    """

    class _NameOnly:
        name = "ADMITTED"

    assert ar._verdict_class(_NameOnly()) == "admitted"  # no .token attribute
    assert ar._verdict_class("duplicate:40") == "duplicate"  # suffixed form
    assert ar._verdict_class("DUPLICATE") == "duplicate"  # enum token
    assert ar._verdict_class(None) is None  # engine did not judge


def test_write_passes_cdtext_path_when_given() -> None:
    """The raw lead-in blob, laid in verbatim. Supported on both transports.

    No caller supplies it yet — the RBI keeps CD-Text only as decoded strings in
    the TOC text and discards the raw packs, so round-tripping it through a burn
    needs a new container block. Covered here so the capability cannot silently
    rot before that lands.
    """
    dev = _FakeWriteDevice()
    _do_write(dev, cdtext_path=Path("/x/a.cdtext"))
    assert dev.kwargs["cdtext_path"] == "/x/a.cdtext"

    dev = _FakeWriteDevice()
    _do_write(dev)
    assert dev.kwargs["cdtext_path"] is None


# ---------------------------------------------------------------------------
# The seven entry points that were subprocess-only until the CLI retirement.
# Written while the subprocess still existed and the suite was pinned to it, so
# each had to install a fake binding explicitly; that scaffolding is now simply
# how every test here works.
# ---------------------------------------------------------------------------


class _SpeedDevice:
    """A Device whose only job is to report a speed pair — in the API's order."""

    def __init__(self, pair: tuple[int, int]) -> None:
        self._pair = pair

    def __enter__(self) -> _SpeedDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_speed(self) -> tuple[int, int]:
        return self._pair


def test_read_speed_swaps_the_bindings_pair_into_our_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Device.get_speed()` is `(max, current)`; `read_speed()` is `(current, max)`.

    Both are two ints in the same units, so handing one straight to the other
    type-checks, runs, and silently swaps every caller's reading of the drive:
    `drive_speed` would take the advertised ceiling for the current rate. The
    numbers here are deliberately far apart and non-multiples, so an accidental
    identity mapping cannot pass by coincidence.
    """
    fake = _FakeBinding()
    monkeypatch.setattr(fake, "Device", lambda _p: _SpeedDevice((7056, 706)))
    _install(monkeypatch, fake)

    assert ar.read_speed("/dev/sr0") == (706, 7056)


def test_read_speed_reports_unknown_rather_than_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drive that reports 0 has not told us it runs at 0 kB/s.

    The subprocess path returns None when the line is absent; the binding returns
    an unsigned 0 for the same "did not say". Collapsing those would make
    `drive_speed` believe it had a measurement.
    """
    fake = _FakeBinding()
    monkeypatch.setattr(fake, "Device", lambda _p: _SpeedDevice((0, 0)))
    _install(monkeypatch, fake)

    assert ar.read_speed("/dev/sr0") == (None, None)


def test_read_speed_on_a_device_failure_is_unknown_not_a_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeBinding()

    def _boom(_p: str) -> object:
        msg = "no medium"
        raise _FakeBindingError(msg)

    monkeypatch.setattr(fake, "Device", _boom)
    _install(monkeypatch, fake)

    assert ar.read_speed("/dev/sr0") == (None, None)


class _RecordingDevice:
    """Records which best-effort method was called on it."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def __enter__(self) -> _RecordingDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def eject(self) -> None:
        self._calls.append("eject")

    def park_spindle(self) -> None:
        self._calls.append("park_spindle")


@pytest.mark.parametrize(
    ("fn", "method"), [("eject", "eject"), ("park_spindle", "park_spindle")]
)
def test_tray_and_spindle_call_the_matching_device_method(
    fn: str, method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    fake = _FakeBinding()
    monkeypatch.setattr(fake, "Device", lambda _p: _RecordingDevice(calls))
    _install(monkeypatch, fake)

    getattr(ar, fn)("/dev/sr0")

    assert calls == [method]


@pytest.mark.parametrize("fn", ["eject", "park_spindle"])
def test_a_device_that_will_not_open_is_swallowed(
    fn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """These two are the seam's only "never raises" calls, open included.

    Everywhere else a device that will not open is the caller's problem. Here the
    whole operation is a courtesy: a tray that will not open has not broken a rip
    that already finished, so refusing to eject must not become an exception on
    the way out.
    """
    fake = _FakeBinding()

    def _boom(_p: str) -> object:
        msg = "device busy"
        raise _FakeBindingError(msg)

    monkeypatch.setattr(fake, "Device", _boom)
    _install(monkeypatch, fake)

    getattr(ar, fn)("/dev/sr0")  # must return, not raise


@pytest.mark.parametrize("fn", ["eject", "park_spindle"])
def test_an_abi_mismatch_is_not_swallowed_by_the_courtesy_calls(
    fn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one thing these two must NOT hide.

    A skewed extension is a build fault the user has to be told about, and it is
    one line away from the swallowed case above. Letting the seam's only
    best-effort calls absorb it would mean discovering it on the next operation
    that matters — which, for `park_spindle`, is the rip after this one.
    """
    fake = _FakeBinding()

    def _skew(_p: str) -> object:
        msg = "compiled against 0.2 but loaded 0.3"
        raise _FakeAbiMismatch(msg)

    monkeypatch.setattr(fake, "Device", _skew)
    _install(monkeypatch, fake)

    with pytest.raises(RuntimeError, match="Rebuild the binding"):
        getattr(ar, fn)("/dev/sr0")


def test_engine_version_comes_from_the_binding_without_a_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A module function, not a Device method, on purpose.

    The banner is written into the RLOG of a rip that has already succeeded, so
    making it depend on the drive still being openable would let a tray ejected
    one second early turn a good rip's provenance into "version unknown".
    """
    fake = _FakeBinding()
    monkeypatch.setattr(fake, "version_string", lambda: "0.4.0", raising=False)

    def _boom(_p: str) -> object:
        msg = "engine_version must not open a device"
        raise AssertionError(msg)

    monkeypatch.setattr(fake, "Device", _boom)
    _install(monkeypatch, fake)

    banner = ar.engine_version()
    assert banner == "accudisc 0.4.0"
    assert "transport" not in banner, (
        "the [transport: ...] suffix went with the second carrier — a constant "
        "in a provenance field is noise that reads like information"
    )


# ---- step 1b: lead-in, span-to-file, feature probe ---------------------------


class _LeadInDevice:
    def __init__(self, fulltoc: bytes | None, cdtext: bytes | None) -> None:
        self._fulltoc = fulltoc
        self._cdtext = cdtext
        self.opens = 0

    def __enter__(self) -> _LeadInDevice:
        self.opens += 1
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read_full_toc_raw(self) -> bytes | None:
        return self._fulltoc

    def read_cdtext_raw(self) -> bytes | None:
        return self._cdtext


def _bind(monkeypatch: pytest.MonkeyPatch, device: object) -> _FakeBinding:
    fake = _FakeBinding()
    monkeypatch.setattr(fake, "Device", lambda _p: device)
    _install(monkeypatch, fake)
    return fake


def test_lead_in_takes_one_device_for_both_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Both dumps come from the same lead-in, so they must come from one spin-up.

    Two devices would be two spin-ups for data sitting in the same place, which
    is the entire reason this is cheap enough to run in front of a rip.
    """
    dev = _LeadInDevice(b"FULLTOC", b"CDTEXT")
    _bind(monkeypatch, dev)
    ft, ct = tmp_path / "a.fulltoc", tmp_path / "a.cdtext"

    ar.read_lead_in("/dev/sr0", ft, ct)

    assert dev.opens == 1
    assert ft.read_bytes() == b"FULLTOC"
    assert ct.read_bytes() == b"CDTEXT"


def test_a_disc_without_cd_text_leaves_no_cdtext_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent CD-Text is the ordinary case, not a failure — most discs have none.

    Writing an empty file would be worse than writing nothing: every caller tests
    for the file's existence rather than catching, so a zero-byte sidecar reads as
    a successful capture of a disc whose CD-Text says nothing.
    """
    _bind(monkeypatch, _LeadInDevice(b"FULLTOC", None))
    ft, ct = tmp_path / "a.fulltoc", tmp_path / "a.cdtext"

    ar.read_lead_in("/dev/sr0", ft, ct)

    assert ft.exists()
    assert not ct.exists()


def test_a_failed_lead_in_read_leaves_no_file_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _Boom(_LeadInDevice):
        def read_full_toc_raw(self) -> bytes:
            msg = "no medium present"
            raise _FakeBindingError(msg)

    _bind(monkeypatch, _Boom(None, None))
    ft = tmp_path / "a.fulltoc"

    ar.read_lead_in("/dev/sr0", ft)  # cosmetic caller: must not raise

    assert not ft.exists(), (
        "an empty sidecar reads as a successful capture of a disc with no TOC, "
        "and every caller tests for the file rather than catching"
    )


def test_read_span_writes_the_bindings_bytes_to_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The file is this function's only contract; the binding hands back memory.

    Routed through `_read_span_binding` — the same function `read_span_bytes` has
    used since the first flip — rather than `Device.read_span`/`read_to_file`,
    neither of which can carry the progress callback the recovery TUI needs.
    """
    fake = _FakeBinding()
    _install(monkeypatch, fake)
    monkeypatch.setattr(ar, "_read_span_binding", lambda *a, **k: b"\x11\x22" * 8)

    out = tmp_path / "span.pcm"
    ar.read_span("/dev/sr0", 100, 2, out)

    assert out.read_bytes() == b"\x11\x22" * 8


def test_read_span_progress_callback_survives_the_flip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A silent multi-minute stream reads as a hang, so the callback is the point."""
    fake = _FakeBinding()
    _install(monkeypatch, fake)
    seen: list[object] = []
    monkeypatch.setattr(
        ar,
        "_read_span_binding",
        lambda _m, _d, _s, _c, _sp, cb: seen.append(cb) or b"",
    )

    def _cb(done: int, total: int) -> None:
        return None

    ar.read_span("/dev/sr0", 0, 1, tmp_path / "s.pcm", progress_cb=_cb)
    assert seen == [_cb]


class _FeaturesDevice:
    def __init__(self, verdict: _FakeC2Verdict) -> None:
        self._verdict = verdict

    def __enter__(self) -> _FeaturesDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def probe_features(self) -> object:
        class _F:
            c2_verdict = self._verdict
            combos = {  # noqa: RUF012 — the binding's spelling, deliberately
                "c2": True,
                "sub_raw": True,
                "sub_q": True,
                "c2_sub_raw": True,
                "c2_sub_q": False,
            }

        return _F()


def test_probe_combos_publishes_our_key_spelling_not_the_carriers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`Features.combos` says `c2_sub_raw`; the CLI and this module say `c2+sub_raw`.

    One character on two keys — enough that a caller testing `combos["c2+sub_raw"]`
    gets a KeyError on one carrier and a working single-pass capture on the other.
    Absorbing that is what the seam is for; the assertion is on the whole dict so a
    future key cannot be added on one path only.
    """
    _bind(monkeypatch, _FeaturesDevice(_FakeC2Verdict.SUPPORTED))

    assert ar.probe_combos("/dev/sr0") == {
        "c2": True,
        "sub_raw": True,
        "sub_q": True,
        "c2+sub_raw": True,
        "c2+sub_q": False,
    }


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (_FakeC2Verdict.SUPPORTED, True),
        (_FakeC2Verdict.UNSUPPORTED, False),
        (_FakeC2Verdict.UNVERIFIED, False),
    ],
)
def test_c2_support_is_claimed_and_functional_or_it_is_false(
    verdict: _FakeC2Verdict, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UNVERIFIED is "could not tell", not a weaker yes.

    It is the member most easily read as one, and treating it as support would ask
    a drive for C2 pointers that were never demonstrated — the subprocess answers
    False for the same drive, since only exit 0 means supported there.
    """
    _bind(monkeypatch, _FeaturesDevice(verdict))
    assert ar.drive_supports_c2("/dev/sr0") is expected


# ── the C2 damage map (disc map lane) ─────────────────────────────────────────


class _C2Chunk:
    """A chunk whose per-sector C2 block can be flagged individually.

    The default ``_FakeReadChunk`` fills C2 with a non-zero filler, which reads
    as "every sector damaged" — fine for a slicing test and useless for a census
    one, where the whole question is which sectors got marked.
    """

    def __init__(self, flags: list[bool], audio_len: int = 8, c2_len: int = 4) -> None:
        self.nsec = len(flags)
        self.audio_len = audio_len
        self.c2_len = c2_len
        self.sub_len = 0
        self.sector_len = audio_len + c2_len
        self.data = b"".join(
            b"A" * audio_len + (b"\x00\x08\x00\x00" if f else b"\x00" * c2_len)
            for f in flags
        )


def test_the_c2_census_marks_only_the_flagged_sectors() -> None:
    damage = bytearray(4)
    ar._census_c2(_C2Chunk([False, True, False, True]), damage, 0)
    assert list(damage) == [0, 1, 0, 1]


def test_the_c2_census_writes_at_the_chunks_absolute_offset() -> None:
    """The sink is handed chunk-relative indices and an LBA-indexed map, so an
    off-by-a-chunk here paints damage onto the wrong part of the disc — a map
    that is wrong in the one way it cannot look wrong."""
    damage = bytearray(10)
    ar._census_c2(_C2Chunk([True, False]), damage, 6)
    assert list(damage) == [0, 0, 0, 0, 0, 0, 1, 0, 0, 0]


def test_a_pcm_only_chunk_leaves_the_census_alone() -> None:
    """``c2_len == 0`` means C2 was not requested, not that it came back clean."""
    damage = bytearray(2)
    chunk = _C2Chunk([True, True])
    chunk.c2_len = 0
    ar._census_c2(chunk, damage, 0)
    assert list(damage) == [0, 0]


def test_the_damage_map_is_handed_over_before_the_first_sector_is_read(
    tmp_path: Path,
) -> None:
    """Handed over at ALLOCATION, not returned at the end.

    This is the whole reason the lane is computed in the sink rather than read
    from ``ReadResult.status_map``: the binding allocates that buffer internally
    and surfaces it only on the returned result, which does not exist until the
    read has finished. A map that appears when the read is over cannot drive a
    live display, so this one must arrive before any sector does.
    """
    seen: list[tuple[int, bytes]] = []
    device = _FakeDiscDevice(leadout=4, chunks=(_FakeReadChunk(4),))

    def _map_cb(buf: bytearray) -> None:
        seen.append((len(buf), bytes(buf)))

    _run_disc(device, tmp_path, map_cb=_map_cb)

    assert len(seen) == 1, "handed over exactly once, not per chunk"
    length, contents = seen[0]
    assert length == 4  # one byte per sector, sized from the lead-out
    assert contents == b"\x00" * 4  # nothing marked yet


def test_the_damage_map_is_populated_with_no_c2_file_requested(
    tmp_path: Path,
) -> None:
    """``c2_recovery = "off"`` suppresses the bitmap FILE, not the read.

    C2 pointers are on the wire unconditionally, so the map must still fill. If
    it were gated on the output file, that escape hatch would silently produce a
    blank map — and a blank map is indistinguishable from a clean disc.
    """
    captured: list[bytearray] = []
    device = _FakeDiscDevice(leadout=2, chunks=(_C2Chunk([True, False]),))
    _run_disc(device, tmp_path, output_c2=None, map_cb=captured.append)
    assert list(captured[0]) == [1, 0]


# ── caller-owned status map (AccuDisc 0.5.0+) ─────────────────────────────────


def test_the_caller_buffer_is_feature_detected_never_version_checked() -> None:
    """AccuDisc shipped this in the Python layer with the C library untouched,
    so ``library_version()`` is identical either side of it. A version guard
    would be permanently false while looking exactly correct."""
    assert ar._supports_caller_map(_ModernDiscDevice())
    assert not ar._supports_caller_map(_FakeDiscDevice())


def test_feature_detection_survives_a_device_with_no_read_at_all() -> None:
    """Detection must answer False, not raise: the fallback census still works,
    and a crash here would take out a rip that had every reason to succeed."""
    assert not ar._supports_caller_map(SimpleNamespace())


def test_pending_sectors_are_not_damage() -> None:
    """The frontier is PENDING, and marking it as damage would draw the entire
    unread remainder of the disc as destroyed the moment the map went live."""
    module = _disc_binding(_FakeDiscDevice())
    damage = bytearray(4)
    status = bytearray([
        _FakeMapState.PENDING,
        _FakeMapState.OK,
        _FakeMapState.C2,
        _FakeMapState.PENDING,
    ])
    ar._damage_from_status_map(module, status, damage)
    assert list(damage) == [0, 0, 1, 0]


def test_recovered_is_a_success_not_a_failure() -> None:
    """RECOVERED means the engine got the sector back. Sweeping every non-OK
    state into "damage" would report a successful repair as loss."""
    module = _disc_binding(_FakeDiscDevice())
    damage = bytearray(4)
    status = bytearray([
        _FakeMapState.RECOVERED,
        _FakeMapState.HARD,
        _FakeMapState.SUSPECT,
        _FakeMapState.OK,
    ])
    ar._damage_from_status_map(module, status, damage)
    assert list(damage) == [0, 1, 1, 0]


def test_a_modern_binding_is_given_a_buffer_and_the_diy_census_is_not_used(
    tmp_path: Path,
) -> None:
    """The engine's own map is used when it can be.

    ``_C2Chunk`` carries CLEAN C2 bytes, so the DIY census would mark nothing,
    while the status buffer says HARD. Only one of the two can produce the
    result — which is what makes this test able to tell them apart at all.
    """
    device = _ModernDiscDevice(leadout=2, chunks=(_C2Chunk([False, False]),))
    device._map_fill = lambda i: (_FakeMapState.HARD if i == 0 else _FakeMapState.OK)  # type: ignore[method-assign]
    captured: list[bytearray] = []
    _run_disc(device, tmp_path, map_cb=captured.append)

    assert isinstance(device.read_kwargs["status_map"], bytearray), (
        "the engine must be handed OUR buffer, not asked to allocate one"
    )
    assert list(captured[0]) == [1, 0]


def test_an_older_binding_falls_back_to_the_diy_census(tmp_path: Path) -> None:
    """A 0.4.x checkout is one `git checkout` away in a tree we do not control,
    so the fallback is a live path rather than dead code."""
    device = _FakeDiscDevice(leadout=2, chunks=(_C2Chunk([True, False]),))
    captured: list[bytearray] = []
    _run_disc(device, tmp_path, map_cb=captured.append)

    assert device.read_kwargs["status_map"] is False, "no buffer on an old binding"
    assert list(captured[0]) == [1, 0]


def test_detection_is_not_pinned_to_one_spelling_of_the_new_annotation() -> None:
    """A later `bool | memoryview` must still read as supported.

    AccuDisc uses `from __future__ import annotations`, so what we inspect is
    source TEXT, not a type. Matching the current spelling exactly would make a
    future widening read as an old binding and drop us to the fallback census —
    silently, on a binding that supports the feature. The test is therefore
    "bool is not the whole annotation", not "the annotation is what we expect".
    """

    class _Widened:
        def read(self, *, status_map: bool | memoryview = False) -> None: ...

    class _Quoted:
        def read(self, *, status_map: "bool" = False) -> None: ...  # noqa: UP037

    assert ar._supports_caller_map(_Widened())
    assert not ar._supports_caller_map(_Quoted()), "quoting is not a new feature"


def test_the_capability_set_is_preferred_over_the_signature() -> None:
    """`accudisc.features` is the intended signal; the annotation never was.

    The two are made to DISAGREE here — the device's signature says old, the
    feature set says supported — because a test where both agree cannot show
    which one was consulted.
    """
    module = _disc_binding(_FakeDiscDevice())
    module.features = frozenset({"caller_map_buffers", "subq_map"})  # type: ignore[attr-defined]
    assert ar._supports_caller_map(_FakeDiscDevice(), module)


def test_the_capability_set_can_also_say_NO() -> None:
    """A `features` set without the name means the binding lacks it — the
    absence must be believed, not treated as "ask the signature instead".
    Otherwise a capability could never be retired or staged."""
    module = _disc_binding(_ModernDiscDevice())
    module.features = frozenset({"subq_map"})  # type: ignore[attr-defined]
    assert not ar._supports_caller_map(_ModernDiscDevice(), module)


def test_a_binding_without_features_falls_back_to_the_signature() -> None:
    """The fallback has to answer for exactly the bindings that lack `features`,
    which is the only population it can ever be asked about."""
    module = _disc_binding(_FakeDiscDevice())
    assert not hasattr(module, "features")
    assert ar._supports_caller_map(_ModernDiscDevice(), module)
    assert not ar._supports_caller_map(_FakeDiscDevice(), module)


def test_a_non_set_features_attribute_is_ignored_rather_than_trusted() -> None:
    """`in` works on str, list and dict, so a `features` that is not a set would
    still answer — plausibly and by accident. A string "subq_map" contains
    "subq_map"; it also contains "map". Type is checked before membership."""
    module = _disc_binding(_ModernDiscDevice())
    module.features = "caller_map_buffers subq_map"  # type: ignore[attr-defined]
    assert ar._supports_caller_map(_ModernDiscDevice(), module)  # via signature
    module.features = None  # type: ignore[attr-defined]
    assert ar._supports_caller_map(_ModernDiscDevice(), module)


# ── the binding's SHAPES, not just its symbol names ───────────────────────────
#
# `_BINDING_SURFACE` is checked at import and names the symbols we call. Nothing
# checked the shapes we destructure — `ReadStats` fields, `Chunk` attributes,
# `MapState` members — and a rename there is silent breakage on a green suite.
# That is AccuDisc's own criterion from §2026-08-08e turned on us: if an artefact
# is load-bearing for someone, does anything fail when it changes? These tests
# are the answer. (They found the same gap in their own dataclasses when we
# reported ours — fixed in their 4ba3517.)
#
# Names, not counts, and deliberately not a subset test: an ADDED field is
# additive and harmless, a RENAMED one breaks us. So each test asserts the names
# we use are present, and does not care what else is.


def _real_binding():
    """The actual AccuDisc binding, or a skip.

    These are the only tests here that must run against the real thing — a fake
    cannot detect an upstream rename, which is the entire point. The seam's own
    resolution logic is reused so the skip is honest about what is missing.

    The cache clear is load-bearing, not hygiene. ``_import_binding`` is a
    ``functools.cache`` on a module global, and the namespace-package tests above
    deliberately poison it with a *negative* result; inheriting that gives a skip
    reading "binding unavailable" on a machine where it is installed and working.
    A guard that silently skips is a guard that is not running — which is the
    exact failure these tests exist to catch, arriving in the tests themselves.
    """
    ar._import_binding.cache_clear()
    module, why = ar._import_binding()
    if module is None:
        pytest.skip(f"AccuDisc binding unavailable: {why}")
    return module


_USED_STATS_FIELDS = frozenset({
    "sectors_read",
    "hard_errors",
    "sectors_suspect",
    "sectors_flagged",
    "subq_total",
    "subq_ok",
    "subq_bad",
})
_USED_CHUNK_ATTRS = frozenset({
    "data",
    "nsec",
    "sector_len",
    "audio_len",
    "c2_len",
    "sub_len",
})


def test_readstats_still_carries_every_field_we_read() -> None:
    """`_log_read_caveats` rebuilds the caveat verdict from these, and it is the
    ONLY source of that signal since the CLI went — a rename would silence it
    while every test kept passing.

    Checked by ATTRIBUTE, not by `dataclasses.fields`. Written the other way
    first, and it failed on `subq_bad`, which is a derived **property**
    (`subq_total - subq_ok`) and therefore not a field. The binding was right and
    the test was wrong — but wrong in the direction that matters, because
    attribute access is what our code does, so a field-based check both misses
    renames of properties and invents failures for their existence.
    """
    module = _real_binding()
    missing = {n for n in _USED_STATS_FIELDS if not hasattr(module.ReadStats, n)}
    assert not missing, f"ReadStats no longer provides: {missing}"


def test_chunk_still_carries_every_length_the_sink_slices_by() -> None:
    """`_split_streams` de-interleaves by these lengths rather than by constants,
    precisely so a pcm-only read is not mis-sliced. A rename turns that safety
    into an AttributeError mid-rip, after the drive has spun up."""
    module = _real_binding()
    names = {f.name for f in dataclasses.fields(module.Chunk)}
    missing = {n for n in _USED_CHUNK_ATTRS if n not in names}
    assert not missing, f"Chunk no longer provides: {missing}"


def test_mapstate_still_names_every_state_the_damage_lane_classifies() -> None:
    """MEMBER NAMES, not values.

    Every other assertion about this enum checks values against the C constants,
    and a rename leaves all of them passing — `MapState.HARD` becoming
    `MapState.UNREADABLE` breaks every consumer and moves no number. We look
    members up by name via `getattr`, so the name IS the interface.

    PENDING and RECOVERED are included although they are not in
    `_MAP_DAMAGE_STATES`: they are the two states that must NOT be damage, and
    the projection is only correct while both remain distinguishable.
    """
    module = _real_binding()
    names = {m.name for m in module.MapState}
    needed = set(ar._MAP_DAMAGE_STATES) | {"PENDING", "OK", "RECOVERED"}
    assert needed <= names, f"missing: {needed - names}"


def test_the_real_binding_is_reachable_from_the_test_suite() -> None:
    """The shape tests above are worthless if they silently skip.

    They did, for exactly one commit: `test_a_namespace_package_is_not_the_binding`
    leaked a phantom `accudisc` into `sys.modules` — an insertion `monkeypatch`
    does not track — so every later import got it from cache with no `sys.path`
    involved. The shape tests reported "binding unavailable" on a machine where
    it is installed and working.

    This asserts rather than skips, so the *absence* of the guard is itself a
    failure. A skipped test and a passing one are indistinguishable in a summary
    line, which is the whole shape of AccuDisc's NDEBUG finding.
    """
    ar._import_binding.cache_clear()
    module, why = ar._import_binding()
    assert module is not None, (
        f"the shape tests would silently skip: {why}. If the binding is genuinely "
        f"absent this test is the right place to loosen — deliberately, not by "
        f"letting three guards go quiet."
    )
