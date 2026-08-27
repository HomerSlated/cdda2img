"""
setup.py — cdda2img setup wizard and maintenance tasks.

Public API: ``run_setup_wizard()``.  No input() calls here — all interactive
prompts use questionary.  Non-TTY callers receive a ``RuntimeError``.
"""

from __future__ import annotations

import logging
import os
import random
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_SECTION_CHOICES = [
    ("create-config", "Config: Create from template"),
    ("update-config", "Config: Update from template"),
    ("validate-config", "Config: Validate / repair"),
    ("edit-config", "Config: Edit ($EDITOR)"),
    ("create-profile", "Profiles: Create"),
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
        "edit-config": _section_edit_config,
        "create-profile": _section_create_profile,
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
    """Run the real two-stage validator over the config (§9.6).

    This used to check only that the TOML parsed and that no key was unknown to
    the ``Config`` dataclass — neither of the two stages. An out-of-range value
    passed silently, which is exactly the class the split exists to catch.
    """
    from cdda2img.config import config_path
    from cdda2img.validation import CONFIG_SCHEMA, validate

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

    print(f"  Config: {path}")
    errors = validate(raw, CONFIG_SCHEMA)
    if not errors:
        drives = raw.get("drives", [])
        print(
            f"  Drives configured: {len(drives)}"
            if drives
            else "  No drives configured."
        )
        print("  Config OK.")
        return True

    spec = [e for e in errors if e.stage == "spec"]
    sanity = [e for e in errors if e.stage == "sanity"]
    for label, group in (("structure", spec), ("values", sanity)):
        for e in group:
            print(f"  [{label}] {e}")

    # Only unknown top-level keys can be repaired mechanically. A wrong type or an
    # illegal value needs a decision about what the user meant, and guessing is how
    # the old lenient loader hid mistakes.
    unknown = [
        e.where for e in spec if "unknown key" in e.message and "." not in e.where
    ]
    if unknown and _confirm(f"  Remove {len(unknown)} unknown key(s)?"):
        for k in unknown:
            raw.pop(k, None)
        _write_config_dict(path, raw)
        print("  Removed. Re-run validation to check what remains.")
    elif not unknown:
        print(f"  Edit {path} to fix these, then re-run.")
    return False


def _section_edit_config() -> bool:
    """Open the config in $EDITOR and re-validate on save (§9.6).

    Re-validating afterwards is the point: with `load_config` now strict, saving a
    broken config would make every subcommand except `setup` refuse to start, and
    the user would find out at the start of their next rip rather than here.
    """
    import shlex
    import subprocess

    from cdda2img.config import config_path

    path = config_path()
    if not path.is_file():
        print(f"  Config not found: {path}")
        print("  Run 'Config: Create from template' first.")
        return False

    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        print("  Neither $VISUAL nor $EDITOR is set; cannot open an editor.")
        print(f"  Edit {path} by hand, then run 'Config: Validate / repair'.")
        return False

    try:
        # shlex.split so EDITOR="code -w" and friends work. Deliberately inherits
        # the terminal: this is an interactive editor, not a captured subprocess.
        subprocess.run([*shlex.split(editor), str(path)], check=False)  # noqa: S603
    except OSError as exc:
        print(f"  Could not run {editor!r}: {exc}")
        return False

    print()
    return _section_validate_config()


def _text(prompt: str, default: str = "") -> str | None:
    return _q().text(prompt, default=default).ask()  # type: ignore[return-value]


def _section_create_profile() -> bool:
    """Create a recovery profile in the user profiles dir (§9.7).

    Two guards, both deliberately without a --force escape:

    1. **Name sanitiser** — anything outside ``[a-z0-9_-]`` is an error, never
       silently mangled. Two names that differ only in case or punctuation would map
       to one filename, and the loser would vanish without a message.
    2. **Overwrite guard** — a name matching any shipped or existing user profile is
       refused. Shipped names are reserved; shadowing `track-ladder` with a local
       file would make every future measurement labelled `track-ladder`
       incomparable with the bench that named it.

    The file is written atomically through the same validator the loader uses, so a
    profile cannot be born invalid.
    """
    from cdda2img.recovery_profile import (
        ProfileError,
        list_profiles,
        load_profile,
        user_profiles_dir,
    )
    from cdda2img.validation import PROFILE_SCHEMA, validate

    existing = list_profiles()
    print("  Existing profiles: " + ", ".join(sorted(existing)))

    name = _text("  New profile name ([a-z0-9_-]):")
    if not name:
        print("  Cancelled.")
        return False
    name = name.strip()
    if not _is_profile_name(name):
        print(f"  Invalid name {name!r}: use only lowercase letters, digits, - and _.")
        return False
    if name in existing:
        print("  Profile already exists, please choose a different name.")
        return False

    base = _select("  Start from which profile?", sorted(existing))
    if base is None:
        print("  Cancelled.")
        return False
    try:
        template = load_profile(base)
    except ProfileError as exc:
        print(f"  {exc}")
        return False

    data: dict[str, object] = {
        k: v for k, v in template.__dict__.items() if k not in ("name", "experimental")
    }
    data["name"] = name
    data["experimental"] = False

    errors = validate(data, PROFILE_SCHEMA)
    if errors:
        print("  Refusing to write an invalid profile:")
        for e in errors:
            print(f"    - {e}")
        return False

    dest = user_profiles_dir() / f"{name}.toml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {name} — created by `cdda2img setup`, based on {base}.", ""]
    lines += [_render_scalar_value(k, data[k]) for k in sorted(data)]
    tmp = dest.with_suffix(".toml.tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(dest)

    print(f"  Wrote {dest}")
    print(f"  Edit it to taste, then use it with: cdda2img rip --profile {name}")
    return True


def _render_scalar_value(key: str, value: object) -> str:
    """One TOML scalar. Profiles are flat by design, so this is the whole writer."""
    if isinstance(value, bool):
        return f"{key} = {str(value).lower()}"
    if isinstance(value, (int, float)):
        return f"{key} = {value}"
    return f'{key} = "{value}"'


def _is_profile_name(name: str) -> bool:
    from cdda2img.validation import _is_profile_name as check

    return check(name)


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


def _print_offset_candidates(info) -> None:
    """Report every candidate offset when the sources disagree. Saves nothing.

    Picking one here would be the wizard guessing on the user's behalf, and a
    wrong read offset shifts every future rip silently.
    """
    print("  The sources DISAGREE on this drive's read offset:")
    for value, srcs in info.candidates:
        print(f"    {value:+d} samples  ({'+'.join(sorted(srcs))})")
    if info.truncated:
        print("    … (more candidates than the library could return)")
    print("  Nothing saved — add the one you trust as `read_offset` by hand.")


def _section_read_offset(device: str | None) -> bool:
    """Look the drive's read offset up in AccuDisc's table and offer to save it.

    A lookup, not a measurement — which is the whole difference between this
    section and :func:`_section_write_offset` below. The local AccurateRip
    scrape this used to query was retired on 2026-08-27.
    """
    from cdda2img.accudisc_reader import drive_offset_lookup
    from cdda2img.config import load_config
    from cdda2img.drive_info import probe_drive_inquiry, probe_drive_name

    cfg = load_config(strict=False)
    if device is None:
        device = cfg.default_device
    drive_name = probe_drive_name(device)
    if drive_name:
        print(f"  Drive: {drive_name}")
    else:
        print(f"  Drive: {device} (name probe failed)")

    inquiry = probe_drive_inquiry(device)
    if inquiry is None:
        print("  Cannot read the drive's INQUIRY strings from sysfs.")
        return False

    try:
        info = drive_offset_lookup(*inquiry)
    except RuntimeError as exc:
        print(f"  AccuDisc offset lookup failed: {exc}")
        return False

    if info is None:
        print("  Drive not found in AccuDisc's offset table.")
        return True

    if info.read_offset is None:
        _print_offset_candidates(info)
        return True

    offset = info.read_offset
    src = "+".join(sorted(info.sources)) or "unknown source"
    print(
        f"  Read offset: {offset:+d} samples"
        f"  ({src}, {info.ar_submissions} AR submission(s),"
        f" {info.ar_agree_pct}% agree)"
    )
    if info.generic_product:
        print("  NB: this product string is generic; the vendor earned the match.")

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
        device = load_config(strict=False).default_device

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
        cfg = load_config(strict=False)
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
        cfg = load_config(strict=False)
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
        cfg = load_config(strict=False)
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
