"""
setup.py — cdda2img setup wizard and maintenance tasks.

Public API: ``run_setup_wizard()``.  No input() calls here — all interactive
prompts use questionary.  Non-TTY callers receive a ``RuntimeError``.
"""

from __future__ import annotations

import logging
import random
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_SECTION_CHOICES = [
    ("create-config", "Config: Create from template"),
    ("update-config", "Config: Update from template"),
    ("validate-config", "Config: Validate / repair"),
    ("read-offset", "Drive: Detect read offset"),
    ("write-offset", "Drive: Measure write offset"),
    ("create-catalogue", "Catalogue: Create database"),
    ("validate-catalogue", "Catalogue: Validate structure"),
    ("verify-catalogue", "Catalogue: Verify file locations"),
    (None, "Exit"),
]

# ── public entry point ────────────────────────────────────────────────────────


def run_setup_wizard(
    *,
    section: str | None = None,
    device: str | None = None,
    speed: int = 4,
    verify_test: bool = False,
) -> bool:
    """Run the setup wizard, optionally jumping directly to *section*.

    Returns True when the wizard completed without fatal error.
    """
    if section is None:
        section = _prompt_section_menu()
        if section is None:
            return True

    dispatch: dict[str, object] = {
        "create-config": _section_create_config,
        "update-config": _section_update_config,
        "validate-config": _section_validate_config,
        "read-offset": lambda: _section_read_offset(device),
        "write-offset": lambda: _section_write_offset(device, speed),
        "create-catalogue": _section_create_catalogue,
        "validate-catalogue": _section_validate_catalogue,
        "verify-catalogue": lambda: _section_verify_catalogue(verify_test),
    }
    fn = dispatch.get(section)
    if fn is None:
        log.warning("Unknown setup section: %s", section)
        return False
    return fn()  # type: ignore[call-arg]


# ── questionary helpers ───────────────────────────────────────────────────────


def _q():  # type: ignore[return]
    """Lazy import of questionary (avoids overhead when wizard is not used)."""
    import questionary  # type: ignore[import-untyped]

    return questionary


def _prompt_section_menu() -> str | None:
    """Show the top-level section menu. Returns the chosen section key or None."""
    labels = [label for _, label in _SECTION_CHOICES]
    key_by_label = {label: key for key, label in _SECTION_CHOICES}
    print("\ncdda2img Setup Wizard")
    print("─" * 40)
    choice = _q().select("What would you like to do?", choices=labels).ask()
    if choice is None:
        return None
    return key_by_label[choice]


def _confirm(prompt: str, default: bool = False) -> bool:
    result = _q().confirm(prompt, default=default).ask()
    return bool(result)


def _select(prompt: str, choices: list[str]) -> str | None:
    return _q().select(prompt, choices=choices).ask()  # type: ignore[return-value]


# ── WOTCD loader ──────────────────────────────────────────────────────────────

_WOTCD_ITEMS: list[str] = []


def _load_wotcd() -> list[str]:
    global _WOTCD_ITEMS
    if _WOTCD_ITEMS:
        return _WOTCD_ITEMS
    import contextlib
    import importlib.resources

    with contextlib.suppress(Exception):
        ref = importlib.resources.files("cdda2img").joinpath(
            "../../docs/research/WOTCD.md"
        )
        p = Path(str(ref))
        if not p.is_file():
            raise FileNotFoundError
        _WOTCD_ITEMS = _parse_wotcd(p.read_text())
        if _WOTCD_ITEMS:
            return _WOTCD_ITEMS

    fallback = Path(__file__).parent.parent.parent / "docs" / "research" / "WOTCD.md"
    if fallback.is_file():
        _WOTCD_ITEMS = _parse_wotcd(fallback.read_text())
    return _WOTCD_ITEMS


def _parse_wotcd(text: str) -> list[str]:
    import re

    items = []
    for line in text.splitlines():
        m = re.match(r"^\d+\.\s+(.+)$", line.strip())
        if m:
            items.append(m.group(1))
    return items


# ── Config sections ───────────────────────────────────────────────────────────


def _section_create_config() -> bool:
    from cdda2img.config import _example_path, config_path

    dest = config_path()
    example = _example_path()
    if not example.is_file():
        print(f"  ERROR: template not found at {example}")
        return False
    if dest.is_file() and not _confirm(
        f"  Config already exists at {dest}. Overwrite?"
    ):
        print("  Skipped.")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copy(example, dest)
    print(f"  Created {dest}")
    return True


