#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# metmux — metadata multiplexer.
# Copyright (C) 2026 Michaël Bruzy
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.
"""metmux — interactive, multi-format, command-line metadata editor with right-click integration. Available on Windows, macOS and Linux.

Reads, rewrites or wipes the metadata of one or more files, entirely from
the keyboard. Each file is routed by its extension to the engine that knows
the format (exiftool, ffmpeg, mutagen, or the standard library alone).
Designed for a right-click from the file manager; equally usable in a shell.

Usage   : python3 metmux.py [--mode={single|group|ask|wipe}] file [file ...]
          Full options: --help. Formats and right-click integrations: README.md.
License : GPL-3.0-or-later (see LICENSE).
"""

import atexit, base64, codecs, datetime, io, json, os, platform, re, select, shutil, signal, subprocess, sys, tempfile, threading, time, zipfile
import email, email.generator, plistlib, sqlite3   # stdlib engines (eml, plist, sqlite)
import xml.etree.ElementTree as ET
from email import policy as _email_policy
from pathlib import Path

try:
    import msvcrt                                 # Windows console keyboard; POSIX has no such module
except ImportError:                               # (the paste guard reads the console buffer through it)
    msvcrt = None

__version__ = "1.0.0"

# Homebrew's exiftool, found even when launched from Finder with a bare PATH
# (Apple Silicon then Intel prefix).
_EXIFTOOL_FALLBACK_PATHS = ("/opt/homebrew/bin/exiftool", "/usr/local/bin/exiftool")

EXIFTOOL = (shutil.which("exiftool")
            or next((p for p in _EXIFTOOL_FALLBACK_PATHS
                     if Path(p).exists()), "exiftool"))
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

DIM, RED, BOLD, RESET = "\033[2m", "\033[31m", "\033[1m", "\033[0m"
YELLOW = "\033[93m"           # bright yellow: informational messages
UNDERLINE = "\033[4m"
NBSP = "\u00a0"                 # non-breaking space: see the Windows padding quirk in render()
PROMPT_CELLS = "> "             # the cells the prompt takes on screen (Ctrl-U walks back over them)
PROMPT = f"{BOLD}>{RESET} "     # every prompt in metmux is this constant (Ctrl-U's math relies on it)
# Achromatic greys (256-colour ramp).
GREY  = "\033[38;5;245m"        # medium grey: read-only fields
FAINT = "\033[38;5;239m"        # darker grey: missing values "(empty)"
# \033[3J also drops the scrollback (same policy as render()): Terminal.app pushes a
# cleared screen INTO the scrollback, so without it the previous frame stayed readable
# by scrolling up.
CLEAR = "\033[2J\033[3J\033[H"


def clear_screen():
    """Blank the screen for a view drawn outside render() (help, focus, wipe, summary).
       Drops _LAST_FRAME too: a resize must not redraw the main view over these screens;
       the next render() re-arms the live redraw."""
    global _LAST_FRAME
    _LAST_FRAME = None
    print(CLEAR, end="")


# ============================================================
#  Cross-OS compatibility
# ============================================================

_WIN_CONSOLE = platform.system() == "Windows"   # the two console quirks below are Windows-only


def enable_windows_ansi():
    """Enable ANSI escape handling on Windows (macOS/Linux handle it natively)."""
    if not _WIN_CONSOLE:
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        handle = k.GetStdHandle(-11)              # -11 = standard output (stdout)
        mode = ctypes.c_ulong()
        if k.GetConsoleMode(handle, ctypes.byref(mode)):
            # 0x0004 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
            k.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass  # if it fails, colours show as plain text, without breaking the rest


def enable_windows_vt_input():
    """Ask the Windows console for VT input, so it may bracket pastes (PASTE_BEGIN…END)
       like a Unix terminal — the only certain paste signal. Nothing is built on top of it:
       refused, or granted without markers ever coming, the reader falls back on the arrival
       rule. Returns the previous mode, to be restored on exit."""
    if not _WIN_CONSOLE:
        return None
    try:
        import ctypes
        k = ctypes.windll.kernel32
        handle = k.GetStdHandle(-10)                  # -10 = standard input (stdin)
        mode = ctypes.c_ulong()
        if not k.GetConsoleMode(handle, ctypes.byref(mode)):
            return None                               # not a console (a pipe): nothing to ask
        # 0x0200 = ENABLE_VIRTUAL_TERMINAL_INPUT, asked for. 0x0010 (MOUSE) and 0x0008 (WINDOW)
        # are turned OFF: they only queue events metmux has no use for, which would wake the
        # kernel wait for nothing. Every other flag is left as it was; restored on close.
        if not k.SetConsoleMode(handle, (mode.value | 0x0200) & ~0x0010 & ~0x0008):
            return None
        return mode.value
    except Exception:
        return None                                   # old console: no VT input


def restore_windows_console_mode(mode):
    if mode is None or not _WIN_CONSOLE:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(-10), mode)
    except Exception:
        pass


def open_externally(path):
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", str(path)])
        elif system == "Windows":
            os.startfile(str(path))
        else:
            subprocess.run(["xdg-open", str(path)])
        return True
    except Exception:
        return False


# ============================================================
#  Startup configuration and cleanup of temporaries
# ============================================================

DEFAULT_LANG = "en"
DEFAULT_DATE_ORDER = "DMY"      # internal order for ambiguous dates (config date_format: eu→DMY, us→MDY)


def load_config(cfg_path=None):
    """Reads config.json next to the script: `lang` ("fr"/"en") and `date_format`
       ("eu" = day/month · "us" = month/day; case-insensitive).
       Absent/unreadable/malformed/unknown key → default (en, eu), without crashing."""
    global DEFAULT_LANG, DEFAULT_DATE_ORDER
    if cfg_path is None:
        cfg_path = Path(__file__).resolve().parent / "config.json"
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as f:   # -sig: tolerates a BOM (Notepad)
            cfg = json.load(f)
    except Exception:
        return
    if isinstance(cfg, dict):
        if cfg.get("lang") in ("fr", "en"):
            DEFAULT_LANG = cfg["lang"]
        fmt = cfg.get("date_format")
        if isinstance(fmt, str):
            # public regional name → internal order; case-insensitive.
            order = {"eu": "DMY", "us": "MDY"}.get(fmt.strip().lower())
            if order:
                DEFAULT_DATE_ORDER = order


def save_config(cfg_path=None):
    """Rewrites config.json from the current globals: `lang` (DEFAULT_LANG) and
       `date_format` (DEFAULT_DATE_ORDER → "eu"/"us"). Any other key already present
       is preserved. Atomic, best-effort write: returns True if the file was written,
       False otherwise; never raises — a read-only disk must not break the session."""
    if cfg_path is None:
        cfg_path = Path(__file__).resolve().parent / "config.json"
    cfg_path = Path(cfg_path)
    try:
        with open(cfg_path, "r", encoding="utf-8-sig") as f:   # -sig: tolerates a BOM (Notepad)
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    data["lang"] = DEFAULT_LANG
    data["date_format"] = "us" if DEFAULT_DATE_ORDER == "MDY" else "eu"
    try:
        tmp = cfg_path.with_name(cfg_path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, cfg_path)
        return True
    except Exception:
        return False


_TEMP_FILES = []                # extracted binaries, removed on exit

@atexit.register
def _cleanup_temp():
    for f in _TEMP_FILES:
        try:
            Path(f).unlink(missing_ok=True)
        except Exception:
            pass


def _replace_keep_mode(tmp, path):
    """Atomic replace that PRESERVES the target's permission bits. The temporary is
       created with the umask default (typically 0644): replacing without copying the
       mode would silently widen a private file (0600) to world-readable on a mere
       metadata edit. Best-effort (never blocks the write itself)."""
    try:
        shutil.copymode(path, tmp)
    except OSError:
        pass
    Path(tmp).replace(path)


def _fresh_tmp(path):
    """Temporary sibling for the atomic rewrites that NEVER collides with an existing
       file: "X.ext.tmp" may already exist as the USER'S own file (editor leftover,
       manual backup) — writing to it, or deleting it when the write fails, would
       destroy an unrelated file. Numbered variants until a free name; every candidate
       keeps the ".tmp" suffix so folder expansion still excludes it (_is_sidecar)."""
    cand = path.with_suffix(path.suffix + ".tmp")
    n = 1
    while cand.exists():
        cand = path.with_suffix(f"{path.suffix}.{n}.tmp")
        n += 1
    return cand


def _sibling_tmp(path):
    """Same net as _fresh_tmp, but KEEPING the original extension: ffmpeg and mutagen infer
       the container from it, so the "X.mkv.tmp" _fresh_tmp produces would break them. The
       marker goes before the suffix instead ("X.metmux_tmp.mkv"), numbered for the same
       anti-collision reason, and carried by every candidate so folder expansion still
       skips it (_is_sidecar)."""
    cand = path.with_name(f"{path.stem}.metmux_tmp{path.suffix}")
    n = 1
    while cand.exists():
        cand = path.with_name(f"{path.stem}.metmux_tmp{n}{path.suffix}")
        n += 1
    return cand

# --- Routing by extension ---
AUDIO_EXTS = {"mp3", "flac", "ogg", "oga", "opus", "wma", "aiff", "aif",
              "ape", "wv", "mpc", "tta", "ofr"}
VIDEO_ONLY_FFMPEG = {"mkv", "avi", "flv", "wmv", "asf", "webm", "mka"}
OOXML_EXTS = {"docx", "xlsx", "pptx"}
ODF_EXTS = {"odt", "ods", "odp"}
EPUB_EXTS = {"epub"}
IPYNB_EXTS = {"ipynb"}
CBZ_EXTS = {"cbz"}
PLAYLIST_EXTS = {"m3u", "m3u8"}
# ── stdlib extensions (0 pip dependency) ──────────────────────────────
PLIST_EXTS = {"plist", "webloc", "mobileconfig"}
EML_EXTS = {"eml"}
MBOX_EXTS = {"mbox"}
CUE_EXTS = {"cue"}
GEOJSON_EXTS = {"geojson"}
HAR_EXTS = {"har"}
SQLITE_EXTS = {"sqlite", "sqlite3", "db"}
KMZ_EXTS = {"kmz"}
MUSICXML_EXTS = {"musicxml"}
TCX_EXTS = {"tcx"}
# Application packages (zip) — read-only: editing = invalidated signature.
ARCHIVE_EXTS = {"jar", "war", "ear", "apk", "xpi", "ipa"}
# Linux has no userspace API to set a file's creation date (btime): FileCreateDate is
# editable only where the OS metmux runs on can write it. See _write_btime().
_FILE_CREATE_WRITABLE = platform.system() in ("Darwin", "Windows")
# EDITABLE file dates written VIA EXIFTOOL. Only FileModifyDate: exiftool sets the mtime
# durably on every platform. FileAccessDate (atime) is READ-ONLY (volatile — any read resets
# it — and exiftool cannot set it durably). FileCreateDate is editable too where the OS allows
# (below), but metmux writes it ITSELF, never through exiftool.
FILE_DATE_TAGS = ("FileModifyDate",)
# File dates are absolute instants (mtime/btime), UNLIKE the naive metadata dates for which
# metmux stores no timezone. A UTC offset typed on one of these (23:15:00+03:00) is therefore
# meaningful — it fixes the instant — and must survive input parsing through to the writer
# (exiftool for the mtime, the OS syscall for the btime), which both honour it.
FILE_INSTANT_DATE_TAGS = frozenset({"FileModifyDate", "FileCreateDate"})
FILE_BASE_TAGS = (("FileName",) + FILE_DATE_TAGS
                  + (("FileCreateDate",) if _FILE_CREATE_WRITABLE else ()))
# The bulk "dates" command never targets these: the volatile atime is read-only, the creation
# date is the file's birth — set deliberately, never as collateral of a bulk shift. (cmd_dates'
# `tag in w` guard already excludes atime; this covers the btime, writable on macOS/Windows.)
BULK_DATE_SKIP = frozenset({"FileAccessDate", "FileCreateDate"})
# READ-ONLY external data: visible but not editable (≠ FILE_BASE_TAGS). FileSize/FileType/
# Directory/FileAccessDate come from exiftool for non-exiftool engines. FileCreateDate appears
# here ONLY where we cannot write it (Linux): shown read-only. Where we CAN write it, it lives
# in FILE_BASE_TAGS above (editable) instead. Its VALUE comes from os.stat on macOS/Linux —
# exiftool does not expose the btime there; on Windows exiftool reads it natively, and
# _inject_create_date only fills the gap when it did not.
FILE_EXTRA_TAGS = (("FileSize", "FileType", "Directory", "FileAccessDate")
                   + (() if _FILE_CREATE_WRITABLE else ("FileCreateDate",)))

SUGGESTED = {
    # ── Non-exiftool engines ────────────────────────────────────────
    "mutagen": (
        "title", "artist", "album", "albumartist", "composer",
        "date", "originaldate", "tracknumber", "discnumber",
        "genre", "copyright", "language", "conductor", "lyricist",
        "performer", "isrc", "bpm", "encodedby", "compilation",
    ),
    "ffmpeg": (
        "title", "artist", "album", "date", "year", "genre",
        "comment", "description", "composer", "encoder",
        "language", "copyright", "publisher", "show", "episode",
        "season", "synopsis", "network", "director",
    ),
    "ooxml": (
        "Title", "Subject", "Creator", "Keywords", "Description",
        "LastModifiedBy", "Revision", "Category", "Manager",
        "Company", "ContentStatus", "Language", "Comments",
    ),
    "odf": (
        "Title", "Subject", "Creator", "Keywords", "Description",
        "Language", "InitialCreator", "Generator",
    ),
    "epub": (
        "Title", "Creator", "Description", "Publisher", "Subject",
        "Language", "Date", "Rights", "Identifier", "Contributor",
        "Coverage", "Source", "Type", "Format",
    ),
    "ipynb": (
        "Title", "Authors",
    ),

    # ── exiftool engine: whitelist by category ──────────────────
    "exiftool_image": (
        # Descriptive
        "Title", "Description", "Subject", "Keywords", "Caption",
        "Headline", "Comment", "ImageDescription", "UserComment",
        "ObjectName", "SpecialInstructions",
        # People / rights
        "Creator", "Author", "Artist", "By-line", "By-lineTitle",
        "Credit", "Source", "CopyrightNotice", "Rights", "Copyright",
        "Contact", "Writer-Editor", "OwnerName",
        # Location
        "City", "State", "Province-State", "Country", "Country-PrimaryLocationName",
        "Country-PrimaryLocationCode", "Sub-location", "Location",
        "GPSLatitude", "GPSLongitude", "GPSAltitude",
        "GPSLatitudeRef", "GPSLongitudeRef", "GPSAltitudeRef",
        # Categorisation
        "Category", "SupplementalCategories", "Urgency", "Rating", "Label",
        # Dates
        "DateTimeOriginal", "CreateDate", "ModifyDate", "DateCreated",
        "DigitalCreationDate", "DigitalCreationTime",
        # Software
        "Software",
    ),
    "exiftool_pdf": (
        "Title", "Author", "Subject", "Keywords", "Description",
        "Creator", "Producer", "Publisher", "Copyright",
        "CreateDate", "ModifyDate", "MetadataDate",
        "Language", "Trapped",
    ),
    # Only tags exiftool can actually WRITE. "Synopsis", "Cast", "Studio",
    # "ContentRating" and "Lyricist" exist in its READ tables only; "Show", "EpisodeID"
    # and "Network" are FFmpeg names it does not know; "Episode"/"Season" resolve to XMP
    # structures, not scalars; "LocationName" needs a group prefix or it is silently
    # dropped. Offering any of them means a write that can only fail.
    "exiftool_video": (
        # Descriptive
        "Title", "Description", "Comment", "LongDescription", "Genre",
        "Keywords", "Category", "Information",
        # People / production
        "Artist", "Author", "Director", "Producer", "Composer",
        "Performer", "Writer", "Publisher",
        # Programme / season
        "TVShow", "TVEpisode", "TVEpisodeID", "TVSeason", "TVNetworkName",
        "Album", "TrackNumber",
        # Rights
        "Copyright", "Rating", "ParentalRating",
        # Location / GPS
        "GPSCoordinates", "Location",
        # Internal dates
        "Date", "Year", "MediaCreateDate", "MediaModifyDate",
        "TrackCreateDate", "TrackModifyDate",
        "CreateDate", "ModifyDate",
        # Software
        "Encoder", "EncodedBy", "Language",
    ),
    "exiftool_audio": (
        "Title", "Artist", "Album", "AlbumArtist", "Composer",
        "Performer", "Conductor", "OriginalLyricist",
        "Date", "Year", "Track", "TrackNumber", "DiscNumber",
        "Genre", "Comment", "Description", "Lyrics", "LongDescription",
        "Copyright", "Publisher", "Label", "ISRCCode", "BeatsPerMinute",
        "Rating", "Compilation", "Grouping",
        "EncodedBy", "Encoder", "Language",
    ),
    "exiftool_other": (
        # For unknown / text / office formats going through exiftool
        "Title", "Author", "Creator", "Subject", "Keywords",
        "Description", "Comment", "Copyright", "Rights",
        "CreateDate", "ModifyDate", "Date", "Language",
        "Publisher", "Producer",
    ),
}

FR = {
    "Title": "Titre", "Subtitle": "Sous-titre",
    "Description": "Description", "Comment": "Commentaire",
    "Keywords": "Mots-clés", "Subject": "Sujet",
    "Category": "Catégorie", "Rating": "Note",
    "Language": "Langue", "Copyright": "Copyright", "Rights": "Droits",
    "Artist": "Artiste", "AlbumArtist": "Artiste de l'album",
    "Author": "Auteur", "Authors": "Auteurs",
    "Creator": "Créateur", "Composer": "Compositeur",
    "Publisher": "Éditeur", "Producer": "Producteur",
    "Director": "Réalisateur", "Performer": "Interprète",
    "Conductor": "Chef d'orchestre", "Lyricist": "Parolier",
    "Writer": "Scénariste", "Engineer": "Ingénieur du son",
    "Contributor": "Contributeur",
    "By-line": "Signature", "Credit": "Crédit",
    "LastModifiedBy": "Dernier modificateur",
    "Album": "Album", "TrackNumber": "Numéro de piste",
    "DiscNumber": "Numéro de disque", "Genre": "Genre",
    "Year": "Année", "Date": "Date", "Lyrics": "Paroles",
    "BPM": "Tempo", "Compilation": "Compilation",
    "RecordingTime": "Date d'enregistrement",
    "EncoderSettings": "Encodeur", "Encoder": "Encodeur",
    "SampleRate": "Fréquence d'échantillonnage",
    "AudioBitrate": "Débit audio", "ChannelMode": "Canaux",
    "AudioChannels": "Nombre de canaux", "AudioFormat": "Format audio",
    "Make": "Fabricant", "Model": "Modèle",
    "LensModel": "Objectif", "LensMake": "Fabricant d'objectif",
    "FNumber": "Ouverture", "Aperture": "Ouverture",
    "ExposureTime": "Vitesse d'obturation", "ShutterSpeed": "Vitesse d'obturation",
    "ISO": "ISO", "FocalLength": "Focale",
    "FocalLengthIn35mmFormat": "Focale équivalent 35 mm",
    "Flash": "Flash", "WhiteBalance": "Balance des blancs",
    "ExposureMode": "Mode d'exposition", "MeteringMode": "Mesure",
    "ExposureCompensation": "Correction d'exposition",
    "Orientation": "Orientation", "ColorSpace": "Espace colorimétrique",
    "ImageWidth": "Largeur", "ImageHeight": "Hauteur",
    "ImageSize": "Dimensions", "Megapixels": "Mégapixels",
    "Software": "Logiciel", "ImageDescription": "Description de l'image",
    "ImageUniqueID": "Identifiant unique",
    "GPSLatitude": "Latitude", "GPSLongitude": "Longitude",
    "GPSAltitude": "Altitude", "GPSPosition": "Position GPS",
    "Location": "Lieu", "City": "Ville",
    "State": "Région", "Country": "Pays",
    "Duration": "Durée", "FrameRate": "Images par seconde",
    "VideoFrameRate": "Images par seconde",
    "VideoCodec": "Codec vidéo", "AudioCodec": "Codec audio",
    "CompressorName": "Compresseur", "MediaType": "Type de média",
    "Series": "Série", "Number": "Numéro", "Penciller": "Dessinateur",
    "Web": "Site web", "TrackCount": "Nombre de pistes",
    "Show": "Émission", "Season": "Saison", "Episode": "Épisode",
    "Network": "Chaîne", "Chapters": "Chapitres",
    # exiftool names (QuickTime): same fields as the FFmpeg ones just above, which is
    # why they share their FR label — the user reads "Saison" on an MP4 as on an MKV.
    "TVShow": "Émission", "TVSeason": "Saison", "TVEpisode": "Épisode",
    "TVEpisodeID": "Identifiant d'épisode", "TVNetworkName": "Chaîne",
    "LongDescription": "Synopsis", "ParentalRating": "Classification",
    "OriginalLyricist": "Parolier", "ISRCCode": "ISRC",
    "BeatsPerMinute": "Tempo",
    "PageCount": "Nombre de pages", "PageSize": "Format de page",
    "PDFVersion": "Version PDF", "Linearized": "Linéarisé",
    "Encrypted": "Chiffré", "WordCount": "Nombre de mots",
    "CharacterCount": "Nombre de caractères",
    "ParagraphCount": "Nombre de paragraphes",
    "LineCount": "Nombre de lignes",
    "Application": "Application", "AppVersion": "Version de l'application",
    "Company": "Société", "Manager": "Responsable", "Template": "Gabarit",
    "RevisionNumber": "Numéro de révision",
    "TotalEditTime": "Temps d'édition total",
    "Identifier": "Identifiant",
    "CreateDate": "Date de création (interne)",
    "ModifyDate": "Date de modification (interne)",
    "DateTimeOriginal": "Date de prise de vue",
    "DateTimeDigitized": "Date de numérisation",
    "DateCreated": "Date de création",
    "CreationDate": "Date de création (fichier)", "ModDate": "Date de modification",
    "MetadataDate": "Date des métadonnées",
    "HistoryWhen": "Date d'historique",
    "TrackCreateDate": "Date de création (piste)",
    "TrackModifyDate": "Date de modification (piste)",
    "MediaCreateDate": "Date de création (média)",
    "MediaModifyDate": "Date de modification (média)",
    "GPSDateTime": "Date GPS", "GPSDateStamp": "Date GPS (jour)",
    "SubSecDateTimeOriginal": "Date de prise de vue (précise)",
    "SubSecCreateDate": "Date de création (précise)",
    "SubSecModifyDate": "Date de modification (précise)",
    "FileName": "Nom du fichier", "Directory": "Dossier",
    "FileSize": "Taille", "FileType": "Type de fichier",
    "FileTypeExtension": "Extension", "MIMEType": "Type MIME",
    "FileCreateDate": "Date de création (fichier)",
    "FileModifyDate": "Date de modification (fichier)",
    "FileAccessDate": "Date d'accès (fichier)",
    "FileInodeChangeDate": "Date de changement inode",
    "FilePermissions": "Permissions",
    # Lowercase variants (Mutagen/FFmpeg)
    "title": "Titre", "artist": "Artiste", "album": "Album",
    "albumartist": "Artiste de l'album", "album_artist": "Artiste de l'album",
    "composer": "Compositeur", "tracknumber": "Numéro de piste",
    "track": "Numéro de piste", "discnumber": "Numéro de disque",
    "disc": "Numéro de disque", "date": "Date", "year": "Année",
    "genre": "Genre", "comment": "Commentaire", "lyrics": "Paroles",
    "performer": "Interprète", "copyright": "Copyright",
    "language": "Langue", "encoder": "Encodeur",
    "encodedby": "Encodé par", "description": "Description",
    "organization": "Organisation", "isrc": "ISRC", "bpm": "Tempo",
    # Lowercase mutagen aliases (mp3, flac, ogg…)
    "originaldate": "Date d'origine",
    "OriginalDate": "Date d'origine",   # canonical form (via _THEME_TAGS) of the same tag
    "conductor": "Chef d'orchestre",
    "lyricist": "Parolier",
    "compilation": "Compilation",
}

FR.update({
    # plist / Apple packages
    "URL": "URL", "PayloadDisplayName": "Nom du profil",
    "PayloadDescription": "Description du profil",
    "PayloadOrganization": "Organisation (profil)", "PayloadIdentifier": "Identifiant du profil",
    "PayloadVersion": "Version du profil", "PayloadType": "Type de charge",
    "CFBundleName": "Nom du bundle", "CFBundleDisplayName": "Nom affiché",
    "CFBundleIdentifier": "Identifiant du bundle",
    "CFBundleShortVersionString": "Version (courte)", "CFBundleVersion": "Version (build)",
    "NSHumanReadableCopyright": "Copyright",
    # e-mail / mbox
    "From": "Expéditeur", "To": "Destinataire", "Cc": "Copie",
    "ReplyTo": "Répondre à", "Comments": "Commentaires", "Sender": "Émetteur",
    "MessageID": "Identifiant du message", "MessageCount": "Nombre de messages",
    # geojson
    "Name": "Nom", "FeatureCount": "Nombre d'entités", "BBox": "Cadre englobant",
    "CRS": "Système de coordonnées",
    # har
    "CreatorVersion": "Version du créateur", "Browser": "Navigateur",
    "HARVersion": "Version HAR", "EntryCount": "Nombre d'échanges",
    # sqlite
    "ApplicationID": "Identifiant d'application", "UserVersion": "Version du schéma",
    "SQLiteVersion": "Version SQLite", "Encoding": "Encodage",
    "TableCount": "Nombre de tables",
    # musicxml
    "EncodingDate": "Date d'encodage",
    # tcx
    "Sport": "Sport", "Device": "Appareil",
    # application packages
    "Version": "Version", "Vendor": "Fournisseur", "SpecTitle": "Titre (spécification)",
    "CreatedBy": "Créé par", "BuiltBy": "Compilé par", "MainClass": "Classe principale",
})

# ============================================================
#  Interface localisation (full FR/EN)
# ============================================================
# FR (above) localises the field LABELS; this table localises the interface
# "chrome": menus, prompts, notices, help, summaries. tr(key, **kw) returns the
# string for the current language (DEFAULT_LANG), formatted with kw. Command WORDS
# stay international (all/in/edit/eu/us/undo/wipe/dates/single/n/p/q…) so muscle
# memory carries across languages; the only francophone synonym we add is "aide"
# (doubles "help"), shown in the FR hint but accepted in both languages.

# Theme headers of the unified view. The internal theme KEYS stay English
# (THEME_ORDER, _THEME_TAGS, tests) — only the on-screen header is localised, at
# render time. Also covers the media labels returned by media_label().
HEADER_FR = {
    "File": "Fichier",
    "Description": "Description",
    "People & rights": "Personnes & droits",
    "Location": "Lieu",
    "Dates": "Dates",
    "Image": "Image", "Video": "Vidéo", "Audio": "Audio", "Technical": "Technique",
}

_UI = {
    # --- render / views ---
    "no_field":   {"en": "(no field)", "fr": "(aucun champ)"},
    "help_hint":  {"en": 'type "help" for the list of commands',
                   "fr": 'tapez « help » ou « aide » pour la liste des commandes'},
    # --- fmt() ---
    "empty":      {"en": "(empty)", "fr": "(vide)"},
    "binary_fmt": {"en": "[binary, {n} {unit}]", "fr": "[binaire, {n} {unit}]"},
    "text_fmt":   {"en": "[text, {n} chars]", "fr": "[texte, {n} caractères]"},
    # --- help panel ---
    # Section headings. Two of them carry the condition of what they hold, so the rows
    # underneath stay short (the panel is the SAME in every mode: nothing appears or
    # disappears, a command that only lives somewhere says where).
    "help_sec_fields":  {"en": "Fields", "fr": "Champs"},
    "help_sec_dates":   {"en": "Dates", "fr": "Dates"},
    "help_sec_views":   {"en": "Views", "fr": "Vues"},
    "help_sec_nav":     {"en": "Navigate", "fr": "Naviguer"},
    "help_sec_nav_if":  {"en": "several files", "fr": "plusieurs fichiers"},
    "help_sec_undo":    {"en": "Erase, undo", "fr": "Effacer, annuler"},
    "help_sec_conf":    {"en": "Settings", "fr": "Réglages"},
    "help_sec_conf_if": {"en": "saved in config.json", "fr": "enregistrés dans config.json"},
    "help_sec_session": {"en": "Session", "fr": "Session"},
    # Typography: a heading takes a capital, a label (a row's description, a caption)
    # takes none and ends without a full stop, a sentence takes both.
    "help_field":   {"en": "Field", "fr": "Champ"},     # placeholders, in the typed forms
    "help_value":   {"en": "value", "fr": "valeur"},
    "help_nothing": {"en": "(nothing)", "fr": "(rien)"},
    "help_forms":   {"en": "recognised forms:", "fr": "formats reconnus :"},
    "help_edit":    {"en": "edit", "fr": "modifier"},   # also the caption of its forms
    "help_append":  {"en": "append to a list (Keywords, Subject…)",
                     "fr": "ajouter à une liste (Mots-clés, Sujet…)"},
    "help_clear":   {"en": "clear", "fr": "vider"},     # also the caption of its forms
    "help_focus":   {"en": "show the whole value, open the image",
                     "fr": "voir toute la valeur, ouvrir l'image"},
    "help_forms_edit":  {"en": "Title : Journey    Title Journey    title journey    t Journey",
                         "fr": "Titre : Voyage    Titre Voyage    titre voyage    t Voyage"},
    "help_forms_clear": {"en": "Title :    Title (space)    t :    t (space)",
                         "fr": "Titre :    Titre (espace)    t :    t (espace)"},
    "help_dates_abs": {"en": "overwrite all the present dates",
                       "fr": "remplacer toutes les dates présentes"},
    "help_dates_rel": {"en": "shift all the dates (d/h/m/s)",
                       "fr": "décaler toutes les dates (d/h/m/s)"},
    "help_dates_one": {"en": "shift a single date", "fr": "décaler une seule date"},
    "help_cap_date":    {"en": "date", "fr": "date"},
    "help_cap_sep":     {"en": "separator", "fr": "séparateur"},
    "help_cap_time":    {"en": "time", "fr": "heure"},
    "help_cap_compact": {"en": "compact", "fr": "compact"},
    "help_all":     {"en": "every field", "fr": "tous les champs"},
    "help_in":      {"en": "only the filled-in fields", "fr": "seulement les champs renseignés"},
    "help_editv":   {"en": "only the editable fields", "fr": "seulement les champs modifiables"},
    "help_next":    {"en": "next file", "fr": "fichier suivant"},
    "help_prev":    {"en": "previous file", "fr": "fichier précédent"},
    "help_group":   {"en": "edit the whole batch at once (group view)",
                     "fr": "éditer tout le lot d'un coup (vue groupe)"},
    "help_single":  {"en": "edit the batch file by file (individual view)",
                     "fr": "éditer le lot fichier par fichier (vue individuelle)"},
    "help_paste":    {"en": "To paste a whole block of text, enter the field name first.",
                      "fr": "Pour coller un bloc de texte entier, entrez le nom du champ d'abord."},
    "help_killline": {"en": "erase the line being typed, however long",
                      "fr": "effacer la ligne en cours de saisie, quelle que soit sa longueur"},
    "help_wipe":     {"en": "erase all the metadata", "fr": "effacer toutes les métadonnées"},
    "help_undo":     {"en": "undo the last change", "fr": "annuler le dernier changement"},
    "help_undo_all": {"en": "undo every change", "fr": "annuler tous les changements"},
    "help_instant":  {"en": "Every change is written instantly, and undoable until metmux closes.",
                      "fr": "Tout changement est écrit instantanément, et annulable jusqu'à la fermeture."},
    "help_lang":      {"en": "display language", "fr": "langue de l'affichage"},
    "help_dateorder": {"en": "date order: eu = day/month, us = month/day",
                       "fr": "ordre des dates : eu = jour/mois, us = mois/jour"},
    "help_help":  {"en": "show this help", "fr": "afficher l'aide"},
    "help_quit":  {"en": "quit metmux", "fr": "quitter metmux"},
    "help_close": {"en": "Press Enter to close.", "fr": "Entrée pour fermer."},
    # --- session_single ---
    "cannot_read_file": {"en": "Cannot read file.", "fr": "Lecture du fichier impossible."},
    "field_not_found":  {"en": "Field not found: {focus}", "fr": "Champ introuvable : {focus}"},
    "focus_editable":   {"en": "Enter = back · New value = edit.",
                         "fr": "Entrée = retour · Nouvelle valeur = modifier."},
    "focus_readonly":   {"en": "Enter to go back (not editable).",
                         "fr": "Entrée pour revenir (non modifiable)."},
    "unreadable_date":  {"en": "Unreadable date.", "fr": "Date illisible."},
    "write_failed":     {"en": "Write failed.", "fr": "Échec de l'écriture."},
    "epub_id_protected": {"en": "An EPUB's unique identifier cannot be cleared.",
                         "fr": "L'identifiant unique d'un EPUB ne peut pas être vidé."},
    "degraded":         {"en": "{eng} missing — external data only · {how}",
                         "fr": "{eng} manquant — données externes seulement · {how}"},
    "mismatch":         {"en": "extension .{ext}, content .{real}",
                         "fr": "extension .{ext}, contenu .{real}"},
    "file_position":    {"en": "file {i}/{n}", "fr": "fichier {i}/{n}"},
    "footer_view":      {"en": "view", "fr": "vue"},
    "footer_nav":       {"en": "navigate", "fr": "naviguer"},
    # input() fallback only (no raw reader): the arrows are not read there, so the footer
    # shows the typed keys alone rather than promising a gesture that does nothing.
    "nav_footer":       {"en": "p (previous)   n (next)", "fr": "p (précédent)   n (suivant)"},
    "nav_footer_arrows": {"en": "← (p)   → (n)", "fr": "← (p)   → (n)"},
    "group_only_multi": {"en": '"g"/"group": only with multiple files.',
                         "fr": "« g »/« group » : seulement avec plusieurs fichiers."},
    "lang_not_saved":   {"en": "Language changed — config.json not saved.",
                         "fr": "Langue changée — config.json non enregistré."},
    "date_format_set":  {"en": "Date format: {label}.", "fr": "Format de date : {label}."},
    "date_format_not_saved": {"en": "Date format: {label} — config.json not saved.",
                              "fr": "Format de date : {label} — config.json non enregistré."},
    "date_fmt_us":      {"en": "US (month/day)", "fr": "US (mois/jour)"},
    "date_fmt_eu":      {"en": "EU (day/month)", "fr": "EU (jour/mois)"},
    "change_undone":    {"en": "Change undone.", "fr": "Changement annulé."},
    "nothing_undo":     {"en": "Nothing to undo.", "fr": "Rien à annuler."},
    "all_undone":       {"en": "All changes undone.", "fr": "Tous les changements annulés."},
    "metadata_wiped":   {"en": "Metadata wiped.", "fr": "Métadonnées effacées."},
    "wipe_failed":      {"en": "Wipe failed.", "fr": "Échec de l'effacement."},
    "no_date_present":  {"en": "No date present.", "fr": "Aucune date présente."},
    "shift_no_effect":  {"en": "Shift had no effect: dates unchanged.",
                         "fr": "Décalage sans effet : dates inchangées."},
    "dates_updated_err":{"en": "{touched} date(s) updated, {errors} failure(s).",
                         "fr": "{touched} date(s) mise(s) à jour, {errors} échec(s)."},
    "dates_updated":    {"en": "{touched} date(s) updated.",
                         "fr": "{touched} date(s) mise(s) à jour."},
    "unknown_field":    {"en": "Unknown field: {name}", "fr": "Champ inconnu : {name}"},
    "cannot_open_binary": {"en": "Cannot open this binary.",
                           "fr": "Impossible d'ouvrir ce binaire."},
    "field_not_editable": {"en": "{tag}: field not editable.",
                           "fr": "{tag} : champ non modifiable."},
    # --- session_group ---
    "cannot_read_files": {"en": "Cannot read one or more files.",
                          "fr": "Lecture d'un ou plusieurs fichiers impossible."},
    "single_only_multi": {"en": '"s"/"single": only with multiple files.',
                          "fr": "« s »/« single » : seulement avec plusieurs fichiers."},
    "group_tag":     {"en": "group", "fr": "groupe"},
    "group_title":   {"en": "Group", "fr": "Groupe"},
    "n_files":       {"en": "{n} file(s): {preview}", "fr": "{n} fichier(s) : {preview}"},
    "wipe_confirm_inline": {"en": "Wipe all metadata from {n} file(s)? [y/N]",
                            "fr": "Effacer toutes les métadonnées de {n} fichier(s) ? [y/N]"},
    "wipe_cancelled": {"en": "Wipe cancelled.", "fr": "Effacement annulé."},
    "files_cleaned":  {"en": "{done} file(s) cleaned", "fr": "{done} fichier(s) nettoyé(s)"},
    "failures":       {"en": "{n} failure(s)", "fr": "{n} échec(s)"},
    "no_date_in_files": {"en": "No date present in the files.",
                         "fr": "Aucune date présente dans les fichiers."},
    "group_use_field_value": {"en": "In group mode, use: Field value",
                              "fr": "En mode groupe, utilisez : Champ valeur"},
    "rename_unavailable_group": {"en": "Renaming unavailable in group mode (name collision).",
                                 "fr": "Renommage indisponible en mode groupe (collision de noms)."},
    "skipped":        {"en": "{n} skipped", "fr": "{n} ignoré(s)"},
    # --- choose_session_mode (--mode=ask) ---
    # The opening screen answers ONE question — the whole batch at once, or file by file.
    # How to walk the batch (the arrows, n/p) belongs to the help, not here.
    "choose_title":   {"en": "{n} files selected", "fr": "{n} fichiers sélectionnés"},
    "choose_head":    {"en": "Edit", "fr": "Éditer"},
    "choose_group":   {"en": "as a group: one edit applies to every file",
                       "fr": "en groupe : une modification s'applique à tous les fichiers"},
    "choose_single":  {"en": "individually: file by file",
                       "fr": "individuellement : fichier par fichier"},
    "choose_quit":    {"en": "quit", "fr": "quitter"},
    "choose_switch_note": {"en": "The choice is not final: g or s to switch at any time.",
                           "fr": "Le choix n'est pas définitif : g ou s pour basculer à tout moment."},
    "choose_invalid": {"en": "Type g (group), s (single) or q (quit).",
                       "fr": "Tapez g (groupe), s (single) ou q (quitter)."},
    # --- session_wipe / one-shot ---
    "wipe_confirm":   {"en": "Wipe all metadata from {n} file(s)?",
                       "fr": "Effacer toutes les métadonnées de {n} fichier(s) ?"},
    "and_more":       {"en": "… and {n} more", "fr": "… et {n} de plus"},
    "cleanup_partial":{"en": "Depending on the format, the cleanup may be partial.",
                       "fr": "Selon le format, le nettoyage peut être partiel."},
    "cancelled":      {"en": "Cancelled.", "fr": "Annulé."},
    "failed_name":    {"en": "failed: {name}", "fr": "échec : {name}"},
    "close_or_undo":  {"en": "Enter to close · u to undo.",
                       "fr": "Entrée pour fermer · u pour annuler."},
    # --- _finish ---
    "changes_made":   {"en": "{n} change(s) made", "fr": "{n} changement(s) effectué(s)"},
    "summary_wiped":  {"en": "wiped", "fr": "purgé"},   # the summary row of a whole-file wipe
    "enter_close":    {"en": "Enter to close.", "fr": "Entrée pour fermer."},
    # --- _wipe_caveat ---
    "caveat_av":  {"en": "Audio/video: undo restores neither cover art, tracks, nor chapters.",
                   "fr": "Audio/vidéo : l'annulation ne restaure ni pochette, ni pistes, ni chapitres."},
    "caveat_img": {"en": "Images: undo restores neither the embedded thumbnail nor the maker notes.",
                   "fr": "Image : l'annulation ne restaure ni la vignette intégrée, ni les données constructeur (MakerNotes)."},
    "caveat_pdf": {"en": "PDF: the metadata is neutralised but remains technically "
                         "recoverable in the file (exiftool limitation).",
                   "fr": "PDF : les métadonnées sont neutralisées mais restent techniquement "
                         "récupérables dans le fichier (limite d'exiftool)."},
    # --- CLI (main) ---
    "cli_help": {"en": ("Usage: metmux.py --mode=MODE file [file ...]\n"
                        "Modes:\n"
                        "  single (default)  file-by-file editing\n"
                        "  group             batch editing (same fields on all the files)\n"
                        "  ask               with several files, ask first: group or file by file\n"
                        "  wipe              erase all metadata\n"
                        "Options:\n"
                        "  --gather          merge near-simultaneous launches into one session\n"
                        "                    (used by the Windows context menu)\n"
                        "  -V, --version     print the version and quit\n"
                        "  -h, --help        show this help"),
                 "fr": ("Utilisation : metmux.py --mode=MODE fichier [fichier ...]\n"
                        "Modes :\n"
                        "  single (défaut)   édition fichier par fichier\n"
                        "  group             édition en lot (mêmes champs sur tous les fichiers)\n"
                        "  ask               à plusieurs fichiers, demander d'abord : groupe ou fichier par fichier\n"
                        "  wipe              effacer toutes les métadonnées\n"
                        "Options :\n"
                        "  --gather          fusionner les lancements quasi simultanés en une session\n"
                        "                    (utilisé par le menu contextuel Windows)\n"
                        "  -V, --version     afficher la version et quitter\n"
                        "  -h, --help        afficher cette aide")},
    "invalid_mode": {"en": "Invalid mode: {mode}", "fr": "Mode invalide : {mode}"},
    "no_valid_file": {"en": "No valid file.", "fr": "Aucun fichier valide."},
    "args_ignored": {"en": "Argument(s) ignored (neither a file nor a folder): {items}",
                     "fr": "Argument(s) ignoré(s) (ni fichier ni dossier) : {items}"},
    "mode_tip": {"en": 'Tip: the mode attaches to the "=", e.g. --mode=wipe',
                 "fr": "Astuce : le mode s'attache au « = », par ex. --mode=wipe"},
    "enter_continue": {"en": "Enter to continue…", "fr": "Entrée pour continuer…"},
    "exiftool_required": {"en": "exiftool is not found and is required.",
                          "fr": "exiftool est introuvable alors qu'il est requis."},
    "missing_tools": {"en": "Missing tools — external data only for the affected files:",
                      "fr": "Outils manquants — données externes seulement pour les fichiers concernés :"},
    # --- apply_filename (rename errors) ---
    "name_invalid":  {"en": 'Invalid name (avoid "/", "\\", "%" and empty names).',
                      "fr": "Nom invalide (évitez « / », « \\ », « % » et les noms vides)."},
    "name_exists":   {"en": "A file already has this name.",
                      "fr": "Un fichier porte déjà ce nom."},
    "name_too_long": {"en": "Invalid name (too long for the file system).",
                      "fr": "Nom invalide (trop long pour le système de fichiers)."},
    "rename_failed": {"en": "Rename failed.", "fr": "Échec du renommage."},
    # --- paste guard (ask) ---
    "paste_blocked": {"en": "Paste ignored. Type a field name first, then paste its value.",
                      "fr": "Collage ignoré. Tapez d'abord un nom de champ, puis collez sa valeur."},
    "paste_multiline": {"en": "Multi-line paste ignored. Type the field name, then paste its value on one line.",
                        "fr": "Collage multi-ligne ignoré. Tapez le nom du champ, puis collez sa valeur sur une seule ligne."},
    # Said on the prompt line WHILE a slow console is still handing the block over, so the verdict
    # is known at once and the screen never looks hung. Erased when the block ends.
    "paste_reading":  {"en": "reading the pasted text…", "fr": "lecture du texte collé…"},
    "paste_dropping": {"en": "paste ignored — reading it out of the way…",
                       "fr": "collage ignoré — lecture du bloc en cours…"},
}


