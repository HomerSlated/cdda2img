# cdda2img

**cdda2img** is a command-line tool for creating and extracting archive images of Red Book standard CD-DA Audio CDs. It supports volume normalization, TOC generation with CD-TEXT metadata, and checksum-verified container packaging.

## Features

- Create `.cdc` container files from WAV tracklists
- Extract TOC and PCM audio from containers
- Built-in SHA-256 integrity checks
- Unified CLI interface: `cdda2img c` to create, `cdda2img x` to extract

## Requirements

- Python 3.8+
- ffmpeg
- sox

## License

GPLv3 or later

---

*Copyright © 2025 HazenSparkle*