def _section_update_config() -> bool:
    from cdda2img.config import config_path, update_config_from_template

    dest = config_path()
    ok = update_config_from_template(dest)
    if ok:
        print(f"  Config updated from template: {dest}")
    else:
        print("  Update failed — see log for details.")
    return ok


def _section_validate_config() -> bool:
    from cdda2img.config import Config, config_path

    path = config_path()
    if not path.is_file():
        print(f"  Config not found: {path}")
        return False

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[import-not-found,no-redef]  # LINT-011

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except Exception as exc:
        print(f"  TOML parse error: {exc}")
        return False

    known = set(Config.__dataclass_fields__)
    unknown = [k for k in raw if k not in known]
    print(f"  Config: {path}")
    if unknown:
        print(f"  Unknown keys: {', '.join(unknown)}")
        if _confirm("  Remove unknown keys?"):
            for k in unknown:
                del raw[k]
            _write_config_dict(path, raw)
            print("  Done.")
    else:
        print("  No unknown keys.")

    drives = raw.get("drives", [])
    if drives:
        print(f"  Drives configured: {len(drives)}")
    else:
        print("  No drives configured.")
    print("  Config OK.")
    return True


def _write_config_dict(path: Path, raw: dict) -> None:
    """Naively round-trip a flat dict back to TOML (no comment preservation)."""
    from cdda2img.config import _render_drive, _render_scalar

    lines = []
    for k, v in raw.items():
        if k == "drives":
            continue
        lines.append(_render_scalar(k, v))
    for d in raw.get("drives", []):
        lines.append("")
        lines.append(_render_drive(d))
    lines.append("")
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines))
    tmp.replace(path)


# ── Drive sections ────────────────────────────────────────────────────────────


def _section_read_offset(device: str | None) -> bool:
    from cdda2img.config import load_config
    from cdda2img.db import open_drive_offsets_db
    from cdda2img.drive_info import (
        ensure_drive_offsets,
        find_drive_offset,
        probe_drive_name,
    )

    cfg = load_config()
    if device is None:
        device = cfg.default_device
    drive_name = probe_drive_name(device)
    if drive_name:
        print(f"  Drive: {drive_name}")
    else:
        print(f"  Drive: {device} (name probe failed)")

    try:
        conn = open_drive_offsets_db(cfg)
        ensure_drive_offsets(conn)
        result = find_drive_offset(conn, drive_name) if drive_name else None
        conn.close()
    except Exception as exc:
        print(f"  AccurateRip lookup failed: {exc}")
        return False

    if result is None:
        print("  Drive not found in AccurateRip catalogue.")
        return True

    offset, confidence = result
    print(f"  Read offset: {offset:+d} samples  (confidence: {confidence})")

    if not drive_name:
        return True
    if _confirm(f"  Save read_offset={offset:+d} for {drive_name!r} to config?"):
        from cdda2img.config import save_drive_read_offset

        save_drive_read_offset(drive_name, offset)
        print("  Saved.")
    return True


