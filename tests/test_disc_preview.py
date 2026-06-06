"""Tests for the cosmetic rip disc-title preview helpers."""

import types

from cdda2img.cdda2img import _disc_preview_label, _fmt_kv
from cdda2img.lookup_result import DiscMeta


def test_fmt_kv_aligns_values_at_column_16():
    # 3-space indent + 13-wide label field → value always starts at column 16.
    assert _fmt_kv("Drive", "X").index("X") == 16
    assert _fmt_kv("Read offset", "X").index("X") == 16
    assert _fmt_kv("Disc", "X").index("X") == 16


def test_cdtext_album_is_authoritative(monkeypatch):
    # When the fast scan captured CD-Text, use it as-is — no network lookup.
    import cdda2img.mb_lookup as mb

    def _boom(_disc):
        msg = "MB lookup must not run when CD-Text is present"
        raise AssertionError(msg)

    monkeypatch.setattr(mb, "lookup_disc_id", _boom)
    disc = types.SimpleNamespace(album="Eliminator", artist="ZZ Top")
    assert _disc_preview_label(disc) == "Eliminator - ZZ Top"


def test_mb_plurality_pick(monkeypatch):
    import cdda2img.mb_lookup as mb

    metas = [
        DiscMeta(album="Eliminator", artist="ZZ Top"),
        DiscMeta(album="Eliminator", artist="ZZ Top"),
        DiscMeta(album="Eliminator (Reissue)", artist="ZZ Top"),
    ]
    monkeypatch.setattr(mb, "lookup_disc_id", lambda _disc: metas)
    disc = types.SimpleNamespace(album=None, artist=None)
    assert _disc_preview_label(disc) == "Eliminator - ZZ Top"


def test_mb_no_match_is_unknown(monkeypatch):
    import cdda2img.mb_lookup as mb

    monkeypatch.setattr(mb, "lookup_disc_id", lambda _disc: [])
    disc = types.SimpleNamespace(album=None, artist=None)
    assert _disc_preview_label(disc) == "(unknown)"