def tr(key, **kw):
    """Interface string for the current language (DEFAULT_LANG), formatted with kw.
       Falls back to English when a language lacks the key. A string carrying a
       "{placeholder}" is always .format()-ed (so its caller must pass the kw)."""
    entry = _UI.get(key, {})
    s = entry.get(DEFAULT_LANG) or entry.get("en") or key
    return s.format(**kw) if "{" in s else s


# Fields that are truly "year only": we store 4 digits there, never a full date
# (otherwise "2024" would become "2024-01-01", with an invented month/day). Subset of
# DATE_TAGS, handled separately by write() and cmd_dates.
YEAR_TAGS = {"year", "Year"}

DATE_TAGS = {
    "CreateDate", "ModifyDate", "CreationDate", "ModDate",
    "DateTimeOriginal", "DateTimeDigitized", "DateCreated",
    "FileCreateDate", "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
    "MetadataDate", "HistoryWhen", "RecordingTime",
    "TrackCreateDate", "TrackModifyDate", "MediaCreateDate", "MediaModifyDate",
    "GPSDateTime", "GPSDateStamp",
    "SubSecDateTimeOriginal", "SubSecCreateDate", "SubSecModifyDate",
    # Editable date fields that escaped normalisation (written raw/rejected):
    "originaldate", "Date", "DigitalCreationDate",
    "date", "creation_time",
} | YEAR_TAGS

# Dates of the WORK itself (album release year, film/book date, recording date), not of
# the file. Two rules apart from the rest of DATE_TAGS:
#   1. Granularity preserved as typed: "1982" is never padded to "1982-01-01" (like
#      YEAR_TAGS, but these also accept a fuller date).
#   2. The bulk "dates" command skips them: a timezone shift or a scan stamp must never
#      rewrite a release year.
SEMANTIC_DATE_TAGS = {"date", "Date", "originaldate", "RecordingTime"} | YEAR_TAGS

OPENABLE_BINARY = {
    "ThumbnailImage", "PreviewImage", "JpgFromRaw", "JpgFromRaw2",
    "OtherImage", "CoverArt", "Picture", "PreviewTIFF", "ThumbnailTIFF",
}

EQUIV_EXT = {("jpg", "jpeg"), ("tif", "tiff"), ("htm", "html")}

# Multi-value fields where "Field +value" appends instead of overwriting.
LIST_FIELDS = {"Keywords", "Subject", "Category", "SupplementalCategories"}

DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})( \d{2}:\d{2}:\d{2}\S*)?$")
MAX_LEN = 80


# ============================================================
#  Permissive date parser
# ============================================================

def _dm(a, b, order):
    """(day, month) from the first two numbers of an ambiguous date, per the order:
       "DMY" (EU) = day then month; "MDY" (US) = month then day."""
    return (b, a) if order == "MDY" else (a, b)


def parse_date(s, order=None, granular=False):
    """Returns 'YYYY:MM:DD HH:MM:SS' or None if unreadable. For an ambiguous date
       (day and month without a leading 4-digit year), `order` decides: "DMY"
       (day/month, EU) or "MDY" (month/day, US); None follows the config (DEFAULT_DATE_ORDER).
       When `granular` is set (content/publication dates, cf. SEMANTIC_DATE_TAGS), the
       result is TRUNCATED to the precision actually typed — "1982" stays "1982",
       "03/1982" → "1982:03", "15/03/1982" → "1982:03:15" — so a release year is never
       padded with an invented month/day. The calendar is still validated in full."""
    if order is None:
        order = DEFAULT_DATE_ORDER
    def mk(y, m, d, hh, mm, ss, precision="full"):
        return _try_make(y, m, d, hh, mm, ss, precision if granular else "full")
    s = s.strip()
    if not s:
        return None
    # A trailing time offset (+01:00, -0500, Z — including negative US offsets) is
    # only stripped when there is a time, spotted by a ":". Without a time, a trailing
    # "-2024" is the year of a DD-MM-YYYY date, not a "-20:24" timezone.
    if ":" in s:
        s = re.sub(r"([+-]\d{2}:?\d{2}|Z)$", "", s).strip()

    digits = re.sub(r"\D", "", s)

    if digits == s:
        n = len(digits)
        if n == 4:  # YYYY
            return mk(digits, "01", "01", "00", "00", "00", "Y")
        if n == 6:  # YYYYMM or DDMMYY
            if digits.startswith(("19", "20")):
                return mk(digits[:4], digits[4:6], "01", "00", "00", "00", "YM")
            d, m = _dm(digits[:2], digits[2:4], order)
            return mk("20" + digits[4:6], m, d, "00", "00", "00", "YMD")
        if n == 8:  # YYYYMMDD or DDMMYYYY
            if digits.startswith(("19", "20")):
                r = mk(digits[:4], digits[4:6], digits[6:8], "00", "00", "00", "YMD")
                if r:  # valid YYYYMMDD; otherwise fall back to DDMMYYYY (e.g. 20122024)
                    return r
            d, m = _dm(digits[:2], digits[2:4], order)
            return mk(digits[4:8], m, d, "00", "00", "00", "YMD")
        if n == 10:  # YYYYMMDDHH
            if digits.startswith(("19", "20")):
                return _try_make(digits[:4], digits[4:6], digits[6:8], digits[8:10], "00", "00")
        if n == 12:  # YYYYMMDDHHMM or DDMMYYHHMMSS
            if digits.startswith(("19", "20")):
                r = _try_make(digits[:4], digits[4:6], digits[6:8],
                              digits[8:10], digits[10:12], "00")
                if r:
                    return r
            d, m = _dm(digits[:2], digits[2:4], order)
            return _try_make("20" + digits[4:6], m, d,
                             digits[6:8], digits[8:10], digits[10:12])
        if n == 14:  # YYYYMMDDHHMMSS or DDMMYYYYHHMMSS
            if digits.startswith(("19", "20")):
                r = _try_make(digits[:4], digits[4:6], digits[6:8],
                              digits[8:10], digits[10:12], digits[12:14])
                if r:  # same as n==8: fall back to DDMMYYYYHHMMSS if YYYYMMDD invalid
                    return r
            d, m = _dm(digits[:2], digits[2:4], order)
            return _try_make(digits[4:8], m, d,
                             digits[8:10], digits[10:12], digits[12:14])
        return None

    # ISO's "T" between date and time — the very shape the non-exiftool engines display —
    # reads as a separator, so a shown date value can be typed or pasted back as-is.
    s = re.sub(r"(?<=\d)[Tt](?=\d)", " ", s)
    # Spoken time separators: "14h00m30s" reads like a clock, so h/m/s all become ":"
    # and the trailing one is stripped. Only the time part can hold a letter — a date
    # never does — so this cannot corrupt a date.
    norm = re.sub(r"[hHmMsS]", ":", s)
    norm = re.sub(r"\s*à\s*", " ", norm)
    norm = norm.strip(":")

    parts = norm.split()
    date_part = parts[0] if parts else ""
    time_part = " ".join(parts[1:]) if len(parts) > 1 else ""

    date_tokens = [t for t in re.split(r"[/\-.:]", date_part) if t]
    if len(date_tokens) == 1:
        # Just the year as "2024" without a separator — already handled above
        return None
    if len(date_tokens) == 2:
        # MM/YYYY or YYYY/MM
        a, b = date_tokens
        if len(a) == 4:
            return mk(a, b, "01", "00", "00", "00", "YM")
        y = b if len(b) == 4 else ("20" + b if len(b) == 2 else b)
        return mk(y, a, "01", "00", "00", "00", "YM")
    if len(date_tokens) == 3:
        if len(date_tokens[0]) == 4:
            y, m, d = date_tokens
        else:
            d, m = _dm(date_tokens[0], date_tokens[1], order)
            y = date_tokens[2]
            if len(y) == 2:
                y = "20" + y
        hh, mm, ss = "00", "00", "00"
        if time_part:
            time_tokens = [t for t in re.split(r"[:\-\s]", time_part) if t]
            if len(time_tokens) >= 1: hh = time_tokens[0]
            if len(time_tokens) >= 2: mm = time_tokens[1]
            if len(time_tokens) >= 3: ss = time_tokens[2]
        return mk(y, m, d, hh, mm, ss, "full" if time_part else "YMD")

    return None

def _try_make(y, m, d, hh, mm, ss, precision="full"):
    """Builds the canonical date, validating the FULL calendar every time (so an
       invalid day/month is rejected even when only the year is kept). `precision`
       truncates the returned string — "Y" (year), "YM" (year:month), "YMD" (date), or
       "full" (default: date+time) — used by parse_date(granular=True) to preserve the
       precision the user typed for content/publication dates."""
    try:
        y, m, d = int(y), int(m), int(d)
        hh, mm, ss = int(hh), int(mm), int(ss)
        if not (1000 <= y <= 9999):       # anti-absurdity guard; no longer excludes
            return None                    # legitimate old dates (photo/scan/document < 1900)
        datetime.datetime(y, m, d, hh, mm, ss)  # validates the real calendar (rejects Feb 30, month 13…)
        if precision == "Y":
            return f"{y:04d}"
        if precision == "YM":
            return f"{y:04d}:{m:02d}"
        if precision == "YMD":
            return f"{y:04d}:{m:02d}:{d:02d}"
        return f"{y:04d}:{m:02d}:{d:02d} {hh:02d}:{mm:02d}:{ss:02d}"
    except (ValueError, OverflowError):
        # OverflowError: an oversized component overflows datetime's C int and is NOT a
        # ValueError subclass. Reject (None) rather than crash the session.
        return None


def _typed_offset(s):
    """Trailing UTC offset typed on a date ('+03:00', '+0300', 'Z'), normalised to
       '+HH:MM'; '' if none; None when present but absurd (≥ 24 h / ≥ 60 min:
       datetime.timezone refuses it — treat the date as unreadable, never crash the
       btime writer). parse_date() strips offsets (metadata dates are naive); file dates
       are absolute instants, so it is recovered here. A time (a ':') is required: a
       trailing '-2024' is a year, not '-20:24' — same guard as parse_date's stripping."""
    s = s.strip()
    if ":" not in s:
        return ""
    if s.endswith("Z"):
        return "+00:00"
    m = re.search(r"([+-])(\d{2}):?(\d{2})$", s)
    if not m:
        return ""
    if int(m.group(2)) > 23 or int(m.group(3)) > 59:
        return None
    return f"{m.group(1)}{m.group(2)}:{m.group(3)}"


def to_exif(val, tag):
    """For a NON-EMPTY date tag: force parsing or return None if unreadable.
       An empty value ("") is a clear, valid for ANY field (dates included);
       the other tags: returns val unchanged."""
    if tag in DATE_TAGS and val != "":
        canon = parse_date(val, granular=tag in SEMANTIC_DATE_TAGS)
        if canon is not None and tag in FILE_INSTANT_DATE_TAGS:
            # Re-attach the typed offset (dropped by parse_date) so the file-date writer can
            # honour it: "23:15:00+03:00" is the instant 20:15 UTC, not 23:15 local.
            off = _typed_offset(val)
            if off is None:                # absurd offset (+24:00…): unreadable, not a crash
                return None
            canon += off
        return canon
    return val


# Internal "canonical" form produced by parse_date: YYYY:MM:DD HH:MM:SS
CANON_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})$")


def format_date(canonical, engine):
    """Converts a canonical date (colons) to the format expected by the target
       engine. exiftool wants colons; all others want ISO 8601 (dashes + T). If the
       value is not canonical, we leave it untouched (case of a date already stored
       in ISO during a copy)."""
    m = CANON_RE.match(canonical or "")
    if not m:
        # Reduced granularity (produced by a copy that preserves "year only" or
        # "date only"): adapt to the engine without inventing a month/day/hour.
        if re.match(r"^\d{4}$", canonical or ""):
            return canonical
        ym = re.match(r"^(\d{4}):(\d{2})$", canonical or "")
        if ym:                                     # "2024:03": year+month, no invented day
            y, mo = ym.groups()
            return f"{y}:{mo}" if engine == "exiftool" else f"{y}-{mo}"
        md = re.match(r"^(\d{4}):(\d{2}):(\d{2})$", canonical or "")
        if md:
            y, mo, d = md.groups()
            return f"{y}:{mo}:{d}" if engine == "exiftool" else f"{y}-{mo}-{d}"
        return canonical
    y, mo, d, hh, mi, ss = (int(x) for x in m.groups())
    if engine == "exiftool":
        return f"{y:04d}:{mo:02d}:{d:02d} {hh:02d}:{mi:02d}:{ss:02d}"
    iso = f"{y:04d}-{mo:02d}-{d:02d}T{hh:02d}:{mi:02d}:{ss:02d}"
    if engine == "ooxml":
        # W3CDTF: no fake "Z" (we don't know the original timezone).
        # Exactly midnight = date only, so as not to invent a 00:00:00 time.
        if (hh, mi, ss) == (0, 0, 0):
            return f"{y:04d}-{mo:02d}-{d:02d}"
        return iso
    if engine == "mutagen" and (hh, mi, ss) == (0, 0, 0):
        return f"{y:04d}-{mo:02d}-{d:02d}"     # audio convention: date only if midnight
    return iso


def _year_str(value):
    """Reduces a canonical date (or already short one) to its year "YYYY". Used for
       fields that are truly "year only" (YEAR_TAGS): "25/12/2024" → "2024", without
       inventing a January 1st as format_date would on a full date field."""
    m = CANON_RE.match(value or "")
    if m:
        return m.group(1)
    return value[:4] if re.match(r"^\d{4}", value or "") else value


# --- Relative date shift (--mode command "dates +2h", "dates -1d") ---
# Only "m" is case-sensitive: "M" (intended "1 month") must not be silently read
# as "1 minute" → we reject it. The other UNAMBIGUOUS units are tolerated in
# uppercase ("+2H", "+3D", "+10S"): no reason to break usage.
OFFSET_RE = re.compile(r"^([+-])\s*((?:\d+\s*[dDhHmsS]\s*)+)$")
OFFSET_UNIT_RE = re.compile(r"(\d+)\s*([dDhHmsS])")


def parse_offset(s):
    """'+2h', '-1d', '+1d2h', '-90m', '+2H' -> signed timedelta, or None.
       Units: d (days), h (hours), m (minutes), s (seconds). Uppercase tolerated
       EXCEPT "M" (ambiguous month/minute) which is rejected; lowercase "m" = minute.
       Deliberately no years/months: timedelta does not represent them properly
       (variable calendar durations). For that, use an absolute date."""
    s = s.strip()
    m = OFFSET_RE.match(s)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    days = hours = minutes = seconds = 0
    # An oversized number overflows at int() (>4300 digits: ValueError) or at the
    # timedelta construction (OverflowError): reject both — same guard as _try_make.
    try:
        for num, unit in OFFSET_UNIT_RE.findall(m.group(2)):
            n, u = int(num), unit.lower()
            if u == "d": days += n
            elif u == "h": hours += n
            elif u == "m": minutes += n
            elif u == "s": seconds += n
        return sign * datetime.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    except (OverflowError, ValueError):
        return None


def parse_stored_dt(value):
    """Re-reads an already-stored date — colons (exiftool) OR ISO (other engines) —
       into a datetime object, or None. Used by the copy (canonical normalisation).
       Accepts a fractional second (so as NOT to silently skip a date that carries one);
       the fraction drops at normalisation. For the relative shift, see
       _shift_stored_date()."""
    if not isinstance(value, str):
        return None
    v = re.sub(r"([+-]\d{2}:?\d{2}|Z)$", "", value.strip()).strip()
    for fmt in ("%Y:%m:%d %H:%M:%S.%f", "%Y:%m:%d %H:%M:%S", "%Y:%m:%d",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y"):
        try:
            return datetime.datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


# Splitting of a stored date for the relative SHIFT, preserving everything
# parse_stored_dt discards (fraction, timezone, granularity, style): we only rewrite
# the shifted calendar part, the rest is restored as-is.
_STORED_DT_RE = re.compile(
    r"^\s*(\d{4})"                          # year
    r"(?:([:\-])(\d{2})"                    # 2: date separator  3: month
    r"(?:\2(\d{2})"                         # 4: day (same separator)
    r"(?:([ T])(\d{2}):(\d{2}):(\d{2})"     # 5: date/time separator  6-8: h:m:s
    r"(\.\d+)?"                             # 9: fraction
    r"(Z|[+-]\d{2}:?\d{2})?"               # 10: timezone
    r")?)?)?\s*$")


def _shift_stored_date(value, offset):
    """Shifts a stored date by a timedelta while PRESERVING fraction, timezone,
       granularity (year only / date only / date+time) and style (colons or ISO).
       Returns the shifted string, or None if unreadable.
       - Year only / date only: a sub-day shift does not fabricate a time; we only
         shift the part that is present."""
    if not isinstance(value, str):
        return None
    m = _STORED_DT_RE.match(value)
    if not m:
        return None
    y, dsep, mo, d, tsep, hh, mm, ss, frac, tz = m.groups()
    # An oversized offset pushes the date out of datetime's range (OverflowError) —
    # same guard as _try_make.
    try:
        if mo is None:                              # year only
            base = datetime.datetime(int(y), 1, 1)
            return f"{(base + offset).year:04d}"
        if d is None:                               # year+month (rare): not cleanly shiftable
            return None
        if hh is None:                              # date only
            base = datetime.datetime(int(y), int(mo), int(d))
            nd = base + offset
            return f"{nd.year:04d}{dsep}{nd.month:02d}{dsep}{nd.day:02d}"
        base = datetime.datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss))
        nd = base + offset                          # date+time: we keep frac + timezone + style
        return (f"{nd.year:04d}{dsep}{nd.month:02d}{dsep}{nd.day:02d}{tsep}"
                f"{nd.hour:02d}:{nd.minute:02d}:{nd.second:02d}{frac or ''}{tz or ''}")
    except (ValueError, OverflowError):
        return None


# ============================================================
#  Routing + ExifTool sub-categorisation
# ============================================================

def engine_for(path):
    ext = path.suffix.lstrip(".").lower()
    if ext in OOXML_EXTS: return "ooxml"
    if ext in ODF_EXTS:   return "odf"
    if ext in EPUB_EXTS:  return "epub"
    if ext in IPYNB_EXTS: return "ipynb"
    if ext in CBZ_EXTS:   return "cbz"
    if ext in PLAYLIST_EXTS: return "m3u"
    if ext in PLIST_EXTS:    return "plist"
    if ext in EML_EXTS:      return "eml"
    if ext in MBOX_EXTS:     return "mbox"
    if ext in CUE_EXTS:      return "cue"
    if ext in GEOJSON_EXTS:  return "geojson"
    if ext in HAR_EXTS:      return "har"
    if ext in SQLITE_EXTS:   return "sqlite"
    if ext in KMZ_EXTS:      return "kmz"
    if ext in MUSICXML_EXTS: return "musicxml"
    if ext in TCX_EXTS:      return "tcx"
    if ext in ARCHIVE_EXTS:  return "archive"
    if ext in AUDIO_EXTS: return "mutagen"
    if ext in VIDEO_ONLY_FFMPEG: return "ffmpeg"
    return "exiftool"


def exiftool_category(data):
    mime = (data.get("MIMEType") or "").lower()
    if mime.startswith("audio/"): return "exiftool_audio"
    if mime.startswith("image/"): return "exiftool_image"
    if mime.startswith("video/"): return "exiftool_video"
    if "pdf" in mime:             return "exiftool_pdf"
    return "exiftool_other"


# ============================================================
#  ExifTool engine
# ============================================================

def _ext_arg(path):
    """Path safe as a positional argument to exiftool/ffmpeg/ffprobe: a name starting
       with "-" would be read as an OPTION ("-photo.jpg" → "Invalid TAG name"), so it is
       prefixed with "./". Absolute or directory-prefixed paths are unchanged."""
    s = str(path)
    return os.path.join(".", s) if s.startswith("-") else s


# ---- Resident exiftool (Windows) ---------------------------------------------------
# What costs on Windows is LAUNCHING exiftool, not its work: a packed Perl interpreter,
# unpacked and re-scanned by the antivirus at every launch, and metmux launches it three
# times per edited field — that IS the multi-second field edit reported on Windows.
# "-stay_open True -@ -" keeps ONE exiftool alive and feeds it commands through a
# pipe. POSIX keeps spawning: a launch is cheap there, and a resident
# process would only add moving parts. METMUX_EXIFTOOL_DAEMON=0/1 forces it off/on (the
# bench turns it ON to prove the Windows path on Linux).

_ET_STAY = None                   # the live daemon, or False once we know it cannot run
_ET_SEQ = 0                       # command counter; it names the {ready} marker
_ET_DEATHS = 0                    # a resident that keeps dying is worse than none: after
_ET_MAX_DEATHS = 3                # this many, we give it up and launch exiftool as before
_ET_JOB = None                    # Windows job handle holding the resident's life to ours
_ET_LOCK = threading.RLock()      # one command at a time on the shared pipes (re-entrant:
                                  # _et_send holds it while starting the daemon)

# exiftool's argfile parser eats what a shell would have preserved: a leading space, ONE space
# right after "-TAG=", an empty line, a line starting with "#" (FilterArgfileLine in exiftool).
# The "#[CSTR]" form escapes all that but corrupts "$" and "@" (verified: "Cost $5 @home" is
# written back as "Cost \$5 \@home") — so it is not usable either. Any argument the argfile
# would alter therefore goes back to a plain launch: exact, and rare enough to cost nothing.
_ET_ARG_EATEN = re.compile(r"^-[-:\w]+#?[-+<]?=[ \t]")


def _et_argfile_safe(args):
    # The NUL is here for exactness, not for safety: write() strips control characters long
    # before this (test_write_strips_nul_no_exiftool_crash). Should one ever reach us anyway,
    # it takes the plain-launch path and behaves exactly as it does today.
    return all(a and not a[0].isspace() and a[0] != "#"
               and "\n" not in a and "\r" not in a and "\x00" not in a
               and not _ET_ARG_EATEN.match(a)
               for a in args)


def _et_daemon_wanted():
    env = os.environ.get("METMUX_EXIFTOOL_DAEMON", "")
    if env in ("0", "1"):
        return env == "1"
    return os.name == "nt"


class _EtPipe:
    """One of the daemon's two pipes, drained by its own thread. exiftool can write a lot on
       stderr (a warning per file) while we are still reading stdout: a pipe holds ~64 KB, so
       leaving one unread would block exiftool mid-write — it would never reach the marker we
       are waiting for on the other one, and both sides would hang forever."""

    def __init__(self, stream):
        self.buf, self.eof = "", False
        self.cond = threading.Condition()
        self._dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
        threading.Thread(target=self._pump, args=(stream,), daemon=True).start()

    def _pump(self, stream):
        fd = stream.fileno()
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            with self.cond:
                if not chunk:                       # exiftool closed the pipe: it is gone
                    self.eof = True
                    self.cond.notify_all()
                    return
                self.buf += self._dec.decode(chunk)
                self.cond.notify_all()

    def _find_line_start(self, marker):
        """Index of `marker` only where it BEGINS a line (position 0 or right after a \\n).
           exiftool always prints the real marker alone on its line, but the same text can
           surface mid-line inside a metadata value on stdout ("Comment": "{ready3}") or
           inside a filename that a Warning re-prints on stderr; an unanchored find would
           match that occurrence first, truncate the answer, AND leave the true marker in
           the buffer to poison the next command. A value's own newlines are JSON-escaped
           and a filename holds none, so no false occurrence can itself start a line."""
        start = 0
        while True:
            i = self.buf.find(marker, start)
            if i < 0 or i == 0 or self.buf[i - 1] == "\n":
                return i
            start = i + 1

    def take(self, marker):
        """Everything written before `marker`, plus the rest of the marker's own line (-echo4
           appends the exit status there). None if exiftool died before answering."""
        with self.cond:
            while True:
                i = self._find_line_start(marker)
                if i >= 0:
                    end = self.buf.find("\n", i)
                    if end >= 0:
                        head, tail = self.buf[:i], self.buf[i + len(marker):end]
                        self.buf = self.buf[end + 1:]
                        # "\r\n" -> "\n" as subprocess's text=True does, so the daemon and the
                        # plain launch hand the caller byte-identical text
                        return head.replace("\r\n", "\n"), tail.strip()
                if self.eof:
                    return None
                self.cond.wait(0.5)