def _section_write_offset(device: str | None, speed: int) -> bool:  # noqa: C901
    from cdda2img import write_offset as wo
    from cdda2img.config import load_config, save_drive_write_offset

    if device is None:
        device = load_config().default_device

    drive_name, read_offset = wo.probe_drive(device, None)
    slug = wo.drive_slug(drive_name, device)
    res_path = wo.results_path(slug)
    wdir = wo.work_dir()
    wav = wdir / "test.wav"
    toc = wdir / "test.toc"
    ripped_bin = wdir / "ripped.bin"
    ripped_toc = wdir / "ripped.toc"

    wotcd = _load_wotcd()

    print()
    print("  Write-offset measurement: burn-and-read-back")
    print("  Each cycle consumes one blank disc.")
    print(f"  Device: {device}  read_offset={read_offset:+d}")
    if drive_name:
        print(f"  Drive:  {drive_name}")

    results = wo.load_results(res_path)
    results.setdefault("drive", {})["name"] = drive_name or ""
    results["drive"]["read_offset"] = read_offset

    if results["cycles"]:
        s = results.get("summary", {})
        print(
            f"\n  Resuming: {s.get('tests', 0)} existing test(s)  "
            f"write_offset={s.get('write_offset', '?'):+}  "
            f"confidence={s.get('confidence', 0)}%"
        )

    if not wav.is_file() or not toc.is_file():
        print("  Generating test signal (75 s)...")
        wo.generate_test_signal(wav, toc)
        print(f"  Signal written to {wav}")

    while True:
        cycle_n = len(results["cycles"]) + 1
        print(f"\n{'─' * 60}")
        print(
            f"  Disc #{cycle_n}  [{drive_name or device}  read_offset={read_offset:+d}]"
        )

        if wotcd:
            quote = random.choice(wotcd)  # noqa: S311
            print(f"\n  Creative use for your new coaster: {quote}")
        print()

        if not _confirm("  Insert a blank disc and press Enter to burn"):
            break

        print("  Burning...")
        try:
            wo.burn_disc(toc, device, speed)
        except RuntimeError as exc:
            print(f"  Burn failed: {exc}")
            if not _confirm("  Try again with another disc?"):
                break
            continue

        if not _confirm(
            "  Disc ejected. Reinsert the burned disc and press Enter to rip"
        ):
            break

        print("  Ripping...")
        try:
            wo.rip_disc(device, ripped_bin, ripped_toc)
        except RuntimeError as exc:
            print(f"  Rip failed: {exc}")
            wo.eject(device)
            if not _confirm("  Try again with another disc?"):
                break
            continue
        wo.eject(device)

        cycle = wo.analyse_cycle(ripped_bin, read_offset)
        if cycle is None:
            continue

        results["cycles"].append(cycle)
        results["summary"] = wo.summarise_cycles(results["cycles"])
        res_path.parent.mkdir(parents=True, exist_ok=True)
        wo.save_results(res_path, results)

        s = results["summary"]
        print(
            f"\n  write_offset = {s['write_offset']:+d}  "
            f"({s['tests']} test(s), {s['confidence']}% confidence)"
        )
        if s["variance"]:
            print("  WARNING: variance detected — consider more cycles")

        if (
            s["confidence"] >= 80
            and drive_name
            and _confirm(
                f"  Save write_offset={s['write_offset']:+d} for {drive_name!r}?"
            )
        ):
            save_drive_write_offset(drive_name, s["write_offset"])
            print("  Saved.")

        if not _confirm("  Another disc?"):
            break

    if results["cycles"]:
        s = results["summary"]
        print(f"\n{'═' * 60}")
        print(f"  Tests:        {s['tests']}")
        print(f"  Write offset: {s['write_offset']:+d} samples")
        print(f"  Variance:     {s['variance']}")
        print(f"  Confidence:   {s['confidence']}%")
        print(f"\n  Results saved to {res_path}")
    return True


# ── Catalogue sections ────────────────────────────────────────────────────────


def _section_create_catalogue() -> bool:
    from cdda2img.catalogue import catalogue_db_path, open_catalogue_db
    from cdda2img.config import load_config

    try:
        cfg = load_config()
        db_path = cfg.catalogue_path or catalogue_db_path()
    except Exception:
        db_path = catalogue_db_path()

    if db_path.is_file() and not _confirm(
        f"  Catalogue already exists at {db_path}. Recreate?"
    ):
        print("  Skipped.")
        return True

    try:
        conn = open_catalogue_db(db_path)
        conn.close()
    except Exception as exc:
        print(f"  Failed to create catalogue: {exc}")
        return False
    else:
        print(f"  Catalogue created: {db_path}")
        return True


def _section_validate_catalogue() -> bool:
    from cdda2img.catalogue import _SCHEMA_VERSION, catalogue_db_path, open_catalogue_db
    from cdda2img.config import load_config

    try:
        cfg = load_config()
        db_path = cfg.catalogue_path or catalogue_db_path()
    except Exception:
        db_path = catalogue_db_path()

    if not db_path.is_file():
        print(f"  Catalogue not found: {db_path}")
        return False

    print(f"  Catalogue: {db_path}")
    ok = True

    # Structural check
    raw = sqlite3.connect(db_path)
    try:
        ic = raw.execute("PRAGMA integrity_check").fetchone()[0]
        if ic == "ok":
            print("  integrity_check: OK")
        else:
            print(f"  integrity_check: FAILED ({ic})")
            ok = False
    finally:
        raw.close()

    # Schema version + migration
    try:
        conn = open_catalogue_db(db_path)
        ver = conn.execute(
            "SELECT value FROM db_meta WHERE key='schema_version'"
        ).fetchone()
        conn.close()
        if ver and ver[0] == _SCHEMA_VERSION:
            print(f"  Schema version: {ver[0]} (current)")
        elif ver:
            print(f"  Schema version: {ver[0]} (migrated to {_SCHEMA_VERSION})")
    except Exception as exc:
        print(f"  Schema check failed: {exc}")
        ok = False

    # VACUUM offer
    if ok and _confirm(
        "  Run VACUUM to reclaim space and rebuild indices?", default=False
    ):
        conn2 = sqlite3.connect(db_path)
        try:
            conn2.execute("VACUUM")
            conn2.commit()
            print("  VACUUM complete.")
        except Exception as exc:
            print(f"  VACUUM failed: {exc}")
        finally:
            conn2.close()

    return ok


