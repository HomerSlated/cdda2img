# cdda2img TUI — Design Brief

## Concept

An **audio console** aesthetic — fixed layout, no scrolling, like a hardware rack unit or
mixing desk. Think dark background, clean monospace typography, amber/green VU meters. The
recording light prototype in `src/cdda2img/test_tui.py` sets the right tone: it is a tool,
not an app.

---

## Layout: three-panel console

```
┌──────────────────────────────────────────────────────────────┐
│  HEADER: cdda2img · CD Audio to Image         [ ● REC ]      │
├─────────────────────┬────────────────────────────────────────┤
│  LEFT PANEL         │  RIGHT PANEL                           │
│  Input & Options    │  Track List                            │
│                     │                                        │
│                     │                                        │
├─────────────────────┴────────────────────────────────────────┤
│  BOTTOM PANEL: Progress / Stage / Log                        │
├──────────────────────────────────────────────────────────────┤
│  FOOTER: key bindings                                        │
└──────────────────────────────────────────────────────────────┘
```

---

## Tabs / Modes

Two top-level modes, switchable from the header or via a tab bar:

| Tab | Purpose |
|-----|---------|
| **Create** (`c`) | Pack audio files into an RBI image |
| **Extract** (`x`) | Unpack an RBI image to FLAC or raw PCM |

---

## Create tab

### Left panel: Configuration

| Element | Type | Notes |
|---------|------|-------|
| Input directory | Path field + browse | Shows resolved path; warns if empty or no audio files found |
| Mode | Toggle: `Master` / `Remaster` | Master = verbatim; Remaster = silence trim + RG |
| Loudness | Radio: `ReplayGain 2.0` / `None` | Only visible in Remaster mode |
| Strategy | Dropdown/radio: `fcfs` / `aatc` / `bech` / `ball` | With one-line description of each |
| Disc count | Read-only display | Auto-computed from strategy + file list |
| Output location | Path field | Defaults to current directory |
| **[Create Image]** | Primary action button | Disabled until input is valid |

### Right panel: Track list

| Element | Type | Notes |
|---------|------|-------|
| Track list | Scrollable table | Columns: #, filename, duration, status icon |
| Status icons | Per-row indicator | `·` pending, `→` active, `✓` done, `!` warning |
| Disc dividers | Separator rows | `── Disc 2 ──` between disc groups |
| Album / Artist | Read-only labels | Derived from tags; editable on click |
| Total duration | Summary line | `N tracks · H:MM:SS · N discs` |

### Bottom panel (during create run)

| Element | Type | Notes |
|---------|------|-------|
| Stage indicator | Label | `Transcoding` → `Silence trim` → `ReplayGain` → `Concatenating` → `Packing` |
| Current track | Label | Track name + index |
| Progress bar | Determinate bar | Per-track and overall (two bars) |
| VU meter | Stereo peak/RMS | Updated from decoded frames during transcode; L/R columns, peak hold |
| Recording light | Pulsing 3×3 grid | Red when active; off when idle |
| RG values | Live readout | Album gain, per-track gain (populates as each track is analysed) |
| Log | Scrolling text area | Warnings, trim decisions, disc assignments |

---

## Extract tab

### Left panel: Configuration

| Element | Type | Notes |
|---------|------|-------|
| Input RBI file | Path field + browse | Shows embedded metadata on selection |
| Output: FLAC + CUE | Checkbox (default on) | `--tracks` |
| Output: Raw PCM + TOC | Checkbox | `--raw` |
| Normalize | Checkbox | `--normalize`; mutually exclusive with RG tags; show warning if both would be active |
| Output directory | Path field | Defaults to current directory |
| **[Extract]** | Primary action button | |

### Right panel: RBI info panel

Shown when a file is selected, before extraction begins.

| Element | Type | Notes |
|---------|------|-------|
| Album / Artist | Labels | From embedded TOC |
| Disc N of M | Label | Multi-disc indicator |
| Track count | Label | |
| Duration | Label | |
| RG block present | Status badge | `ReplayGain ✓` or `No ReplayGain` |
| RG reference | Label | e.g. `−18.0 LUFS` |
| Track list | Table | #, title, duration, track gain (if RG block present) |

### Bottom panel (during extract run)

| Element | Type | Notes |
|---------|------|-------|
| Stage indicator | Label | `Unpacking` → `FLAC encode` → `Embedding tags` → `Writing CUE` |
| Progress | Determinate bars | Per-track and overall |
| Log | Scrolling text area | Warnings, overwrite prompts |

---

## Shared / global elements

| Element | Location | Notes |
|---------|----------|-------|
| Header | Top bar | App name, subtitle, mode tab selector, recording light |
| Footer | Bottom bar | Key bindings: `Tab` navigate, `Enter` activate, `q` quit, `?` help |
| Error overlay | Modal | Red border, dismissable; used for fatal errors (bad file, no ffmpeg) |
| Overwrite prompt | Inline in log | Not a modal — matches the CLI's existing `[y/N]` flow |
| Help overlay | Modal | `?` key; one-page key binding reference |

---

## Visual style

- **Palette**: dark background, amber accents for active states, green for success, red for
  errors and the recording light
- **VU meter**: vertical bars, L/R side by side, peak hold line, RMS fill — classic hardware
  aesthetic; green → amber → red at −6 dB, −3 dB, 0 dBFS
- **Recording light**: 3×3 pulsing red grid; placed in the header right-hand corner; pulses
  when active, off when idle
- **Typography**: monospace throughout; box-drawing characters for borders
- **Motion**: VU meters (real-time), recording light (pulse), progress bars only — no
  decorative animation