def _job_limit_struct():
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION, declared in portable ctypes types (Windows DWORD
       is a uint32; SIZE_T and ULONG_PTR are pointer-sized) so that its layout can be checked
       off Windows too — 144 bytes on 64-bit, 112 on 32-bit. Never guess a memory layout: the
       same rule that guards _build_win_reader."""
    import ctypes

    class _Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", ctypes.c_uint32),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", ctypes.c_uint32),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", ctypes.c_uint32),
                    ("SchedulingClass", ctypes.c_uint32)]

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _Basic),
                    ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    return _Extended


def _et_tie_to_our_life(proc):
    """Windows: have the OS kill the resident along with us. exiftool does NOT die when its
       pipe closes — on EOF its argfile loop sleeps 10 ms and retries forever (its own Perl
       source, reproduced here) — so any death that skips the atexit above (window close
       button, task manager, crash) leaves an orphan, one per session. A job object with
       KILL_ON_JOB_CLOSE ties exiftool's life to a handle the OS closes whatever kills us.
       Best-effort: failing here only costs the orphan we would have had anyway."""
    global _ET_JOB
    if os.name != "nt":
        return                                      # POSIX launches exiftool; no resident to tie
    try:
        import ctypes
        from ctypes import wintypes
        JOB_KILL_ON_CLOSE = 0x00002000
        JOB_EXTENDED_LIMIT = 9                      # JobObjectExtendedLimitInformation
        PROCESS_SET_QUOTA, PROCESS_TERMINATE = 0x0100, 0x0001

        extended = _job_limit_struct()
        if ctypes.sizeof(extended) not in (144, 112):   # 64-bit / 32-bit; any other size
            return                                       # means the struct is wrong: abort

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.CloseHandle.argtypes = [wintypes.HANDLE]

        job = k.CreateJobObjectW(None, None)
        if not job:
            return
        info = extended()
        info.BasicLimitInformation.LimitFlags = JOB_KILL_ON_CLOSE
        handle = None
        if k.SetInformationJobObject(job, JOB_EXTENDED_LIMIT,
                                     ctypes.byref(info), ctypes.sizeof(info)):
            handle = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
        if not handle:
            k.CloseHandle(job)
            return
        tied = k.AssignProcessToJobObject(job, handle)
        k.CloseHandle(handle)
        if not tied:
            k.CloseHandle(job)
            return
        _ET_JOB = job              # held open all session long: the OS closes it when we die,
    except Exception:              # and closing it is what kills exiftool
        pass


def _et_daemon():
    global _ET_STAY
    with _ET_LOCK:
        if _ET_STAY is not None or not _et_daemon_wanted():
            return _ET_STAY or None
        try:
            proc = subprocess.Popen([EXIFTOOL, "-stay_open", "True", "-@", "-"],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except OSError:                             # no exiftool, or it will not start
            _ET_STAY = False
            return None
        _et_tie_to_our_life(proc)
        _ET_STAY = (proc, _EtPipe(proc.stdout), _EtPipe(proc.stderr))
        return _ET_STAY


def _et_stop():
    """Ask the daemon to leave ("-stay_open False"), kill it if it insists. Idempotent."""
    global _ET_STAY
    with _ET_LOCK:
        stay, _ET_STAY = _ET_STAY, None
        if not stay:
            return
        proc = stay[0]
        try:
            proc.stdin.write(b"-stay_open\nFalse\n")
            proc.stdin.flush()
            proc.stdin.close()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        finally:
            # A resident that died mid-command leaves stdin's BufferedWriter open with unflushed
            # bytes: its finalizer would flush them to the dead pipe, raising an ignored-during-
            # finalize BrokenPipeError (pytest surfaces it as PytestUnraisableExceptionWarning).
            # close() drops the buffer and marks the writer closed. No-op on the success path.
            try:
                proc.stdin.close()
            except Exception:
                pass


atexit.register(_et_stop)


def _et_send(args):
    """Run one exiftool command on the resident process. Returns (out, err, status), or None
       if there is no daemon — the caller then launches exiftool the old way."""
    global _ET_SEQ
    with _ET_LOCK:
        stay = _et_daemon()
        if stay is None:
            return None
        proc, out_pipe, err_pipe = stay
        _ET_SEQ += 1
        seq = _ET_SEQ
        marker = "{ready%d}" % seq
        # -charset filename=UTF8: arguments in an argfile are NOT recoded to the system code
        # page (exiftool's WINDOWS UNICODE FILE NAMES section), so without it an accented name
        # would be read in the console's code page — "Café.jpg" would simply not be found. The
        # doc prescribes exactly this pairing: a UTF-8 argfile plus this option.
        # -echo4 writes the marker AND ${status} (the exit code, which -stay_open otherwise
        # hides) on stderr once the command is done; -execute<n> closes stdout with its own.
        lines = ["-charset", "filename=UTF8", *args,
                 "-echo4", marker + "${status}", "-execute%d" % seq]
        try:
            proc.stdin.write(("\n".join(lines) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError):               # broken pipe: the command never arrived
            _et_died()
            return None
        got_out = out_pipe.take(marker)
        got_err = err_pipe.take(marker)
        if got_out is None or got_err is None:      # died mid-command: fall back to a launch
            _et_died()
            return None
        return got_out[0], got_err[0], got_err[1]


def _et_died():
    """The resident is gone. Bury it and let the caller launch exiftool the old way — the
       command is replayed, which is safe: every command metmux sends is idempotent (write the
       same tag, clear the same tags). Past _ET_MAX_DEATHS we stop resurrecting it: an exiftool
       that dies at every command would cost a launch AND a crash each time."""
    global _ET_STAY, _ET_DEATHS
    with _ET_LOCK:
        _et_stop()                                  # leaves _ET_STAY at None (a fresh one may start)
        _ET_DEATHS += 1
        if _ET_DEATHS >= _ET_MAX_DEATHS:
            _ET_STAY = False                        # False, not None: never start one again


def et_run(*args):
    got = _et_send(args) if _et_argfile_safe(args) else None
    if got is not None:
        out, err, status = got
        return out, _et_ok(0 if status == "0" else 1, out, err)
    try:
        # Same filename charset the resident always sets: without it a name outside the
        # ANSI codepage is not found on Windows when the daemon is down or bypassed.
        r = subprocess.run([EXIFTOOL, "-charset", "filename=UTF8", *args],
                           capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
    except OSError:
        # exiftool not found (FileNotFoundError), OR argument too long for execve
        # ("Argument list too long" on a giant pasted value), OR another launch error:
        # clean failure, never a traceback that would kill the session.
        return "", False
    out, err = r.stdout or "", r.stderr or ""
    return out, _et_ok(r.returncode, out, err)


def _et_ok(returncode, out, err):
    """True if exiftool did write. We do NOT let ourselves be fooled by a benign
       warning containing the word "Error" (e.g. "Warning: Error reading PreviewImage"
       on an otherwise successful write): only a stderr line STARTING with "Error"
       is a real error."""
    combined = out + err  # exiftool writes its summary on either one depending on the case
    updated_zero = "0 image files updated" in combined
    unchanged = "image files unchanged" in combined
    has_error = any(line.startswith("Error") for line in err.splitlines())
    return (returncode == 0
            and not has_error
            and "files weren't updated" not in combined
            # "0 updated" is only a failure if nothing was already at the right value
            and not (updated_zero and not unchanged))


def _et_read(path, all_tags, strict):
    """One file's tags through exiftool. `strict` decides what a non-zero exit means: refuse
       the file (et_read), or keep whatever exiftool still delivered (et_read_lenient)."""
    out, ok = et_run("-j", "-a", "-s", *(["-u"] if all_tags else []), _ext_arg(path))
    if strict and not ok:
        return None
    try:
        return _scrub(json.loads(out)[0])
    except (json.JSONDecodeError, IndexError, TypeError):
        return None


def et_read(path, all_tags=False):
    return _et_read(path, all_tags, strict=True)


def et_read_lenient(path, all_tags=False):
    """Like et_read, but TOLERATES a non-zero exit code: exiftool exits in error on an
       unknown type (a non-sqlite ".db") while still delivering external data (name,
       dates, size). Used ONLY as a fallback when a stdlib engine fails — the main
       path (et_read) stays strict ("corrupted → refused")."""
    return _et_read(path, all_tags, strict=False)


# Empty dates written as sentinels by some formats (.doc, .xls).
_DATE_SENTINELS = ("0000:00:00 00:00:00", "0000:00:00 00:00:00+00:00")


def _scrub(entry):
    entry.pop("SourceFile", None)
    entry.pop("ExifToolVersion", None)
    # We only remove empty-date sentinels. A "0" is a sentinel ONLY on a date tag;
    # elsewhere (Rating, TrackNumber, GPSAltitude at sea level…) zero is a legitimate
    # value to keep. Comparisons by type so as not to confuse 0 with False/0.0
    # (Python's "==" semantics).
    for k in list(entry):
        v = entry[k]
        if isinstance(v, str) and v in _DATE_SENTINELS:
            entry.pop(k)
        elif k in DATE_TAGS and isinstance(v, str) and v == "0":
            entry.pop(k)
    return entry


def et_read_many(paths, all_tags=False):
    """Reads N files in a SINGLE exiftool call (big gain in group mode).
       Returns {path_str: data}. Files absent from the result will be re-read
       individually by the caller."""
    if not paths:
        return {}
    # _ext_arg guards a "-name.jpg" in the batch: read as an OPTION, it would poison
    # the whole grouped call (that file simply falls back to the per-file re-read).
    args = ["-j", "-a", "-s", *(["-u"] if all_tags else []), *[_ext_arg(p) for p in paths]]
    out, _ = et_run(*args)
    try:
        arr = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return {}
    result = {}
    for entry in arr:
        sf = entry.get("SourceFile")
        if sf is not None:
            result[sf] = _scrub(entry)
    return result


def et_write(path, tag, value):
    args = ["-overwrite_original"]
    if tag not in FILE_DATE_TAGS:
        # -P preserves the file's mtime: editing a metadata field (Title, EXIF, keyword…)
        # must not "jump" the FileModifyDate — which metmux exposes as a field — to now.
        # Only writes to FILE dates (FileModifyDate, FileCreateDate) deliberately set the
        # mtime → those, without -P. Also fixes the undo of a "dates": the previous mtime
        # value is no longer overwritten when restoring the EXIF dates.
        args += ["-P"]
    if isinstance(value, list) and tag in LIST_FIELDS:
        # Restoring an EXACT list (undo): we clear the field then rewrite each entry
        # separately, WITHOUT -sep, so that an entry containing "," ("Earth, Wind & Fire")
        # stays ONE entry. Re-splitting the list on "," would corrupt at undo a piece of
        # data that undo must return intact.
        args += [f"-{tag}="] + [f"-{tag}={item}" for item in value] + [_ext_arg(path)]
        _, ok = et_run(*args)
        return ok
    if tag in LIST_FIELDS:
        # Text input of a list field (Keywords/Subject…): '-sep ", "' splits
        # "cat, dog" into TWO distinct keywords. Without it, exiftool writes ONE
        # keyword "cat, dog" and the IPTC/XMP list structure is lost on every append.
        args += ["-sep", ", "]
    args += [f"-{tag}={value}", _ext_arg(path)]
    _, ok = et_run(*args)
    return ok


_ET_WRITABLE_EXTS = None


def _et_writable_exts():
    """The formats exiftool can WRITE, asked to exiftool itself ("-listwf"). It READS
       far more than it writes (WAV, MPG, TXT… are read-only for it). Empty set = the
       question got no answer (exiftool absent or mute): we then block nothing."""
    global _ET_WRITABLE_EXTS
    if _ET_WRITABLE_EXTS is None:
        out, ok = et_run("-listwf")
        # Header line ("Writable file extensions:"), then the extensions over several lines.
        exts = out.splitlines()[1:] if ok and out else []
        _ET_WRITABLE_EXTS = frozenset(e.upper() for line in exts for e in line.split())
    return _ET_WRITABLE_EXTS


def _et_content_readonly(data):
    """True when exiftool reads this format but cannot write it: its content fields must
       not be offered (each write would fail). File dates stay editable — they belong to
       the filesystem, and exiftool does write them even on a read-only format.
       Keyed on the type exiftool DETECTED, never on the file's name: a JPEG called
       "photo.foo" is writable, and exiftool writes it."""
    exts = _et_writable_exts()
    ext = (data or {}).get("FileTypeExtension") or ""
    return bool(exts) and bool(ext) and ext.upper() not in exts


def et_writable(path):
    """Editable tags offered for this file (whitelist by category)."""
    data = et_read(path)
    if data is None or _et_content_readonly(data):
        return set()
    return set(SUGGESTED.get(exiftool_category(data), ()))

def et_wipe(path):
    _, ok = et_run("-all=", "-overwrite_original", _ext_arg(path))
    return ok


# ============================================================
#  Mutagen engine
# ============================================================

def mg_load(path):
    try:
        from mutagen import File
        return File(str(path), easy=True)
    except Exception:
        return None


def mg_read(path, all_tags=False):
    f = mg_load(path)
    if f is None:
        return None
    try:
        # str(x) on each item: ASF (.wma) values are attribute OBJECTS
        # (ASFUnicodeAttribute), not str — ", ".join(v) raised and the swallowed
        # exception made every .wma read as {} (engine blind on its own format).
        return {k: (", ".join(str(x) for x in v) if isinstance(v, list) else str(v))
                for k, v in f.items()}
    except Exception:
        return {}

# Display: a multi-value field (several ARTIST…) reads back joined by ", ". NEVER re-split on
# that separator on write: "," is legitimate in a proper name ("Earth, Wind & Fire") and
# re-splitting would silently break it into several values — invisible corruption, the display
# re-joins them. The entered value is written AS-IS, as a single value (cf. mg_write).


def _mg_atomic(path, mutate):
    """Applies `mutate(f)` on a temporary COPY then replaces the original atomically:
       an interruption (Ctrl-C, crash) never leaves the audio file half-written.
       mutagen, on the other hand, writes in place — hence this safeguard."""
    tmp = _sibling_tmp(path)
    try:
        shutil.copy2(path, tmp)
        f = mg_load(tmp)
        if f is None:
            tmp.unlink(missing_ok=True)
            return False
        mutate(f)
        f.save()
        tmp.replace(path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


# Formats whose CONTENT tags stay read-only through mutagen. ASF (.wma): the easy
# keys ("title"…) land in NON-STANDARD extended attributes invisible to metmux's
# whitelist and to other tools — the user would believe a write that nothing shows.
# AIFF: mutagen has no easy wrapper (raw ID3 frames), every f["title"]=… fails and
# the undo could not restore raw frames after a wipe. Reading stays available;
# only the file name/dates are editable.
_MG_READONLY_EXTS = frozenset({"wma", "aiff", "aif"})


def _mg_content_readonly(path):
    return path.suffix.lstrip(".").lower() in _MG_READONLY_EXTS


def mg_write(path, tag, value):
    if _mg_content_readonly(path):
        return False
    if mg_load(path) is None:                       # clean failure if unreadable (without creating a temp)
        return False
    def mutate(f):
        if value == "":
            if tag in f:
                del f[tag]
        else:
            # SINGLE value, exactly as entered — never re-split on "," (doctrine above).
            f[tag] = value
    return _mg_atomic(path, mutate)


def mg_writable(path):
    if _mg_content_readonly(path):
        return set()
    return set(SUGGESTED["mutagen"])


def mg_wipe(path):
    if _mg_content_readonly(path):
        return False
    if mg_load(path) is None:
        return False
    def mutate(f):
        f.delete()
        if hasattr(f, "clear_pictures"):            # FLAC: delete() does not clear PICTURE blocks
            f.clear_pictures()
    return _mg_atomic(path, mutate)


# ============================================================
#  FFmpeg engine
# ============================================================

def ff_read(path, all_tags=False):
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", _ext_arg(path)],
            capture_output=True, text=True, check=True,
            encoding="utf-8", errors="replace",
        )
        tags = json.loads(r.stdout).get("format", {}).get("tags", {}) or {}
        return {k.lower(): v for k, v in tags.items()}
    except Exception:
        return None


def ff_write(path, tag, value):
    tmp = _sibling_tmp(path)
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", _ext_arg(path),
             "-map", "0",                       # keep ALL streams (audio/subtitles/attachments)
             "-codec", "copy", "-map_metadata", "0",
             "-metadata", f"{tag}={value}", _ext_arg(tmp)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            return False
        _replace_keep_mode(tmp, path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def ff_writable(path):
    return set(SUGGESTED["ffmpeg"])


def ff_wipe(path):
    tmp = _sibling_tmp(path)
    try:
        r = subprocess.run(
            # -map_metadata -1 only clears the global; we must also clear the PER-STREAM
            # metadata (track/subtitle titles) and PER-CHAPTER, otherwise they survive.
            # -bitexact prevents the muxer from re-stamping "ENCODER=Lavf<version>" (a tool
            # fingerprint) — reduces the stamp to "Lavf", without a version, for a cleaner wipe.
            [FFMPEG, "-y", "-loglevel", "error", "-bitexact", "-fflags", "+bitexact",
             "-i", _ext_arg(path),
             "-map", "0", "-codec", "copy", "-bitexact",
             "-map_metadata", "-1", "-map_metadata:s", "-1", "-map_metadata:c", "-1",
             "-map_chapters", "-1", _ext_arg(tmp)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            tmp.unlink(missing_ok=True)
            return False
        _replace_keep_mode(tmp, path)               # inside the try, like ff_write: a failed
        return True                                 # rename must not escape or orphan the temp
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


# ============================================================
#  Office engine (OOXML / ODF / EPUB)
# ============================================================

OOXML_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}
OOXML_TAGS = {
    "Title": ("dc", "title"), "Subject": ("dc", "subject"),
    "Creator": ("dc", "creator"), "Description": ("dc", "description"),
    "Keywords": ("cp", "keywords"), "Category": ("cp", "category"),
    "LastModifiedBy": ("cp", "lastModifiedBy"),
    "Language": ("dc", "language"),
    "CreateDate": ("dcterms", "created"),
    "ModifyDate": ("dcterms", "modified"),
}

ODF_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
}
ODF_TAGS = {
    "Title": ("dc", "title"), "Subject": ("dc", "subject"),
    "Description": ("dc", "description"), "Language": ("dc", "language"),
    "Creator": ("meta", "initial-creator"),
    "Keywords": ("meta", "keyword"),
    "CreateDate": ("meta", "creation-date"),
    "ModifyDate": ("dc", "date"),
}

EPUB_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
EPUB_TAGS = {
    "Title": "title", "Creator": "creator", "Subject": "subject",
    "Description": "description", "Publisher": "publisher",
    "Contributor": "contributor", "Language": "language",
    "Rights": "rights", "Identifier": "identifier",
    "CreateDate": "date",
}


# Restoration of the original namespace prefixes: without this, ElementTree invents
# "ns0:/ns1:" and turns the default namespace into a prefixed one — which breaks tools
# that match a literal prefix (gx:, xlink:) or expect an unprefixed default.
_XMLNS_RE = re.compile(rb'xmlns:([A-Za-z_][\w.\-]*)\s*=\s*"([^"]+)"')
_XMLNS_DEFAULT_RE = re.compile(rb'xmlns\s*=\s*"([^"]+)"')

# Snapshot of ElementTree's default prefix registry (xml, html…), taken once at import.
# `register_namespace` mutates a process-GLOBAL state: without a reset, the prefixes
# (and especially the default "") registered for one file would leak into the next in
# batch mode. We restart from this snapshot for each file (cf. _register_ns).
try:
    _NS_BASELINE = dict(ET._namespace_map)
except Exception:                                # private API absent: we do without it (best effort)
    _NS_BASELINE = None


def _register_ns(raw):
    try:
        if _NS_BASELINE is not None:             # isolate state per file (no global accumulation)
            ET._namespace_map.clear()
            ET._namespace_map.update(_NS_BASELINE)
        for m in _XMLNS_RE.finditer(raw):
            ET.register_namespace(m.group(1).decode("ascii"), m.group(2).decode("utf-8"))
        md = _XMLNS_DEFAULT_RE.search(raw)
        if md:
            ET.register_namespace("", md.group(1).decode("utf-8"))
    except Exception:
        pass


# ── XML / zip hardening (stdlib, no external dependency) ───────────────────────
# Anti-"decompression bomb" caps: a legitimate metadata member is small (a few KB to a
# few MB). A booby-trapped file of a few hundred KB can hide a member that balloons to
# several GB once decompressed → OOM on a mere read. We refuse to decompress beyond
# these bounds.
MAX_XML_MEMBER = 64 * 1024 * 1024            # 64 MiB decompressed per read member
MAX_ZIP_RATIO = 200                          # max tolerated inflation (decompressed / compressed)


def _zip_read_member(z, name):
    """Reads an archive member while BOUNDING the announced decompressed size and the
       compression ratio (anti memory-DoS). Raises ValueError beyond the caps — the
       caller (try/except) then fails cleanly by returning None/{}."""
    info = z.getinfo(name)                    # KeyError if absent: handled by the caller
    if info.file_size > MAX_XML_MEMBER:
        raise ValueError(f"member {name} too large ({info.file_size} bytes)")
    if info.compress_size > 0 and info.file_size / info.compress_size > MAX_ZIP_RATIO:
        raise ValueError(f"aberrant compression ratio for {name}")
    return z.read(name)                       # read() stops at file_size anyway


# Anti "billion laughs" / XXE (DoS by entity expansion) WITHOUT depending on the host's
# libexpat (versions < 2.4.0 do not bound amplification). We REFUSE any document that
# declares internal entities (<!ENTITY …>) — the sole vector of these attacks.
# A DOCTYPE WITHOUT an entity (e.g. MusicXML, which references an external DTD that
# ElementTree never downloads) stays accepted.
_XML_DOCTYPE_RE = re.compile(rb'<!DOCTYPE[^>\[]*(?:\[[^\]]*\])?\s*>', re.IGNORECASE | re.DOTALL)
_XML_ENTITY_RE = re.compile(rb'<!ENTITY\b', re.IGNORECASE)


def _xml_reject_entities(raw):
    """Raises ValueError if `raw` declares XML entities (anti billion-laughs / XXE)."""
    m = _XML_DOCTYPE_RE.search(raw)
    if m and _XML_ENTITY_RE.search(m.group(0)):
        raise ValueError("XML entity declaration refused (anti-DoS)")


# Above this size, a member is copied by STREAMING (chunks) instead of zin.read():
# inflating a whole member in RAM would let a trapped zip of a few MB on disk, whose
# member ANNOUNCES gigabytes, exhaust the memory on a mere metadata edit. The read
# path is already capped (_zip_read_member); this bounds the write path too.
_ZIP_STREAM_THRESHOLD = 4 * 1024 * 1024


def _zip_replace(path, member_name, new_bytes):
    tmp = _fresh_tmp(path)
    try:
        with zipfile.ZipFile(path, "r") as zin, \
             zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            zout.comment = zin.comment
            names = [it.filename for it in zin.infolist()]
            for item in zin.infolist():
                # read by ZipInfo (not by name): a duplicate name does not lose an entry
                if item.filename == member_name:
                    zout.writestr(item, new_bytes)
                elif item.file_size <= _ZIP_STREAM_THRESHOLD:
                    zout.writestr(item, zin.read(item))
                else:
                    with zin.open(item) as src, zout.open(item, mode="w") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
            if member_name not in names:
                zout.writestr(member_name, new_bytes)
        _replace_keep_mode(tmp, path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def _zip_meta_members(path):
    """For a stdlib zip engine (OOXML/ODF/EPUB), the list of archive members that carry
       metadata — otherwise None. Used for the undo snapshot of a wipe: we copy ONLY
       these members (small: a core.xml/meta.xml/OPF weighs a few KB whatever the size
       of the document → cost independent of size, not a copy of the file). The wipe can
       then erase everything and the undo restores these members bit-for-bit."""
    eng = engine_for(path)
    if eng == "ooxml":
        return ["docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"]
    if eng == "odf":
        return ["meta.xml"]
    if eng == "epub":
        opf = _epub_opf_path(path)
        return [opf] if opf else []
    if eng == "cbz":
        # cbz_wipe REPLACES ComicInfo.xml outright: without a raw snapshot, every
        # field outside CBZ_TAGS (Colorist, Inker, <Pages>…) was lost forever on undo.
        return [CBZ_MEMBER]
    return None


def _zip_snapshot_members(path):
    """{member_name: bytes} of the PRESENT metadata members, for the undo snapshot."""
    names = _zip_meta_members(path)
    if names is None:
        return None
    out = {}
    try:
        with zipfile.ZipFile(path) as z:
            present = set(z.namelist())
            for m in names:
                if m in present:
                    out[m] = _zip_read_member(z, m)
    except Exception:
        return {}
    return out


def _xml_parse(path, member_name):
    try:
        with zipfile.ZipFile(path) as z:
            raw = _zip_read_member(z, member_name)
        _xml_reject_entities(raw)
        _register_ns(raw)
        return ET.fromstring(raw)
    except Exception:
        return None


def ooxml_read(path, all_tags=False):
    data = {}
    root = _xml_parse(path, "docProps/core.xml")    # None if no core.xml: app.xml alone is enough
    if root is None:
        # No readable core.xml: if it is at least a readable zip, it is a valid
        # container without metadata; otherwise wrong format -> None (refused), like
        # the other zip engines (cbz/kmz/odf/epub) — a corrupted .docx must not open
        # an empty editable session.
        try:
            with zipfile.ZipFile(path):
                pass
        except Exception:
            return None
    if root is not None:
      for name, (ns, tag) in OOXML_TAGS.items():
        el = root.find(f"{{{OOXML_NS[ns]}}}{tag}")
        if el is not None and el.text:
            data[name] = el.text
    app_root = _xml_parse(path, "docProps/app.xml")
    if app_root is not None:
        for child in app_root:
            t = child.tag.split("}", 1)[-1]
            if child.text and t in ("Company", "Application", "AppVersion",
                                     "Manager", "Template", "PageCount", "WordCount"):
                data[t] = child.text
    return data


def ooxml_write(path, tag, value):
    if tag not in OOXML_TAGS:
        return False
    ns, xml_tag = OOXML_TAGS[tag]
    root = _xml_parse(path, "docProps/core.xml")
    if root is None:
        ns_attrs = " ".join(f'xmlns:{p}="{u}"' for p, u in OOXML_NS.items())
        root = ET.fromstring(f'<cp:coreProperties {ns_attrs}/>')
    for prefix, uri in OOXML_NS.items():
        ET.register_namespace(prefix, uri)
    el = root.find(f"{{{OOXML_NS[ns]}}}{xml_tag}")
    if value == "":
        if el is not None:
            root.remove(el)
    else:
        if el is None:
            el = ET.SubElement(root, f"{{{OOXML_NS[ns]}}}{xml_tag}")
        el.text = value
        if ns == "dcterms":
            el.set(f"{{{OOXML_NS['xsi']}}}type", "dcterms:W3CDTF")
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root)
    return _zip_replace(path, "docProps/core.xml", xml_bytes)


def ooxml_writable(path):
    return set(OOXML_TAGS.keys())


def ooxml_wipe(path):
    """Erases ALL metadata: all children of core.xml (beyond OOXML_TAGS: revision,
       lastPrinted, contentStatus…), of app.xml (Company, Manager, Application,
       Template, statistics…) AND of custom.xml (custom document properties —
       arbitrary identifying fields that would otherwise silently survive the wipe).
       Undoable bit-for-bit (snapshot of the raw members)."""
    try:
        with zipfile.ZipFile(path):                 # not a zip -> clean failure, like the
            pass                                     # other zip engines: never a FALSE "cleaned"
    except Exception:                                # on a mis-named .docx (its real metadata survives)
        return False
    ok = True
    for member in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"):
        root = _xml_parse(path, member)
        if root is None or len(root) == 0:
            continue
        for prefix, uri in OOXML_NS.items():
            ET.register_namespace(prefix, uri)
        for child in list(root):
            root.remove(child)
        xml_bytes = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + ET.tostring(root)
        ok = _zip_replace(path, member, xml_bytes) and ok
    return ok


def odf_read(path, all_tags=False):
    root = _xml_parse(path, "meta.xml")
    if root is None:
        return None
    meta = root.find(f"{{{ODF_NS['office']}}}meta")
    if meta is None:
        return {}
    data = {}
    for name, (ns, tag) in ODF_TAGS.items():
        elements = meta.findall(f"{{{ODF_NS[ns]}}}{tag}")
        if elements:
            texts = [e.text for e in elements if e.text]
            if texts:
                data[name] = ", ".join(texts) if len(texts) > 1 else texts[0]
    return data


def odf_write(path, tag, value):
    if tag not in ODF_TAGS:
        return False
    ns, xml_tag = ODF_TAGS[tag]
    root = _xml_parse(path, "meta.xml")
    if root is None:
        return False
    for prefix, uri in ODF_NS.items():
        ET.register_namespace(prefix, uri)
    meta = root.find(f"{{{ODF_NS['office']}}}meta")
    if meta is None:
        return False
    for e in meta.findall(f"{{{ODF_NS[ns]}}}{xml_tag}"):
        meta.remove(e)
    if value != "":
        el = ET.SubElement(meta, f"{{{ODF_NS[ns]}}}{xml_tag}")
        el.text = value
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root)
    return _zip_replace(path, "meta.xml", xml_bytes)


def odf_writable(path):
    return set(ODF_TAGS.keys())


def odf_wipe(path):
    """Erases ALL metadata: all children of <office:meta> (beyond ODF_TAGS:
       dc:creator (last author), meta:generator, meta:user-defined, editing-cycles,
       statistics…). Undoable bit-for-bit (snapshot of meta.xml)."""
    root = _xml_parse(path, "meta.xml")
    if root is None:
        return False
    meta = root.find(f"{{{ODF_NS['office']}}}meta")
    if meta is None:
        return True                                    # no meta block: already without metadata
    for prefix, uri in ODF_NS.items():
        ET.register_namespace(prefix, uri)
    for child in list(meta):
        meta.remove(child)
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root)
    return _zip_replace(path, "meta.xml", xml_bytes)


def _epub_opf_path(path):
    root = _xml_parse(path, "META-INF/container.xml")
    if root is None:
        return None
    ns = "urn:oasis:names:tc:opendocument:xmlns:container"
    rootfile = root.find(f".//{{{ns}}}rootfile")
    return rootfile.get("full-path") if rootfile is not None else None


def epub_read(path, all_tags=False):
    opf = _epub_opf_path(path)
    if not opf:
        return None
    root = _xml_parse(path, opf)
    if root is None:
        return None
    metadata = root.find(f"{{{EPUB_NS['opf']}}}metadata")
    if metadata is None:
        return {}
    data = {}
    for name, xml_tag in EPUB_TAGS.items():
        elements = metadata.findall(f"{{{EPUB_NS['dc']}}}{xml_tag}")
        if elements:
            texts = [e.text for e in elements if e.text]
            if texts:
                data[name] = ", ".join(texts) if len(texts) > 1 else texts[0]
    return data


def epub_write(path, tag, value):
    if tag not in EPUB_TAGS:
        return False
    opf = _epub_opf_path(path)
    if not opf:
        return False
    root = _xml_parse(path, opf)                 # _xml_parse has already re-registered the namespaces
    if root is None:
        return False
    metadata = root.find(f"{{{EPUB_NS['opf']}}}metadata")
    if metadata is None:
        return False
    xml_tag = EPUB_TAGS[tag]
    uid = root.get("unique-identifier")
    elements = metadata.findall(f"{{{EPUB_NS['dc']}}}{xml_tag}")

    if xml_tag == "identifier":
        # We touch ONLY the unique identifier, and NEVER remove/blank it: deleting it
        # would leave the unique-identifier reference dangling (invalid EPUB). A blank is
        # REFUSED (return False) rather than silently no-op'd: reporting a phantom "cleared"
        # — logging the change, stacking an undo step, showing "identifier → (empty)" — when
        # nothing changed would be a lie. The caller turns this into a specific message.
        if value == "":
            return False
        uid_el = next((e for e in elements if e.get("id") == uid), None)
        if uid_el is None:
            uid_el = elements[0] if elements else ET.SubElement(
                metadata, f"{{{EPUB_NS['dc']}}}identifier")
            if uid and not uid_el.get("id"):
                uid_el.set("id", uid)
        uid_el.text = value                      # in-place update → the id attribute is preserved
    else:
        required = xml_tag in ("title", "language")   # mandatory elements of a valid EPUB
        if value == "":
            for e in elements[1:]:
                metadata.remove(e)
            if elements:
                if required:
                    elements[0].text = ""
                else:
                    metadata.remove(elements[0])
        else:
            if elements:
                elements[0].text = value
                for e in elements[1:]:
                    metadata.remove(e)
            else:
                el = ET.SubElement(metadata, f"{{{EPUB_NS['dc']}}}{xml_tag}")
                el.text = value
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root)
    return _zip_replace(path, opf, xml_bytes)


def epub_writable(path):
    return set(EPUB_TAGS.keys())


def epub_wipe(path):
    """Erases ALL metadata from the OPF (dc identifying fields, custom <meta name=…> —
       calibre type —, dc:source/type/format/coverage…). We PRESERVE the strict minimum
       of a valid EPUB: the unique identifier (referenced by unique-identifier), the
       dc:title / dc:language elements (kept but emptied) and the structural EPUB 3
       <meta property|refines>. Undoable bit-for-bit (snapshot of the raw OPF)."""
    opf = _epub_opf_path(path)
    if not opf:
        return False
    root = _xml_parse(path, opf)
    if root is None:
        return False
    metadata = root.find(f"{{{EPUB_NS['opf']}}}metadata")
    if metadata is None:
        return True
    for prefix, uri in EPUB_NS.items():
        ET.register_namespace(prefix, uri)
    uid = root.get("unique-identifier")
    dc = EPUB_NS["dc"]
    kept_required = set()
    for child in list(metadata):
        if child.tag == f"{{{dc}}}identifier" and child.get("id") == uid:
            continue                                       # unique identifier: preserved (validity)
        if child.tag in (f"{{{dc}}}title", f"{{{dc}}}language") and child.tag not in kept_required:
            child.text = ""                                # required: element kept, emptied
            kept_required.add(child.tag)
            continue
        local = child.tag.split("}", 1)[-1]
        if local == "meta" and (child.get("property") or child.get("refines")):
            continue                                       # structural EPUB 3 <meta>: kept
        metadata.remove(child)
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root)
    return _zip_replace(path, opf, xml_bytes)


# ============================================================
#  Jupyter Notebook engine (.ipynb)
# ============================================================

def ipynb_load(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:   # -sig: tolerates a BOM
            return json.load(f)
    except Exception:
        return None


def ipynb_save(path, nb):
    tmp = _fresh_tmp(path)
    try:
        with open(tmp, "w", encoding=_json_enc(path)) as f:
            json.dump(nb, f, indent=1, ensure_ascii=False)
            f.write("\n")
        _replace_keep_mode(tmp, path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def ipynb_read(path, all_tags=False):
    nb = ipynb_load(path)
    if nb is None:
        return None
    if not isinstance(nb, dict):                 # non-object root (e.g. JSON list): nothing to read
        return {}
    meta = nb.get("metadata")
    if not isinstance(meta, dict):               # metadata absent / non-object (42, [1,2], "x"): no crash
        meta = {}
    data = {}
    title = meta.get("title")
    if isinstance(title, str) and title:
        data["Title"] = title
    authors = meta.get("authors")
    if isinstance(authors, list):
        names = [a.get("name") for a in authors
                 if isinstance(a, dict) and isinstance(a.get("name"), str) and a.get("name")]
        if names:
            data["Authors"] = ", ".join(names)
    return data


def ipynb_write(path, tag, value):
    nb = ipynb_load(path)
    if not isinstance(nb, dict):                 # non-object root: read() opened it empty, nothing to write
        return False
    meta = nb.setdefault("metadata", {})
    if tag == "Title":
        if value == "":
            meta.pop("title", None)
        else:
            meta["title"] = value
    elif tag == "Authors":
        if value == "":
            meta.pop("authors", None)
        else:
            names = [n.strip() for n in value.split(",") if n.strip()]
            if not names:
                return False
            meta["authors"] = [{"name": n} for n in names]
    else:
        return False
    return ipynb_save(path, nb)


def ipynb_writable(path):
    return set(SUGGESTED["ipynb"])


def ipynb_wipe(path):
    nb = ipynb_load(path)
    if not isinstance(nb, dict):                 # non-object root: nothing to wipe (aligns with read/write)
        return False
    meta = nb.get("metadata")
    if isinstance(meta, dict):
        meta.pop("title", None)
        meta.pop("authors", None)
    return ipynb_save(path, nb)


# ============================================================
#  Comic Book engine (.cbz: ZIP of pages + ComicInfo.xml)
# ============================================================
# .cbz = ZIP of pages + optional "ComicInfo.xml" at the root. Fields as direct
# children of <ComicInfo>, WITHOUT a namespace; Year/Month/Day assembled into a
# canonical Date. Only ComicInfo.xml is rewritten (via _zip_replace); pages untouched.

CBZ_MEMBER = "ComicInfo.xml"

# Exposed semantic field -> ComicInfo element (direct child, without a namespace).
# The Date is handled separately (composed of Year/Month/Day).
CBZ_TAGS = {
    "Title": "Title",
    "Series": "Series",
    "Number": "Number",
    "Writer": "Writer",
    "Penciller": "Penciller",
    "Publisher": "Publisher",
    "Genre": "Genre",
    "Description": "Summary",      # Summary -> Description (consistent with EPUB/OOXML)
    "Language": "LanguageISO",
    "Web": "Web",
}


def cbz_read(path, all_tags=False):
    root = _xml_parse(path, CBZ_MEMBER)
    if root is None:
        # No ComicInfo.xml: if it is indeed a readable zip, we return an empty
        # dict (valid file, no metadata); otherwise wrong format -> None.
        try:
            with zipfile.ZipFile(path):
                return {}
        except Exception:
            return None
    if root.tag != "ComicInfo":
        return {}
    data = {}
    for name, xml_tag in CBZ_TAGS.items():
        el = root.find(xml_tag)
        if el is not None and el.text and el.text.strip():
            data[name] = el.text.strip()

    def _num(el):
        if el is not None and el.text and el.text.strip().lstrip("-").isdigit():
            try:
                return int(el.text.strip())
            except ValueError:                  # >4300 digits: Python 3.11 str→int limit
                return None
        return None

    yy = _num(root.find("Year"))
    mm = _num(root.find("Month"))
    dd = _num(root.find("Day"))
    if yy is not None:
        try:
            # Validate only the components actually present, and REPORT only those: a bare
            # <Year> stays "1982" and is never padded to an invented January 1st (granularity
            # doctrine, cf. SEMANTIC_DATE_TAGS). A full Year/Month/Day keeps its historic
            # "…:00:00:00" form so existing round-trips are unchanged.
            datetime.date(yy, mm if mm else 1, dd if dd else 1)
            if mm and dd:
                data["Date"] = f"{yy:04d}:{mm:02d}:{dd:02d} 00:00:00"
            elif mm:
                data["Date"] = f"{yy:04d}:{mm:02d}"                 # year+month, no invented day
            else:
                data["Date"] = f"{yy:04d}"                          # bare year, no invented month/day
        except (ValueError, OverflowError):
            # Impossible month/day on a plausible year -> keep the year alone; a <Year>
            # too big for datetime (OverflowError) is not a year at all -> drop the date.
            if 1000 <= yy <= 9999:
                data["Date"] = f"{yy:04d}"
    if all_tags:
        pc = root.find("PageCount")
        if pc is not None and pc.text and pc.text.strip():
            data.setdefault("PageCount", pc.text.strip())
    return data


def _cbz_root(path):
    """Existing <ComicInfo> root, or a new one if absent/unreadable."""
    root = _xml_parse(path, CBZ_MEMBER)
    if root is None or root.tag != "ComicInfo":
        root = ET.Element("ComicInfo")
    return root


def _cbz_set(root, xml_tag, value):
    el = root.find(xml_tag)
    if value == "":
        if el is not None:
            root.remove(el)
    else:
        if el is None:
            el = ET.SubElement(root, xml_tag)
        el.text = value


def _cbz_date_parts(value):
    """(year, month, day) for a ComicInfo <Date>, PRESERVING granularity: a bare year
       yields (year, None, None) so cbz_write never fabricates a Month/Day (granularity
       doctrine, cf. SEMANTIC_DATE_TAGS). Accepts a value already stored (colons or ISO,
       from a copy) or a permissive user input ("15/06/1982", "06/1982", "1982"). Returns
       None if unreadable. The calendar is validated for the precision actually present."""
    v = value.strip()
    # Already-stored form: the string itself carries the granularity ("1982", "1982:06",
    # "1982:06:15", "1982:06:15 00:00:00", or the ISO dash variants). We require a leading
    # 4-digit year, so a day-first user input ("15/06/1982") falls through to parse_date.
    m = re.match(r"^(\d{4})(?:[:\-](\d{2})(?:[:\-](\d{2}))?)?(?:[ T].*)?$", v)
    if not m:
        canon = parse_date(v, granular=True)        # permissive input -> truncated canonical
        if canon is None:
            return None
        m = re.match(r"^(\d{4})(?::(\d{2})(?::(\d{2}))?)?", canon)
    y, mo, d = m.group(1), m.group(2), m.group(3)
    try:
        datetime.date(int(y), int(mo) if mo else 1, int(d) if d else 1)
    except (ValueError, OverflowError):
        return None
    return (int(y), int(mo) if mo else None, int(d) if d else None)


def cbz_write(path, tag, value):
    try:
        root = _cbz_root(path)
        if tag == "Date":
            if value == "":
                for t in ("Year", "Month", "Day"):
                    _cbz_set(root, t, "")
            else:
                parts = _cbz_date_parts(value)
                if parts is None:
                    return False
                y, mo, d = parts
                _cbz_set(root, "Year", str(y))
                # Absent components are CLEARED (""), never invented: a bare year drops any
                # stale Month/Day so the file never claims a precision the user did not give.
                _cbz_set(root, "Month", str(mo) if mo is not None else "")
                _cbz_set(root, "Day", str(d) if d is not None else "")
        elif tag in CBZ_TAGS:
            _cbz_set(root, CBZ_TAGS[tag], value)
        else:
            return False
        xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root)
        return _zip_replace(path, CBZ_MEMBER, xml_bytes)   # creates ComicInfo.xml if missing
    except Exception:
        return False


def cbz_writable(path):
    return set(CBZ_TAGS.keys()) | {"Date"}


def cbz_wipe(path):
    """Clears all editable fields: minimal ComicInfo.xml. The page images and the rest
       of the zip are not touched."""
    try:
        with zipfile.ZipFile(path):
            pass
    except Exception:
        return False
    root = ET.Element("ComicInfo")
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root)
    return _zip_replace(path, CBZ_MEMBER, xml_bytes)


# ============================================================
#  Playlist engine (.m3u / .m3u8)
# ============================================================

_M3U_HEADER = "#EXTM3U"
_PLAYLIST_RE = re.compile(r"^#PLAYLIST:(.*)$", re.IGNORECASE)


def _extinf_head(s):
    """Returns "#EXTINF:<duration[ attrs]>," (title dropped), or None if s is not an
       #EXTINF line. Splits on the FIRST comma OUTSIDE quotes, not the first comma: an
       extended IPTV attribute (group-title="News, Sports") holds its own commas, and a
       plain regex cut would land inside it and corrupt the line."""
    if not s.upper().startswith("#EXTINF:"):
        return None
    in_q = False
    for i, ch in enumerate(s):
        if ch == '"':
            in_q = not in_q
        elif ch == "," and not in_q:
            return s[:i + 1]
    return None


def _split_text_lines(text):
    """Splits on REAL line endings only (\\r\\n, \\r, \\n). str.splitlines() also breaks
       on \\x0b \\x0c \\x1c \\x1d \\x1e \\x85 and U+2028/U+2029 — and a cp1252 playlist
       read as latin-1 can carry 0x85 ("…") INSIDE a media path: that path would be cut
       in two lines and the entry mutilated at the next save."""
    lines = re.split(r"\r\n|\r|\n", text)
    if lines and lines[-1] == "":
        lines.pop()                     # trailing newline: no phantom empty last line
    return lines


def _m3u_load(path):
    """Reads the playlist file into lines (without line endings), or None if unreadable.
       UTF-8 (tolerates the BOM of .m3u8 via utf-8-sig) with a latin-1 fallback for old
       non-UTF-8 .m3u. Never crashes on binary/truncated content."""
    try:
        with io.open(path, "r", encoding="utf-8-sig", errors="strict") as f:
            text = f.read()
    except (UnicodeDecodeError, OSError, ValueError):
        try:
            with io.open(path, "r", encoding="latin-1", errors="replace") as f:
                text = f.read()
        except (OSError, ValueError):
            return None
    except Exception:
        return None
    return _split_text_lines(text)


def _text_format(path):
    """Detects (encoding, line ending) of the existing file in order to PRESERVE them on
       rewrite: BOM (utf-8-sig), old latin-1, and CRLF/CR/LF. Prevents editing a title
       from transforming the whole file (mojibake of paths, massive diff)."""
    try:
        raw = Path(path).read_bytes()
    except Exception:
        return "utf-8", "\n"
    if raw.startswith(b"\xef\xbb\xbf"):
        enc, sample = "utf-8-sig", raw[3:]
    else:
        try:
            raw.decode("utf-8")
            enc = "utf-8"
        except UnicodeDecodeError:
            enc = "latin-1"
        sample = raw
    nl = "\r\n" if b"\r\n" in sample else ("\r" if b"\r" in sample else "\n")
    return enc, nl


def _save_text_lines(path, lines):
    """Atomic write of lines while preserving the original encoding/BOM/line ending."""
    enc, nl = _text_format(path)
    tmp = _fresh_tmp(path)
    try:
        with io.open(tmp, "w", encoding=enc, newline=nl) as f:
            f.write("\n".join(lines))
            if lines:
                f.write("\n")
        _replace_keep_mode(tmp, path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def _m3u_is_media(line):
    s = line.strip()
    return bool(s) and not s.startswith("#")


def m3u_read(path, all_tags=False):
    """Semantic fields of a playlist:
       Title (#PLAYLIST: directive) and TrackCount (read-only: number of #EXTINF
       entries, or failing that of media lines)."""
    lines = _m3u_load(path)
    if lines is None:
        return None
    data = {}
    title = None
    extinf = 0
    media = 0
    for line in lines:
        s = line.strip()
        m = _PLAYLIST_RE.match(s)
        if m:
            if title is None:
                title = m.group(1).strip()
            continue
        if s.upper().startswith("#EXTINF:"):
            extinf += 1
            continue
        if _m3u_is_media(line):
            media += 1
    if title:
        data["Title"] = title
    data["TrackCount"] = str(extinf if extinf else media)
    return data


def m3u_write(path, tag, value):
    """Only Title is editable. Rewrites/inserts "#PLAYLIST:name" at the top, just
       after #EXTM3U (created if missing). value="" clears the #PLAYLIST: directive.
       The media paths and their order are preserved."""
    if tag != "Title":
        return False
    lines = _m3u_load(path)
    if lines is None:
        return False
    out = []
    inserted = False
    header_seen = False
    for line in lines:
        s = line.strip()
        if _PLAYLIST_RE.match(s):
            continue
        out.append(line)
        # startswith, not ==: an IPTV header carrying attributes ("#EXTM3U url-tvg=…")
        # is still THE header — a second bare #EXTM3U must not be inserted above it.
        if not header_seen and s.upper().startswith(_M3U_HEADER):
            header_seen = True
            if value != "":
                out.append(f"#PLAYLIST:{value}")
                inserted = True
    if value != "" and not inserted:
        out = [_M3U_HEADER, f"#PLAYLIST:{value}"] + out
    return _save_text_lines(path, out)


def m3u_writable(path):
    return {"Title"}


def m3u_wipe(path):
    """Clears all editable fields: removes #PLAYLIST: and the human-readable title
       from the #EXTINF lines (keeping "#EXTINF:<duration>,"). The media paths/URLs
       and their order are intact."""
    lines = _m3u_load(path)
    if lines is None:
        return False
    out = []
    for line in lines:
        s = line.strip()
        if _PLAYLIST_RE.match(s):
            continue
        head = _extinf_head(s)
        if head is not None:
            out.append(head)
            continue
        out.append(line)
    return _save_text_lines(path, out)


# ============================================================
#  Property List engine (.plist / .webloc / .mobileconfig) — plistlib
# ============================================================
# plistlib reads XML AND binary (auto-detection). We expose the top-level "scalar"
# fields (broad reading), but we only REWRITE/clear a whitelist of safe semantic
# fields — never the structural keys (PayloadType, PayloadUUID, CFBundleIdentifier…)
# whose alteration would break the file.

PLIST_EDITABLE = (
    "URL",                                              # .webloc: bookmark target
    "PayloadDisplayName", "PayloadDescription", "PayloadOrganization",  # .mobileconfig
    "CFBundleName", "CFBundleDisplayName", "NSHumanReadableCopyright",  # Info.plist
    "Title", "Author", "Comment", "Description",        # generic keys
)
# Fields shown read-only (context), not editable.
PLIST_READONLY = (
    "CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion",
    "PayloadIdentifier", "PayloadVersion", "PayloadType",
)


def _plist_load(path):
    try:
        with open(path, "rb") as f:
            obj = plistlib.load(f)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _plist_fmt(path):
    """Preserves the original format (binary if the header says so, otherwise XML)."""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
        return plistlib.FMT_BINARY if head == b"bplist00" else plistlib.FMT_XML
    except Exception:
        return plistlib.FMT_XML


def _plist_save(path, obj, fmt):
    tmp = _fresh_tmp(path)
    try:
        with open(tmp, "wb") as f:
            plistlib.dump(obj, f, fmt=fmt)
        _replace_keep_mode(tmp, path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def plist_read(path, all_tags=False):
    obj = _plist_load(path)
    if obj is None:
        return None
    data = {}
    for k in PLIST_EDITABLE + PLIST_READONLY:
        v = obj.get(k)
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            data[k] = str(v)
    return data


def plist_write(path, tag, value):
    if tag not in PLIST_EDITABLE:
        return False
    obj = _plist_load(path)
    if obj is None:
        return False
    if value == "":
        obj.pop(tag, None)
    else:
        obj[tag] = value
    return _plist_save(path, obj, _plist_fmt(path))


def plist_writable(path):
    return set(PLIST_EDITABLE)


def plist_wipe(path):
    obj = _plist_load(path)
    if obj is None:
        return False
    for k in PLIST_EDITABLE:
        obj.pop(k, None)
    return _plist_save(path, obj, _plist_fmt(path))


# ============================================================
#  E-mail engine (.eml) — email
# ============================================================
# Reads/writes the headers of an RFC 5322 message without touching the body. The
# rewrite goes through BytesParser → header edit → as_bytes(), which preserves the
# payload (text, attachments) as-is.

EML_EDITABLE = ("Subject", "From", "To", "Cc", "Reply-To", "Comments", "Keywords")
EML_READONLY = ("Date", "Sender", "Message-ID")


def _eml_load(path, decode=False):
    """Loads a .eml. decode=True → policy.default (decodes RFC2047 headers for a
       readable DISPLAY). decode=False → compat32 (default): preserves the headers
       byte for byte, without folding or re-encoding — indispensable for WRITING
       (otherwise DKIM/References broken)."""
    try:
        with open(path, "rb") as f:
            if decode:
                return email.message_from_binary_file(f, policy=_email_policy.default)
            return email.message_from_binary_file(f)
    except Exception:
        return None


def eml_read(path, all_tags=False):
    msg = _eml_load(path, decode=True)             # reading: we decode for the display
    if msg is None:
        return None
    data = {}
    for h in EML_EDITABLE + EML_READONLY:
        # policy.default parses structured headers (addresses, Date) LAZILY, here —
        # outside the try of _eml_load. A malformed header (bogus From/To/Date from a
        # corrupted .eml) raises AttributeError/IndexError/TypeError/OverflowError there.
        # We isolate EACH header: a rotten field kills neither the read nor the session,
        # and the other valid headers are still read.
        try:
            v = msg.get(h)
            if v:
                data[h.replace("-", "")] = str(v).strip()
        except Exception:
            continue
    return data


def _eml_header_name(tag):
    """Maps the exposed tag (without a dash) to the real header."""
    for h in EML_EDITABLE:
        if h.replace("-", "") == tag:
            return h
    return None


def _eml_save(path, msg):
    """Rewrites the message while PRESERVING its original line endings (CRLF of a
       conforming .eml) and without folding/re-encoding the untouched headers (compat32
       policy of the loaded message)."""
    try:
        linesep = "\r\n" if b"\r\n" in Path(path).read_bytes() else "\n"
    except Exception:
        linesep = "\r\n"
    tmp = _fresh_tmp(path)
    try:
        with open(tmp, "wb") as f:
            email.generator.BytesGenerator(
                f, mangle_from_=False, policy=msg.policy.clone(linesep=linesep)).flatten(msg)
        _replace_keep_mode(tmp, path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def eml_write(path, tag, value):
    header = _eml_header_name(tag)
    if header is None:
        return False
    msg = _eml_load(path)                               # compat32: preserves the headers intact
    if msg is None:
        return False
    del msg[header]                                     # remove all occurrences
    if value != "":
        msg[header] = value
    return _eml_save(path, msg)


def eml_writable(path):
    return {h.replace("-", "") for h in EML_EDITABLE}


# STRUCTURAL headers kept by the wipe: without them the body / attachments no longer
# read. Everything else (From, To, Subject, Date, Received, X-Originating-IP,
# Message-ID, Return-Path, DKIM-Signature, Sender, References…) is metadata to erase.
_EML_KEEP_HEADERS = {"content-type", "content-transfer-encoding", "mime-version",
                     "content-disposition", "content-id", "content-description"}


def _eml_split_headers(raw):
    """Splits (header block, body) of a raw message on the FIRST empty line — the
       earliest of CRLF CRLF / LF LF by POSITION. Picking by separator type instead
       would, on a mixed-endings message (LF headers, a CRLF CRLF further down in the
       body), cut inside the body — and the wipe-undo snapshot built on that cut would
       paste a body fragment back over the file."""
    cuts = [(i, sep) for sep in (b"\r\n\r\n", b"\n\n")
            for i in (raw.find(sep),) if i != -1]
    if not cuts:
        return raw, b""                                 # message without a body: all headers
    i, sep = min(cuts)
    return raw[:i + len(sep)], raw[i + len(sep):]


def eml_wipe(path):
    """Erases ALL header metadata (From, To, Subject, Date, Received,
       X-Originating-IP, Message-ID, Return-Path, DKIM-Signature…) keeping only the
       STRUCTURAL headers (Content-Type, MIME-Version…) needed to read the body.
       The body/attachments are not touched. Undoable (snapshot of the headers)."""
    msg = _eml_load(path)                              # compat32: headers intact
    if msg is None:
        return False
    for h in {k for k in msg.keys() if k.lower() not in _EML_KEEP_HEADERS}:
        del msg[h]                                     # remove all occurrences of h
    return _eml_save(path, msg)


# ============================================================
#  Mailbox engine (.mbox) — mailbox (READ-ONLY)
# ============================================================
# An .mbox is a concatenation of messages. "Editing its metadata" has no single
# unambiguous meaning: we therefore expose it read-only (number of messages +
# headers of the first), without ever rewriting the file.

def mbox_read(path, all_tags=False):
    try:
        raw = Path(path).read_bytes()
    except Exception:
        return None
    if not raw:
        return {"MessageCount": "0"}
    # mbox separator: a "From " line at the start of a line.
    count = 0
    if raw.startswith(b"From "):
        count = 1
    count += raw.count(b"\nFrom ")
    data = {"MessageCount": str(count)}
    first = raw.split(b"\nFrom ", 1)[0]
    try:
        msg = email.message_from_bytes(first, policy=_email_policy.default)
        for h in ("Subject", "From", "Date"):
            v = msg.get(h)
            if v:
                data[h] = str(v).strip()
    except Exception:
        pass
    return data


def mbox_write(path, tag, value):
    return False                                        # read-only


def mbox_writable(path):
    return set()                                        # no editable content field


def mbox_wipe(path):
    return False


# ============================================================
#  Cue Sheet engine (.cue) — re + io (text)
# ============================================================
# Disc-level metadata (before the first TRACK): TITLE, PERFORMER, REM DATE, REM
# GENRE. We edit these header lines while preserving the order, the FILE/TRACK and
# the rest. Exact round-trip like the m3u engine.

CUE_FIELDS = {                                          # exposed tag -> (cue keyword, is_REM)
    "Title": ("TITLE", False),
    "Performer": ("PERFORMER", False),
    "Date": ("DATE", True),
    "Genre": ("GENRE", True),
}


def _cue_load(path):
    try:
        with io.open(path, "r", encoding="utf-8-sig", errors="strict") as f:
            return _split_text_lines(f.read())
    except UnicodeDecodeError:
        try:
            with io.open(path, "r", encoding="latin-1", errors="replace") as f:
                return _split_text_lines(f.read())
        except Exception:
            return None
    except Exception:
        return None


def _cue_header_end(lines):
    """Index of the first TRACK/FILE line (end of the disc header)."""
    for i, line in enumerate(lines):
        s = line.strip().upper()
        if s.startswith("TRACK ") or s.startswith("FILE "):
            return i
    return len(lines)


def cue_read(path, all_tags=False):
    lines = _cue_load(path)
    if lines is None:
        return None
    end = _cue_header_end(lines)
    data = {}
    tracks = sum(1 for l in lines if l.strip().upper().startswith("TRACK "))
    for line in lines[:end]:
        s = line.strip()
        for tag, (kw, is_rem) in CUE_FIELDS.items():
            m = re.match(rf'^(?:REM\s+)?{kw}\s+(.*)$' if is_rem
                         else rf'^{kw}\s+(.*)$', s, re.IGNORECASE)
            if is_rem != s.upper().startswith("REM"):   # REM line ↔ REM field
                continue
            if m:
                val = m.group(1).strip()
                # Remove only ONE wrapping pair of quotes (the one placed by _cue_line
                # for non-REM fields). `.strip('"')` ate all leading/trailing quotes,
                # thus also those of the value ("say \"hi\"" → "say \"hi"). REM fields
                # are not wrapped.
                if not is_rem and len(val) >= 2 and val[0] == '"' and val[-1] == '"':
                    val = val[1:-1]
                if val:
                    data.setdefault(tag, val)
    if tracks:
        data["TrackCount"] = str(tracks)
    return data


def _cue_line(kw, is_rem, value):
    quoted = f'"{value}"' if not is_rem else value
    return f"REM {kw} {quoted}" if is_rem else f'{kw} {quoted}'


def cue_write(path, tag, value):
    if tag not in CUE_FIELDS:
        return False
    lines = _cue_load(path)
    if lines is None:
        return False
    kw, is_rem = CUE_FIELDS[tag]

    def is_target(s):
        s = s.strip()
        if is_rem:
            return bool(re.match(rf'^REM\s+{kw}\b', s, re.IGNORECASE))
        return bool(re.match(rf'^{kw}\b', s, re.IGNORECASE)) and not s.upper().startswith("REM")

    end = _cue_header_end(lines)
    header, body = lines[:end], lines[end:]
    header = [l for l in header if not is_target(l)]
    if value != "":
        header.append(_cue_line(kw, is_rem, value))
    return _save_text_lines(path, header + body)


def cue_writable(path):
    return set(CUE_FIELDS.keys())


def cue_wipe(path):
    return all(cue_write(path, t, "") for t in CUE_FIELDS)


# ============================================================
#  GeoJSON engine (.geojson) — json
# ============================================================
# Many tools add a top-level "name"; that is the only truly editable field. We also
# read, for context, the type, the number of features, the bbox and the CRS (read-only).

def _geojson_load(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:   # -sig: tolerates a BOM (ArcGIS exports)
            obj = json.load(f)
        return obj if isinstance(obj, dict) and "type" in obj else None
    except Exception:
        return None


def geojson_read(path, all_tags=False):
    obj = _geojson_load(path)
    if obj is None:
        return None
    data = {}
    name = obj.get("name")
    if isinstance(name, str) and name:
        data["Name"] = name
    feats = obj.get("features")
    if isinstance(feats, list):
        data["FeatureCount"] = str(len(feats))
    if isinstance(obj.get("bbox"), list):
        data["BBox"] = ", ".join(str(x) for x in obj["bbox"])
    crs = obj.get("crs")
    if isinstance(crs, dict):
        nm = (crs.get("properties") or {}).get("name")
        if isinstance(nm, str) and nm:
            data["CRS"] = nm
    return data


def _json_indent(path):
    """Indentation of the original JSON (number of spaces, '\\t', or None if compact),
       to preserve the formatting and avoid a massive diff on every write."""
    try:
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r'[{\[]\r?\n([ \t]+)', raw)
    if not m:
        return None
    ind = m.group(1)
    return len(ind) if set(ind) <= {" "} else ind


def _json_enc(path):
    """Write encoding that PRESERVES an existing UTF-8 BOM (same doctrine as the m3u8
       engine): editing one field must not strip bytes the file arrived with."""
    try:
        bom = Path(path).read_bytes().startswith(codecs.BOM_UTF8)
    except Exception:
        bom = False
    return "utf-8-sig" if bom else "utf-8"


def _geojson_save(path, obj):
    indent = _json_indent(path)
    tmp = _fresh_tmp(path)
    try:
        with open(tmp, "w", encoding=_json_enc(path)) as f:
            json.dump(obj, f, ensure_ascii=False, indent=indent)
            f.write("\n")
        _replace_keep_mode(tmp, path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def geojson_write(path, tag, value):
    if tag != "Name":
        return False
    obj = _geojson_load(path)
    if obj is None:
        return False
    if value == "":
        obj.pop("name", None)
    else:
        obj["name"] = value
    return _geojson_save(path, obj)


def geojson_writable(path):
    return {"Name"}


def geojson_wipe(path):
    return geojson_write(path, "Name", "")


# ============================================================
#  HTTP Archive engine (.har) — json
# ============================================================
# Network capture ({"log": {...}}). Descriptive fields (creator, browser, counters)
# read-only; only the log's "comment" is annotatable.

def _har_load(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as f:   # -sig: tolerates a BOM
            obj = json.load(f)
        log = obj.get("log") if isinstance(obj, dict) else None
        return (obj, log) if isinstance(log, dict) else (None, None)
    except Exception:
        return None, None


def har_read(path, all_tags=False):
    _, log = _har_load(path)
    if log is None:
        return None
    data = {}
    creator = log.get("creator") or {}
    if isinstance(creator, dict):
        if creator.get("name"):    data["Creator"] = str(creator["name"])
        if creator.get("version"): data["CreatorVersion"] = str(creator["version"])
    browser = log.get("browser") or {}
    if isinstance(browser, dict) and browser.get("name"):
        data["Browser"] = str(browser["name"])
    if log.get("version"):
        data["HARVersion"] = str(log["version"])
    for key, tag in (("pages", "PageCount"), ("entries", "EntryCount")):
        if isinstance(log.get(key), list):
            data[tag] = str(len(log[key]))
    if log.get("comment"):
        data["Comment"] = str(log["comment"])
    return data


def har_write(path, tag, value):
    if tag != "Comment":
        return False
    obj, log = _har_load(path)
    if log is None:
        return False
    if value == "":
        log.pop("comment", None)
    else:
        log["comment"] = value
    return _geojson_save(path, obj)         # same atomic JSON write as geojson


def har_writable(path):
    return {"Comment"}


def har_wipe(path):
    return har_write(path, "Comment", "")


# ============================================================
#  SQLite engine (.sqlite / .sqlite3 / .db) — sqlite3
# ============================================================
# SQLite reserves two header integers as file metadata: application_id (identifies the
# app type) and user_version (schema version). These are the only editable fields; we
# also read version/encoding/tables (read-only). Opened read-only via URI so as not to
# alter anything.

SQLITE_MAGIC = b"SQLite format 3\x00"


def _is_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except Exception:
        return False


def sqlite_read(path, all_tags=False):
    if not _is_sqlite(path):
        return None
    try:
        # URI built via as_uri() to PERCENT-ENCODE the path. An interpolation
        # "file:{path}?mode=ro" breaks as soon as a name contains "#", "?" or "%":
        # the "?mode=ro" is then swallowed as query/fragment, SQLite opens in CREATE
        # mode by default (instead of read-only) → WRONG metadata read without error
        # AND a stray file created on disk during a mere read.
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
    except Exception:
        return None
    try:
        cur = con.cursor()
        data = {}
        data["ApplicationID"] = str(cur.execute("PRAGMA application_id").fetchone()[0])
        data["UserVersion"] = str(cur.execute("PRAGMA user_version").fetchone()[0])
        data["SQLiteVersion"] = sqlite3.sqlite_version
        try:
            enc = cur.execute("PRAGMA encoding").fetchone()
            if enc:
                data["Encoding"] = str(enc[0])
            tables = cur.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()
            if tables:
                data["TableCount"] = str(tables[0])
        except Exception:
            pass
        return data
    except Exception:
        return None
    finally:
        con.close()


SQLITE_EDITABLE = {"ApplicationID": "application_id", "UserVersion": "user_version"}


def sqlite_write(path, tag, value):
    if tag not in SQLITE_EDITABLE or not _is_sqlite(path):
        return False
    raw = value.strip() if value != "" else "0"        # clearing = reset to 0
    try:
        n = int(raw)
    except ValueError:
        return False
    if not (-2147483648 <= n <= 2147483647):            # signed 32-bit integer
        return False
    try:
        con = sqlite3.connect(str(path))
        try:
            con.execute(f"PRAGMA {SQLITE_EDITABLE[tag]} = {n}")
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def sqlite_writable(path):
    return set(SQLITE_EDITABLE.keys())


def sqlite_wipe(path):
    return all(sqlite_write(path, t, "0") for t in SQLITE_EDITABLE)


# ============================================================
#  KMZ engine (.kmz: ZIP containing doc.kml) — zipfile + xml.etree
# ============================================================
# We edit <name> and <description> of the KML <Document> (KML 2.2 namespace),
# without touching the geometries or the rest of the zip.

KML_NS = "http://www.opengis.net/kml/2.2"
KMZ_TAGS = {"Title": "name", "Description": "description"}


def _kmz_member(path):
    """Name of the internal KML: "doc.kml" preferably, otherwise the first *.kml."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except Exception:
        return None
    if "doc.kml" in names:
        return "doc.kml"
    return next((n for n in names if n.lower().endswith(".kml")), None)