def _section_verify_catalogue(verify_test: bool = False) -> bool:  # noqa: C901
    from cdda2img.catalogue import _compute_b3sum, catalogue_db_path, open_catalogue_db
    from cdda2img.config import load_config

    try:
        cfg = load_config()
        db_path = cfg.catalogue_path or catalogue_db_path()
    except Exception:
        db_path = catalogue_db_path()

    if not db_path.is_file():
        print(f"  Catalogue not found: {db_path}")
        return False

    conn = open_catalogue_db(db_path)
    try:
        rows = conn.execute(
            "SELECT id, album, artist, file_path, b3sum FROM catalogue ORDER BY id"
        ).fetchall()
    except Exception as exc:
        print(f"  Failed to query catalogue: {exc}")
        conn.close()
        return False

    print(f"  Verifying {len(rows)} catalogue entry(s)...")
    missing = 0
    mismatched = 0
    ok_count = 0

    for row_id, album, artist, file_path, stored_b3sum in rows:
        label = f"  [{row_id}] {artist} — {album}"
        p = Path(file_path)

        if not p.is_file():
            missing += 1
            print(f"{label}")
            print(f"       MISSING: {file_path}")
            choice = _select(
                "       Action?", ["Skip", "Search for new location", "Delete record"]
            )
            if choice == "Delete record":
                with conn:
                    conn.execute("DELETE FROM catalogue WHERE id=?", (row_id,))
                print("       Deleted.")
            elif choice == "Search for new location":
                _relocate_entry(conn, row_id, stored_b3sum, label)
            continue

        if stored_b3sum:
            actual = _compute_b3sum(p)
            if actual == stored_b3sum:
                ok_count += 1
                if verify_test:
                    _run_rbi_test(p, label)
            else:
                mismatched += 1
                print(f"{label}")
                print(f"       MISMATCH: {file_path}")
                choice = _select(
                    "       b3sum mismatch — action?",
                    ["Skip", "Delete record", "Update b3sum (file changed)"],
                )
                if choice == "Delete record":
                    with conn:
                        conn.execute("DELETE FROM catalogue WHERE id=?", (row_id,))
                    print("       Deleted.")
                elif choice == "Update b3sum (file changed)":
                    with conn:
                        conn.execute(
                            "UPDATE catalogue SET b3sum=? WHERE id=?", (actual, row_id)
                        )
                    print("       b3sum updated.")
        else:
            ok_count += 1

    conn.close()
    print(f"\n  Results: {ok_count} OK, {missing} missing, {mismatched} mismatch(es)")
    return missing == 0 and mismatched == 0


def _relocate_entry(
    conn: sqlite3.Connection,
    row_id: int,
    stored_b3sum: str | None,
    label: str,
) -> None:
    from cdda2img.catalogue import _compute_b3sum

    scan_dir_str = _q().text("       Directory to scan for .rbi files:").ask()
    if not scan_dir_str:
        return
    scan_dir = Path(scan_dir_str)
    if not scan_dir.is_dir():
        print(f"       Not a directory: {scan_dir}")
        return

    candidates = list(scan_dir.rglob("*.rbi"))
    print(f"       Found {len(candidates)} .rbi file(s)...")
    matched = None
    if stored_b3sum:
        for c in candidates:
            if _compute_b3sum(c) == stored_b3sum:
                matched = c
                break
    if matched:
        print(f"       Match: {matched}")
        if _confirm("       Update path in catalogue?"):
            with conn:
                conn.execute(
                    "UPDATE catalogue SET file_path=?, file_basename=? WHERE id=?",
                    (str(matched.resolve()), matched.name, row_id),
                )
            print("       Path updated.")
    else:
        print("       No match found by b3sum.")


def _run_rbi_test(p: Path, label: str) -> None:
    import contextlib

    with contextlib.suppress(Exception):
        from cdda2img.container import verify_container

        result = verify_container(p)
        if not result:
            print(f"{label}")
            print(f"       VERIFY FAILED: {p}")
