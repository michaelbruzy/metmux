<div align="center">

# metmux

**Interactive, multi-format, command-line metadata editor with right-click integration.**

**100% local: no network, no telemetry.**

**Compatible with: Windows, macOS, Linux.**

</div>

metmux is a Python program that lets you edit the metadata of a wide variety of files through a TUI (an interactive interface in the terminal).

Its name is a contraction of *metadata multiplexer*: *met* for metadata, its core business; *mux* for multiplexer, the system that picks the right specialised engine (exiftool, ffmpeg, mutagen…) according to the file type.

Its goal is practicality: it launches with a simple right-click on any file(s) or folder(s). It is a single tool where several would normally be needed, in a language that is literal and quick to use.

<div align="center">
  <img src="https://raw.githubusercontent.com/michaelbruzy/metmux/main/assets/readme_demo.gif" alt="metmux demo" width="800">
</div>

## Installation

```sh
pipx install metmux
```

or, in an environment you manage yourself:

```sh
pip3 install metmux
```

Both bring [mutagen](https://pypi.org/project/mutagen/) (the audio engine) automatically. Two external programs unlock the other engines; pip cannot install them, and metmux tells you at startup if one is missing:

| Tool | Unlocks | Required? |
|------|---------|-----------|
| **exiftool** | images, PDF, and the dates of all files | yes |
| **ffmpeg / ffprobe** | video containers (MKV, AVI, WebM…) | recommended |

- **Windows**: `winget install -e --id OliverBetz.ExifTool` and `winget install -e --id Gyan.FFmpeg`
- **macOS**: `brew install exiftool ffmpeg`
- **Debian / Ubuntu**: `sudo apt install libimage-exiftool-perl ffmpeg`

Manual installations and other systems: [full installation guide](https://github.com/michaelbruzy/metmux#installation).

## Usage

```sh
metmux photo.jpg               # edit one file
metmux --mode=group ~/Music    # batch editing (same fields on all the files)
metmux --mode=wipe secret.jpg  # erase all metadata
```

Everything then happens at a single `>` prompt: field names, values, date shifts, wipe, undo, rename. `metmux --help` lists the modes, and the `help` panel inside the session documents every command.

The recommended way to launch metmux is the right-click integration (Windows, macOS, Linux). It relies on the scripts of the [`integrations/` folder](https://github.com/michaelbruzy/metmux/tree/main/integrations), which live in the repository rather than in this package.

<div align="center">
  <img src="https://raw.githubusercontent.com/michaelbruzy/metmux/main/assets/readme_integrations.png" alt="Right-click integration on macOS, Windows and Linux" width="800">
</div>

## Supported formats

Images and RAW (jpg, png, webp, heic, cr3, nef, dng…), PDF, audio (mp3, flac, ogg, opus, m4a…), video (mp4, mov, mkv, avi, webm…), Office (docx, xlsx, pptx, odt…), EPUB, CBZ, Jupyter notebooks, playlists, e-mail, property lists, SQLite databases, and more. Whatever the format, the file name and the file-system dates stay editable.

The full table, engine by engine: [supported formats](https://github.com/michaelbruzy/metmux#supported-formats).

## Documentation

The complete documentation lives on GitHub: [English](https://github.com/michaelbruzy/metmux#readme) · [Français](https://github.com/michaelbruzy/metmux/blob/main/README.fr.md).

## License

[GNU GPL v3.0 or later](https://github.com/michaelbruzy/metmux/blob/main/LICENSE) © 2026 Michaël Bruzy.