def _kml_container(root):
    """Element carrying name/description: <Document> if it exists, otherwise root."""
    doc = root.find(f"{{{KML_NS}}}Document")
    if doc is None:
        doc = root.find("Document")
    return doc if doc is not None else root


def kmz_read(path, all_tags=False):
    member = _kmz_member(path)
    if member is None:
        try:
            with zipfile.ZipFile(path):
                return {}
        except Exception:
            return None
    root = _xml_parse(path, member)
    if root is None:
        return {}
    cont = _kml_container(root)
    data = {}
    for tag, xml_tag in KMZ_TAGS.items():
        el = cont.find(f"{{{KML_NS}}}{xml_tag}")
        if el is None:
            el = cont.find(xml_tag)
        if el is not None and el.text and el.text.strip():
            data[tag] = el.text.strip()
    return data


def kmz_write(path, tag, value):
    if tag not in KMZ_TAGS:
        return False
    member = _kmz_member(path)
    if member is None:
        return False
    root = _xml_parse(path, member)
    if root is None:
        return False
    ET.register_namespace("", KML_NS)
    cont = _kml_container(root)
    xml_tag = KMZ_TAGS[tag]
    # An element without a child is "falsy" in ElementTree: test "is None", never "or".
    el = cont.find(f"{{{KML_NS}}}{xml_tag}")
    if el is None:
        el = cont.find(xml_tag)
    if value == "":
        if el is not None:
            cont.remove(el)
    else:
        if el is None:
            el = ET.SubElement(cont, f"{{{KML_NS}}}{xml_tag}")
        el.text = value
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root)
    return _zip_replace(path, member, xml_bytes)


def kmz_writable(path):
    return set(KMZ_TAGS.keys())


def kmz_wipe(path):
    return all(kmz_write(path, t, "") for t in KMZ_TAGS)


# ============================================================
#  MusicXML engine (.musicxml) — xml.etree
# ============================================================
# Uncompressed score. Metadata in <work><work-title> and <identification> (creator,
# rights, encoding). We edit title/author/rights.

def _xml_file_parse(path):
    try:
        raw = Path(path).read_bytes()
        _xml_reject_entities(raw)
        _register_ns(raw)                        # preserve the original prefixes
        return ET.fromstring(raw)
    except Exception:
        return None


def _xml_doctype(path):
    """The file's DOCTYPE, if any. ElementTree does not model it, so we capture it here to
       restore it verbatim on rewrite: a MusicXML without its DOCTYPE no longer validates."""
    try:
        m = _XML_DOCTYPE_RE.search(Path(path).read_bytes())
    except Exception:
        return b""
    return m.group(0) if m else b""


# `standalone="no"` declares the use of external markup (same story as the DOCTYPE).
# ElementTree does not model it: we capture it to restore it identically.
_XMLDECL_SA_RE = re.compile(rb'<\?xml[^>]*\bstandalone\s*=\s*["\'](yes|no)["\']', re.IGNORECASE)


def _xml_standalone(path):
    try:
        m = _XMLDECL_SA_RE.search(Path(path).read_bytes()[:512])   # the XML declaration is at the top
    except Exception:
        return None
    return m.group(1).decode("ascii").lower() if m else None


def _xml_file_save(path, root):
    tmp = _fresh_tmp(path)
    try:
        doctype = _xml_doctype(path)             # captured BEFORE rewriting
        standalone = _xml_standalone(path)       # original standalone="no"/"yes", restored as-is
        body = ET.tostring(root, encoding="unicode")
        decl = b'<?xml version="1.0" encoding="UTF-8"'
        if standalone:
            decl += b' standalone="' + standalone.encode("ascii") + b'"'
        decl += b'?>\n'
        out = decl
        if doctype:
            out += doctype + b"\n"
        out += body.encode("utf-8")
        Path(tmp).write_bytes(out)
        _replace_keep_mode(tmp, path)
        return True
    except Exception:
        tmp.unlink(missing_ok=True)
        return False


def musicxml_read(path, all_tags=False):
    root = _xml_file_parse(path)
    if root is None or not root.tag.startswith("score-"):
        return None
    data = {}
    wt = root.find("work/work-title")
    if wt is None:
        wt = root.find("movement-title")
    if wt is not None and wt.text and wt.text.strip():
        data["Title"] = wt.text.strip()
    ident = root.find("identification")
    if ident is not None:
        for creator in ident.findall("creator"):
            if creator.text and creator.text.strip():
                data.setdefault("Creator", creator.text.strip())
                break
        rights = ident.find("rights")
        if rights is not None and rights.text and rights.text.strip():
            data["Rights"] = rights.text.strip()
        soft = ident.find("encoding/software")
        if soft is not None and soft.text and soft.text.strip():
            data["Software"] = soft.text.strip()
        edate = ident.find("encoding/encoding-date")
        if edate is not None and edate.text and edate.text.strip():
            data["EncodingDate"] = edate.text.strip()
    return data


# Enforced order of the MusicXML score-header (children of <score-*> BEFORE
# part-list/part). Creating a header at the end of <score-*> (ET.SubElement) breaks
# this ordered content model: we insert it at the right rank.
_MUSICXML_HEADER_ORDER = ("work", "movement-number", "movement-title",
                          "identification", "defaults", "credit", "part-list")


def _musicxml_insert_ordered(root, tag):
    """Creates <tag> and inserts it among the children of root at the rank imposed by
       the schema (before the first child of a strictly higher rank)."""
    order = _MUSICXML_HEADER_ORDER
    rank = order.index(tag) if tag in order else len(order)
    el = ET.Element(tag)
    pos = len(root)
    for i, child in enumerate(root):
        c_rank = order.index(child.tag) if child.tag in order else len(order)
        if c_rank > rank:
            pos = i
            break
    root.insert(pos, el)
    return el


def _musicxml_set_simple(root, parent_tag, child_tag, value):
    """Sets root/<parent>/<child> = value (creates the nodes as needed, at the right
       rank of the score-header), or removes <child> if value is empty."""
    parent = root.find(parent_tag)
    if parent is None:
        if value == "":
            return
        parent = _musicxml_insert_ordered(root, parent_tag)
    el = parent.find(child_tag)
    if value == "":
        if el is not None:
            parent.remove(el)
    else:
        if el is None:
            el = ET.SubElement(parent, child_tag)
        el.text = value


def musicxml_write(path, tag, value):
    root = _xml_file_parse(path)
    if root is None or not root.tag.startswith("score-"):
        return False
    if tag == "Title":
        _musicxml_set_simple(root, "work", "work-title", value)
        # <movement-title> serves as a fallback title on read: we keep it consistent,
        # otherwise two divergent titles remain in the file.
        mt = root.find("movement-title")
        if mt is not None:
            if value == "":
                root.remove(mt)
            else:
                mt.text = value
    elif tag == "Rights":
        _musicxml_set_simple(root, "identification", "rights", value)
    elif tag == "Creator":
        ident = root.find("identification")
        if ident is None:
            if value == "":
                return _xml_file_save(path, root)
            ident = _musicxml_insert_ordered(root, "identification")
        creator = ident.find("creator")
        if value == "":
            if creator is not None:
                ident.remove(creator)
        else:
            if creator is None:
                creator = ET.SubElement(ident, "creator")
                creator.set("type", "composer")
            creator.text = value
    else:
        return False
    return _xml_file_save(path, root)


def musicxml_writable(path):
    return {"Title", "Creator", "Rights"}


def musicxml_wipe(path):
    return all(musicxml_write(path, t, "") for t in ("Title", "Creator", "Rights"))


# ============================================================
#  TCX engine (.tcx) — xml.etree (READ-ONLY)
# ============================================================
# Garmin activity data recorded by a sensor: we expose it read-only (sport, date,
# device), without rewriting (measured data).

TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"


def tcx_read(path, all_tags=False):
    root = _xml_file_parse(path)
    if root is None or not root.tag.endswith("TrainingCenterDatabase"):
        return None
    data = {}

    def find(parent, tag):
        el = parent.find(f"{{{TCX_NS}}}{tag}")
        return el if el is not None else parent.find(tag)

    activities = find(root, "Activities")
    if activities is not None:
        act = find(activities, "Activity")
        if act is not None:
            sport = act.get("Sport")
            if sport:
                data["Sport"] = sport
            idel = find(act, "Id")
            if idel is not None and idel.text and idel.text.strip():
                data["Date"] = idel.text.strip()
    for holder in ("Author", "Creator"):
        h = find(root, holder)
        if h is not None:
            name = find(h, "Name")
            if name is not None and name.text and name.text.strip():
                data["Device"] = name.text.strip()
                break
    return data


def tcx_write(path, tag, value):
    return False                                        # read-only


def tcx_writable(path):
    return set()


def tcx_wipe(path):
    return False


# ============================================================
#  Application packages engine (.jar/.war/.ear/.apk/.xpi/.ipa) — zipfile
# ============================================================
# Signed ZIP containers: editing = invalidated signature → READ-ONLY. We read the
# internal manifest by sub-type:
#   • jar/war/ear/apk : META-INF/MANIFEST.MF (main attributes)
#   • xpi             : manifest.json (WebExtension)
#   • ipa             : Payload/*.app/Info.plist
# The AndroidManifest.xml of a .apk is in binary XML (outside stdlib): we fall back
# to MANIFEST.MF, which stays semantic (not just the FileType).

MANIFEST_MF_TAGS = {
    "Implementation-Title": "Title",
    "Implementation-Version": "Version",
    "Implementation-Vendor": "Vendor",
    "Specification-Title": "SpecTitle",
    "Bundle-Name": "Title",
    "Bundle-SymbolicName": "Identifier",
    "Bundle-Version": "Version",
    "Created-By": "CreatedBy",
    "Built-By": "BuiltBy",
    "Main-Class": "MainClass",
}


def _read_manifest_mf(path):
    try:
        with zipfile.ZipFile(path) as z:
            raw = _zip_read_member(z, "META-INF/MANIFEST.MF")   # bound anti-bomb
    except Exception:
        return None
    text = raw.decode("utf-8", errors="replace")
    # The MANIFEST.MF format folds long lines (continuation by a space); we only read
    # the main section (up to the first empty line).
    main = text.split("\n\n", 1)[0].split("\r\n\r\n", 1)[0]
    data = {}
    for line in main.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, val = line.partition(":")
            tag = MANIFEST_MF_TAGS.get(key.strip())
            if tag and val.strip():
                data.setdefault(tag, val.strip())
    return data


def _read_manifest_json(path):
    try:
        with zipfile.ZipFile(path) as z:
            obj = json.loads(_zip_read_member(z, "manifest.json"))   # bound anti-bomb
    except Exception:
        return None
    if not isinstance(obj, dict):
        return {}
    data = {}
    for key, tag in (("name", "Title"), ("version", "Version"),
                     ("description", "Description")):
        v = obj.get(key)
        if isinstance(v, str) and v:
            data[tag] = v
    author = obj.get("author")
    if isinstance(author, str) and author:
        data["Author"] = author
    elif isinstance(author, dict) and isinstance(author.get("name"), str):
        data["Author"] = author["name"]
    return data


def _read_ipa_plist(path):
    try:
        with zipfile.ZipFile(path) as z:
            member = next((n for n in z.namelist()
                           if re.match(r"Payload/[^/]+\.app/Info\.plist$", n)), None)
            if member is None:
                return {}
            obj = plistlib.loads(_zip_read_member(z, member))   # bound anti-bomb
    except Exception:
        return None
    if not isinstance(obj, dict):
        return {}
    data = {}
    for key, tag in (("CFBundleName", "Title"), ("CFBundleDisplayName", "Title"),
                     ("CFBundleShortVersionString", "Version"),
                     ("CFBundleIdentifier", "Identifier")):
        v = obj.get(key)
        if isinstance(v, str) and v:
            data.setdefault(tag, v)
    return data


def archive_read(path, all_tags=False):
    ext = path.suffix.lstrip(".").lower()
    if ext == "xpi":
        return _read_manifest_json(path)
    if ext == "ipa":
        return _read_ipa_plist(path)
    return _read_manifest_mf(path)                      # jar/war/ear/apk


def archive_write(path, tag, value):
    return False                                        # signed → read-only


def archive_writable(path):
    return set()


def archive_wipe(path):
    return False


# ============================================================
#  Dispatch by engine
# ============================================================

ENGINES = {
    "exiftool": (et_read, et_write, et_writable, et_wipe),
    "mutagen":  (mg_read, mg_write, mg_writable, mg_wipe),
    "ffmpeg":   (ff_read, ff_write, ff_writable, ff_wipe),
    "ooxml":    (ooxml_read, ooxml_write, ooxml_writable, ooxml_wipe),
    "odf":      (odf_read, odf_write, odf_writable, odf_wipe),
    "epub":     (epub_read, epub_write, epub_writable, epub_wipe),
    "ipynb":    (ipynb_read, ipynb_write, ipynb_writable, ipynb_wipe),
    "cbz":      (cbz_read, cbz_write, cbz_writable, cbz_wipe),
    "m3u":      (m3u_read, m3u_write, m3u_writable, m3u_wipe),
    "plist":    (plist_read, plist_write, plist_writable, plist_wipe),
    "eml":      (eml_read, eml_write, eml_writable, eml_wipe),
    "mbox":     (mbox_read, mbox_write, mbox_writable, mbox_wipe),
    "cue":      (cue_read, cue_write, cue_writable, cue_wipe),
    "geojson":  (geojson_read, geojson_write, geojson_writable, geojson_wipe),
    "har":      (har_read, har_write, har_writable, har_wipe),
    "sqlite":   (sqlite_read, sqlite_write, sqlite_writable, sqlite_wipe),
    "kmz":      (kmz_read, kmz_write, kmz_writable, kmz_wipe),
    "musicxml": (musicxml_read, musicxml_write, musicxml_writable, musicxml_wipe),
    "tcx":      (tcx_read, tcx_write, tcx_writable, tcx_wipe),
    "archive":  (archive_read, archive_write, archive_writable, archive_wipe),
}

# Engines WITHOUT any editable content field (write/writable/wipe inert). Only the
# name and file dates are editable there. The default "edit" view would be nearly
# empty there: we open on "all" to show the readable content right away (subject,
# number of messages, manifest…).
_READONLY_ENGINES = frozenset({"mbox", "tcx", "archive"})


def engine_available(eng):
    """True if the external tool required by this engine is present. exiftool and the
       pure-Python engines are assumed available (exiftool is guaranteed by the
       preflight); only ffmpeg and mutagen may be missing at this stage."""
    if eng == "ffmpeg":
        return shutil.which("ffmpeg") is not None
    if eng == "mutagen":
        try:
            import mutagen  # noqa: F401
            return True
        except Exception:
            return False
    return True


def _offset_suffix(dt):
    """Local UTC offset of an aware datetime as '+02:00' / '-05:00' / '+00:00', the exact
       form exiftool appends to file dates (FileModifyDate). '' for a naive datetime."""
    z = dt.strftime("%z")                      # '+0200', '-0500', '+0000' — or '' if naive
    return f"{z[:3]}:{z[3:]}" if z else ""


