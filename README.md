# cdda2img

**cdda2img** is a command-line tool for creating and extracting archive images
of Red Book standard CD-DA Audio CDs. It supports volume normalization, TOC
generation with CD-TEXT metadata, and checksum-verified container packaging.

## Goals
Although the logical format of the physical media itself is clearly defined, by
both the Red Book standard and IEC 60908-1999, there is no formal definition
for a CD-DA container format, beyond a raw sector copy of the disc. This is
because the CD was not initially designed as a general purpose storage device,
so CD-DA does not contain a filesystem, such as the CD-Rom ISO 9660 introduced
years later, which can be easily constructed as a corresponding file image,
either by copying physical media, or by mastering new media to an image.

As a result, attempts to archive audio discs have resulted in several
proprietary and incompatible CD-DA archive image formats, including CUE/BIN,
IMG/CCD, and MDS/MDF, with further extensions such as SUB for subchannel data.
Although these are supported, to one degree or another, by most CD recording
software and hardware, I wanted to create something that was more open and
fully documented, partly as a learning exercise, partly just to scratch an
itch, but mostly because I found the unnecessarily convoluted and clandestine
nature of the current situation to be strange and annoying.

This is a very early WIP, so the RBI (Red Book Image) CD-DA archive image
format does not currently represent my final objective, and there is also
currently no support for reading the input from physical media. I want to
refine the format and the process first, so currently it only "masters" images
from any audio files supported by ffmpeg, and the output is simply a container
with a TOC and a single WAV file.

I'm aware that tools such as cdrdao can create BIN/CUE raw images of CD-DA
discs, but I wanted something that could also master CD-DA disc image files
from audio files, and also wanted a fully documented, open specification audio
disc image format, that contains everything in a single file, rather than up to
three separate files (e.g. IMG/CCD/SUB). In other words, my goal is to create
the ISO 9660 equivalent to CD-DA.

## Features

- Create `.rbi` container files from WAV tracklists
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