def _os_create_ts(path):
    """Raw btime timestamp, or None where the platform/filesystem records none. Never
       raises. Separate from _os_create_date: preserving a btime across a rewrite must
       round-trip the instant itself, not a rendered string."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    bt = getattr(st, "st_birthtime", None)
    if bt is None and os.name == "nt":
        # Windows grew st_birthtime in Python 3.12 only (gh-99726); before that the
        # creation time is st_ctime. Windows only: on POSIX st_ctime is inode-change time.
        bt = st.st_ctime
    return bt or None    # None, or 0 = "no real creation time"


def _os_create_date(path):
    """The filesystem creation date (btime), read straight from the OS via st_birthtime —
       NOT from exiftool, which does not expose it on macOS and cannot read it on Linux.
       Returns 'YYYY:MM:DD HH:MM:SS+HH:MM' (local instant, UTC offset appended) so
       FileCreateDate reads exactly like exiftool's FileModifyDate; None where the platform/
       filesystem records no birth time (the caller omits the field). Never raises."""
    bt = _os_create_ts(path)
    if bt is None:
        return None
    try:
        dt = datetime.datetime.fromtimestamp(bt).astimezone()   # local instant, tz-aware
        return dt.strftime("%Y:%m:%d %H:%M:%S") + _offset_suffix(dt)
    except (OverflowError, OSError, ValueError):
        return None


def _inject_create_date(path, data):
    """Adds FileCreateDate (from the OS) to an already-read data dict, unless exiftool
       already provided one (Windows) — read-only, cf. FILE_EXTRA_TAGS."""
    if isinstance(data, dict) and "FileCreateDate" not in data:
        cd = _os_create_date(path)
        if cd:
            data["FileCreateDate"] = cd
    return data


def _set_btime_darwin(path, dt):
    """macOS: set the creation time via the setattrlist() syscall (ATTR_CMN_CRTIME), reached
       through ctypes — no external tool. Returns True on success. Raises OSError on a libc
       error (caught by the caller)."""
    import ctypes, ctypes.util
    ATTR_BIT_MAP_COUNT = 5
    ATTR_CMN_CRTIME = 0x00000200

    class _attrlist(ctypes.Structure):
        _fields_ = [("bitmapcount", ctypes.c_ushort), ("reserved", ctypes.c_uint16),
                    ("commonattr", ctypes.c_uint32), ("volattr", ctypes.c_uint32),
                    ("dirattr", ctypes.c_uint32), ("fileattr", ctypes.c_uint32),
                    ("forkattr", ctypes.c_uint32)]

    class _timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libSystem.dylib",
                       use_errno=True)
    libc.setattrlist.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_size_t, ctypes.c_ulong]
    libc.setattrlist.restype = ctypes.c_int
    req = _attrlist()
    req.bitmapcount = ATTR_BIT_MAP_COUNT
    req.commonattr = ATTR_CMN_CRTIME
    val = _timespec()
    val.tv_sec = int(dt.timestamp())
    val.tv_nsec = 0
    rc = libc.setattrlist(os.fsencode(str(path)), ctypes.byref(req),
                          ctypes.byref(val), ctypes.sizeof(val), 0)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return True


def _set_btime_windows(path, dt):
    """Windows: set the creation time via the Win32 SetFileTime API through ctypes — no
       external tool. Returns True on success, False otherwise. (Standard API; not exercised
       in the Linux CI, only on Windows.)"""
    import ctypes
    from ctypes import wintypes
    FILE_WRITE_ATTRIBUTES = 0x0100
    FILE_SHARE_ALL = 0x0007                    # read | write | delete
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), FILE_WRITE_ATTRIBUTES, FILE_SHARE_ALL, None,
                                  OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
    if not handle or handle == INVALID_HANDLE:
        return False
    try:
        ft = int((dt.timestamp() + 11644473600) * 10_000_000)   # Unix epoch → FILETIME (100ns, 1601)
        creation = _FILETIME(ft & 0xFFFFFFFF, (ft >> 32) & 0xFFFFFFFF)
        kernel32.SetFileTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FILETIME),
                                         ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME)]
        return bool(kernel32.SetFileTime(handle, ctypes.byref(creation), None, None))
    finally:
        kernel32.CloseHandle(handle)


def _stored_dt_tz(value):
    """Trailing UTC offset of a stored date ('+02:00', '+0200', 'Z') as a datetime.timezone,
       or None when there is none. Lets the btime writer honour an explicit offset instead of
       assuming the machine's local one — matching exiftool, which honours it for file dates."""
    s = value.strip()
    if s.endswith("Z"):
        return datetime.timezone.utc
    m = re.search(r"([+-])(\d{2}):?(\d{2})$", s)
    if not m:
        return None
    delta = datetime.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return datetime.timezone(delta if m.group(1) == "+" else -delta)


def _set_os_create_date(path, value):
    """Write the btime at the OS level — exiftool cannot (cf. _os_create_date). `value`
       is canonical 'YYYY:MM:DD HH:MM:SS' with an optional '+HH:MM' offset; empty is
       rejected (a birth time cannot be cleared). Returns True/False. Never raises."""
    if not isinstance(value, str) or not value:
        return False
    dt = parse_stored_dt(value)
    if dt is None:
        return False
    try:
        tz = _stored_dt_tz(value)
    except ValueError:                          # absurd offset (≥ ±24 h): datetime.timezone
        return False                            # refuses it — keep the never-raises promise
    if tz is not None:                          # honour the pasted offset; a naive value keeps
        dt = dt.replace(tzinfo=tz)              # the machine-local reading (dt.timestamp())
    return _write_btime(path, dt)


def _write_btime(path, dt):
    """Write a btime through whichever syscall the OS offers; False where it offers
       none (Linux). Never raises."""
    try:
        system = platform.system()
        if system == "Darwin":
            return _set_btime_darwin(path, dt)
        if system == "Windows":
            return _set_btime_windows(path, dt)
    except OSError:
        return False
    return False


def _keep_create_date(path, ts):
    """Put back a btime captured BEFORE a rewrite (see write(): editing a field must not re-date
       the file's birth). `ts` is the raw timestamp _os_create_ts() returned, so the instant goes
       back exactly as it came. Best-effort: a failure never fails the write itself, which has
       already succeeded — the field was written, only its birth date drifted."""
    if not ts:
        return False
    try:
        dt = datetime.datetime.fromtimestamp(ts)        # naive local: .timestamp() gives ts back
    except (OverflowError, OSError, ValueError):
        return False
    return _write_btime(path, dt)


def read(path, raw=False):
    """Read. If raw, includes all tags; otherwise filters at the display level.
       Does NOT inject empty fields here: it is the display (edit mode) that adds the
       missing suggested fields, so as not to pollute the "all" view."""
    eng = engine_for(path)
    data = ENGINES[eng][0](path, all_tags=raw)
    fellback = False
    if data is None:
        if eng != "exiftool":
            # Dedicated engine absent/unreadable (missing tool, or non-conforming content
            # — a ".db" that is not SQLite, a ".docx" that is not a zip…): we pull in at
            # least the external data via exiftool (file dates, renaming) rather than
            # refusing the file outright. TOLERANT fallback (exiftool exits in error on an
            # unknown type but still delivers name/dates/size).
            data = et_read_lenient(path, all_tags=raw)
            fellback = True
        if data is None:
            return None
    if eng != "exiftool" and not fellback:
        et_basic = et_read(path) or {}
        for t in FILE_BASE_TAGS + FILE_EXTRA_TAGS:
            if t not in data and t in et_basic:
                data[t] = et_basic[t]
    return _inject_create_date(path, data)


def read_many(paths, raw=False):
    """Like read(), but for several files while limiting exiftool calls to a SINGLE
       one (instead of one per file). Returns {path: data}."""
    et_all = et_read_many(paths, all_tags=raw)
    results = {}
    for p in paths:
        eng = engine_for(p)
        if eng == "exiftool":
            d = et_all.get(str(p))
            results[p] = d if d is not None else et_read(p, all_tags=raw)
        else:
            data = ENGINES[eng][0](p, all_tags=raw)
            base = et_all.get(str(p)) or et_read(p) or {}
            if data is None:
                results[p] = base or None
                continue
            for t in FILE_BASE_TAGS + FILE_EXTRA_TAGS:
                if t not in data and t in base:
                    data[t] = base[t]
            results[p] = data
    for p, d in results.items():
        _inject_create_date(p, d)              # OS btime, exiftool never provides it on macOS
    return results


# Reserved device names on Windows: a file named this way redirects to the device
# (loss of content). We refuse them, with or without an extension.
_WIN_RESERVED = {"con", "prn", "aux", "nul",
                 *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _valid_new_name(value):
    """Rejects path separators, the "%" (interpreted by exiftool in -FileName, which
       would produce a corrupted name), empty names, Windows reserved names (CON, NUL,
       COM1…), control characters (a NUL crashes exiftool; the others are
       illegal/tricky) and a name starting with "." (hidden file: invisible in the
       manager AND excluded from metmux's folder expansion — the user would think they
       lost the file)."""
    if not ("/" not in value and "\\" not in value and "%" not in value
            and value.strip(". ") != ""):
        return False
    if value.startswith(".") or re.search(r"[\x00-\x1f\x7f]", value):
        return False
    stem = value.split(".", 1)[0].strip().lower()
    return stem not in _WIN_RESERVED


def apply_filename(path, raw_value):
    """Renames via exiftool → (new_path, None) | (None, error).
       Refuses paths and overwriting an existing file."""
    value = raw_value.strip()
    if not _valid_new_name(value):
        return None, tr("name_invalid")
    if "." not in value:
        value = value + path.suffix
    target = path.with_name(value)
    try:
        # Refuse only a GENUINE collision — a DIFFERENT file already bearing that name. A
        # case-only rename ("photo.jpg" → "Photo.jpg") on a case-insensitive file system
        # (APFS on macOS, NTFS on Windows) makes target.exists() True while target IS path;
        # target.resolve() != path.resolve() used to misread that as a collision (resolve()
        # does not fold case on macOS) and refuse the rename. os.path.samefile confirms the
        # same directory entry (same inode), and the case-insensitive name match rules out a
        # distinct hard-linked sibling with an unrelated name.
        if target.exists() and not (os.path.samefile(target, path)
                                    and path.name.lower() == value.lower()):
            return None, tr("name_exists")
    except OSError:
        # Name too long for the file system (ENAMETOOLONG) or another error:
        # target.exists()/samefile would raise — we refuse cleanly rather than crash.
        return None, tr("name_too_long")
    if et_write(path, "FileName", value):
        _log_change("rename", target, "FileName", value, path.name)
        _RENAMES[str(path)] = target             # walk/group re-entry follow the rename
        if _undo_active():
            _UNDO.record_rename(path, target)
        return target, None
    return None, tr("rename_failed")


# Renames done during the run (undo's rename-back included: it goes through
# apply_filename too), so a batch path taken at launch can be refreshed instead of
# failing as unreadable when its file was renamed from another screen.
_RENAMES = {}


def _live_path(p):
    """The current path of `p` after the session's renames. Follows the map only while
       the file is NOT at the recorded name (an undone rename stops at the original,
       which exists again); the seen-set stops rename cycles (A→B then B→A)."""
    seen = {str(p)}
    while str(p) in _RENAMES and not p.exists():
        nxt = _RENAMES[str(p)]
        if str(nxt) in seen:
            break
        seen.add(str(nxt))
        p = nxt
    return p


# ============================================================
#  Session summary + features (preflight)
# ============================================================

_CHANGELOG = []                 # (action, tag, value, path, old) of successful changes

# Install command per missing tool, matched to the OS metmux RUNS on (the README
# details the other package managers — apt is the Linux default suggested here).
_INSTALL_HINTS = {
    "exiftool": {"Darwin": "brew install exiftool",
                 "Windows": "winget install -e --id OliverBetz.ExifTool",
                 "Linux": "sudo apt install libimage-exiftool-perl"},
    "ffmpeg":   {"Darwin": "brew install ffmpeg",
                 "Windows": "winget install -e --id Gyan.FFmpeg",
                 "Linux": "sudo apt install ffmpeg"},
    "mutagen":  {"Darwin": "pip3 install mutagen",
                 "Windows": "pip3 install mutagen",
                 "Linux": "sudo apt install python3-mutagen"},
}


def _install_hint(tool):
    hints = _INSTALL_HINTS[tool]
    return hints.get(platform.system(), hints["Linux"])

def _log_change(action, path, tag="", value="", old=None):
    # `old` = the field's value FROM BEFORE this change (captured by write()/apply_filename()).
    # It lets _finish() collapse repeated edits of one field into a single "origin → final".
    # An undo's own rewrites are not session changes: _restore_entry removes the undone
    # rows instead (logging them showed phantom rows after a wipe or rename was undone).
    if _UNDO_RESTORING:
        return
    _CHANGELOG.append((action, tag, value, str(path), old))


def _unlog(actions, path, tag=None):
    """Removes the LAST logged row matching (action, path[, tag]) — the row of the change
       an undo has just reverted."""
    p = str(path)
    for i in range(len(_CHANGELOG) - 1, -1, -1):
        action, t, _value, lp, _old = _CHANGELOG[i]
        if action in actions and lp == p and (tag is None or t == tag):
            del _CHANGELOG[i]
            return


def _missing_dependencies(paths):
    """External tools required for this batch but not found: list of
       (name, install command)."""
    engines = {engine_for(p) for p in paths}
    missing = []
    if not (shutil.which("exiftool") or any(
            Path(p).exists() for p in _EXIFTOOL_FALLBACK_PATHS)):
        missing.append(("exiftool", _install_hint("exiftool")))
    if "ffmpeg" in engines and not shutil.which("ffmpeg"):
        missing.append(("ffmpeg", _install_hint("ffmpeg")))
    if "mutagen" in engines:
        try:
            import mutagen  # noqa: F401
        except Exception:
            missing.append(("mutagen (Python module)", _install_hint("mutagen")))
    return missing


def _display_val(v):
    """A captured/written value as shown in the end-of-session summary. Empty (absent
       field, "", empty list) → tr("empty"); a multi-value → joined by ", "."""
    if v is None or v is _ABSENT or v == "" or v == []:
        return tr("empty")
    if isinstance(v, list):
        joined = ", ".join(str(x) for x in v if x not in ("", None))
        return joined or tr("empty")
    return str(v)


def _collapse_changelog():
    """Collapses the raw log into ONE row per (file, field): its ORIGINAL value (before
       the first edit) and its FINAL value (after the last one). Editing a field back and
       forth thus shows a single "origin → final"; a field returned to its origin drops
       out entirely (net no-op). A wipe stays its own row (whole-file purge, no field).
       Preserves first-touch order."""
    rows = {}                                # key -> dict, insertion order = first touch
    for action, tag, value, p, old in _CHANGELOG:
        if action == "wipe":
            for k in [k for k in rows if k[0] == p and k[1] != "\0wipe"]:
                del rows[k]
            rows[(p, "\0wipe")] = {"action": "wipe", "tag": "", "path": p,
                                   "origin": None, "final": ""}
            continue
        key = (p, tag)
        if key not in rows:
            rows[key] = {"action": action, "tag": tag, "path": p,
                         "origin": old, "final": value}
        else:
            rows[key]["action"] = action
            rows[key]["final"] = value
    out = []
    for r in rows.values():
        if r["action"] == "wipe":
            out.append(r)
            continue
        r["origin_s"] = _display_val(r["origin"])
        r["final_s"] = _display_val(r["final"])
        if r["origin_s"] == r["final_s"]:
            continue
        out.append(r)
    return out


def _finish():
    """End-of-session summary, on its own cleared screen. One line per net field change
       (origin → final); nothing to show if no change survived."""
    rows = _collapse_changelog()
    if not rows:
        return
    clear_screen()
    print(f"{BOLD}{tr('changes_made', n=len(rows))}{RESET}\n")
    for r in rows[:20]:
        name = Path(r["path"]).name
        if r["action"] == "wipe":
            print(f"{DIM}  • {name} — {tr('summary_wiped')}{RESET}")
        else:
            lbl = label_of(r["tag"], DEFAULT_LANG)
            print(f"{DIM}  • {name} — {lbl} : {r['origin_s']} → {r['final_s']}{RESET}")
    if len(rows) > 20:
        print(f"{DIM}  {tr('and_more', n=len(rows) - 20)}{RESET}")
    print(f"\n{DIM}{tr('enter_close')}{RESET}")
    ask()


# ============================================================
#  Undo (undo / ua) — snapshot of the METADATA STATE, bounded to the session
# ============================================================
#
#  We remember the value(s) FROM BEFORE each change (never a copy of the file):
#  undoing = rewriting them. Consequences: no stray .bak, and the cost does not
#  depend on the size of the file (undoing a tweak on a 64 GB video costs only its
#  few KB of metadata). The snapshot lives in a temporary folder erased on close —
#  the undo is therefore bounded to the session.

class _AbsentType:
    """Sentinel: the field did not exist before (to RE-DELETE, not to empty)."""
    __slots__ = ()
    def __repr__(self):
        return "<absent>"

_ABSENT = _AbsentType()

_UNDO = None
_UNDO_RESTORING = False     # True during an undo (we don't record the inverse)


def _undo_active():
    return _UNDO is not None and not _UNDO_RESTORING


def _undo_capture_value(path, tag):
    """Current stored value of `tag` (or _ABSENT if the field does not exist).
       mutagen's multi-value structure is preserved: read() would join the list by ","
       and the restore would flatten it (never re-split on "," — cf. mg_read's note)."""
    if engine_for(path) == "mutagen" and tag not in FILE_BASE_TAGS:
        f = mg_load(path)
        if f is None or tag not in f:
            return _ABSENT
        v = f[tag]
        return list(v) if isinstance(v, list) else v
    data = read(path, raw=True) or {}
    v = data.get(tag, _ABSENT)
    if engine_for(path) == "exiftool" and tag in LIST_FIELDS and v is not _ABSENT:
        # Capture the EXACT list (exiftool: string for 1 entry, list for several) so the
        # restore keeps "one entry with a comma" distinct from "two entries".
        return list(v) if isinstance(v, list) else [v]
    return v


def _undo_full_snapshot(path):
    """COMPLETE snapshot for a wipe.
       - zip engines (OOXML/ODF/EPUB): copy of the raw metadata members → the undo
         restores bit-for-bit even what the extended wipe removes (app.xml, dc:creator…).
       - mutagen: we preserve the multi-value structure that read() would flatten
         (the cover art is not capturable here — cf. _wipe_caveat)."""
    members = _zip_snapshot_members(path)
    if members is not None:
        return {"__members__": {m: base64.b64encode(b).decode("ascii")
                                for m, b in members.items()}}
    if engine_for(path) == "eml":
        try:
            headers, _ = _eml_split_headers(Path(path).read_bytes())
        except Exception:
            headers = b""
        return {"__eml_headers__": base64.b64encode(headers).decode("ascii")}
    if engine_for(path) == "m3u":                      # text playlist (small): whole file →
        try:                                           # the undo restores the #EXTINF titles that
            return {"__raw_file__":                    # read() does not capture (lost otherwise at wipe)
                    base64.b64encode(Path(path).read_bytes()).decode("ascii")}
        except Exception:
            return {}
    if engine_for(path) == "mutagen":
        f = mg_load(path)
        if f is not None:
            # str() each item: ASF/APEv2 (.wma/.wv/.ape/.mpc) values are attribute
            # OBJECTS (ASFUnicodeAttribute, APETextValue) that json.dumps cannot
            # serialise — the snapshot would crash the wipe itself, mid-batch. Their
            # str() is the exact stored text and the engines accept it back on restore.
            return {k: ([str(x) for x in f[k]] if isinstance(f[k], list) else str(f[k]))
                    for k in f}
    return read(path, raw=True) or {}


def _undo_restore_value(path, tag, value):
    """Puts a captured value back into the form expected by the target engine's write.
       mutagen: the LIST of a multi-value field is kept as-is (mg_write does
       f[tag]=list, multi-value intact). Exception: a mutagen date/year field is
       SINGLE-value stored in a list (`['2019']`); we turn it back into a string,
       otherwise write() would pass it as-is to format_date()/_year_str() which expect
       a string (TypeError). Outside mutagen: list joined by ",", which et_write
       re-splits via "-sep"."""
    if not isinstance(value, list):
        return value
    if engine_for(path) == "mutagen" and tag not in DATE_TAGS and tag not in YEAR_TAGS:
        return value
    if engine_for(path) == "exiftool" and tag in LIST_FIELDS:
        return value
    return ", ".join(str(x) for x in value if x not in ("", None))


class _UndoBatch:
    """Context: groups all the writes it wraps into ONE undo step (a batch command, or
       a date shift, then undoes with a single "u")."""
    def __init__(self, undo):
        self._undo = undo
    def __enter__(self):
        self._undo._open_batch()
        return self
    def __exit__(self, *exc):
        self._undo._commit_batch()
        return False


class SessionUndo:
    """Undo stack of a session. Each "step" groups the writes of ONE user command
       (a field; a batch; a date shift; a wipe)."""

    def __init__(self):
        self._dir = None         # temporary folder, created on the 1st capture
        self._steps = []          # stack of steps; each step = list of entries
        self._batch = None        # entries of the current command (None outside a batch)

    @property
    def dir(self):
        return self._dir

    def _ensure_dir(self):
        if self._dir is None:
            self._dir = Path(tempfile.mkdtemp(prefix="metmux-undo-"))
        return self._dir

    def has_changes(self):
        return bool(self._steps) or bool(self._batch)

    # --- grouping a multi-write command into ONE single step ---
    def batch(self):
        return _UndoBatch(self)

    def _open_batch(self):
        self._commit_batch()
        self._batch = []

    def _commit_batch(self):
        if self._batch:
            self._steps.append(self._batch)
        self._batch = None

    # --- recording (called by write/wipe/apply_filename BEFORE mutation) ---
    def _add(self, entry):
        self._ensure_dir()
        if self._batch is not None:
            self._batch.append(entry)
        else:
            self._steps.append([entry])

    def record_write(self, path, tag, old):
        self._add(("write", str(path), tag, old))

    def record_wipe(self, path, snapshot):
        self._add(("wipe", str(path), snapshot))

    def record_rename(self, old_path, new_path):
        self._add(("rename", str(old_path), str(new_path)))

    def dump_snapshot(self, data):
        """Serialises a COMPLETE snapshot (for wipe) in the session folder."""
        d = self._ensure_dir()
        idx = sum(len(s) for s in self._steps) + (len(self._batch) if self._batch else 0)
        f = d / f"snap-{idx}.json"
        # default=str: a non-JSON-native value slipped into a snapshot must degrade
        # to its text, never crash the wipe that is being protected.
        f.write_text(json.dumps(data, default=str), encoding="utf-8")
        return str(f)

    # --- undo ---
    # `track` = path of the file followed by the session: if an undo renames it, we
    # return its new path so the session does not read a stale path. Called without
    # `track` (tests, one-shot modes): returns a bool.
    def undo_last(self, track=None):
        self._commit_batch()
        if not self._steps:
            return track if track is not None else False
        track = self._restore_step(self._steps.pop(), track)
        return track if track is not None else True

    def undo_all(self, track=None):
        self._commit_batch()
        if not self._steps:
            return track if track is not None else False
        while self._steps:
            track = self._restore_step(self._steps.pop(), track)
        return track if track is not None else True

    def _restore_step(self, step, track=None):
        global _UNDO_RESTORING
        _UNDO_RESTORING = True
        try:
            for entry in reversed(step):
                track = self._restore_entry(entry, track)
        finally:
            _UNDO_RESTORING = False
        return track

    def _restore_entry(self, entry, track=None):
        kind = entry[0]
        if kind == "write":
            _, path, tag, old = entry
            p = Path(path)
            if old is _ABSENT:
                ok = write(p, tag, "")                  # re-delete (clear -> absent)
            else:
                ok = write(p, tag, _undo_restore_value(p, tag, old))
            if ok:
                _unlog(("write", "clear"), path, tag)
        elif kind == "wipe":
            _, path, snapshot = entry
            data = json.loads(Path(snapshot).read_text(encoding="utf-8"))
            _restore_full_metadata(Path(path), data)
            _unlog(("wipe",), path)
        elif kind == "rename":
            _, old_path, new_path = entry
            renamed, _ = apply_filename(Path(new_path), Path(old_path).name)
            if renamed is not None:
                _unlog(("rename",), new_path)
            if track is not None and Path(track) == Path(new_path):
                track = Path(old_path)
        return track

    def cleanup(self):
        if self._dir is not None and self._dir.exists():
            shutil.rmtree(self._dir, ignore_errors=True)
        self._dir = None
        self._steps = []
        self._batch = None


def _restore_full_metadata(path, data):
    """Rewrites the snapshot taken before a wipe, restoring EVERYTHING the engine knows
       how to re-import — including what is outside the edit whitelist (Make, Model, ISO
       on the exiftool side; free Vorbis keys on the mutagen side). Only the
       non-round-trippable binary blobs stay lost (documented limitation)."""
    # Same net as write()/wipe(): the engine paths below also rename a temporary over
    # the original, so without it the undo of a wipe re-dated the file's birth.
    orig_btime = _os_create_ts(path)
    if "__members__" in data:
        for member, b64 in data["__members__"].items():
            _zip_replace(path, member, base64.b64decode(b64))
    elif "__eml_headers__" in data:
        try:
            _, body = _eml_split_headers(Path(path).read_bytes())
            Path(path).write_bytes(base64.b64decode(data["__eml_headers__"]) + body)
        except Exception:
            pass
    elif "__raw_file__" in data:
        try:
            Path(path).write_bytes(base64.b64decode(data["__raw_file__"]))
        except Exception:
            pass
    else:
        eng = engine_for(path)
        if eng == "exiftool":
            _et_restore(path, data)
        elif eng == "mutagen":
            _mg_restore(path, data)
        else:
            w = writable(path)                          # stdlib: the snapshot = the editable fields
            for tag, val in data.items():
                if tag in FILE_BASE_TAGS or tag not in w:
                    continue
                if isinstance(val, str) and val.startswith("(Binary data"):
                    continue
                write(path, tag, _undo_restore_value(path, tag, val))
    if orig_btime:
        _keep_create_date(path, orig_btime)


def _et_restore(path, data):
    """Restores via "exiftool -json=": writes in a single call all the writable tags of
       the snapshot, ignores composites/read-only/blobs. We exclude the file name and
       system dates so as never to rename them or touch them."""
    skip = set(FILE_BASE_TAGS) | set(FILE_EXTRA_TAGS) | {"SourceFile", "ExifToolVersion"}
    payload = {k: v for k, v in data.items()
               if k not in skip and not (isinstance(v, str) and v.startswith("(Binary data"))}
    if not payload:
        return
    fd, jp = tempfile.mkstemp(suffix=".json", prefix="metmux-etrestore-")
    os.close(fd)
    js = Path(jp)
    try:
        js.write_text(json.dumps([payload]), encoding="utf-8")
        et_run("-json=" + str(js), "-overwrite_original", _ext_arg(path))
    finally:
        js.unlink(missing_ok=True)


def _mg_restore(path, data):
    """Restores ALL the captured mutagen keys (beyond the UI whitelist), in a single
       atomic replacement. Lists stay lists (multi-value)."""
    def mutate(f):
        for tag, val in data.items():
            if tag not in FILE_BASE_TAGS:
                f[tag] = val
    _mg_atomic(path, mutate)


def _with_undo(fn):
    """Decorates a session: opens a fresh undo stack, cleans it up on exit (the
       snapshot never survives the run — undo bounded to it). REENTRANT: when an
       outer scope already owns a stack (run_sessions wraps the whole run), the
       inner session reuses it — that is what lets u/ua survive the n/p walk and
       the group ⇄ single flips instead of dying at each file change."""
    def wrapper(*args, **kwargs):
        global _UNDO
        if _UNDO is not None:                    # outer stack active: share it
            return fn(*args, **kwargs)
        _UNDO = SessionUndo()
        try:
            return fn(*args, **kwargs)
        finally:
            _UNDO.cleanup()
            _UNDO = None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _undo_prompt(done_msg):
    """Result screen of the one-shot wipe mode: Enter to close, or "u" to undo
       everything. With no change to undo, we just wait for Enter."""
    if _UNDO is not None and _UNDO.has_changes():
        print(f"\n{DIM}{tr('close_or_undo')}{RESET}")
        ans = ask()
        if ans is not None and ans.strip().lower() in ("u", "undo", "ua", "undo all"):
            _UNDO.undo_all()
            print(done_msg)
            ask()
    else:
        ask()


# Characters FORBIDDEN in XML 1.0 (Char production), to remove from a value before
# writing. Valid: tab, line feed, carriage return, and [#x20-#xD7FF] [#xE000-#xFFFD]
# [#x10000-#x10FFFF]. Everything else — C0 controls, surrogates, non-characters
# U+FFFE/U+FFFF — breaks the ElementTree engines (docx/odf/epub/cbz/kmz/musicxml),
# which serialise them as-is producing a non-re-parsable file even though write()
# "succeeds", and a NUL crashes exiftool. A value pasted from a PDF/terminal may
# contain some.
_XML_ILLEGAL_RE = re.compile(r"[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]")


def _strip_ctrl(s):
    return _XML_ILLEGAL_RE.sub("", s) if isinstance(s, str) else s


def write(path, tag, value):
    """Writes a value. Sets the date format to the target engine's standard and logs
       the change. Renaming (FileName) does NOT go through here: see apply_filename()."""
    if tag == "FileName":
        return False
    if isinstance(value, str):                  # sanitise: never corrupt the XML nor crash
        value = _strip_ctrl(value)
    elif isinstance(value, list):
        # Sanitise AND remove entries that became empty: a string reduced to "" by
        # _strip_ctrl (a keyword made entirely of control chars, or a "Keywords +<ctrl>"
        # append) would otherwise inject a "-Keywords=" in the middle of the exiftool
        # command — which ERASES the already-set keywords ("cat, dog" lost while ADDING a
        # keyword). On the stdlib engines' side, an empty one would make an "a, , b" at
        # the join.
        value = [s for s in (_strip_ctrl(v) for v in value) if s != ""]
    old = _undo_capture_value(path, tag) if _undo_active() else None
    # Preserve the file's birth date (btime). EVERY engine rewrites the file as a temporary sibling
    # and renames it over the original, so the inode that survives is the TEMPORARY'S: the file is
    # "born" at the instant of the edit. The mtime was already put back (exiftool -P, os.utime
    # elsewhere), the btime was not — so editing any field re-dated the file's birth to now.
    # Alternative rejected: exiftool's -overwrite_original_in_place covers only exiftool's own
    # engines and rewrites the original IN PLACE, giving up "the original is untouched until the
    # rename". Capturing the instant and putting it back costs one stat and covers every engine.
    # Skipped when the tag IS FileCreateDate: that write is the deliberate one.
    orig_btime = None if tag == "FileCreateDate" else _os_create_ts(path)
    if tag == "FileCreateDate":                 # btime: metmux writes it itself,
        ok = _set_os_create_date(path, value)   # never exiftool
    elif tag in FILE_DATE_TAGS:                 # other file dates: always via exiftool (colons)
        ok = et_write(path, tag, value)
    else:
        eng = engine_for(path)
        if tag in YEAR_TAGS:
            stored = _year_str(value)
        elif tag in DATE_TAGS:
            stored = format_date(value, eng)
        else:
            stored = value
        if isinstance(stored, list) and eng not in ("exiftool", "mutagen"):
            # Only exiftool (exact multi-value, no re-split) and mutagen (f[tag]=list)
            # handle a list. The other engines write a SINGLE value (an OOXML/ODF/EPUB
            # Subject/Keywords field is a string): we join the list — otherwise
            # ET.tostring would raise a TypeError on a list .text (session crash on append).
            stored = ", ".join(str(x) for x in stored)
        # Preserve the mtime: the stdlib engines rewrite via tmp.replace() (mtime → now),
        # whereas metmux EXPOSES FileModifyDate as a field. exiftool already preserves it
        # via -P; we align the others by capturing the previous mtime and putting it back
        # afterwards (editing a metadata field must not date the file).
        try:
            orig_mtime = os.stat(path).st_mtime if eng != "exiftool" else None
        except OSError:
            orig_mtime = None
        ok = ENGINES[eng][1](path, tag, stored)
        if ok and orig_mtime is not None:
            try:
                os.utime(path, (os.stat(path).st_atime, orig_mtime))
            except OSError:
                pass
    if ok:
        if orig_btime:
            _keep_create_date(path, orig_btime)
        _log_change("clear" if value == "" else "write", path, tag, value, old)
        if _undo_active():
            _UNDO.record_write(path, tag, old)
    return ok


def write_refusal_reason(path, tag, value):
    """A specific, honest message when write() returned False on a legitimate REFUSAL
       (not a genuine failure), or None when "Write failed." is the right thing to show.
       Currently: an EPUB's unique identifier, which is never blanked (a dangling
       unique-identifier reference is an invalid EPUB)."""
    if value == "" and tag == "Identifier" and engine_for(path) == "epub":
        return tr("epub_id_protected")
    return None


def writable(path):
    return ENGINES[engine_for(path)][2](path) | set(FILE_BASE_TAGS)


def writable_from_data(path, data):
    """Set of editable tags derived from ALREADY-read data, without re-reading the file
       (avoids an exiftool call per file in group mode)."""
    eng = engine_for(path)
    if eng == "exiftool":
        if _et_content_readonly(data):
            return set(FILE_BASE_TAGS)
        return set(SUGGESTED.get(exiftool_category(data or {}), ())) | set(FILE_BASE_TAGS)
    if eng in ("mutagen", "ffmpeg") and not engine_available(eng):
        # Dedicated engine absent (as in degraded single mode): do not present its
        # fields as editable — they would fail one by one. Only the file data.
        return set(FILE_BASE_TAGS)
    return ENGINES[eng][2](path) | set(FILE_BASE_TAGS)


def wipe(path):
    snapshot = None
    if _undo_active():
        snapshot = _UNDO.dump_snapshot(_undo_full_snapshot(path))
    # Same rewrite, same net as write(): clearing a file's metadata does not make the file be
    # born again. (Its mtime DOES move — a wipe changes the bytes; its birth date does not.)
    orig_btime = _os_create_ts(path)
    ok = ENGINES[engine_for(path)][3](path)
    if ok:
        if orig_btime:
            _keep_create_date(path, orig_btime)
        _log_change("wipe", path)
        if _undo_active() and snapshot is not None:
            _UNDO.record_wipe(path, snapshot)
    return ok


# Image extensions (exiftool engine) whose wipe-undo cannot restore the binary blobs
# (IFD1 thumbnail, MakerNotes): read(raw=True) captures them only as "(Binary data …)".
_IMAGE_EXTS = frozenset({"jpg", "jpeg", "tiff", "tif", "png", "gif", "webp",
                         "heic", "heif", "cr2", "cr3", "nef", "arw", "dng",
                         "orf", "rw2", "raf"})


def _wipe_caveat(paths):
    """Sober caveats to show at wipe time, so as never to promise more than we deliver.
       Returns "" if none apply.
       - Audio (mutagen) / video (ffmpeg): the undo snapshot (metadata, not file) captures
         neither cover art, nor per-track metadata, nor chapters → the undo loses them.
       - Images: same mechanism — the embedded thumbnail and the MakerNotes are binary
         blobs the snapshot cannot round-trip → the undo loses them.
       - PDF: exiftool neutralises the metadata by an INCREMENTAL update without physically
         removing it — it stays technically RECOVERABLE in the bytes."""
    notes = []
    if any(engine_for(p) in ("mutagen", "ffmpeg") for p in paths):
        notes.append(tr("caveat_av"))
    if any(p.suffix.lstrip(".").lower() in _IMAGE_EXTS for p in paths):
        notes.append(tr("caveat_img"))
    if any(p.suffix.lower() == ".pdf" for p in paths):
        notes.append(tr("caveat_pdf"))
    return " ".join(notes)


# ============================================================
#  Default filter: hides unhelpful read-only tags
# ============================================================

VIEWS = ("all", "in", "edit")                # available views, in cycle order


def visible_tags(data, w, mode):
    """Builds the displayed set according to the requested view:
       - "in"   : only the fields actually present (non-empty value), editable or not
                  — "what is really in the file".
       - "edit" : only the editable fields (present + offered empty, to be able to
                  fill them in).
       - "all"  : unified view = "edit" + all the technical (read-only) tags actually
                  present in the file.
       "in" ⊆ {present}; "edit" ⊆ "all"."""
    if mode == "in":
        return {t: v for t, v in data.items() if v not in ("", None)}
    shown = {t: v for t, v in data.items() if t in w}
    for t in w:
        shown.setdefault(t, "")
    if mode == "all":
        for t, v in data.items():
            shown.setdefault(t, v)
    return shown


def view_footer(mode, nav=None, batch=None):
    """One labelled line per register: the views (current one underlined; on a batch,
    also group/single), then the batch navigation (arrows, file-by-file walk only).
    `batch` is the batch's current view ("group"/"single"), None on a lone file.
    Printed wrapped in DIM by the caller: DIM is re-armed after each RESET so the rest
    of the line stays dimmed."""
    def key(word, active, letter=None):
        shown = f"{UNDERLINE}{word}{RESET}{DIM}" if active else word
        return f"{shown} ({letter})" if letter else shown

    views = " · ".join(key(m, m == mode) for m in VIEWS)
    if batch:
        # Named views — a bare "g · s" says nothing to a first-timer. The words are
        # metmux's own command words (help: "g | group", "s | single").
        views += f"  {GREY}│{RESET}{DIM}  " + " · ".join((key("group", batch == "group", "g"),
                                                          key("single", batch == "single", "s")))
    lines = [(tr("footer_view"), views)]
    if nav:
        lines.append((tr("footer_nav"), nav))
    col = max(len(label) for label, _ in lines) + 3
    return "\n".join(f"{label}{' ' * (col - len(label))}{body}" for label, body in lines)

# ============================================================
#  UI helpers
# ============================================================

def ext_mismatch(path, data):
    real = (data.get("FileTypeExtension") or "").lower() if data else ""
    file_ext = path.suffix.lstrip(".").lower()
    if not real or not file_ext or real == file_ext:
        return None
    if tuple(sorted((real, file_ext))) in EQUIV_EXT:
        return None
    return file_ext, real


def fmt(v, full=False):
    # `full`: skip the MAX_LEN truncation — the focus view exists precisely to read a
    # value the list view shortened, so it must never shorten it again.
    if isinstance(v, str) and v.startswith("(Binary data"):
        m = re.search(r"(\d+)\s*bytes", v)
        n = int(m.group(1)) if m else 0
        unit = (n // 1048576, "MB") if n >= 1048576 else (n // 1024, "KB") if n >= 1024 else (n, "B")
        return tr("binary_fmt", n=unit[0], unit=unit[1])
    if v in ("", None):
        return f"{FAINT}{tr('empty')}{RESET}"
    if isinstance(v, list):
        v = ", ".join(str(x) for x in v)
    if isinstance(v, str):
        m = DATE_RE.match(v)
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            # Display order follows the configured locale (us = MDY, eu = DMY); the value
            # stays stored canonical YYYY:MM:DD — only the on-screen rendering is localised.
            lead = f"{mo}/{d}" if DEFAULT_DATE_ORDER == "MDY" else f"{d}/{mo}"
            v = f"{lead}/{y}{m.group(4) or ''}"
    s = str(v)
    return s if full or len(s) <= MAX_LEN else tr("text_fmt", n=len(s))


# Session command words: a field alias must NEVER equal one, otherwise typing it runs
# the COMMAND (undo, navigation… or even "en" which changes the language AND rewrites
# config.json) instead of selecting the field displayed next to it.
_RESERVED_ALIASES = frozenset({
    "q", "quit", "exit", "n", "p", "help", "aide",
    "all", "in", "edit", "fr", "en", "eu", "us", "u", "undo", "ua",
    "wipe", "single", "group", "g", "s", "dates",
})


def aliases_of(tags):
    # FIXED (sorted) order: `tags` comes from a set; without sorting, the alias of a
    # given field would change from one launch to the next (no muscle memory possible)
    # and the alias/command collision would occur at random. We also exclude the
    # command words from the candidates so a displayed alias is never hijacked.
    out, used = {}, set()
    for t in sorted(tags):
        # Successive candidates: uppercase initials, then prefixes 1..len, then numeric suffix
        cands = []
        upper = "".join(x for x in t if x.isupper()).lower()
        if upper:
            cands.append(upper)
        for i in range(1, len(t) + 1):
            cands.append(t[:i].lower())
        c = next((x for x in cands
                  if x not in used and x not in _RESERVED_ALIASES), None)
        if c is None:
            base = t.lower()
            n = 2
            while f"{base}{n}" in used or f"{base}{n}" in _RESERVED_ALIASES:
                n += 1
            c = f"{base}{n}"
        used.add(c)
        out[t] = c
    return out


def label_of(t, lang):
    c = canon(t)
    return FR.get(c, c) if lang == "fr" else c


def _match_tag(name, aliases, tags, lang):
    """Returns the canonical tag matching `name` (exact name, alias, or FR label),
       case-insensitive, or None. `tags` often comes from a set: iterate SORTED so a
       label two tags share in a mixed batch ("Title" exiftool / "title" mutagen) routes
       to the same tag on every launch, not one picked by the process hash seed."""
    low = name.strip().lower()
    if not low:
        return None
    for t in sorted(tags):
        if (t.lower() == low or aliases.get(t) == low
                or (lang == "fr" and FR.get(t, "").lower() == low)):
            return t
    return None


def _command(text):
    """The typed line, folded for COMMAND matching only: "Q", "ALL", "Wipe" work, as the
       opening screen and the y/N confirmations already did. A field name and a VALUE keep
       their case: they are resolved from the untouched line, never from this one."""
    return text.lower()


def resolve(text, aliases, tags, lang):
    """Parses 'Field : value', 'Field value' or 'Field'.
       - Form with ":": the colon only splits the name from the value if the left part
         is a KNOWN field; otherwise the ":" belongs to the value (time "14:00", URL,
         text) and we fall back to "Field value".
       - Form without ":": we test the LONGEST prefix of words first, so that
         "Artiste de l'album X" finds AlbumArtist (and not Artist + "de l'album X").
       - Trailing space on a field alone = clear; field alone without a space = focus
         (val=None)."""
    # We only strip a "(alias)" suffix if what is between parentheses is a KNOWN alias
    # (case of the displayed label "Titre (t)" copied as-is). Otherwise a real value
    # ending in a parenthesis — "Comment big photo (sunset)" — would be silently
    # trimmed of its end.
    known_aliases = set(aliases.values())

    def _strip_alias_suffix(s):
        if s.endswith(")") and "(" in s:
            inner = s[s.rfind("(") + 1:-1].strip()
            if inner in known_aliases:
                return s[:s.rfind("(")].strip()
        return s

    trailing_space = text.endswith(" ") and not text.endswith(": ")
    cleaned = text.strip()

    if ":" in cleaned:                               # ":" present: explicit form POSSIBLE
        name, _, val = cleaned.partition(":")
        name = _strip_alias_suffix(name.strip())
        t = _match_tag(name, aliases, tags, lang)
        if t:                                        # the ":" does split a known field
            return t, val.strip()
        # otherwise the ":" is part of the value: we fall back to "Field value".

    base = _strip_alias_suffix(cleaned)              # "Titre (t)" → "Titre" (known alias only)
    words = base.split()
    if not words:
        return None, None

    for k in range(len(words), 0, -1):               # longest prefix first
        t = _match_tag(" ".join(words[:k]), aliases, tags, lang)
        if t:
            rest = words[k:]
            if rest:
                return t, " ".join(rest)             # "Field value"
            return t, ("" if trailing_space else None)  # field alone: clear or focus
    return None, None


def open_binary(path, tag):
    """Extracts an embedded binary (thumbnail, cover art) to a temp and opens it.
       Via -b to stdout (bytes) to avoid the pitfalls of exiftool's -w flag."""
    if tag not in OPENABLE_BINARY:
        return False
    try:
        r = subprocess.run([EXIFTOOL, f"-{tag}", "-b", _ext_arg(path)], capture_output=True)
        if r.returncode != 0 or not r.stdout:
            return False
        tmp = tempfile.NamedTemporaryFile(delete=False, prefix=f"{tag}_", suffix=".jpg")
        tmp.write(r.stdout)
        tmp.close()
        _TEMP_FILES.append(tmp.name)
        return open_externally(tmp.name)
    except Exception:
        return False


# ============================================================
#  Thematic grouping (unified view)
# ============================================================

# On-screen order of themes. "__media__" = technical (read-only) block whose label
# adapts to the file type (Image / Video / Audio / Technical).
THEME_ORDER = ("File", "__media__", "Description",
               "People & rights", "Location", "Dates")

_THEME_TAGS = {
    # Identity → size/type → permissions → dates (lifecycle:
    # creation < modification < access) → software.
    "File": ("FileName", "Directory", "FileSize", "FileType",
             "FileTypeExtension", "MIMEType", "FilePermissions",
             "FileCreateDate", "FileModifyDate", "FileAccessDate",
             "FileInodeChangeDate", "Software", "Generator"),
    # Titles → body/summaries → subject & classification → comments & notes →
    # sort/priority → media context (series/album) → language.
    "Description": ("Title", "Subtitle", "Headline", "ObjectName",
                    "Description", "ImageDescription", "Caption",
                    "Caption-Abstract", "Synopsis", "LongDescription",
                    "Information",
                    "Subject", "Keywords", "Category", "SupplementalCategories",
                    "Genre", "Grouping",
                    "Comment", "Comments", "UserComment", "SpecialInstructions",
                    "Lyrics",
                    "Rating", "ContentRating", "ParentalRating", "Label", "Urgency",
                    "ContentStatus", "Revision",
                    "Album", "TrackNumber", "Track", "Disc", "DiscNumber",
                    "Compilation", "Show", "Episode", "EpisodeID", "Season",
                    "TVShow", "TVSeason", "TVEpisode", "TVEpisodeID",
                    "Language"),
    # Authors → contributors/production → ownership & credits → rights →
    # encoding.
    "People & rights": ("Creator", "Author", "Authors", "InitialCreator",
                    "Artist", "AlbumArtist", "By-line", "By-lineTitle",
                    "Contributor",
                    "Director", "Producer", "Composer", "Performer",
                    "Conductor", "Lyricist", "OriginalLyricist",
                    "Writer", "Cast", "Studio",
                    "Network", "TVNetworkName", "Manager", "Company",
                    "OwnerName", "Credit", "Source", "Contact",
                    "Writer-Editor", "LastModifiedBy", "Publisher",
                    "Copyright", "CopyrightNotice", "Rights",
                    "EncodedBy", "Encoder"),
    # From the most precise to the broadest (IPTC order: sub-location → city → state →
    # country), then GPS (latitude → longitude → altitude, each Ref adjacent).
    "Location": ("Sub-location", "City", "State", "Province-State", "Country",
             "Country-PrimaryLocationName", "Country-PrimaryLocationCode",
             "Location", "LocationName",
             "GPSCoordinates", "GPSPosition",
             "GPSLatitude", "GPSLatitudeRef",
             "GPSLongitude", "GPSLongitudeRef",
             "GPSAltitude", "GPSAltitudeRef"),
    # Shooting (real moment) first, then creation before modification.
    "Dates": ("DateTimeOriginal", "CreateDate", "ModifyDate", "DateCreated",
              "DigitalCreationDate", "DigitalCreationTime", "MetadataDate",
              "Date", "Year", "MediaCreateDate", "MediaModifyDate",
              "TrackCreateDate", "TrackModifyDate", "OriginalDate"),
}

_THEME_OF = {t: theme for theme, tags in _THEME_TAGS.items() for t in tags}
_TAG_PRIORITY = {t: i for i, t in enumerate(
    t for tags in _THEME_TAGS.values() for t in tags)}

# Case-insensitive index: the mutagen engines (FLAC/MP3/OGG…) and ffmpeg
# (MKV/AVI/WEBM…) return their keys in lowercase (title, artist…). We bring them back
# to the known canonical form to recover theme, order and FR label.
# Unknown tag → left as-is (falls into the technical block, alpha sort).
_CANON = {t.lower(): t for t in
          list(FR) + [t for tags in _THEME_TAGS.values() for t in tags]}


def canon(tag):
    """Canonical form of a tag (handles the lowercase mutagen/ffmpeg keys)."""
    return _CANON.get(tag.lower(), tag)


def tag_theme(tag):
    """Display theme of a tag for the unified view (case ignored)."""
    c = canon(tag)
    if c in _THEME_OF:
        return _THEME_OF[c]
    low = c.lower()
    if c.startswith("GPS") or "location" in low or "country" in low:
        return "Location"
    if "date" in low or "time" in low:
        return "Dates"
    if c.startswith("File"):
        return "File"
    return "__media__"            # rest = technical (dimensions, colours, codec…)


def media_label(data):
    mime = (data.get("MIMEType") or "").lower()
    if mime.startswith("image/"): return "Image"
    if mime.startswith("video/"): return "Video"
    if mime.startswith("audio/"): return "Audio"
    return "Technical"


def themed_layout(tags, data):
    """Orders the tags by theme; returns a list of items:
       ('H', label) for a theme header, ('T', tag) for a tag."""
    buckets = {}
    for t in tags:
        buckets.setdefault(tag_theme(t), []).append(t)
    out = []
    for theme in THEME_ORDER:
        ts = buckets.get(theme)
        if not ts:
            continue
        ts.sort(key=lambda t: (_TAG_PRIORITY.get(canon(t), 10**6), canon(t)))
        out.append(("H", media_label(data) if theme == "__media__" else theme))
        out.extend(("T", t) for t in ts)
    return out


_IN_ALT = False        # True while the edit view is active (historical name — everything
                       # stays in the NORMAL screen buffer, cf. render() and SPEC §2)
_LAST_FRAME = None     # last (title, subtitle, rows, msg, footer, scrollable) drawn — for a live resize
_rendering = False     # True while render() runs — guards the SIGWINCH redraw against re-entrancy
# Internal scroll for the clamped edit view. When the frame is taller than the screen, we do
# NOT let it overflow into the terminal's scrollback (that reopened the stacking bug) — we keep
# it clamped to exactly the screen height and scroll a WINDOW over the field list instead. The
# mouse wheel drives it via mouse-event reporting (render() turns that on only while the frame
# overflows), the keyboard via ↑/↓ and PgUp/PgDn. All of it repaints in place: no flash, no stack.
_edit_scroll = 0       # current scroll offset, in field-lines, into the clamped list (0 = top)
_edit_max_scroll = 0   # highest valid offset for the last frame — bounds the wheel/keys
_edit_page = 1         # PgUp/PgDn step (the visible window height) from the last frame
_mouse_on = False      # mouse-event reporting is currently enabled on the terminal


def _on_winch(signum, frame):
    """SIGWINCH: redraw the current screen at the new width so the full-width rules
       reflow live. Skipped before the first frame or while a render() is in flight.
       Errors are swallowed: a signal handler must never break the main loop. The
       in-progress line (_typed) is re-echoed after the redraw; _typed is "" outside
       typing, so nothing stale is re-echoed mid-command."""
    if _LAST_FRAME is None or _rendering:
        return
    try:
        render(*_LAST_FRAME)
        _reecho_typed()
    except Exception:
        pass


_RESIZE_POLL = 0.05      # seconds between two size checks on Windows, window at rest
_RESIZE_POLL_FAST = 0.02   # …and while it is being dragged: track the edge closely
_RESIZE_SETTLE = 0.5     # stay on the fast cadence this long after the last change seen


def _resize_tick(last):
    """One poll of the window size: redraws the current frame when it changed, and returns the
       size just measured (the caller's next `last`). Split out of the loop below so it can be
       driven straight from the bench."""
    size = (term_width(), term_height())
    if size != last:
        _on_winch(None, None)
    return size


def _watch_terminal_size():
    """Windows has no SIGWINCH: poll the size on a daemon thread, redrawing through the
       same path as POSIX (_on_winch). Polling is the only way in — the console queues a
       WINDOW_BUFFER_SIZE event only when read in raw input mode, so there is no event
       to wait on. Two cadences: slow at rest, fast for _RESIZE_SETTLE after a change,
       to track a drag without polling hard all session."""
    last = (term_width(), term_height())
    hot = 0.0                            # until when the drag-tracking cadence applies
    while True:
        time.sleep(_RESIZE_POLL_FAST if time.monotonic() < hot else _RESIZE_POLL)
        try:
            size = _resize_tick(last)
            if size != last:
                hot = time.monotonic() + _RESIZE_SETTLE
            last = size
        except Exception:                # a closed terminal must not raise out of a daemon thread
            return


def _reecho_typed():
    """Repaint the in-progress line after a full redraw (resize, resume, scroll) and put
       the caret back where the user left it — the arrow keys may have moved it mid-line,
       and echoing the text leaves the terminal cursor at its end."""
    if _typed:
        sys.stdout.write(_typed + "\b" * (len(_typed) - _cursor))
        sys.stdout.flush()


def _restore_raw_term():
    """Hand the terminal back to the shell in its saved (cooked) mode and stop reporting
       paste/mouse. Called on suspend (Ctrl-Z) so the shell prompt is not left echo-less and
       in our cbreak mode. No-op off raw mode. _mouse_on is dropped so render() re-emits
       MOUSE_ON on resume (it only toggles on a transition)."""
    global _mouse_on
    if not _raw_mode or _saved_term is None:
        return
    try:
        import termios
        termios.tcsetattr(_raw_fd, termios.TCSADRAIN, _saved_term)
    except Exception:
        pass
    try:
        sys.stdout.write(PASTE_OFF + MOUSE_OFF)
        sys.stdout.flush()
    except Exception:
        pass
    _mouse_on = False


def _reenter_raw():
    """Re-arm cbreak and bracketed paste after a resume (SIGCONT). The shell may have left
       the terminal cooked (echo on) — our own echo would then double every keystroke — so we
       re-assert cbreak. No-op off raw mode."""
    if not _raw_mode:
        return
    try:
        import tty
        tty.setcbreak(_raw_fd)
    except Exception:
        pass
    try:
        sys.stdout.write(PASTE_ON)
        sys.stdout.flush()
    except Exception:
        pass


def _on_tstp(signum, frame):
    """Ctrl-Z (SIGTSTP): give the shell a clean cooked terminal, then suspend for real via
       the default action. Without this the shell would run in our cbreak mode (no echo, no
       line editing) while metmux is stopped. _on_cont re-arms us on resume."""
    _restore_raw_term()
    try:
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)   # let the default action stop us
        os.kill(os.getpid(), signal.SIGTSTP)            # …now (execution resumes here on SIGCONT)
    except Exception:
        pass


def _on_cont(signum, frame):
    """Resumed with `fg` (SIGCONT): re-arm our cbreak reader (the shell left the terminal
       cooked → our echo would double the terminal's) and repaint the current frame."""
    try:
        signal.signal(signal.SIGTSTP, _on_tstp)         # re-install (we reset it in _on_tstp)
    except Exception:
        pass
    _reenter_raw()
    if _LAST_FRAME is not None and not _rendering:
        try:
            render(*_LAST_FRAME)
            _reecho_typed()
        except Exception:
            pass


def term_width(default=80):
    """Live terminal width in columns, re-measured on every call so a window resize
       shows up at the next redraw. Queries the tty directly (TIOCGWINSZ) rather than
       the COLUMNS env var, which a running process keeps at its launch-time value and
       so goes stale on resize. Falls back to `default` when no stream is a terminal
       (piped or redirected output)."""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            return os.get_terminal_size(stream.fileno()).columns
        except (OSError, ValueError, AttributeError):
            continue
    return default


def term_height(default=24):
    """Live terminal height in rows, re-measured on every call — companion to term_width().
       render() clamps its frame to this height so in-place painting from \033[H never
       scrolls (see the clamp comment there). Falls back to `default` off a terminal."""
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        try:
            return os.get_terminal_size(stream.fileno()).lines
        except (OSError, ValueError, AttributeError):
            continue
    return default


def _enter_echoed():
    """True when validating with Enter puts a newline on the screen render() paints, forcing
       the frame to leave its bottom row free. ONLY on the input() fallback: the terminal's
       line mode echoes the newline and we cannot stop it. Our raw readers withhold Enter's
       echo (_read_line_raw); off a terminal nothing is echoed either."""
    if _raw_mode or _win_raw:
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:                # a stream swapped for an object without isatty()
        return False


def render(title, subtitle, rows, msg, footer=None, scrollable=False):
    """rows: list of (label, alias, val, writable_bool); a theme header is
       (None, label, None, None). Visual code (detailed in the README): editable =
       BOLD + bright, read-only = medium-grey line, "(empty)" in dark grey.
       footer: hint above the prompt. scrollable: a browse view — repainted with a full
       clear and free to overflow into the scrollback (native mouse wheel), never clamped."""
    global _IN_ALT, _LAST_FRAME, _rendering, _edit_scroll, _edit_max_scroll, _edit_page, _mouse_on
    _LAST_FRAME = (title, subtitle, rows, msg, footer, scrollable)   # remembered for a live resize
    _rendering = True                                                # SIGWINCH must not redraw over us
    if scrollable:                                       # a browse view: leaving the edit view resets
        _edit_scroll = 0                                 # its scroll, so returning starts at the top
    # The frame is built as ONE string and painted in a single write. How it is painted (in
    # place, or with a full clear) is decided at the bottom, where `prefix` is chosen.
    buf = []                                             # title + the clampable field list
    tail = []                                            # footer, message, hint, prompt (always kept)

    def emit(s="", end="\n"):
        buf.append(s + end)

    def emit_tail(s="", end="\n"):
        tail.append(s + end)

    # Tab/window title. Some terminals append the running command after it themselves:
    # that part is the terminal's, not metmux's.
    emit(f"\033]0;metmux · {title}\007", end="")
    emit(f"{BOLD}{title}{RESET}  {DIM}{subtitle}{RESET}\n")
    if not rows:
        emit(f"{DIM}{tr('no_field')}{RESET}")
    else:
        data_rows = [r for r in rows if r[0] is not None]
        col = max((len(f"{lbl} ({a})") for lbl, a, _, _ in data_rows), default=0)
        for lbl, a, val, writable_flag in rows:
            if lbl is None:
                head = HEADER_FR.get(a, a) if DEFAULT_LANG == "fr" else a
                # Full-width rule: the label, then a horizontal line filling the
                # terminal to the right edge. term_width() re-measures the live width
                # each redraw, so shrinking the window no longer wraps the rule onto
                # extra lines. "- len(head) - 2" leaves one column free on the right,
                # which keeps a terminal without deferred auto-wrap from spilling over.
                rule = "─" * max(0, term_width() - len(head) - 2)
                emit(f"\n{GREY}{head} {rule}{RESET}" if rule else f"\n{GREY}{head}{RESET}")
                continue
            pad = " " * (col - len(lbl) - len(a) - 3)
            if writable_flag:
                name = f"{BOLD}{lbl}{RESET} {GREY}({a}){RESET}"
                shown_val = val
            else:
                name = f"{GREY}{lbl} ({a}){RESET}"
                # Whole line in medium grey. We relaunch GREY after each internal reset
                # ("(empty)", group suffixes) to stay uniform.
                shown_val = f"{GREY}{val.replace(RESET, RESET + GREY)}{RESET}"
            emit(f"{name}{pad} : {shown_val}")
    if footer:
        emit_tail(f"\n{DIM}{footer}{RESET}")
    if msg:
        # Red signals an error; a message that already carries its colour
        # (confirmation in yellow) keeps its own.
        colour = "" if msg.startswith("\033[") else RED
        emit_tail(f"\n{colour}{msg}{RESET}")
    emit_tail(f"\n{DIM}{tr('help_hint')}{RESET}")
    emit_tail(PROMPT, end="")

    # Split the frame into its visual lines, then clamp to term_height(). In-place painting
    # from \033[H only stays aligned while the frame fits: writing past the last row scrolls
    # the terminal, so \033[H drifts off the frame's top and each redraw stacks a fresh copy
    # into the scrollback — the runaway guarded against here.
    tail_str = "".join(tail)                             # begins with "\n" (hint is always present)
    lines = ("".join(buf) + tail_str).split("\n")
    n_tail = tail_str.count("\n")
    height = term_height()
    # Clamp ONLY the in-place edit view: a scrollable browse view is meant to overflow into the
    # scrollback for the native wheel — clamping it would hide fields the user scrolls to reach.
    # The clamped frame pins the title on top and the tail (footer/msg/hint/prompt — the prompt
    # must stay on screen to type into) on the bottom, and scrolls a WINDOW over the field list
    # in between: wheel and arrows move _edit_scroll (bounded to _edit_max_scroll here), so every
    # field stays reachable without the frame overflowing.
    #
    # BUDGET = height - 1 ONLY on the input() fallback (_enter_echoed): there the terminal echoes
    # the validating newline and, with the "> " prompt on the LAST row, that newline scrolls the
    # whole frame up one line BEFORE the next redraw. Leaving the bottom row free lets Enter land
    # without scrolling. Our raw readers withhold Enter's echo (_read_line_raw), so they use the
    # FULL height — still clamped, which keeps the over-tall guard without sparing a row.
    _edit_max_scroll = 0                                 # 0 ⇒ nothing to scroll: disables wheel/keys + mouse
    budget = height - 1 if (height and _enter_echoed()) else height
    if not scrollable and budget and len(lines) > budget:
        tail_lines = lines[-n_tail:]
        head = lines[:len(lines) - n_tail]
        if len(head) >= 2 and head[1] == "":
            anchor, region = head[:2], head[2:]
        else:
            anchor, region = head[:1], head[1:]
        window = budget - n_tail - len(anchor)
        if window >= 1:
            _edit_max_scroll = max(0, len(region) - window)
            _edit_page = window                          # PgUp/PgDn step
            off = min(max(_edit_scroll, 0), _edit_max_scroll)
            _edit_scroll = off                           # write the clamped value back (bounds the keys)
            lines = anchor + region[off:off + window] + tail_lines
        else:                                            # window shorter than the pinned rows: show its bottom
            lines = lines[-budget:]
    # Windows console: on a resize it pads every line with spaces and hands those spaces the
    # attributes of the line's LAST NON-BLANK character, ignoring the reset that follows it
    # (microsoft/terminal#75, closed without a fix: an application must work around it itself).
    # The footer line ends on the current view, underlined, so widening the window dragged that
    # underline to the right edge as a rule. Underline is the only one of our attributes a blank
    # cell shows (a grey or a bold does not), so we close such a line with a NON-BREAKING space:
    # the console counts only U+0020 as blank, so the NBSP becomes the last non-blank character
    # and lends the padding its own plain attributes. One column, invisible, Windows only.
    if _WIN_CONSOLE:
        lines = [ln + NBSP if UNDERLINE in ln else ln for ln in lines]
    # One screen line per element; \033[K erases each line's stale tail (a value that shrank
    # between two frames), the trailing \033[J the leftover lines below (a shorter frame).
    body = "\033[K\n".join(lines)
    # The NORMAL screen buffer, never the alternate one (SPEC §2): the alt buffer looked tidy but
    # on macOS Terminal.app it stranded the frame dozens of lines down the page. _IN_ALT tracks
    # "the edit view is active" (the name is historical). A same-view redraw homes and overwrites
    # IN PLACE — the screen is never blanked, which is what killed the per-keystroke flash (a full
    # \033[2J erase, wipe-then-repaint, WAS that flash); \033[3J drops the saved lines so a resize
    # cannot spill a copy of the frame into the scrollback. The edit view resets once on entry
    # with \033c (RIS: clears a scroll region / origin mode a shell prompt left set, which would
    # otherwise pin \033[H below the top). A browse view repaints with a full clear — it is not
    # redrawn on every keystroke, so its flash costs nothing.
    # Mouse reporting rides with the clamp: ON only while the frame overflows AND we own a real
    # terminal, toggled on the transition. OFF in browse views (their wheel scrolls the native
    # scrollback) and the moment the window fits again, so ordinary selection comes back.
    want_mouse = _raw_mode and not scrollable and _edit_max_scroll > 0
    mouse_seq = ""
    if want_mouse and not _mouse_on:
        mouse_seq, _mouse_on = MOUSE_ON, True
    elif not want_mouse and _mouse_on:
        mouse_seq, _mouse_on = MOUSE_OFF, False
    if scrollable:                                   # browse view: full clear, normal buffer
        _IN_ALT = False
        prefix = "\033[2J\033[3J\033[H"
    elif not _IN_ALT:                                # first frame of the edit view: reset, land at the top
        _IN_ALT = True
        # REGRESSION: \033c is a FULL reset (RIS) — it also switches bracketed paste back off,
        # the very mode main() had just enabled. metmux was therefore killing its own paste
        # detection on the first frame it drew, on every platform, and the guard was left with
        # nothing but the arrival heuristic. The mode is re-asserted with the reset that clears it.
        prefix = "\033c" + PASTE_ON + "\033[2J\033[3J\033[H"
    else:                                            # same edit view: home + overwrite in place, no flash
        prefix = "\033[3J\033[H"
    # A browse view that overflows puts its "> " prompt on the LAST row, where the input()
    # fallback's echoed newline would scroll the frame (same flicker as the budget note above).
    # It cannot clamp without hiding fields, so it pushes ONE blank line under the prompt and
    # steps the cursor back onto it (\n scrolls once here, inside this single paint, then
    # \033[A + reprint of the prompt re-anchors the caret): Enter lands on that free row
    # without scrolling. Skipped for the raw readers — they echo no newline.
    reserve = ("\n\033[A\r" + PROMPT
               if scrollable and height and _enter_echoed() and len(lines) >= height else "")
    sys.stdout.write(mouse_seq + prefix + body + "\033[J" + reserve)
    sys.stdout.flush()
    _rendering = False                               # frame complete: a resize may redraw again


def panel_rule(head_width, span):
    """The rule of a section heading, on a panel that is NOT the file view: it reaches the
       panel's own right edge — `span`, the widest line the panel prints — and stops there,
       because past its text a wide window would stretch it for nothing. A window narrower
       than the text clamps it instead, so it never wraps. Same layout as the file view's
       rule: heading, one space, rule, one spare column left free on the right."""
    span = min(term_width() - 1, span)
    return "─" * max(0, span - head_width - 1)


def _help_sections():
    """The panel, as data: (heading, condition of the heading, rows, notes).
       A row is (cell, description), a cell a list of (text, style) — so a word to type
       AS IT IS can stand out from a placeholder to replace. A note is (caption, text),
       caption None for a full sentence. The date examples follow the configured order
       (eu/us), like every date metmux shows."""
    fld, val = tr("help_field"), tr("help_value")
    lead = "12/25" if DEFAULT_DATE_ORDER == "MDY" else "25/12"   # the ambiguous pair, in its order
    day = f"{lead}/2024"
    fields = [
        ([(fld, ""), (" : ", BOLD), (val, "")], tr("help_edit")),
        ([(fld, ""), (" ", ""), ("+", BOLD), (val, "")], tr("help_append")),
        ([(fld, ""), (" : ", BOLD), (tr("help_nothing"), GREY)], tr("help_clear")),
        ([(fld, "")], tr("help_focus")),
    ]
    dates = [
        ([("dates", BOLD), (" ", ""), (val, "")], tr("help_dates_abs")),
        ([("dates +2h", BOLD), (" | ", GREY), ("-1d", BOLD)], tr("help_dates_rel")),
        ([(fld, ""), (" ", ""), ("+2h", BOLD)], tr("help_dates_one")),
    ]
    date_notes = [
        (None, tr("help_forms")),
        (tr("help_cap_date"), f"2024    2024/12    {day}    2024/12/25"),
        (tr("help_cap_sep"), "    ".join(day.replace("/", c) for c in "-.:")),
        (tr("help_cap_time"), f"{day} 14:00    {day} 14h00    {day} 14:00:30"),
        (tr("help_cap_compact"), "20241225    202412251400    20241225140000"),
    ]
    views = [([(m, BOLD)], tr(k)) for m, k in (("all", "help_all"), ("in", "help_in"),
                                               ("edit", "help_editv"))]
    nav = [
        ([("→", BOLD), (" | ", GREY), ("n", BOLD)], tr("help_next")),
        ([("←", BOLD), (" | ", GREY), ("p", BOLD)], tr("help_prev")),
        ([("g", BOLD), (" | ", GREY), ("group", BOLD)], tr("help_group")),
        ([("s", BOLD), (" | ", GREY), ("single", BOLD)], tr("help_single")),
    ]
    undo = [
        ([("Ctrl-U", BOLD)], tr("help_killline")),
        ([("wipe", BOLD)], tr("help_wipe")),
        ([("u", BOLD), (" | ", GREY), ("undo", BOLD)], tr("help_undo")),
        ([("ua", BOLD), (" | ", GREY), ("undo all", BOLD)], tr("help_undo_all")),
    ]
    conf = [
        ([("fr", BOLD), (" | ", GREY), ("en", BOLD)], tr("help_lang")),
        ([("eu", BOLD), (" | ", GREY), ("us", BOLD)], tr("help_dateorder")),
    ]
    # "aide" is only advertised in French; it works in both languages regardless.
    session = [
        ([("help", BOLD), (" | ", GREY), ("aide", BOLD)] if DEFAULT_LANG == "fr"
         else [("help", BOLD)], tr("help_help")),
        ([("q", BOLD), (" | ", GREY), ("quit", BOLD)], tr("help_quit")),
    ]
    return [
        (tr("help_sec_fields"), None, fields,
         [(None, tr("help_forms")), (tr("help_edit"), tr("help_forms_edit")),
          (tr("help_clear"), tr("help_forms_clear")), (None, tr("help_paste"))]),
        (tr("help_sec_dates"), None, dates, date_notes),
        (tr("help_sec_views"), None, views, []),
        (tr("help_sec_nav"), tr("help_sec_nav_if"), nav, []),
        (tr("help_sec_undo"), None, undo, [(None, tr("help_instant"))]),
        (tr("help_sec_conf"), tr("help_sec_conf_if"), conf, []),
        (tr("help_sec_session"), None, session, []),
    ]


def show_help():
    """One panel, identical in every mode. Visual grammar of the file view: heading +
       rule, aligned rows. Ink levels: BOLD = typed as-is (operative punctuation
       included), plain = read, GREY = scaffolding."""
    clear_screen()                             # own screen: don't leave a stale notice above the panel
    sections = _help_sections()
    plain = lambda cell: "".join(t for t, _ in cell)
    col = max(len(plain(c)) for _, _, rows, _ in sections for c, _ in rows) + 2
    cap = max((len(c) for _, _, _, notes in sections for c, _ in notes if c), default=0) + 3
    # The rules span the panel: measure its widest line first, with the same formulas
    # that print the rows below. Never hardcode a width — a translation that widens the
    # text must widen the rules with it.
    widths = [len(head) + (len(cond) + 3 if cond else 0) for head, cond, _, _ in sections]
    widths += [2 + col + 2 + len(desc) for _, _, rows, _ in sections for _, desc in rows]
    widths += [2 + (cap if caption else 0) + len(text)
               for _, _, _, notes in sections for caption, text in notes]
    span = max(widths)
    for head, condition, rows, notes in sections:
        title, width = f"{GREY}{head}{RESET}", len(head)
        if condition:
            title += f" {GREY}({condition}){RESET}"
            width += len(condition) + 3
        rule = panel_rule(width, span)
        print(f"{title} {GREY}{rule}{RESET}" if rule else title)
        for cell, desc in rows:
            painted = "".join(f"{s}{t}{RESET}" if s else t for t, s in cell)
            print(f"  {painted}{' ' * (col - len(plain(cell)))}{GREY}—{RESET} {desc}")
        for caption, text in notes:
            print(f"  {GREY}{caption}{' ' * (cap - len(caption))}{RESET}{text}" if caption
                  else f"  {GREY}{text}{RESET}")
    print(f"\n{GREY}{tr('help_close')}{RESET}")
    print(PROMPT, end="", flush=True)
    ask()


# --- Guard against accidental paste in the command bar ---
# The bug it exists for: a pasted text ran line by line, each line matched as a command ("a "
# overwrote Artist with the rest of the sentence), and the pasted line breaks acted as Enter —
# the block validated ITSELF.
#
# Two ways to tell a paste from typing:
#   1. Bracketed paste (certain): the terminal wraps pasted text between PASTE_BEGIN…PASTE_END
#      once PASTE_ON is enabled. Watch out for anything that resets the terminal — see the
#      \033c note in render(), which switched this very mode back off for a long time.
#   2. Arrival burst (a guess): at the prompt the reader sits blocked in os.read(), so each
#      keystroke comes back on its own. Two characters out of ONE read were not typed one by
#      one — but they may have been TYPED AHEAD while metmux was busy (the tty queues them and
#      hands them over together), so an unbracketed burst is only taken for a paste when it
#      holds SEVERAL lines — which type-ahead does not produce, and which is exactly the shape
#      that used to be destructive.
# Whatever the route: nothing a burst brought in ever validates itself. Its Enter is dropped, and
# what it leaves on the line is echoed, in view, awaiting the user's own Enter.
PASTE_ON, PASTE_OFF = "\033[?2004h", "\033[?2004l"
PASTE_BEGIN, PASTE_END = "\033[200~", "\033[201~"
_PASTE_GRACE = 0.06         # silence (s) that closes a burst that could still be a typed command
_PASTE_TAIL = 0.25          # …that closes a SHORT pasted block: no console slices those
_PASTE_SLOW_GRACE = 1.0     # …and a big one, which an old console does hand over in slices
_PASTE_SLICED_AFTER = 400   # characters: the size past which a console starts slicing (arbitrary)
_PASTE_DROP = 3.0           # seconds: after a refused block, its late slices are dropped in silence
_POLL_NAP = 0.004           # longest sleep between two looks at the Windows console…
_POLL_STEP = 0.0002         # …reached gradually: the first looks do not sleep at all (see below)
_win_wait = None            # (kernel32, console input handle) — cached; False if unavailable
_PASTE_HINT_AFTER = 0.5     # seconds of swallowing past which the screen says what it is doing
_PASTE_CAP = 200_000        # characters: hard stop, a paste never loops for ever
_PASTE_MAX = 30.0           # seconds: hard stop on the drain itself (an old console is slow)
_TYPEAHEAD_MAX = 120        # characters: past this, a one-line burst was pasted, not typed ahead
_drop_until = 0.0           # a refused block may still be dribbling in: swallow it, say nothing
_WIN_PASTE_WAIT = 0.025     # Windows: how long we wait for a paste to show up in the console buffer
                            # (paid once per command there, so short — the slices then get _PASTE_GRACE)
# Mouse-event reporting. Enabled ONLY while the edit frame overflows the screen: it makes the
# terminal send the scroll wheel to us (button 64 = up, 65 = down) instead of scrolling its own
# scrollback — so we scroll the clamped window in place rather than letting the frame overflow.
# 1000 = report button presses (wheel included); 1006 = SGR encoding (clean ASCII, coordinates
# past column 223, what modern Terminal.app / iTerm speak). Turned off again the moment the
# window is large enough to hold the whole frame, so normal mouse selection comes back.
MOUSE_ON  = "\033[?1000h\033[?1006h"
MOUSE_OFF = "\033[?1006l\033[?1000l"
_WHEEL_STEP = 1             # field-lines scrolled per wheel notch (one line at a time)
_paste_notice = None


def _take_paste_notice():
    """Retrieves then clears the message of the last blocked paste (or None)."""
    global _paste_notice
    n, _paste_notice = _paste_notice, None
    return n


def _value_prefix(text, aliases, tags, lang):
    """True when `text` — what was TYPED on the line BEFORE a paste began — already names a
       field and opens its value: "c ", "Comment: ", "k +", "dates ". Only then is a pasted
       block let into the line, as that field's value.

       The test is anchored on the typed part ON PURPOSE, never on the pasted text itself:
       pasting "a man, a long time ago…" with an empty line would otherwise read "a" as the
       alias of Artist and write the rest of the sentence into it — the exact accident this
       guard exists for. A field alone ("c", no space) is a focus request, not a value slot:
       it does not open the door either."""
    if not text.strip():
        return False
    if _command(text).startswith("dates "):      # the one command that takes a free value
        return True
    tag, val = resolve(text, aliases, tags, lang)
    return bool(tag) and val is not None


# --- Raw keyboard reader ---
# Line (canonical) mode keeps the half-typed line inside the terminal, invisible to us: on a
# resize we cannot re-show it, and — far worse — the terminal itself decides what a pasted block
# means. It echoes it, cuts it on its line breaks and hands us the pieces one command at a time,
# with nothing left to refuse. So we read the keyboard ourselves, on BOTH systems:
#   POSIX   — cbreak on the tty (termios), characters decoded from os.read().
#   Windows — msvcrt: getwch() takes one key, unechoed, straight from the console input buffer,
#             and kbhit() says whether another is already waiting. No tty, no termios, no
#             bracketed paste; the burst rule below is the whole evidence there.
# Both feed the SAME reader (_read_line_raw): the echo, the caret, the paste guard and _typed
# (which a resize re-echoes) are shared. Off a terminal (pipe, redirection) we still fall back
# to input() — the exact previous behaviour, so tests/CI are untouched.
_pending = b""              # undecoded bytes read from the tty (surplus past the current line)
_char_buf = ""              # decoded chars not yet consumed (one decode() can yield several)
_typed = ""                 # visible in-progress line, re-echoed on a resize (_on_winch)
_cursor = 0                 # caret position inside _typed (arrow keys move it; insert/erase there)
_in_paste = False           # inside a bracketed-paste block: don't echo, don't touch _typed
_raw_mode = False           # True once cbreak is armed (a real POSIX terminal)
_win_raw = False            # True once we read the Windows console key by key (msvcrt)
_raw_fd = 0                 # tty file descriptor the reader pulls bytes from
_saved_term = None          # original (cooked) termios, restored on suspend (Ctrl-Z) and exit
_chunk = 0                  # id of the read the current character came from (paste guard)
_feed = 0                   # id of the decode() that released it (several chars can share one)
_chunk_idle = False         # that read caught us waiting, with nothing queued: see _next_char_win
_last_key = 0.0             # when the last character came out of the Windows console (see below)
_KEY_GAP = 0.015            # seconds under which two characters CANNOT both have been struck:
                            # 15 ms is 66 keys a second — four times the fastest human, and well
                            # under the ~30 ms of a key held down on auto-repeat
# Windows sends a special key as a two-key sequence: a \x00 or \xe0 lead, then a scan code (that
# is msvcrt's protocol). Read in bulk (below), the same keys come as a virtual-key code instead.
# Both are translated into the VT sequences the reader already speaks, so ONE set of key handlers
# serves both systems. Anything not listed is swallowed, exactly as an unknown escape is.
_WIN_KEYS = {"K": "\x1b[D", "M": "\x1b[C", "H": "\x1b[A", "P": "\x1b[B",
             "G": "\x1b[H", "O": "\x1b[F", "S": "\x1b[3~", "I": "\x1b[5~", "Q": "\x1b[6~"}
_WIN_VKEYS = {0x25: "\x1b[D", 0x27: "\x1b[C", 0x26: "\x1b[A", 0x28: "\x1b[B",
              0x24: "\x1b[H", 0x23: "\x1b[F", 0x2E: "\x1b[3~", 0x21: "\x1b[5~", 0x22: "\x1b[6~"}
_WIN_READ = 512             # console records taken per read: a whole pasted block in one call
_win_reader = None          # (kernel32, handle, buffer) once built; False if it cannot be
# surrogateescape: a stray non-UTF-8 byte (non-UTF-8 locale, Meta/8-bit key, line noise) is
# escaped, never a crash — parity with input(), whose stdin uses surrogateescape too.
_decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")


def _build_win_reader():
    """Set up a BULK read of the console input buffer, or return False and let msvcrt do the job
       one key at a time. The layout of an INPUT_RECORD is a contract: if what ctypes builds is not
       the size Windows documents, we do not guess — we hand the keyboard back to msvcrt."""
    try:
        import ctypes

        class _KEY(ctypes.Structure):
            _fields_ = [("bKeyDown", ctypes.c_int), ("wRepeatCount", ctypes.c_ushort),
                        ("wVirtualKeyCode", ctypes.c_ushort), ("wVirtualScanCode", ctypes.c_ushort),
                        ("UnicodeChar", ctypes.c_wchar), ("dwControlKeyState", ctypes.c_ulong)]

        class _EVENT(ctypes.Union):
            _fields_ = [("KeyEvent", _KEY), ("blob", ctypes.c_char * 16)]

        class _RECORD(ctypes.Structure):
            _fields_ = [("EventType", ctypes.c_ushort), ("Event", _EVENT)]

        if ctypes.sizeof(_KEY) != 16 or ctypes.sizeof(_RECORD) != 20:
            return False                             # not the documented layout: do not guess
        k = ctypes.windll.kernel32
        handle = k.GetStdHandle(-10)                 # -10 = standard input
        if not handle or handle == -1:
            return False
        return (ctypes, k, handle, (_RECORD * _WIN_READ)())
    except Exception:
        return False                                 # no ctypes or no console: msvcrt fallback


def _win_read_keys():
    """Everything the console holds, in ONE call, as text. None when the bulk read cannot
       be had (the caller falls back to msvcrt). Blocks until there is something.

       Bulk matters twice: key-by-key reads take the console's lock thousands of times —
       the very lock it needs to push the pasted text at us (measured on an old console) —
       and one read hands the paste guard its firmest evidence, characters
       that arrived together (the signal POSIX gets from os.read())."""
    global _win_reader
    if _win_reader is None:
        _win_reader = _build_win_reader()
    if not _win_reader:
        return None
    ctypes, k, handle, buf = _win_reader
    n = ctypes.c_ulong(0)
    if not k.ReadConsoleInputW(handle, buf, _WIN_READ, ctypes.byref(n)):
        return None
    out = []
    for i in range(n.value):
        rec = buf[i]
        if rec.EventType != 1 or not rec.Event.KeyEvent.bKeyDown:   # 1 = KEY_EVENT
            continue                                 # key up, mouse, focus…: not text
        key = rec.Event.KeyEvent
        if key.UnicodeChar and key.UnicodeChar != "\x00":
            out.append(key.UnicodeChar * max(1, key.wRepeatCount))
        else:
            out.append(_WIN_VKEYS.get(key.wVirtualKeyCode, ""))     # arrows, Home, Delete…
    return "".join(out)


def _next_char_win():
    """One character from the Windows console, unechoed, without waiting for Enter: the
       console's line editor never sees the input, so it can neither echo a pasted block
       nor chop it into commands. Bulk read where possible (_win_read_keys), msvcrt
       otherwise — the downstream reader is the same.

       _chunk counts ARRIVALS; two characters of one arrival cannot both have been typed
       (the paste guard). An arrival ends only when nothing was waiting (kbhit) AND the
       key came more than _KEY_GAP after the previous one. The clock clause closes a
       hole: a console that hands a paste over one character at a time leaves kbhit
       false between every pair, so on kbhit alone the paste ran as commands — the
       original accident. Only the clock separates a machine's pace from a hand's
       (measured).

       _chunk_idle is stronger: a read that caught us WAITING on an empty buffer cannot
       hold a typed-ahead command — type-ahead is queued while metmux works, so it is
       already there when we look. Such a burst is a paste whatever its shape."""
    global _char_buf, _chunk, _feed, _chunk_idle, _last_key
    while True:
        if _char_buf:
            ch, _char_buf = _char_buf[0], _char_buf[1:]
            _feed += 1                               # inside one read, each character is its own
            return ch                                # arrival: that is what a burst is counted on
        waiting = msvcrt.kbhit()
        got = _win_read_keys()                       # the whole buffer, in one call…
        if got is None:                              # …or, where that cannot be had, one key
            ch = msvcrt.getwch()                     # (blocks when nothing is waiting)
            got = (_WIN_KEYS.get(msvcrt.getwch(), "") if ch in ("\x00", "\xe0")   # special key:
                   else ch)                                                       # scan code follows
        now = time.monotonic()
        if not waiting and now - _last_key > _KEY_GAP:
            _chunk += 1                              # struck, and struck at a human's pace:
            _chunk_idle = True                       # a keystroke of its own, nothing behind it
        _last_key = now
        _char_buf = got                              # (empty: only key-ups and events — read again)


def _next_char():
    """Next decoded character, or None at EOF. A multibyte character split across two
       os.read() is held by the incremental decoder; one decode() can yield several
       characters, drained one at a time from _char_buf; undecoded surplus stays in
       _pending.

       _chunk names the os.read() a character came out of (see the paste guard's header);
       _feed names the decode() that released it — a held byte and the character after it
       come out together ("é" typed in a Latin-1 locale, then Enter) without having been
       two arrivals."""
    global _pending, _char_buf, _chunk, _feed
    if _win_raw:
        return _next_char_win()
    while True:
        if _char_buf:
            ch, _char_buf = _char_buf[0], _char_buf[1:]
            return ch
        if not _pending:
            chunk = os.read(_raw_fd, 4096)           # PEP 475: auto-resumed after a SIGWINCH
            if not chunk:                            # EOF: release any byte the decoder still holds
                _char_buf = _decoder.decode(b"", final=True)
                _decoder.reset()
                if not _char_buf:
                    return None
                _feed += 1
                continue
            _pending = chunk
            _chunk += 1                              # a fresh arrival from the terminal
        b, _pending = _pending[:1], _pending[1:]
        _char_buf = _decoder.decode(b)               # 0, 1, or (on an escape) several chars
        if _char_buf:
            _feed += 1


def _win_wait_for_key(timeout):
    """Sleep in the kernel until the console has something, or `timeout` runs out. True if
       woken, False on timeout, None if this console cannot be waited on (the caller polls).
       Point of the ctypes: nothing is held while we sleep. A poll takes the console's own
       lock thousands of times a second — the lock it needs to push the pasted text at us —
       so a poll can CREATE the slowness it measures."""
    global _win_wait
    if _win_wait is None:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            handle = k.GetStdHandle(-10)             # -10 = standard input
            _win_wait = (k, handle) if handle and handle != -1 else False
        except Exception:
            _win_wait = False
    if not _win_wait:
        return None
    try:
        k, handle = _win_wait
        return k.WaitForSingleObject(handle, max(0, int(timeout * 1000))) == 0   # WAIT_OBJECT_0
    except Exception:
        return None


def _win_flush_input():
    """Throw away everything the console has queued, in ONE call. False if it cannot be done."""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        return bool(k.FlushConsoleInputBuffer(k.GetStdHandle(-10)))
    except Exception:
        return False


def _dump_block():
    """A block that will be refused WHATEVER it says: thrown away without being read. The
       verdict depends only on what was TYPED before the block, so it is known without reading
       it — and an old console hands text over slowly (measured). Emptying is one call; the
       console keeps pushing, so we keep emptying until it goes quiet. False if it cannot be
       emptied that way: the caller then reads the block, as before."""
    global _char_buf, _pending
    stop = time.monotonic() + _PASTE_MAX
    while time.monotonic() < stop:
        if not _win_flush_input():
            return False
        _char_buf, _pending = "", b""            # …and what we had already taken of it
        if not _input_waiting(_PASTE_TAIL):
            return True
    return True


def _input_waiting(timeout=0):
    """True if a character is already available: decoded, still in the byte buffer, waiting on
       the tty, or sitting in the Windows console buffer. `timeout` (seconds) is how long we
       let the terminal deliver the next character before calling the burst over.

       A console has no select(): wait on it through the kernel (_win_wait_for_key), poll only
       where that cannot be had — and the poll must LOOK AGAIN before it sleeps. A console
       hands a paste over one character at a time, so a FIXED nap is paid PER CHARACTER:
       the old flat nap turned a paste into seconds of waiting on text already in flight
       (guarded by test_reading_a_pasted_block_costs_no_waiting)."""
    if _char_buf or _pending:
        return True
    try:
        if _win_raw:
            stop = time.monotonic() + timeout
            nap = 0.0                                # first looks: no sleep at all, just look again
            while True:
                if msvcrt.kbhit():
                    return True
                left = stop - time.monotonic()
                if left <= 0:
                    return False
                woke = _win_wait_for_key(min(left, 0.05))
                if woke is None:                     # no kernel wait here: poll, gently
                    time.sleep(nap)                  # sleep(0) yields the thread and comes back
                    nap = min(_POLL_NAP, nap + _POLL_STEP)
                elif woke:
                    time.sleep(0.001)                # woken. kbhit is re-read at the top; if it
                    #                                  says no, the console was signalling an EVENT
                    #                                  and not a key (getwch will eat it), and this
                    #                                  floor keeps that case off a hot loop
        return bool(select.select([sys.stdin], [], [], timeout)[0])
    except Exception:                                # select unavailable (non-Unix): don't insist
        return False


def _push_char(ch):
    """Return a character to the front of the stream so the next _next_char() yields it
       again. Used by _read_escape() to give back an ESC that belongs to the NEXT sequence."""
    global _char_buf
    _char_buf = ch + _char_buf


def _read_escape():
    """Consume the escape sequence whose leading ESC was just read; return it whole (ESC
       included). The caller matches the paste markers; arrow/delete/other sequences are
       returned too, for the caller to discard. An ESC always starts a NEW sequence, so one
       met here (right after the leading ESC, or embedded mid-sequence) is pushed back rather
       than swallowed: eating it would strip the leading ESC off the sequence that follows —
       e.g. a mouse report ESC[<..M — and its bare bytes ('[<..M') would then be echoed as
       typed text. Pushing it back lets the reader resynchronise on the real sequence."""
    ch = _next_char()
    if ch == "\x1b":                                 # ESC ESC: the 2nd ESC opens the next sequence
        _push_char(ch)
        return "\x1b"
    if ch is None or ch not in ("[", "O"):           # lone ESC, or a 2-char (Alt-key) sequence
        return "\x1b" + (ch or "")
    seq = "\x1b" + ch                                # CSI '[' or SS3 'O'
    while True:
        ch = _next_char()
        if ch is None:
            return seq
        if ch == "\x1b":                             # embedded ESC: a new sequence begins here
            _push_char(ch)                           # give it back, return the (truncated) current one
            return seq
        seq += ch
        if "\x40" <= ch <= "\x7e":                   # CSI final byte (@..~): sequence complete
            return seq


def _scroll_edit(delta):
    """Shift the clamped edit view's internal scroll by `delta` field-lines and repaint in
       place. Bounded to [0, _edit_max_scroll], both set by the last render(); a no-op when
       nothing is hidden or the offset would not change. Repaints via _LAST_FRAME — the same
       path as the resize redraw — and re-echoes the in-progress line so typing survives a
       scroll. render() re-clamps _edit_scroll itself, so this only needs a coarse bound."""
    global _edit_scroll
    if _edit_max_scroll <= 0 or _LAST_FRAME is None or _rendering:
        return
    new = min(_edit_max_scroll, max(0, _edit_scroll + delta))
    if new == _edit_scroll:
        return
    _edit_scroll = new
    render(*_LAST_FRAME)
    _reecho_typed()


def _handle_scroll_seq(seq):
    """If `seq` is a scroll input — mouse wheel (only sent while render() has mouse reporting
       on) or ↑/↓/PgUp/PgDn — scroll the edit view and return True. Returns False for anything
       else, which the caller then swallows as before. Wheel: SGR reports "\\033[<64;x;yM" (up)
       and "\\033[<65;x;yM" (down); the legacy X10 form "\\033[M" is consumed defensively so its
       three payload bytes never leak into the typed line on a terminal without SGR mouse."""
    delta = None
    if seq.startswith("\x1b[<") and seq[-1:] in ("M", "m"):   # SGR mouse report (mode 1006)
        try:
            btn = int(seq[3:-1].split(";")[0])
        except ValueError:
            return True                                       # malformed report: consume, do nothing
        if btn == 64:                                         # wheel up
            delta = -_WHEEL_STEP
        elif btn == 65:                                       # wheel down
            delta = _WHEEL_STEP
        else:                                                 # click/other button: consume, ignore
            return True
    elif seq == "\x1b[M":                                     # legacy X10 mouse: button, x, y follow
        b0 = _next_char(); _next_char(); _next_char()
        code = (ord(b0) - 32) if b0 else -1
        if code == 64:
            delta = -_WHEEL_STEP
        elif code == 65:
            delta = _WHEEL_STEP
        else:
            return True
    elif seq in ("\x1b[A", "\x1bOA"):                         # arrow up
        delta = -1
    elif seq in ("\x1b[B", "\x1bOB"):                         # arrow down
        delta = 1
    elif seq == "\x1b[5~":                                    # PgUp
        delta = -_edit_page
    elif seq == "\x1b[6~":                                    # PgDn
        delta = _edit_page
    else:
        return False
    _scroll_edit(delta)
    return True


def _is_paste(raw):
    """Tell an unbracketed burst that is a paste from a command typed ahead while metmux
       was busy. Only two shapes mean paste: SEVERAL lines (nobody types two commands
       blind inside one read), or one line longer than _TYPEAHEAD_MAX — an arbitrary cut,
       far above metmux's longest command, far below a pasted paragraph; either way the
       cost of being wrong is one message, never a write."""
    lines = [x for x in raw.splitlines() if x.strip()]
    return len(lines) > 1 or len(raw.strip()) > _TYPEAHEAD_MAX


def _kill_line():
    """Ctrl-U: wipe the whole input line, screen included. Line mode provided this; the
       raw reader must too — a long pasted value is unerasable one backspace at a time,
       and backspaces cannot climb back over a line wrap. So: count the cells the line
       occupies (prompt included), walk the cursor up to the prompt's row, erase from
       there to the end of the screen (the prompt is the last thing on it)."""
    global _typed, _cursor
    if not _typed:
        _cursor = 0
        return
    width = max(1, term_width())
    up = (len(PROMPT_CELLS) + _cursor) // width       # rows between the prompt and the caret
    sys.stdout.write((f"\033[{up}A" if up else "") + "\r\033[J" + PROMPT)
    sys.stdout.flush()
    _typed, _cursor = "", 0


def _erase_before_caret():
    """Erase the character just before the caret, on the line AND on the screen, and return it.
       (Simple cell math; MVP, no wide-char width.) Used by backspace, and to take back the one
       character a burst had already echoed as typed before it gave itself away."""
    global _typed, _cursor
    if _cursor <= 0:
        return ""
    ch = _typed[_cursor - 1]
    _typed = _typed[:_cursor - 1] + _typed[_cursor:]
    _cursor -= 1
    tail = _typed[_cursor:]
    sys.stdout.write("\b" + tail + " " + "\b" * (len(tail) + 1))
    sys.stdout.flush()
    return ch


def _paste_hint_off(shown):
    """Erase the transient note and repaint the prompt line, which it may have pushed over a
       wrap — same walk back up as Ctrl-U, then the line in progress, caret back where it was."""
    width = max(1, term_width())
    up = (len(PROMPT_CELLS) + _cursor + shown) // width
    sys.stdout.write((f"\033[{up}A" if up else "") + "\r\033[J" + PROMPT + _typed
                     + "\b" * (len(_typed) - _cursor))
    sys.stdout.flush()


def _collect_paste(first, marked=False, certain=False, paste_ok=None):
    """Swallow a whole pasted burst, from its first character, and return (text, marked): the
       raw text, and whether the terminal bracketed it. Nothing is echoed — the caller decides
       what becomes of it. Escape sequences met inside are dropped (a paste carries text, not
       keys).

       Bracketed, PASTE_END ends the burst on the spot. Otherwise only a SILENCE can — and a
       slow console feeds a big block in slices, seconds apart. Cutting at the first gap made
       each slice its own refusal, each sending metmux round its loop (exiftool re-read,
       repaint): a few pasted paragraphs froze the session. Past
       _PASTE_HINT_AFTER the screen would look hung, so it says what it is doing."""
    global _in_paste
    out, shown, held = [first], 0, len(first)
    settled = certain                                # once known to be a paste, never un-known
    start = time.monotonic()
    stop = start + _PASTE_MAX
    while held < _PASTE_CAP and time.monotonic() < stop:
        if not _input_waiting():                     # a gap: the block may be over, or still coming
            if not settled:                          # joined at a gap, and only while the answer
                settled = _is_paste("".join(out))    # can still change — never per character
            # Waiting pays on ONE case only: a block being TAKEN IN must land in a single
            # stroke, not piece by piece into the field. A REFUSED block gets the short
            # wait (its late slices are dropped in silence, see _drop_until). A tty gets
            # no patience at all: a second's wait would swallow the Enter typed right
            # after the paste.
            if _in_paste:
                wait = _PASTE_SLOW_GRACE             # an end marker IS coming: wait for it
            elif _win_raw and settled:
                taking = bool(paste_ok and paste_ok(_typed))
                wait = (_PASTE_SLOW_GRACE if taking and held >= _PASTE_SLICED_AFTER
                        else _PASTE_TAIL)
            else:
                wait = _PASTE_GRACE
            if not shown and time.monotonic() - start > _PASTE_HINT_AFTER:
                # The verdict does not depend on the block, only on what was typed BEFORE it, so
                # it is known from the first character: say it now rather than at the end
                # of a slow console's delivery.
                note = tr("paste_reading" if (paste_ok and paste_ok(_typed)) else "paste_dropping")
                sys.stdout.write(GREY + note + RESET)
                sys.stdout.flush()
                shown = len(note)
            if not _input_waiting(wait):
                break
        ch = _next_char()
        if ch is None:
            break
        if ch == "\x1b":
            seq = _read_escape()
            if seq == PASTE_BEGIN:
                _in_paste, marked = True, True
            elif seq == PASTE_END:
                _in_paste, marked = False, True
                break
            continue
        out.append(ch)
        held += 1
    _in_paste = False
    if shown:
        _paste_hint_off(shown)
    return "".join(out), marked


def _insert_at_caret(text):
    """Put `text` into the line at the caret and echo it there (the reader owns the echo)."""
    global _typed, _cursor
    if not text:
        return
    _typed = _typed[:_cursor] + text + _typed[_cursor:]
    _cursor += len(text)
    tail = _typed[_cursor:]
    sys.stdout.write(text + tail + "\b" * len(tail))
    sys.stdout.flush()


def _paste_value(raw, paste_ok):
    """Decide what a burst KNOWN to be a paste becomes. Returns the value it leaves in the line,
       or None once the refusal notice is armed.

       It is let in only when paste_ok() says the line ALREADY opens a field's value (see
       _value_prefix). It then lands at the caret as a single line — its own line breaks collapse
       to spaces — so it cannot validate itself: the user still presses Enter, as for any other
       value."""
    global _typed, _cursor, _paste_notice
    value = " ".join(raw.split())                      # a pasted paragraph becomes one value
    if not value or not (paste_ok and paste_ok(_typed)):
        _paste_notice = tr("paste_blocked")
        _typed, _cursor = "", 0
        return None
    _insert_at_caret(value)
    return value


def _read_line_raw(nav=False, paste_ok=None):
    """Read one line (str, no trailing newline) from the terminal in cbreak mode, echoing
       printable characters ourselves (terminal echo is off). None at EOF on an empty line.
       Keeps the in-progress text in _typed so a resize re-echoes it.

       Line editing: ←/→ move the caret (_cursor) inside the line, Home/End jump, Delete
       erases under it, and typing/backspace act AT the caret — echoed by rewriting the
       tail then backing up (cell-per-char; the wide-char caveat of the backspace echo
       applies here too).

       Paste: characters that share a read were not typed one by one (see the guard's header).
       Such a burst is swallowed whole: a certain paste (bracketed, or several lines) goes to
       _paste_value — kept as the named field's value or refused, never a command, its line
       breaks acting as no Enter; a single unbracketed line is indistinguishable from a command
       typed AHEAD, so it is replayed into the line as typing, in view, only its Enter withheld.
       `line` and _typed therefore stay equal throughout: nothing enters the line unechoed.

       nav=True (the batched single session): ←/→ pressed on an EMPTY line return "p"/"n",
       the exact navigation commands, so the arrows walk the batch without an Enter."""
    global _typed, _cursor, _in_paste, _chunk_idle, _drop_until, _paste_notice
    line = ""
    chunk, feed, run, echoed = _chunk, _feed, 0, False   # `run`: arrivals taken from THIS read
    replay = ""                                          # burst characters given back as typing
    if _win_raw and _input_waiting():                    # something was queued before this line
        _chunk_idle = False                              # even began: it MAY be a typed-ahead
    while True:                                          # command, so judge it on its shape alone
        if replay:                                       # …one at a time, as if keyed in
            ch, replay = replay[0], replay[1:]
            if ch < " ":                                 # Enter included: a burst validates
                continue                                 # nothing, and leaves no control char
        else:
            ch = _next_char()
            if ch is None:
                if line:
                    _typed, _cursor = "", 0
                    return line
                return None
            if ch == "\x1b":
                seq = _read_escape()
                if seq == PASTE_BEGIN:                # the terminal says it outright: a paste
                    _in_paste = True
                    raw, _ = _collect_paste("", marked=True, paste_ok=paste_ok)
                    if _paste_value(raw, paste_ok) is None:
                        return ""                     # refused; the notice carries the message
                    line, chunk, feed, run, echoed = _typed, _chunk, _feed, 0, False
                elif seq == PASTE_END:
                    _in_paste = False
                elif _handle_scroll_seq(seq):
                    pass
                elif seq in ("\x1b[D", "\x1bOD"):     # ← : caret left / previous file
                    if nav and line == "":
                        return "p"
                    if _cursor > 0:
                        _cursor -= 1
                        sys.stdout.write("\b")
                        sys.stdout.flush()
                elif seq in ("\x1b[C", "\x1bOC"):     # → : caret right / next file
                    if nav and line == "":
                        return "n"
                    if _cursor < len(_typed):
                        sys.stdout.write(_typed[_cursor])  # rewriting the char steps over it
                        sys.stdout.flush()
                        _cursor += 1
                elif seq in ("\x1b[H", "\x1bOH", "\x1b[1~"):   # Home
                    if _cursor:
                        sys.stdout.write("\b" * _cursor)
                        sys.stdout.flush()
                        _cursor = 0
                elif seq in ("\x1b[F", "\x1bOF", "\x1b[4~"):   # End
                    if _cursor < len(_typed):
                        sys.stdout.write(_typed[_cursor:])
                        sys.stdout.flush()
                        _cursor = len(_typed)
                elif seq == "\x1b[3~":                # Delete: erase under the caret
                    if _cursor < len(_typed):
                        line = _typed = _typed[:_cursor] + _typed[_cursor + 1:]
                        tail = _typed[_cursor:]
                        sys.stdout.write(tail + " " + "\b" * (len(tail) + 1))
                        sys.stdout.flush()
                # any other sequence: swallowed, no echo, no _typed change
                continue

            burst = False
            if _chunk != chunk:                       # another read: the count starts over
                chunk, feed, run, echoed = _chunk, _feed, 0, False
                # A character landed alone: wait _KEY_GAP for a follower BEFORE echoing
                # anything — the paste is then caught on its FIRST character and none of
                # it reaches the screen. The backward test ("did the PREVIOUS one come
                # fast?") left the first character unjudgeable, and a console hiccup let
                # a line of the paste run as a command. Cost: a struck key echoes 15 ms
                # late (invisible).
                burst = _win_raw and _input_waiting(_KEY_GAP)
            elif _feed != feed:                       # same read, new decode: a 2nd arrival…
                feed = _feed
                burst = run >= 1                      # …out of a single read, so: not typed
            if burst:
                back = _erase_before_caret() if echoed else ""    # take back the one echoed
                certain = _chunk_idle                 # it caught us waiting: nothing was typed ahead
                if (_win_raw and certain and not (paste_ok and paste_ok(_typed))
                        and _dump_block()):
                    # Certainly a paste (it caught us idle), certainly refused (the line
                    # names no field): thrown away unread.
                    _paste_notice = tr("paste_blocked")
                    _typed, _cursor = "", 0
                    _drop_until = time.monotonic() + _PASTE_DROP
                    return ""
                if _win_raw and not _typed and time.monotonic() < _drop_until:
                    # A refused block is still dribbling its tail: swallow it in silence — the
                    # message is already on screen, and repeating it per slice (each a loop turn
                    # through exiftool) is what used to freeze the session. This is what lets the
                    # wait above stay SHORT: being cut early now costs nothing. The empty line is
                    # the whole condition: type anything and what is pasted next is the user's.
                    _collect_paste(back + ch, certain=True)
                    _drop_until = time.monotonic() + _PASTE_DROP
                    chunk, feed, run, echoed = _chunk, _feed, 0, False
                    continue
                raw, marked = _collect_paste(back + ch, certain=certain, paste_ok=paste_ok)
                if marked or certain or _is_paste(raw):
                    if _paste_value(raw, paste_ok) is None:       # a paste, beyond doubt
                        _drop_until = time.monotonic() + _PASTE_DROP
                        return ""
                    line = _typed
                else:
                    replay = raw                      # short, one line: maybe typed ahead
                chunk, feed, run, echoed = _chunk, _feed, 0, False
                continue
            run, echoed = run + 1, False

        if ch == "\n" or ch == "\r":
            # We DON'T echo the newline: render() repositions the cursor itself, so an echoed \n
            # only moved the caret down one row first — visible as the caret dropping onto the
            # reserved bottom row, or (prompt on the very last row: the help panel) as the whole
            # screen scrolling up a line before the redraw. Reading the keyboard ourselves is
            # what lets us withhold it; the input() fallback cannot, which _enter_echoed()
            # reports so the frame keeps its reserved row.
            _typed, _cursor, _drop_until = "", 0, 0.0   # the user acted: a refused block's
            return line                                 # tail is over; the next paste is theirs
        if ch == "\x7f" or ch == "\x08":             # backspace / delete-left
            _erase_before_caret()
            line = _typed
            continue
        if ch == "\x15":                             # Ctrl-U: wipe the line in one stroke
            _kill_line()
            line = ""
            continue
        if ch == "\x03":                             # Ctrl-C (ISIG is on under cbreak; Windows
            raise KeyboardInterrupt                  # hands the key over, so we raise it ourselves)
        if ch == "\x04":                             # Ctrl-D
            if line == "":
                return None
            continue
        if ch < " ":                                 # any other control key: swallowed, never
            continue                                 # inserted (it would sit invisible in a value)
        # printable character
        line = _typed = _typed[:_cursor] + ch + _typed[_cursor:]
        _cursor += 1
        tail = _typed[_cursor:]
        sys.stdout.write(ch + tail + "\b" * len(tail))
        sys.stdout.flush()                           # we echo (terminal echo is off in cbreak)
        echoed = True                                # …so a burst knows what to take back


def _read_raw(nav=False, paste_ok=None):
    if _raw_mode or _win_raw:
        try:
            return _read_line_raw(nav, paste_ok)
        except KeyboardInterrupt:
            print()
            return None
    try:
        return input()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _drain_surplus():
    """In cbreak mode, drain input that is ALREADY waiting after a line was read, and return
       the number of real (non-empty) TEXT lines it held — the count ask()'s paste guard uses.
       The reader catches a paste as it arrives, so this is the second net: it only fires on a
       burst it did not see coming (a paste whose first character reached us alone).
       Escape sequences that land in the SAME read burst as Enter (a mouse report when reporting
       is on, or an arrow key) are processed as the reader would — wheel/arrow scroll, the rest
       swallowed — and DELIBERATELY do not count as text, so a stray control sequence is never
       mistaken for a paste (which used to freeze the prompt and swallow the next command).
       Waits out _PASTE_GRACE between slices, so one paste raises one message, never a burst
       of them (the slices of a big paste used to be reported one by one)."""
    lines, cur = 0, ""
    stop = time.monotonic() + _PASTE_MAX
    while _input_waiting() and time.monotonic() < stop:
        ch = _next_char()
        if ch is None:
            break
        if ch == "\x1b":                             # escape sequence: scroll or swallow, never text
            _handle_scroll_seq(_read_escape())
            continue
        if ch == "\n" or ch == "\r":
            if cur.strip():
                lines += 1
            cur = ""
        else:
            cur += ch
        if (cur or lines) and not _input_waiting():   # text: let a paste still in flight catch up
            _input_waiting(_PASTE_GRACE)              # (a lone escape sequence waits for nothing)
    if cur.strip():
        lines += 1
    return lines


def _win_surplus():
    """Windows console: input() gives back the FIRST line of a paste and leaves the rest in the
       console input buffer, which is how a pasted text used to run as a burst of commands there
       (no cbreak reader, no bracketed paste, and select() only watches sockets). msvcrt reads
       that same buffer: kbhit() sees what is waiting, getwch() takes it. Drain it and return how
       many characters were dropped. The buffer is given _WIN_PASTE_WAIT to show something at all
       (that wait is paid on every command, so it is short), then _PASTE_GRACE between slices: a
       big paste crosses the pipe in several, and only silence tells us it is over."""
    if msvcrt is None:
        return 0
    dropped = 0
    stop = time.monotonic() + _PASTE_MAX
    quiet = time.monotonic() + _WIN_PASTE_WAIT
    while dropped < _PASTE_CAP and time.monotonic() < stop:
        if msvcrt.kbhit():
            msvcrt.getwch()
            dropped += 1
            quiet = time.monotonic() + _PASTE_GRACE
            continue
        if time.monotonic() >= quiet:
            break
        time.sleep(0.005)
    return dropped


def ask(nav=False, paste_ok=None):
    """Read a line; no paste ever acts as a command.

       On a raw reader (POSIX cbreak, Windows console) the paste was already handled: a
       block is either refused whole (notice armed, nothing echoed) or dropped into the
       line as the named field's value, awaiting Enter. paste_ok (cf. _value_prefix)
       tells the two apart; a prompt that passes none accepts no paste.

       The input() fallback (a tty we could not take over) gets the line echoed and
       already cut on its line breaks by the terminal: all that is left is to drain the
       REST so it never runs.

       nav=True: ←/→ on an empty line return "p"/"n" so the arrows walk the batch."""
    global _paste_notice
    _paste_notice = None                             # a notice is one-shot: nothing stale survives
    line = _read_raw(nav, paste_ok)
    if line is None:
        return None
    if _paste_notice:                                # the reader refused a paste: no command
        return ""

    if PASTE_BEGIN in line:                          # markers, but no cbreak reader to catch them
        chunks, guard = [line], 0
        while PASTE_END not in chunks[-1] and guard < 100_000:
            nxt = _read_raw()
            if nxt is None:
                break
            chunks.append(nxt)
            guard += 1
        content = "\n".join(chunks).replace(PASTE_BEGIN, "").replace(PASTE_END, "")
        kept = [ln for ln in content.split("\n") if ln.strip()]
        if len(kept) <= 1:
            return content.strip()
        _paste_notice = tr("paste_multiline")
        return ""

    if sys.stdin.isatty():
        try:
            if _raw_mode or _win_raw:
                extra = _drain_surplus()
            elif msvcrt is not None:                 # Windows console we do not own: drain it raw
                extra = _win_surplus()
            else:                                    # non-cbreak tty (rare): line-based drain
                extra = 0
                while select.select([sys.stdin], [], [], 0)[0]:
                    if _read_raw() is None:
                        break
                    extra += 1
            if extra:
                _paste_notice = tr("paste_multiline")
                return ""
        except Exception:                            # select unavailable (e.g. non-Unix): we don't insist
            pass

    return line


# ============================================================
#  'dates' command
# ============================================================

def cmd_dates(path, value):
    """Absolute : 'dates 2024' overwrites all present dates with that date.
       Relative : 'dates +2h' / '-1d' shifts all present dates.
       Returns (touched, errors, present) — `present` = number of targeted dates
       (changed or not), to tell "no date" from "present but unchanged".
       (None, 0, 0) if the value is unreadable."""
    data = read(path) or {}
    w = writable(path)
    touched, errors, present = 0, 0, 0

    # FILE dates last: DATE_TAGS is a set (non-deterministic order), the fixed order
    # keeps FileModifyDate as the last value set. Content dates (SEMANTIC_DATE_TAGS)
    # are skipped — see that constant's header.
    ordered = sorted((DATE_TAGS - SEMANTIC_DATE_TAGS) - set(FILE_DATE_TAGS)) + \
              [t for t in FILE_DATE_TAGS if t in DATE_TAGS]

    offset = parse_offset(value)
    if offset is not None:                       # --- relative shift ---
        for tag in ordered:
            if tag in BULK_DATE_SKIP:            # atime + btime: see BULK_DATE_SKIP's header
                continue
            if tag in data and tag in w and data[tag] not in ("", None):
                present += 1
                shifted = _shift_stored_date(data[tag], offset)
                if shifted is None:
                    errors += 1
                    continue
                if shifted == data[tag]:         # nothing to shift (+0h, or +2h on a date only)
                    continue
                if write(path, tag, shifted):
                    touched += 1
                else:
                    errors += 1
        return touched, errors, present

    parsed = parse_date(value)                    # --- absolute date ---
    if not parsed:
        return None, 0, 0
    # A typed UTC offset ("dates 25/12/2024 14:00+03:00") is stripped by parse_date
    # (metadata dates are naive) but fixes the INSTANT of the file dates — re-attach
    # it for those, exactly as to_exif does on the per-field path.
    suffix = _typed_offset(value)
    if suffix is None:                            # absurd offset: the whole input is unreadable
        return None, 0, 0
    for tag in ordered:
        if tag in BULK_DATE_SKIP:                # atime + btime: see BULK_DATE_SKIP's header
            continue
        if tag in data and tag in w and data[tag] not in ("", None):
            present += 1
            stored = parsed + suffix if tag in FILE_INSTANT_DATE_TAGS else parsed
            if write(path, tag, stored):
                touched += 1
            else:
                errors += 1
    return touched, errors, present


# ============================================================
#  Sessions
# ============================================================

def _shared_command(cmd):
    """The commands the two sessions answer IDENTICALLY: the help panel, the three views, the
       display language and the date order. They live here, in one place, so the two sessions
       cannot drift apart.

       Returns (handled, msg, view): `view` is the session-local value the command changed,
       None when it changed none. Not a command of ours: handled is False and the caller
       carries on with its own. The language is NOT returned — it lives in DEFAULT_LANG, which
       the sessions read straight, so there is no second copy of it to keep in step."""
    global DEFAULT_LANG, DEFAULT_DATE_ORDER     # "fr/en" and "eu/us" persist in config.json
    if cmd in ("help", "aide"):
        show_help()
        notice = _take_paste_notice()           # a paste refused on the help screen is reported here
        return True, (f"{YELLOW}{notice}" if notice else None), None
    if cmd in VIEWS:
        return True, None, cmd
    if cmd in ("fr", "en"):
        DEFAULT_LANG = cmd
        return True, (None if save_config() else tr("lang_not_saved")), None
    if cmd in ("eu", "us"):
        DEFAULT_DATE_ORDER = "MDY" if cmd == "us" else "DMY"
        label = tr("date_fmt_us") if cmd == "us" else tr("date_fmt_eu")
        msg = (f"{YELLOW}{tr('date_format_set', label=label)}" if save_config()
               else tr("date_format_not_saved", label=label))
        return True, msg, None
    return False, None, None


@_with_undo
def session_single(path, position=None):
    eng = engine_for(path)
    # 100% read-only format: open on "all" (the "edit" view would show only name + file
    # dates there, hiding the real content until you type "all").
    focus, msg, mode = None, None, "all" if eng in _READONLY_ENGINES else "edit"
    # Dedicated engine missing: we stay readable (external data via exiftool),
    # but only the file data is editable.
    degraded = eng in ("mutagen", "ffmpeg") and not engine_available(eng)

    while True:
        data = read(path)
        if data is None:
            print(f"{RED}{tr('cannot_read_file')}{RESET}")
            ask()
            return "skip"                            # unreadable: auto-advance — distinct from a
                                                     # user "next" so the walk may exit past the end

        w = set(FILE_BASE_TAGS) if degraded else writable(path)
        shown = visible_tags(data, w, mode)
        aliases = aliases_of(shown.keys())
        mismatch = ext_mismatch(path, data) if eng == "exiftool" else None

        if focus:
            if focus not in shown:
                msg, focus = tr("field_not_found", focus=focus), None
                continue
            v = shown[focus]
            clear_screen()
            print(f"{BOLD}{path.name}{RESET}  {DIM}(focus){RESET}\n")
            print(f"{BOLD}{label_of(focus, DEFAULT_LANG)}{RESET}  {DIM}({aliases[focus]}){RESET}\n")
            print(fmt(v, full=True))
            hint = tr("focus_editable") if focus in w else tr("focus_readonly")
            print(f"\n{DIM}{hint}{RESET}")
            print(f"\n{PROMPT}", end="", flush=True)
            # The field is named on the screen and the line IS its value: a paste needs no
            # typed prefix here, it just may not validate itself (Enter still does).
            line = ask(paste_ok=lambda _prefix, ok=(focus in w): ok)
            if line is None:
                return "quit"
            notice = _take_paste_notice()
            if notice:
                msg, focus = f"{YELLOW}{notice}", None
                continue
            line_stripped = line.strip()
            if line_stripped == "" or focus not in w:
                focus = None
                continue
            new_val = to_exif(line_stripped, focus)
            if new_val is None:
                msg = tr("unreadable_date")
                focus = None
                continue
            if focus == "FileName":
                new_path, err = apply_filename(path, new_val)
                if new_path is not None:
                    path = new_path
                else:
                    msg = err
            elif not write(path, focus, new_val):
                msg = tr("write_failed")
            focus = None
            continue

        rows = []
        for kind, payload in themed_layout(shown.keys(), data):
            if kind == "H":
                rows.append((None, payload, None, None))
            else:
                t = payload
                rows.append((label_of(t, DEFAULT_LANG), aliases[t], fmt(shown[t]), t in w))
        dfmt = "us" if DEFAULT_DATE_ORDER == "MDY" else "eu"
        subtitle = f"[{DEFAULT_LANG}|{dfmt}|{eng}]"
        if degraded:
            how = _install_hint("ffmpeg" if eng == "ffmpeg" else "mutagen")
            subtitle += f" {RED}⚠ {tr('degraded', eng=eng, how=how)}{RESET}"
        if position:
            subtitle += f" · {tr('file_position', i=position[0], n=position[1])}"
        if mismatch:
            subtitle += f" {RED}⚠ {tr('mismatch', ext=mismatch[0], real=mismatch[1])}{RESET}"
        nav = None
        if position:                                 # sequential navigation in the batch; the
            nav = tr("nav_footer_arrows" if (_raw_mode or _win_raw)      # arrows need our own
                     else "nav_footer")                                  # reader, either system's
        render(path.name, subtitle, rows, msg,
               view_footer(mode, nav, batch="single" if position else None),
               scrollable=(mode != "edit"))
        msg = None                                   # notices are one-shot: any next action clears them

        line = ask(nav=bool(position),
                   paste_ok=lambda prefix: _value_prefix(prefix, aliases, shown.keys(), DEFAULT_LANG))
        if line is None:
            return "quit"
        notice = _take_paste_notice()
        if notice:
            msg = f"{YELLOW}{notice}"
            continue
        line_stripped = line.strip()
        cmd = _command(line_stripped)               # commands ignore the case; a field name never does
        if cmd in ("q", "quit", "exit"):
            return "quit"
        if position and cmd == "n":
            return "next"
        if position and cmd == "p":
            return "prev"
        # "g"/"group": back to the whole-batch view (run_sessions re-enters
        # session_group), mirror of "s"/"single" in group mode. Meaningless outside
        # a batch: say so.
        if cmd in ("g", "group"):
            if position:
                return "group"
            msg = tr("group_only_multi")
            continue
        if line_stripped == "":
            continue
        handled, shared_msg, view = _shared_command(cmd)
        if handled:
            msg, mode = shared_msg, view or mode
            continue
        if cmd in ("u", "undo"):
            if _UNDO.has_changes():
                path = _UNDO.undo_last(track=path)      # follows any undone rename
                msg = f"{YELLOW}{tr('change_undone')}"
            else:
                msg = tr("nothing_undo")
            continue
        if cmd in ("ua", "undo all"):
            if _UNDO.has_changes():
                path = _UNDO.undo_all(track=path)
                msg = f"{YELLOW}{tr('all_undone')}"
            else:
                msg = tr("nothing_undo")
            continue
        if cmd == "wipe":
            if wipe(path):
                note = _wipe_caveat([path])
                msg = f"{YELLOW}{tr('metadata_wiped')}" + (f" {note}" if note else "")
            else:
                msg = tr("wipe_failed")
            continue
        if cmd.startswith("dates "):
            with _UNDO.batch():
                touched, errors, present = cmd_dates(path, line_stripped[6:].strip())
            if touched is None:
                msg = tr("unreadable_date")
            elif present == 0:
                msg = tr("no_date_present")
            elif touched == 0 and errors == 0:
                msg = f"{YELLOW}{tr('shift_no_effect')}"
            elif errors:
                msg = tr("dates_updated_err", touched=touched, errors=errors)
            else:
                msg = f"{YELLOW}{tr('dates_updated', touched=touched)}"
            continue

        tag, val = resolve(line, aliases, shown.keys(), DEFAULT_LANG)
        if not tag:
            msg = tr("unknown_field", name=line_stripped)
            continue
        if val is None:
            v = shown[tag]
            if isinstance(v, str) and v.startswith("(Binary data"):
                if not open_binary(path, tag):
                    msg = tr("cannot_open_binary")
            else:
                focus = tag
            continue
        if tag not in w:
            msg = tr("field_not_editable", tag=tag)
            continue
        offset = (parse_offset(val)
                  if tag in DATE_TAGS and isinstance(val, str) else None)
        if offset is not None:
            # Relative shift on ONE date field ("FileModifyDate +1d2h"): shift the
            # stored value, exactly like cmd_dates does for all of them at once.
            cur = data.get(tag)
            if cur in ("", None):
                msg = tr("no_date_present")
                continue
            shifted = _shift_stored_date(cur, offset)
            if shifted is None:
                msg = tr("unreadable_date")
            elif shifted == cur:                 # e.g. a sub-day shift on a date-only value
                msg = f"{YELLOW}{tr('shift_no_effect')}"
            elif not write(path, tag, shifted):
                msg = tr("write_failed")
            continue
        append = (tag in LIST_FIELDS and isinstance(val, str)
                  and val.startswith("+") and len(val) > 1)
        if append:
            val = val[1:].lstrip()
        new_val = to_exif(val, tag)
        if new_val is None:
            msg = tr("unreadable_date")
            continue
        if append:
            # Append as the EXACT list (current items + new one), never re-joined on ","
            # (cf. et_write: a re-split would break "Paris, France" in two).
            cur = shown.get(tag)
            items = (list(cur) if isinstance(cur, list)
                     else [cur] if cur not in (None, "") else [])
            new_val = [str(x) for x in items if str(x).strip()] + [new_val]
        if tag == "FileName":
            new_path, err = apply_filename(path, new_val)
            if new_path is not None:
                path = new_path
            else:
                msg = err
        elif not write(path, tag, new_val):
            msg = write_refusal_reason(path, tag, new_val) or tr("write_failed")


@_with_undo
def session_group(paths):
    msg, mode = None, "edit"

    while True:
        paths = [_live_path(p) for p in paths]   # follow a rename undone from the single view,
                                                 # like walk_single/run_sessions already do
        reads_map = read_many(paths)
        reads = [reads_map.get(p) for p in paths]
        if any(d is None for d in reads):
            print(f"{RED}{tr('cannot_read_files')}{RESET}")
            ask()
            return

        wrs = [writable_from_data(p, reads_map.get(p)) for p in paths]
        writable_any = set().union(*wrs)

        all_tags = set().union(*(d.keys() for d in reads))
        if mode == "edit":
            universe = set(writable_any)
        elif mode == "in":
            universe = set(all_tags)                 # present somewhere; we'll filter
        else:                                        # "all": editable + present technical
            universe = all_tags | writable_any
        merged = {}
        for tag in universe:
            vals, present = set(), 0
            for d in reads:
                v = (d or {}).get(tag)
                if isinstance(v, list):          # multi-value (exiftool JSON): joined for
                    v = ", ".join(str(x) for x in v)   # display, like fmt() — never repr()
                if v not in ("", None):
                    present += 1
                    vals.add(str(v))
            display = next(iter(vals)) if len(vals) == 1 else ("***" if vals else "")
            merged[tag] = (display, present, len(paths))
        if mode == "in":
            merged = {t: mv for t, mv in merged.items() if mv[1] > 0}
        else:
            for t in writable_any:                   # editable fields absent: addable
                merged.setdefault(t, ("", 0, len(paths)))

        aliases = aliases_of(merged.keys())
        rows = []
        for kind, payload in themed_layout(merged.keys(), reads[0] or {}):
            if kind == "H":
                rows.append((None, payload, None, None))
                continue
            t = payload
            display, present, total = merged[t]
            val = "***" if display == "***" else fmt(display)
            suffix = "" if present == total else f" {DIM}[{present}/{total}]{RESET}"
            rows.append((label_of(t, DEFAULT_LANG), aliases[t], f"{val}{suffix}", t in writable_any))

        preview = ", ".join(p.name for p in paths[:3]) + (f" … (+{len(paths)-3})" if len(paths) > 3 else "")
        dfmt = "us" if DEFAULT_DATE_ORDER == "MDY" else "eu"
        subtitle = (f"[{tr('group_tag')}|{DEFAULT_LANG}|{dfmt}] · "
                    + tr("n_files", n=len(paths), preview=preview))
        # No nav line in the group view: the batch is not walked here. The way back to file
        # by file ("s") sits on the view line, next to "g" — they are the two views of a batch.
        render(tr("group_title"), subtitle, rows, msg,
               view_footer(mode, batch="group" if len(paths) > 1 else None),
               scrollable=(mode != "edit"))
        msg = None

        line = ask(paste_ok=lambda prefix: _value_prefix(prefix, aliases, merged.keys(), DEFAULT_LANG))
        if line is None:
            return
        notice = _take_paste_notice()
        if notice:
            msg = f"{YELLOW}{notice}"
            continue
        line_stripped = line.strip()
        cmd = _command(line_stripped)               # commands ignore the case; a field name never does
        if cmd in ("q", "quit", "exit"):
            return
        if line_stripped == "":
            continue
        if cmd in ("s", "single"):
            # Split the batch into one-by-one sessions: signalled to run_sessions()
            # through the return value. Pointless on a single file: say so.
            if len(paths) > 1:
                return "single"
            msg = tr("single_only_multi")
            continue
        handled, shared_msg, view = _shared_command(cmd)
        if handled:
            msg, mode = shared_msg, view or mode
            continue
        if cmd in ("u", "undo"):
            msg = f"{YELLOW}{tr('change_undone')}" if _UNDO.undo_last() else tr("nothing_undo")
            continue
        if cmd in ("ua", "undo all"):
            msg = f"{YELLOW}{tr('all_undone')}" if _UNDO.undo_all() else tr("nothing_undo")
            continue
        if cmd == "wipe":
            print(f"\n{BOLD}{tr('wipe_confirm_inline', n=len(paths))}{RESET} ",
                  end="", flush=True)
            ans = ask()
            if ans is None or ans.strip().lower() != "y":
                msg = f"{YELLOW}{tr('wipe_cancelled')}"
                continue
            done, failed = 0, []
            with _UNDO.batch():                      # the whole batch = ONE undo step
                for p in paths:
                    if wipe(p):
                        done += 1
                    else:
                        failed.append(p.name)
            msg = tr("files_cleaned", done=done)
            if failed:
                msg += ", " + tr("failures", n=len(failed))
            msg += "."
            note = _wipe_caveat(paths)
            if note:
                msg += f" {note}"
            if not failed:                           # all cleaned: confirmation (yellow), not an error
                msg = f"{YELLOW}{msg}"
            continue
        if cmd.startswith("dates "):
            total_touched, total_errors, total_present, illegible = 0, 0, 0, False
            with _UNDO.batch():
                for p in paths:
                    touched, errors, present = cmd_dates(p, line_stripped[6:].strip())
                    if touched is None:
                        illegible = True
                        break
                    total_touched += touched
                    total_errors += errors
                    total_present += present
            if illegible:
                msg = tr("unreadable_date")
            elif total_present == 0:
                msg = tr("no_date_in_files")
            elif total_touched == 0 and total_errors == 0:
                msg = f"{YELLOW}{tr('shift_no_effect')}"
            elif total_errors:
                msg = tr("dates_updated_err", touched=total_touched, errors=total_errors)
            else:
                msg = f"{YELLOW}{tr('dates_updated', touched=total_touched)}"
            continue

        tag, val = resolve(line, aliases, merged.keys(), DEFAULT_LANG)
        if not tag:
            msg = tr("unknown_field", name=line_stripped)
            continue
        if val is None:
            msg = tr("group_use_field_value")
            continue
        if tag == "FileName":
            msg = tr("rename_unavailable_group")
            continue

        offset = (parse_offset(val)
                  if tag in DATE_TAGS and isinstance(val, str) else None)
        if offset is not None:
            # Relative shift on ONE date field across the batch: each file shifts from
            # its OWN stored value (the merged "***" display has no single value to move).
            touched = errors = present = 0
            with _UNDO.batch():
                for p, wr, d in zip(paths, wrs, reads):
                    cur = (d or {}).get(tag)
                    if tag not in wr or cur in ("", None):
                        continue
                    present += 1
                    shifted = _shift_stored_date(cur, offset)
                    if shifted is None:
                        errors += 1
                    elif shifted != cur:
                        if write(p, tag, shifted):
                            touched += 1
                        else:
                            errors += 1
            if present == 0:
                msg = tr("no_date_in_files")
            elif touched == 0 and errors == 0:
                msg = f"{YELLOW}{tr('shift_no_effect')}"
            elif errors:
                msg = tr("dates_updated_err", touched=touched, errors=errors)
            else:
                msg = f"{YELLOW}{tr('dates_updated', touched=touched)}"
            continue

        append = (tag in LIST_FIELDS and isinstance(val, str)
                  and val.startswith("+") and len(val) > 1)
        if append:
            val = val[1:].lstrip()
        new_val = to_exif(val, tag)
        if new_val is None:
            msg = tr("unreadable_date")
            continue
        errors = skipped = 0
        with _UNDO.batch():
            for p, wr, d in zip(paths, wrs, reads):
                if tag not in wr:
                    skipped += 1
                    continue
                v = new_val
                if append:
                    cur = (d or {}).get(tag)            # exact list (cf. session_single)
                    items = (list(cur) if isinstance(cur, list)
                             else [cur] if cur not in (None, "") else [])
                    v = [str(x) for x in items if str(x).strip()] + [new_val]
                if not write(p, tag, v):
                    errors += 1
        parts = []
        if errors: parts.append(tr("failures", n=errors))
        if skipped: parts.append(tr("skipped", n=skipped))
        if parts: msg = " · ".join(parts)


@_with_undo
def session_wipe(paths):
    clear_screen()
    print(f"{BOLD}{tr('wipe_confirm', n=len(paths))}{RESET}\n")
    for p in paths[:10]:
        print(f"{DIM}  • {p.name}{RESET}")
    if len(paths) > 10:
        print(f"{DIM}  {tr('and_more', n=len(paths)-10)}{RESET}")
    print(f"\n{DIM}{tr('cleanup_partial')}{RESET}")
    print(f"\n{BOLD}[y/N]{RESET} ", end="", flush=True)
    ans = ask()
    if ans is None or ans.strip().lower() != "y":
        print(tr("cancelled"))
        ask()
        return

    done, failed_names = 0, []
    with _UNDO.batch():                            # the whole batch = ONE undo step
        for p in paths:
            if wipe(p):
                done += 1
            else:
                failed_names.append(p.name)
    summary = f"\n{tr('files_cleaned', done=done)}"
    if failed_names:
        summary += f", {RED}{tr('failures', n=len(failed_names))}{RESET}"
    print(summary + ".")
    for name in failed_names[:10]:                 # NAME the failures (not just a count)
        print(f"{DIM}  • {tr('failed_name', name=name)}{RESET}")
    if len(failed_names) > 10:
        print(f"{DIM}  {tr('and_more', n=len(failed_names) - 10)}{RESET}")
    note = _wipe_caveat(paths)
    if note:
        print(f"\n{DIM}{note}{RESET}")
    _undo_prompt(tr("wipe_cancelled"))


def _is_sidecar(p):
    """Backup file (.bak) or temporary file (.tmp, *_tmp markers): NEVER to be
       expanded from a folder — a wipe/group must not touch the metmux temporaries
       nor the backup copies left by other tools. ".exifedit_tmp" is the marker metmux
       wrote under its former name: no longer produced, still recognised, so a temporary
       an older version left on disk after a crash is not swept into a batch."""
    name = p.name.lower()
    return (p.suffix.lower() in (".bak", ".tmp")
            or ".exifedit_tmp" in name or ".metmux_tmp" in name)


def collect_paths(args):
    """Expands the args into files: folder → DIRECT files (non-recursive, dotfiles,
       backups/temporaries and symbolic links excluded), then dedups by resolved path
       keeping the order."""
    paths = []
    for a in args:
        p = Path(a)
        if p.is_file():
            paths.append(p)
        elif p.is_dir():
            paths.extend(sorted(c for c in p.iterdir()
                                if c.is_file() and not c.is_symlink()  # no stepping out of scope
                                and not c.name.startswith(".")
                                and not _is_sidecar(c)))
    seen, uniq = set(), []
    for p in paths:
        k = str(p.resolve())
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq


def walk_single(paths):
    """Walk a batch file by file, both directions, driven by session_single's return.
       An index, not a for loop: "prev" must be able to go back. Both ends CLAMP — a →
       held one press too long must not dump the user out of the batch; only "q" leaves
       ("skip", an unreadable file, auto-advances and may walk off the end). Returns
       "group" when a session asks for the whole-batch view."""
    n = len(paths)
    i = 0
    while 0 <= i < n:
        paths[i] = _live_path(paths[i])          # follow a rename made from another screen
        sig = session_single(paths[i], position=(i + 1, n) if n > 1 else None)
        if sig == "quit":
            break
        if sig == "group":
            return "group"
        if sig == "prev":
            i = max(0, i - 1)
        elif sig == "skip":
            i += 1                               # unreadable: may exit past the end (never loops)
        else:
            i = min(n - 1, i + 1)


def choose_session_mode(paths):
    """Opening choice of a multi-file launch (--mode=ask): the whole batch at once
       (group) or file by file (single). Returns "group", "single" or None (quit).
       The full words are accepted too — they are what the session commands use."""
    # Same visual grammar as the help panel: a heading and its rule, then rows whose
    # command sits in a column of its own — what is learnt here is typed later, unchanged.
    rows = [(("g", "group"), tr("choose_group")),
            (("s", "single"), tr("choose_single")),
            (("q", "quit"), tr("choose_quit"))]
    col = max(len(f"{k} | {w}") for (k, w), _ in rows) + 2
    # The rule spans the block it heads — the commands and their note, not the file list
    # above it, whose names can be arbitrarily long.
    span = max([2 + col + 2 + len(label) for _, label in rows]
               + [2 + len(tr("choose_switch_note")), len(tr("choose_head"))])
    msg = None
    while True:
        clear_screen()
        print(f"{BOLD}metmux{RESET}  {GREY}· {tr('choose_title', n=len(paths))}{RESET}\n")
        for p in paths[:10]:
            print(f"  {p.name}")
        if len(paths) > 10:
            print(f"  {GREY}{tr('and_more', n=len(paths)-10)}{RESET}")
        head = tr("choose_head")
        rule = panel_rule(len(head), span)
        print(f"\n{GREY}{head} {rule}{RESET}")
        for (k, word), label in rows:
            cell = f"{BOLD}{k}{RESET}{GREY} | {RESET}{BOLD}{word}{RESET}"
            print(f"  {cell}{' ' * (col - len(f'{k} | {word}'))}{GREY}—{RESET} {label}")
        print(f"  {GREY}{tr('choose_switch_note')}{RESET}")
        if msg:
            print(f"\n{msg}{RESET}")
        print(f"\n{PROMPT}", end="", flush=True)
        line = ask()
        if line is None:
            return None
        notice = _take_paste_notice()
        if notice:
            msg = f"{YELLOW}{notice}"
            continue
        ans = line.strip().lower()
        if ans in ("q", "quit", "exit"):
            return None
        if ans in ("g", "group"):
            return "group"
        if ans in ("s", "single"):
            return "single"
        msg = f"{RED}{tr('choose_invalid')}" if ans else None


@_with_undo
def run_sessions(mode, paths):
    """Session dispatch. "ask" resolves through the opening choice (skipped on a lone
       file); then the user can flip between the two multi-file modes for as long as
       they like — "s"/"single" typed in group mode walks the batch file by file,
       "g"/"group" typed in a batched single session returns to the merged view.
       The WHOLE run shares one undo stack (_with_undo is reentrant: the sessions
       inside reuse this one): u/ua reach back across file changes and mode flips."""
    if mode == "wipe":
        session_wipe(paths)
        return
    nxt = mode
    if nxt == "ask":
        nxt = choose_session_mode(paths) if len(paths) > 1 else "single"
    while nxt in ("group", "single"):
        paths = [_live_path(p) for p in paths]   # follow renames made from the other view
        nxt = session_group(paths) if nxt == "group" else walk_single(paths)
    _finish()


# --- Rendezvous of simultaneous launches (--gather) ---
# The Windows context menu starts ONE metmux per selected file: without a merge, three
# selected files would mean three windows and never a group session. The first process
# to create the rendezvous directory becomes the leader; the others drop their paths
# there and exit right away with EXIT_HANDED_OFF (their console window just flashes),
# and the leader opens the session on the merged batch. macOS and Linux pass all the
# files in one launch, so their integrations don't need the flag.
EXIT_HANDED_OFF = 3         # follower exit code: the .bat closes silently (no pause)
GATHER_QUIET = 0.75         # s without a new drop before the leader closes the door
GATHER_CAP = 10.0           # s max of collection, whatever happens (safety bound)
GATHER_STALE = 20.0         # a rendezvous untouched this long is a leftover of a crash
_GATHER_TICK = 0.05         # s between two looks at the rendezvous directory


def _gather_dir():
    override = os.environ.get("METMUX_GATHER_DIR")   # tests: a hermetic rendezvous
    if override:
        return Path(override)
    # Per-user name: on Linux/macOS the temp directory is shared between users —
    # without the suffix, two users right-clicking at the same time would collide
    # (and the second would not even have the permissions to join the first).
    user = re.sub(r"[^A-Za-z0-9._-]", "_",
                  os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    return Path(tempfile.gettempdir()) / f"metmux-gather-{user}"


def _gather_lead(rdv, args, quiet, cap):
    """Leader side: collect the drops until a quiet period, close the door, return
       the merged list. The door is closed by RENAMING the directory (atomic): a drop
       that lands after the rename fails cleanly and its process leads its own
       session — a path can end up in a second window, never lost."""
    drops = {}
    t0 = last_new = time.monotonic()
    closed = None
    while True:
        for f in rdv.glob("*.paths"):
            if f.name not in drops:
                try:
                    drops[f.name] = json.loads(f.read_text(encoding="utf-8"))
                    last_new = time.monotonic()
                except (OSError, ValueError):        # mid-write or vanished: next tick
                    pass
        now = time.monotonic()
        if now - last_new < quiet and now - t0 < cap:
            time.sleep(_GATHER_TICK)
            continue
        # Try to close the door. os.replace of the directory is atomic; on Windows it
        # can transiently fail while a follower still holds a file open inside — retry
        # over ~1 s, then fall back to collecting in place (microscopic race accepted:
        # the directory must not survive, or later launches would drop into a dead
        # rendezvous that nobody reads).
        target = rdv.with_name(f"{rdv.name}-read-{os.getpid()}")
        for _ in range(20):
            try:
                os.replace(rdv, target)
                closed = target
                break
            except OSError:
                time.sleep(_GATHER_TICK)
        break
    final = closed or rdv
    for f in final.glob("*.paths"):                  # last look: drops during the retries
        if f.name not in drops:
            try:
                drops[f.name] = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    shutil.rmtree(final, ignore_errors=True)
    merged = list(args)                              # the leader was launched first
    for name in sorted(drops):                       # then arrival order (time_ns names)
        merged.extend(drops[name])
    return merged


def gather_args(args, rdv=None, quiet=GATHER_QUIET, cap=GATHER_CAP):
    """Merges the near-simultaneous launches into a single session. Returns the merged
       argument list for the process that leads it, or None for a process whose paths
       were handed to the leader (its caller exits with EXIT_HANDED_OFF)."""
    rdv = Path(rdv) if rdv else _gather_dir()
    for _ in range(3):                               # retries absorb every closing race
        try:
            rdv.mkdir(parents=True)                  # atomic: exactly one process wins
            return _gather_lead(rdv, args, quiet, cap)
        except FileExistsError:
            pass
        try:
            # A leftover of a crashed leader would swallow drops that nobody will ever
            # read: too old (no file landed recently) → clear it and take the lead.
            if time.time() - rdv.stat().st_mtime > GATHER_STALE:
                shutil.rmtree(rdv, ignore_errors=True)
                continue
            # Two-step drop: the leader only reads "*.paths", never a half-written file.
            tmp = rdv / f"{time.time_ns():020d}-{os.getpid()}.tmp"
            tmp.write_text(json.dumps([str(a) for a in args]), encoding="utf-8")
            tmp.replace(tmp.with_suffix(".paths"))
            return None
        except OSError:                              # door closed mid-drop: try again —
            continue                                 # worst case we lead our own session
    return list(args)                                # rendezvous unusable: open alone


def main():
    enable_windows_ansi()
    for stream in (sys.stdin, sys.stdout, sys.stderr):  # UTF-8 whatever the inherited LANG
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    load_config()

    args = sys.argv[1:]
    # Recognised at ANY position, like --mode= below: "metmux.py file --version" must
    # not silently open a session on the file.
    if any(a in ("-V", "--version") for a in args):
        print(f"metmux {__version__}")
        return
    if not args or any(a in ("-h", "--help") for a in args):
        print(tr("cli_help"))
        return

    # --mode recognised wherever it is in argv (not only first), so that
    # "f --mode=wipe" does not silently fall back to single mode.
    mode, gather = "single", False
    rest = []
    for a in args:
        if a.startswith("--mode="):
            mode = a.split("=", 1)[1]
        elif a == "--gather":
            gather = True
        else:
            rest.append(a)
    args = rest

    if mode not in ("single", "group", "ask", "wipe"):
        print(f"{RED}{tr('invalid_mode', mode=mode)}{RESET}")
        print(tr("cli_help"))
        sys.exit(1)

    if gather:
        merged = gather_args(args)
        if merged is None:                         # our paths travel with the leader:
            sys.exit(EXIT_HANDED_OFF)              # exit before touching the terminal
        args = merged

    # Start the resident exiftool NOW (Windows; a no-op elsewhere). Launching the process
    # returns at once — what costs is exiftool loading its Perl, and that cost now runs
    # alongside our own startup (listing the files, the preflight, the first frame)
    # instead of after it. Placed AFTER the gather on purpose: the followers exit above, so
    # a 12-file selection starts ONE exiftool, not twelve.
    _et_daemon()

    paths = collect_paths(args)

    if not paths:
        print(f"{RED}{tr('no_valid_file')}{RESET}")
        ask()
        sys.exit(1)

    # Warn if some arguments were silently ignored (neither a file nor a folder):
    # a path typo, or "--mode wipe" (space) instead of "--mode=wipe" — otherwise the
    # mode would stay "single" without the slightest sign.
    dropped = [a for a in args if not (Path(a).is_file() or Path(a).is_dir())]
    if dropped:
        print(f"{RED}{tr('args_ignored', items=', '.join(dropped))}{RESET}")
        if any("--mode" in a and "=" not in a for a in dropped):
            print(f"{DIM}{tr('mode_tip')}{RESET}")
        print(f"\n{DIM}{tr('enter_continue')}{RESET}")
        ask()

    # Preflight: exiftool is almost always required (system dates included).
    missing = _missing_dependencies(paths)
    if any(name == "exiftool" for name, _ in missing):
        print(f"{RED}{tr('exiftool_required')}{RESET}\n")
        for name, how in missing:
            print(f"  • {name} : {how}")
        ask()
        sys.exit(1)
    if missing:                                      # ffmpeg / mutagen: only some files
        print(f"{DIM}{tr('missing_tools')}{RESET}")
        for name, how in missing:
            print(f"{DIM}  • {name} : {how}{RESET}")
        print(f"\n{DIM}{tr('enter_continue')}{RESET}")
        ask()

    # Live resize: on a real terminal, ask the OS to signal window changes (SIGWINCH)
    # so _on_winch can redraw the current screen at the new width right away. Guarded by
    # isatty (no point off a terminal) and hasattr (SIGWINCH is Unix-only). The previous
    # handler is restored on exit. signal.signal must run on the main thread — it does.
    winch_ok = sys.stdout.isatty() and hasattr(signal, "SIGWINCH")
    old_winch = signal.getsignal(signal.SIGWINCH) if winch_ok else None
    if winch_ok:
        signal.signal(signal.SIGWINCH, _on_winch)
    elif _WIN_CONSOLE and sys.stdout.isatty():
        # No such signal on Windows: poll the size on a daemon thread instead (_watch_terminal_size),
        # so the rules reflow while the window is being dragged rather than at the next command.
        threading.Thread(target=_watch_terminal_size, daemon=True).start()

    # Raw keyboard reader: read the keyboard ourselves instead of input(), so a resize can
    # re-echo the half-typed line (_on_winch reprints _typed) and — the point of the paste
    # guard — so the terminal never gets to echo a pasted block and serve it back to us as a
    # burst of commands. POSIX: cbreak, which keeps ISIG on (Ctrl-C still raises SIGINT) and
    # OPOST on (\n -> \r\n); armed once here, always restored in the finally below (even on
    # exception). Windows: nothing to arm — msvcrt.getwch() bypasses the console's line editor
    # by itself, so there is no mode to set and none to restore. Off a terminal (pipe,
    # redirection) both stay off and _read_raw falls back to input().
    global _raw_mode, _win_raw, _raw_fd, _pending, _char_buf, _typed, _cursor, _in_paste, _decoder, _saved_term
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    raw_ok = os.name == "posix" and interactive
    saved_term = None
    if raw_ok:
        import termios, tty
        _raw_fd = sys.stdin.fileno()
        saved_term = termios.tcgetattr(_raw_fd)
        _saved_term = saved_term                     # also used by the suspend handler (_on_tstp)
        tty.setcbreak(_raw_fd)                        # ICANON & ECHO off; ISIG & OPOST left on
        _pending, _char_buf, _typed, _cursor, _in_paste = b"", "", "", 0, False
        _decoder = codecs.getincrementaldecoder("utf-8")(errors="surrogateescape")
        _raw_mode = True
    win_mode = None
    if not raw_ok and interactive and msvcrt is not None:
        _pending, _char_buf, _typed, _cursor, _in_paste = b"", "", "", 0, False
        _win_raw = True
        win_mode = enable_windows_vt_input()          # markers, if the console will give them

    # Suspend/resume (Ctrl-Z then `fg`): without a SIGCONT handler the shell can leave the
    # terminal cooked (echo on) on resume, so our own echo would double every keystroke. We
    # restore the cooked terminal on SIGTSTP (clean shell prompt) and re-arm cbreak on SIGCONT.
    # Guarded by raw_ok and hasattr (SIGTSTP/SIGCONT are Unix job-control signals). Restored
    # in the finally. signal.signal must run on the main thread — it does.
    tstp_ok = raw_ok and hasattr(signal, "SIGTSTP") and hasattr(signal, "SIGCONT")
    old_tstp = signal.getsignal(signal.SIGTSTP) if tstp_ok else None
    old_cont = signal.getsignal(signal.SIGCONT) if tstp_ok else None
    if tstp_ok:
        signal.signal(signal.SIGTSTP, _on_tstp)
        signal.signal(signal.SIGCONT, _on_cont)

    # We do NOT switch to the alternate screen buffer: on macOS Terminal.app it stranded the
    # frame far down the page (a screenful of blank lines on open). render() runs entirely in
    # the normal buffer and resets it with \033c on the first edit frame, painting at the top.
    # PASTE_ON enables bracketed paste: the terminal wraps pasted text so ask() can refuse it.
    global _IN_ALT, _LAST_FRAME, _mouse_on, _edit_scroll
    _IN_ALT = False                                   # no view drawn yet: the first render enters the edit view
    _mouse_on = False
    _edit_scroll = 0
    sys.stdout.write(PASTE_ON)
    sys.stdout.flush()
    try:
        run_sessions(mode, paths)
    finally:
        if raw_ok and saved_term is not None:
            import termios
            termios.tcsetattr(_raw_fd, termios.TCSADRAIN, saved_term)  # restore line mode + echo
        restore_windows_console_mode(win_mode)        # (the console's own input mode, untouched
        _raw_mode = _win_raw = False                  #  otherwise: getwch needs none of it)
        if winch_ok:
            signal.signal(signal.SIGWINCH, old_winch)
        if tstp_ok:
            signal.signal(signal.SIGTSTP, old_tstp)
            signal.signal(signal.SIGCONT, old_cont)
        _LAST_FRAME = None
        _IN_ALT = False
        _mouse_on = False
        # A closing newline so the shell prompt starts on a fresh line: the reader no longer
        # echoes the Enter that quit us (see _read_line_raw), so without this the prompt would
        # land right after our "> q". Harmless on every other exit path.
        # ?1049l leaves the alt screen defensively (metmux never enters it).
        sys.stdout.write(PASTE_OFF + MOUSE_OFF + "\033[?1049l\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
