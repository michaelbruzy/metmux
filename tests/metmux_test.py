# SPDX-License-Identifier: GPL-3.0-or-later
"""metmux.py test bench.

    pip install pytest mutagen hypothesis
    HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis python3 -B -m pytest tests/ -p no:cacheprovider

We test the engine, not the interactive loop (TUI): the risk lives in the data layer
(parsing, routing, writing/clearing by format). Every test that touches the disk works
on a throwaway copy (tmp_path); the fixtures are built by the bench (zip stdlib;
mp3/mkv via ffmpeg, otherwise the test is skipped — never a false green).
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

import metmux as mm

SCRIPT = Path(__file__).resolve().parent.parent / "metmux.py"


def _plain(s):
    """Screen output without the ANSI colour/formatting codes, so assertions can
       anchor on the visible text (the codes sit between letters: '  \\x1b[1mg\\x1b[0m : ')."""
    import re
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", s)


# ─── Detection of available tools (to skip cleanly, never lie) ──
HAVE_EXIFTOOL = shutil.which("exiftool") is not None or any(
    Path(p).exists() for p in mm._EXIFTOOL_FALLBACK_PATHS)
HAVE_FFMPEG = shutil.which("ffmpeg") is not None

needs_exiftool = pytest.mark.skipif(not HAVE_EXIFTOOL, reason="exiftool required")
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg required (to build the media)")


# ============================================================
#  Test file factories (auto-generated fixtures)
# ============================================================

# Valid 1×1 transparent PNG.
_PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGP4"
            "DwQACfsD/fteaysAAAAASUVORK5CYII=")
PNG_BYTES = base64.b64decode(_PNG_B64)


def write_png(tmp_path, name="img.png"):
    p = tmp_path / name
    p.write_bytes(PNG_BYTES)
    return p


# --- minimal but structurally complete .docx (OOXML zip) ---
_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
    '</Types>')
_DOTRELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
    '</Relationships>')
_DOCUMENT_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body><w:p><w:r><w:t>hello</w:t></w:r></w:p></w:body></w:document>')
_CORE_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
    ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator>'
    '<dcterms:created xsi:type="dcterms:W3CDTF">2020-01-01T00:00:00Z</dcterms:created>'
    '</cp:coreProperties>')
_APP_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
    '<Company>{company}</Company><Application>TestApp</Application></Properties>')


def make_docx(path, title="MyTitle", creator="MyAuthor", company="MyCompany"):
    parts = {
        "[Content_Types].xml": _CONTENT_TYPES,
        "_rels/.rels": _DOTRELS,
        "word/document.xml": _DOCUMENT_XML,
        "docProps/core.xml": _CORE_XML.format(title=title, creator=creator),
        "docProps/app.xml": _APP_XML.format(company=company),
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in parts.items():
            z.writestr(name, content)
    return path


# --- minimal .epub (zip with mimetype, container, opf) ---
_EPUB_CONTAINER = (
    '<?xml version="1.0"?>'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
    '</container>')
_EPUB_OPF = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:title>{title}</dc:title><dc:creator>{creator}</dc:creator><dc:language>fr</dc:language>'
    '</metadata><manifest/><spine/></package>')


def make_epub(path, title="TheTitle", creator="TheAuthor"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", _EPUB_CONTAINER)
        z.writestr("content.opf", _EPUB_OPF.format(title=title, creator=creator))
    return path


# --- media generated by ffmpeg (synthetic sources, tiny duration) ---
def make_mp3(tmp_path):
    out = tmp_path / "audio.mp3"
    r = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=8000",
         "-t", "0.1", "-q:a", "9", "-y", str(out)],
        capture_output=True)
    return out if r.returncode == 0 and out.exists() else None


def make_mkv(tmp_path):
    out = tmp_path / "video.mkv"
    r = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.1", "-y", str(out)],
        capture_output=True)
    return out if r.returncode == 0 and out.exists() else None


def _zip_names(path):
    with zipfile.ZipFile(path) as z:
        return set(z.namelist())


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ============================================================
#  Section 0 — CLI surface (black box, subprocess)
# ============================================================
# We really run `python3 metmux.py ...` and check the input safeguards. These three
# cases exit BEFORE any keyboard read, so there is no blocking.

def test_cli_help():
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    # Language-independent: the usage line and mode words are never translated.
    assert "--mode=MODE" in r.stdout
    assert "single" in r.stdout


def test_cli_no_args():
    r = subprocess.run([sys.executable, str(SCRIPT)],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "--mode=MODE" in r.stdout


def test_cli_bad_mode():
    r = subprocess.run([sys.executable, str(SCRIPT), "--mode=bogus"],
                       capture_output=True, text=True, input="")
    assert r.returncode == 1


def test_cli_help_documents_ask_and_gather():
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                       capture_output=True, text=True)
    assert "ask" in r.stdout
    assert "--gather" in r.stdout


@needs_exiftool
def test_cli_mode_ask_one_file_opens_single_session(tmp_path):
    # --mode=ask with ONE file skips the group-or-single question entirely.
    p = make_docx(tmp_path / "one.docx")
    r = subprocess.run([sys.executable, str(SCRIPT), "--mode=ask", str(p)],
                       capture_output=True, text=True, input="q\n", timeout=120)
    assert r.returncode == 0
    assert "\n  g | group" not in _plain(r.stdout)   # no question on a lone file


@needs_exiftool
def test_cli_mode_ask_two_files_asks_then_opens_group(tmp_path):
    # --mode=ask with SEVERAL files shows the choice screen first.
    a = make_docx(tmp_path / "a.docx")
    b = make_docx(tmp_path / "b.docx")
    r = subprocess.run([sys.executable, str(SCRIPT), "--mode=ask", str(a), str(b)],
                       capture_output=True, text=True, input="g\nq\n", timeout=120)
    assert r.returncode == 0
    assert "\n  g | group" in _plain(r.stdout)       # the question was asked


@needs_exiftool
def test_cli_mode_ask_answer_s_walks_file_by_file(tmp_path):
    # "s" at the choice screen opens the sequential walk ("1/2" = position, language-neutral).
    a = make_docx(tmp_path / "a.docx")
    b = make_docx(tmp_path / "b.docx")
    r = subprocess.run([sys.executable, str(SCRIPT), "--mode=ask", str(a), str(b)],
                       capture_output=True, text=True, input="s\nq\n", timeout=120)
    assert r.returncode == 0
    assert "1/2" in r.stdout


def test_cli_gather_follower_exits_3_and_hands_off(tmp_path):
    # A rendezvous is already open (another instance leads): this launch must drop
    # its path there and exit with the hand-off code (3) BEFORE any terminal work —
    # the Windows .bat closes that window silently on 3. No exiftool needed: the
    # follower leaves before the dependency preflight.
    rdv = tmp_path / "rdv"
    rdv.mkdir()
    f = tmp_path / "x.txt"
    f.write_text("x", encoding="utf-8")
    env = {**os.environ, "METMUX_GATHER_DIR": str(rdv)}
    r = subprocess.run([sys.executable, str(SCRIPT), "--mode=ask", "--gather", str(f)],
                       capture_output=True, text=True, input="", env=env, timeout=120)
    assert r.returncode == 3
    drops = list(rdv.glob("*.paths"))
    assert len(drops) == 1
    assert json.loads(drops[0].read_text(encoding="utf-8")) == [str(f)]


@needs_exiftool
def test_cli_gather_leader_opens_the_session(tmp_path):
    # No rendezvous open: the launch leads one, waits briefly for followers (none
    # here), then opens the normal session on its own file — and leaves no
    # rendezvous directory behind.
    rdv = tmp_path / "rdv"
    p = make_docx(tmp_path / "one.docx")
    env = {**os.environ, "METMUX_GATHER_DIR": str(rdv)}
    r = subprocess.run([sys.executable, str(SCRIPT), "--mode=ask", "--gather", str(p)],
                       capture_output=True, text=True, input="q\n", env=env, timeout=120)
    assert r.returncode == 0
    assert not rdv.exists()


def test_cli_version():
    r = subprocess.run([sys.executable, str(SCRIPT), "--version"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert mm.__version__ in r.stdout


def test_cli_version_recognised_at_any_position(tmp_path):
    # Like --mode=, the option is honoured wherever it sits: "metmux.py file --version"
    # must print the version, not silently open a session on the file.
    r = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path / "x.txt"), "--version"],
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert mm.__version__ in r.stdout
    r = subprocess.run([sys.executable, str(SCRIPT), "--help"],
                       capture_output=True, text=True)
    assert "-V, --version" in r.stdout          # help documents its own options


def test_version_is_stable_and_exposed():
    assert "experimental" not in (mm.__doc__ or "")
    assert mm.__version__ == "1.0.0"


# ============================================================
#  Section 0bis — config.json (default language)
# ============================================================
import json as _json_cfg


@pytest.fixture
def restore_config_globals():
    # load_config() writes to globals: we save and restore them.
    saved = mm.DEFAULT_LANG
    saved_order = mm.DEFAULT_DATE_ORDER
    yield
    mm.DEFAULT_LANG = saved
    mm.DEFAULT_DATE_ORDER = saved_order


def test_config_sets_lang(tmp_path, restore_config_globals):
    cfg = tmp_path / "config.json"
    cfg.write_text(_json_cfg.dumps({"lang": "en"}), encoding="utf-8")
    mm.DEFAULT_LANG = "fr"
    mm.load_config(cfg)
    assert mm.DEFAULT_LANG == "en"


def test_config_absent_keeps_defaults(tmp_path, restore_config_globals):
    mm.DEFAULT_LANG = "fr"
    mm.load_config(tmp_path / "absent.json")
    assert mm.DEFAULT_LANG == "fr"


def test_config_malformed_does_not_crash(tmp_path, restore_config_globals):
    cfg = tmp_path / "config.json"
    cfg.write_text("{ not valid json", encoding="utf-8")
    mm.DEFAULT_LANG = "fr"
    mm.load_config(cfg)                                # must not raise
    assert mm.DEFAULT_LANG == "fr"


def test_config_invalid_lang_ignored(tmp_path, restore_config_globals):
    cfg = tmp_path / "config.json"
    cfg.write_text(_json_cfg.dumps({"lang": "zz"}), encoding="utf-8")
    mm.DEFAULT_LANG = "fr"
    mm.load_config(cfg)
    assert mm.DEFAULT_LANG == "fr"


@pytest.mark.parametrize("value,expected", [
    ("eu", "DMY"), ("us", "MDY"),         # public regional names
    ("Eu", "DMY"), ("US", "MDY"),         # case-insensitive
])
def test_config_sets_date_format(tmp_path, restore_config_globals, value, expected):
    cfg = tmp_path / "config.json"
    cfg.write_text(_json_cfg.dumps({"date_format": value}), encoding="utf-8")
    mm.DEFAULT_DATE_ORDER = "DMY"
    mm.load_config(cfg)
    assert mm.DEFAULT_DATE_ORDER == expected


@pytest.mark.parametrize("value", ["zzz", "dmy", "mdy"])   # dmy/mdy are no longer accepted
def test_config_invalid_date_format_ignored(tmp_path, restore_config_globals, value):
    cfg = tmp_path / "config.json"
    cfg.write_text(_json_cfg.dumps({"date_format": value}), encoding="utf-8")
    mm.DEFAULT_DATE_ORDER = "DMY"
    mm.load_config(cfg)
    assert mm.DEFAULT_DATE_ORDER == "DMY"


@pytest.mark.parametrize("order,expected_fmt", [("DMY", "eu"), ("MDY", "us")])
def test_save_config_writes_current_globals(tmp_path, restore_config_globals, order, expected_fmt):
    cfg = tmp_path / "config.json"
    mm.DEFAULT_LANG = "fr"
    mm.DEFAULT_DATE_ORDER = order
    assert mm.save_config(cfg) is True
    written = _json_cfg.loads(cfg.read_text(encoding="utf-8"))
    assert written == {"lang": "fr", "date_format": expected_fmt}   # internal order → public name


def test_save_then_load_round_trip(tmp_path, restore_config_globals):
    cfg = tmp_path / "config.json"
    mm.DEFAULT_LANG, mm.DEFAULT_DATE_ORDER = "en", "MDY"
    mm.save_config(cfg)
    mm.DEFAULT_LANG, mm.DEFAULT_DATE_ORDER = "fr", "DMY"   # perturb, then re-read
    mm.load_config(cfg)
    assert mm.DEFAULT_LANG == "en"
    assert mm.DEFAULT_DATE_ORDER == "MDY"


def test_save_config_preserves_unknown_keys(tmp_path, restore_config_globals):
    cfg = tmp_path / "config.json"
    cfg.write_text(_json_cfg.dumps({"lang": "en", "date_format": "eu", "future": 42}),
                   encoding="utf-8")
    mm.DEFAULT_LANG, mm.DEFAULT_DATE_ORDER = "fr", "MDY"
    mm.save_config(cfg)
    written = _json_cfg.loads(cfg.read_text(encoding="utf-8"))
    assert written["future"] == 42
    assert written["lang"] == "fr" and written["date_format"] == "us"


def test_save_config_failure_is_silent(tmp_path, restore_config_globals):
    # Non-existent parent folder: best-effort, returns False without raising.
    mm.DEFAULT_LANG, mm.DEFAULT_DATE_ORDER = "en", "DMY"
    assert mm.save_config(tmp_path / "missing" / "config.json") is False


@pytest.mark.parametrize("order,expected", [
    ("DMY", "05/09/2021 21:15:00"),    # eu: day/month/year
    ("MDY", "09/05/2021 21:15:00"),    # us: month/day/year
])
def test_fmt_display_order_follows_locale(restore_config_globals, order, expected):
    # The on-screen date must match the order the user types in (config date_format).
    mm.DEFAULT_DATE_ORDER = order
    assert mm.fmt("2021:09:05 21:15:00") == expected


def test_fmt_display_order_date_only(restore_config_globals):
    # A date without a time is localised the same way (no invented time appended).
    mm.DEFAULT_DATE_ORDER = "MDY"
    assert mm.fmt("2021:09:05") == "09/05/2021"
    mm.DEFAULT_DATE_ORDER = "DMY"
    assert mm.fmt("2021:09:05") == "05/09/2021"


# ============================================================
#  Section 0ter — UI localisation (full FR/EN)
# ============================================================

def test_ui_table_has_both_languages():
    # Every interface string must exist in both languages (no half-translated key).
    for key, entry in mm._UI.items():
        assert "en" in entry and entry["en"], f"{key}: missing en"
        assert "fr" in entry and entry["fr"], f"{key}: missing fr"


def test_ui_placeholders_match_across_languages():
    # A "{name}" in one language must appear in the other, else .format() would
    # KeyError (or silently drop a value) when the language switches.
    import re as _re
    for key, entry in mm._UI.items():
        ph = {lang: set(_re.findall(r"{(\w+)}", txt)) for lang, txt in entry.items()}
        assert ph["en"] == ph["fr"], f"{key}: placeholders differ {ph}"


def test_tr_switches_with_language(restore_config_globals):
    mm.DEFAULT_LANG = "en"
    assert mm.tr("write_failed") == "Write failed."
    mm.DEFAULT_LANG = "fr"
    assert mm.tr("write_failed") == "Échec de l'écriture."


def test_tr_formats_placeholders(restore_config_globals):
    mm.DEFAULT_LANG = "fr"
    assert "3" in mm.tr("changes_made", n=3)
    assert mm.tr("field_not_found", focus="Titre").endswith("Titre")


def test_tr_unknown_key_returns_key(restore_config_globals):
    mm.DEFAULT_LANG = "fr"
    assert mm.tr("does_not_exist") == "does_not_exist"


def test_help_hint_advertises_aide_only_in_french(restore_config_globals):
    mm.DEFAULT_LANG = "en"
    assert "aide" not in mm.tr("help_hint")
    assert "help" in mm.tr("help_hint")
    mm.DEFAULT_LANG = "fr"
    assert "aide" in mm.tr("help_hint")            # francophones get a second command
    assert "help" in mm.tr("help_hint")            # but "help" keeps working


def test_aide_is_a_reserved_alias():
    # No displayed field alias may collide with the "aide" command.
    assert "aide" in mm._RESERVED_ALIASES
    assert "help" in mm._RESERVED_ALIASES


def test_header_fr_covers_every_theme_and_media_label():
    # Each themed header shown in the unified view must have a French translation,
    # including the media-type block labels (Image / Video / Audio / Technical).
    for theme in mm.THEME_ORDER:
        if theme != "__media__":
            assert theme in mm.HEADER_FR, f"theme {theme} not localised"
    for label in ("Image", "Video", "Audio", "Technical"):
        assert label in mm.HEADER_FR, f"media label {label} not localised"


def test_themed_layout_headers_stay_english_internally():
    # The internal theme KEYS must remain English (tests, routing) — only the
    # on-screen render localises them. Guards against accidental coupling.
    layout = mm.themed_layout(["FileSize", "Artist", "Title"], {"MIMEType": "image/png"})
    headers = [lbl for kind, lbl in layout if kind == "H"]
    assert "People & rights" in headers            # canonical English key, not "Personnes & droits"


# ============================================================
#  Section 1 — Date parsing (pure, no file)
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ("2024",                "2024:01:01 00:00:00"),
    ("2024/12",             "2024:12:01 00:00:00"),
    ("12/2024",             "2024:12:01 00:00:00"),
    ("25/12/2024",          "2024:12:25 00:00:00"),
    ("25/12/2024 14:00",    "2024:12:25 14:00:00"),
    ("25-12-2024 14h00",    "2024:12:25 14:00:00"),
    ("25/12/2024 14:00:30", "2024:12:25 14:00:30"),
    ("25/12/2024 14h00m30s", "2024:12:25 14:00:30"),   # a time as it is spoken: h, m and s
    ("25/12/2024 à 14h30",  "2024:12:25 14:30:00"),
    ("25-12-2024",          "2024:12:25 00:00:00"),   # DD-MM-YYYY alone: -YYYY ≠ timezone
    ("2024-12-25 14:30:00+01:00", "2024:12:25 14:30:00"),   # positive timezone stripped
    ("2024-12-25 14:30:00-05:00", "2024:12:25 14:30:00"),   # negative US timezone stripped
    ("2024-12-25T14:00:00", "2024:12:25 14:00:00"),   # ISO "T": the very shape the
                                                      # non-exiftool engines display
    ("2024/12/25",          "2024:12:25 00:00:00"),
    ("20241225",            "2024:12:25 00:00:00"),
    ("20122024",            "2024:12:20 00:00:00"),   # day 20 in DDMMYYYY
    ("19122024",            "2024:12:19 00:00:00"),   # day 19 in DDMMYYYY
    ("20241225140000",      "2024:12:25 14:00:00"),
    ("20122024140000",      "2024:12:20 14:00:00"),   # same on 14 digits
])
def test_parse_date_valid(raw, expected):
    assert mm.parse_date(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "hello",
    "2024/13/01",        # month 13
    "32/12/2024",        # day 32
    "30/02/2024",        # February 30 does not exist
    "31/04/2024",        # April 31 does not exist
    "99999999",
    "1/99999999999/2024", # oversized month: datetime raises OverflowError (≠ ValueError) — fuzz
    "3333333333333333s3333",  # same via the 's' → ':' separator (found by fuzzing)
])
def test_parse_date_invalid(raw):
    assert mm.parse_date(raw) is None


@pytest.mark.parametrize("raw,order,expected", [
    ("07/04/2024", "MDY", "2024:07:04 00:00:00"),   # July 4 US (month first)
    ("07/04/2024", "DMY", "2024:04:07 00:00:00"),   # April 7 EU (day first)
    ("12/25/2024", "MDY", "2024:12:25 00:00:00"),   # month first: valid
    ("12/25/2024", "DMY", None),                     # day first → month 25 invalid
    ("07042024",   "MDY", "2024:07:04 00:00:00"),   # compact 8 digits, month first
    ("07042024",   "DMY", "2024:04:07 00:00:00"),   # compact 8 digits, day first
    ("25/12/2024", "DMY", "2024:12:25 00:00:00"),   # EU default unchanged
])
def test_parse_date_order(raw, order, expected):
    assert mm.parse_date(raw, order=order) == expected


def test_to_exif_passes_non_dates_through():
    # A non-date tag returns the value as-is; a date tag forces parsing.
    assert mm.to_exif("Hello world", "Title") == "Hello world"
    assert mm.to_exif("25/12/2024", "DateTimeOriginal") == "2024:12:25 00:00:00"
    assert mm.to_exif("not a date", "DateTimeOriginal") is None


# ============================================================
#  Section 2 — Shifts and per-engine reformatting (pure)
# ============================================================

@pytest.mark.parametrize("s,valid", [
    ("+2h", True), ("-1d", True), ("+1d2h", True), ("-90m", True), ("+30s", True),
    ("2h", False),       # missing sign
    ("+1y", False),      # years not handled (timedelta does not represent them)
    ("", False), ("abc", False),
])
def test_parse_offset_validity(s, valid):
    assert (mm.parse_offset(s) is not None) is valid


def test_parse_offset_values():
    import datetime as dt
    assert mm.parse_offset("+2h") == dt.timedelta(hours=2)
    assert mm.parse_offset("-1d") == dt.timedelta(days=-1)
    assert mm.parse_offset("+1d2h") == dt.timedelta(days=1, hours=2)


def test_format_date_engines():
    # exiftool wants colons; the others want ISO 8601.
    assert mm.format_date("2024:12:25 14:30:00", "exiftool") == "2024:12:25 14:30:00"
    assert mm.format_date("2024:12:25 00:00:00", "mutagen") == "2024-12-25"
    assert mm.format_date("2024:12:25 14:30:00", "mutagen") == "2024-12-25T14:30:00"
    assert mm.format_date("2024:12:25 14:30:00", "ffmpeg") == "2024-12-25T14:30:00"


def test_format_date_ooxml_no_false_utc():
    # midnight -> date only (no fake time); a time -> local ISO WITHOUT "Z".
    assert mm.format_date("2024:12:25 00:00:00", "ooxml") == "2024-12-25"
    out = mm.format_date("2024:12:25 14:30:00", "ooxml")
    assert out == "2024-12-25T14:30:00"
    assert not out.endswith("Z")


def test_parse_stored_dt():
    import datetime as dt
    assert mm.parse_stored_dt("2024:12:25 14:30:00") == dt.datetime(2024, 12, 25, 14, 30, 0)
    assert mm.parse_stored_dt("2024-12-25T14:30:00") == dt.datetime(2024, 12, 25, 14, 30, 0)
    assert mm.parse_stored_dt("2024-12-25") == dt.datetime(2024, 12, 25, 0, 0, 0)
    assert mm.parse_stored_dt("not a date") is None


def test_parse_stored_dt_accepts_subseconds():
    # A date with a fractional second must NO LONGER be dropped (otherwise the shift
    # "dates +2h" would silently skip it). parse_stored_dt parses it, fraction included.
    import datetime as dt
    assert mm.parse_stored_dt("2024:01:01 12:00:00.123456") == dt.datetime(2024, 1, 1, 12, 0, 0, 123456)
    assert mm.parse_stored_dt("2024-01-01T12:00:00.5") is not None


def test_parse_offset_rejects_only_ambiguous_month():
    # Only "M" is ambiguous (month vs minute): we reject it. The other UNAMBIGUOUS
    # uppercase units stay accepted (no reason to break "+2H", "+3D", "+10S").
    import datetime as dt
    assert mm.parse_offset("+1M") is None                       # month: ambiguous → rejected
    assert mm.parse_offset("+1m") == dt.timedelta(minutes=1)    # lowercase = minute, ok
    assert mm.parse_offset("+2H") == dt.timedelta(hours=2)      # unambiguous → accepted
    assert mm.parse_offset("+3D") == dt.timedelta(days=3)
    assert mm.parse_offset("+10S") == dt.timedelta(seconds=10)


# ── the relative shift PRESERVES timezone, sub-seconds and granularity ──
def test_shift_preserves_timezone():
    import datetime as dt
    off = dt.timedelta(hours=2)
    assert mm._shift_stored_date("2024:01:01 12:00:00+02:00", off) == "2024:01:01 14:00:00+02:00"
    assert mm._shift_stored_date("2024:01:01 10:30:00Z", off) == "2024:01:01 12:30:00Z"


def test_shift_preserves_subseconds():
    import datetime as dt
    off = dt.timedelta(hours=2)
    assert mm._shift_stored_date("2024:01:01 12:00:00.123456", off) == "2024:01:01 14:00:00.123456"
    assert mm._shift_stored_date("2024:01:01 12:00:00.50+01:00", off) == "2024:01:01 14:00:00.50+01:00"


def test_shift_preserves_date_only_granularity():
    # GPSDateStamp is a date ONLY: a sub-day shift must not fabricate a time, and a
    # shift of days stays a date only.
    import datetime as dt
    assert mm._shift_stored_date("2024:01:01", dt.timedelta(hours=2)) == "2024:01:01"
    assert mm._shift_stored_date("2024:01:01", dt.timedelta(days=1)) == "2024:01:02"
    assert mm._shift_stored_date("2024-01-01", dt.timedelta(days=1)) == "2024-01-02"


def test_shift_preserves_year_only_granularity():
    # A year alone must not become an invented full timestamp.
    import datetime as dt
    assert mm._shift_stored_date("2024", dt.timedelta(hours=2)) == "2024"


def test_shift_preserves_iso_style():
    import datetime as dt
    assert mm._shift_stored_date("2024-01-01T12:00:00+02:00", dt.timedelta(hours=2)) == "2024-01-01T14:00:00+02:00"


def test_shift_returns_none_on_garbage():
    import datetime as dt
    assert mm._shift_stored_date("not a date", dt.timedelta(hours=2)) is None


def test_dates_offset_preserves_tz_end_to_end_ooxml(tmp_path):
    # Integration: "dates +2h" on a .docx whose date carries a "Z" keeps the "Z"
    # (and thus no longer distorts the instant). NB: when exiftool is present, read()
    # also merges the file's system dates (including FileAccessDate); cmd_dates must
    # not choke on it — cf. test_dates_skips_volatile_file_access_date for that point.
    p = make_docx(tmp_path / "doc.docx")
    assert mm.ooxml_write(p, "CreateDate", "2020-01-01T00:00:00Z") is True
    touched, errors, present = mm.cmd_dates(p, "+2h")
    assert errors == 0 and touched >= 1
    assert mm.read(p)["CreateDate"] == "2020-01-01T02:00:00Z"


def test_dates_skips_volatile_file_access_date(monkeypatch):
    # Regression: FileAccessDate (atime) is volatile — exiftool does not set it durably
    # (fails on Linux/CI). A bulk "dates" operation must therefore NEVER target it:
    # neither try it, nor count it as an error. We simulate exiftool present (read merges
    # the atime) + a write that fails on FileAccessDate, without depending on a real exiftool.
    p = Path("doc.docx")
    fixed = {
        "CreateDate": "2020:01:01 00:00:00",    # "content" date: shiftable
        "FileModifyDate": "2020:01:01 00:00:00",  # mtime: shiftable
        "FileAccessDate": "2020:01:01 00:00:00",  # atime: NOT to be targeted
    }
    monkeypatch.setattr(mm, "read", lambda path, raw=False: dict(fixed))
    monkeypatch.setattr(mm, "writable", lambda path: set(fixed) | set(mm.FILE_BASE_TAGS))

    attempted = []
    def fake_write(path, tag, value):
        attempted.append(tag)
        return tag != "FileAccessDate"          # exiftool refuses the atime
    monkeypatch.setattr(mm, "write", fake_write)

    # relative shift
    touched, errors, present = mm.cmd_dates(p, "+2h")
    assert errors == 0 and touched >= 1
    assert "FileAccessDate" not in attempted
    assert "FileModifyDate" in attempted

    # absolute date: same safeguard
    attempted.clear()
    touched, errors, present = mm.cmd_dates(p, "2024")
    assert errors == 0 and touched >= 1
    assert "FileAccessDate" not in attempted


# ============================================================
#  Section 3 — Name safeguards + routing (pure)
# ============================================================

@pytest.mark.parametrize("name,ok", [
    ("photo.jpg", True),
    ("no extension", True),
    ("a/b.jpg", False),       # path separator
    ("a\\b.jpg", False),      # Windows separator
    ("ev%il.png", False),     # "%" interpreted by exiftool
    ("", False),
    ("   ", False),
    ("...", False),
])
def test_valid_new_name(name, ok):
    assert mm._valid_new_name(name) is ok


@pytest.mark.parametrize("name,eng", [
    ("a.jpg", "exiftool"), ("a.png", "exiftool"), ("a.pdf", "exiftool"),
    ("a.mp3", "mutagen"), ("a.flac", "mutagen"),
    ("a.mkv", "ffmpeg"), ("a.avi", "ffmpeg"),
    ("a.docx", "ooxml"), ("a.xlsx", "ooxml"),
    ("a.odt", "odf"), ("a.epub", "epub"),
])
def test_engine_for(name, eng):
    assert mm.engine_for(Path(name)) == eng


@pytest.mark.parametrize("mime,cat", [
    ("image/jpeg", "exiftool_image"),
    ("audio/mpeg", "exiftool_audio"),
    ("video/mp4", "exiftool_video"),
    ("application/pdf", "exiftool_pdf"),
    ("text/plain", "exiftool_other"),
])
def test_exiftool_category(mime, cat):
    assert mm.exiftool_category({"MIMEType": mime}) == cat


# ── Fallback to external data when ffmpeg/mutagen is missing ──

def test_engine_available_detects_missing_ffmpeg(monkeypatch):
    monkeypatch.setattr(mm.shutil, "which", lambda name: None)
    assert mm.engine_available("ffmpeg") is False
    monkeypatch.setattr(mm.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    assert mm.engine_available("ffmpeg") is True
    assert mm.engine_available("exiftool") is True       # always assumed present


def test_read_falls_back_to_external_when_engine_missing(monkeypatch):
    # ffmpeg unreadable/absent -> read() must NOT return None, but the System tags.
    system = {"FileName": "clip.mkv", "FileSize": "4.2 MB", "FileType": "MKV"}
    monkeypatch.setitem(mm.ENGINES, "ffmpeg",
                        (lambda p, all_tags=False: None, *mm.ENGINES["ffmpeg"][1:]))
    # the fallback goes through et_read_lenient (tolerates a non-zero exiftool exit on an unknown type)
    monkeypatch.setattr(mm, "et_read_lenient", lambda p, all_tags=False: dict(system))
    data = mm.read(Path("clip.mkv"))
    assert data is not None
    assert data["FileSize"] == "4.2 MB"


def test_read_merges_filesize_for_media(monkeypatch):
    # ffmpeg present: embedded tags kept AND external size/type merged.
    monkeypatch.setitem(mm.ENGINES, "ffmpeg",
                        (lambda p, all_tags=False: {"title": "Clip"}, *mm.ENGINES["ffmpeg"][1:]))
    monkeypatch.setattr(mm, "et_read",
                        lambda p, all_tags=False: {"FileSize": "4.2 MB", "FileType": "MKV"})
    data = mm.read(Path("clip.mkv"))
    assert data["title"] == "Clip"
    assert data["FileSize"] == "4.2 MB"     # external merged (fixes the FileSize omission)


# --- install hints follow the OS metmux runs on ---

def test_install_hints_match_os(monkeypatch):
    # The preflight must not suggest "brew install" to a Windows or Linux user.
    monkeypatch.setattr(mm.platform, "system", lambda: "Windows")
    assert "winget" in mm._install_hint("exiftool")
    monkeypatch.setattr(mm.platform, "system", lambda: "Linux")
    assert mm._install_hint("mutagen").startswith("sudo apt")
    monkeypatch.setattr(mm.platform, "system", lambda: "Darwin")
    assert mm._install_hint("ffmpeg") == "brew install ffmpeg"
    monkeypatch.setattr(mm.platform, "system", lambda: "FreeBSD")   # unknown OS: apt default
    assert mm._install_hint("exiftool") == "sudo apt install libimage-exiftool-perl"


def test_missing_dependencies_names_tools_with_os_hint(monkeypatch):
    # SPEC §3: with exiftool missing, the preflight names it WITH an install command.
    monkeypatch.setattr(mm.platform, "system", lambda: "Windows")
    monkeypatch.setattr(mm.shutil, "which", lambda name: None)
    monkeypatch.setattr(mm, "_EXIFTOOL_FALLBACK_PATHS", ())   # no Homebrew binary on the test host
    missing = dict(mm._missing_dependencies([Path("a.mkv"), Path("b.jpg")]))
    assert "winget" in missing["exiftool"]
    assert "winget" in missing["ffmpeg"]


# ============================================================
#  Section 4 — exiftool primitives on a real image (integration)
# ============================================================

@needs_exiftool
def test_roundtrip_title_png(tmp_path):
    p = write_png(tmp_path)
    assert mm.write(p, "Title", "ZZTEST") is True
    data = mm.et_read(p) or {}
    assert "ZZTEST" in str(data.get("Title"))


@needs_exiftool
def test_date_pipeline_png(tmp_path):
    # Full chain: human input -> parse -> write -> re-read.
    p = write_png(tmp_path)
    canon = mm.to_exif("01/05/2019 12:00", "DateTimeOriginal")
    assert canon == "2019:05:01 12:00:00"
    assert mm.write(p, "DateTimeOriginal", canon) is True
    data = mm.et_read(p) or {}
    assert "2019:05:01 12:00:00" in str(data.get("DateTimeOriginal"))


@needs_exiftool
def test_erase_title_png(tmp_path):
    p = write_png(tmp_path)
    mm.write(p, "Title", "ZZTEST")
    assert mm.write(p, "Title", "") is True
    data = mm.et_read(p) or {}
    assert "ZZTEST" not in str(data.get("Title"))


@needs_exiftool
def test_wipe_changes_file_and_clears_tags(tmp_path):
    p = write_png(tmp_path)
    mm.write(p, "Title", "ZZTEST")
    mm.write(p, "Artist", "ZZARTIST")
    before = sha256(p)
    assert mm.wipe(p) is True
    assert sha256(p) != before
    data = mm.et_read(p) or {}
    assert "ZZTEST" not in str(data.get("Title"))
    assert "ZZARTIST" not in str(data.get("Artist"))


@needs_exiftool
def test_apply_filename_ok(tmp_path):
    p = write_png(tmp_path, "orig.png")
    new, err = mm.apply_filename(p, "renamed.png")
    assert err is None
    assert new is not None and new.name == "renamed.png" and new.exists()
    assert not p.exists()


@needs_exiftool
def test_apply_filename_blocks_percent(tmp_path):
    # a name containing "%" is refused, the original does not move,
    # no file with a corrupted name appears.
    p = write_png(tmp_path, "orig.png")
    before = sha256(p)
    new, err = mm.apply_filename(p, "ev%il.png")
    assert new is None and err is not None
    assert {f.name for f in tmp_path.iterdir()} == {"orig.png"}
    assert sha256(p) == before


@needs_exiftool
def test_apply_filename_no_overwrite(tmp_path):
    # We must never overwrite an existing file during a rename.
    a = write_png(tmp_path, "a.png")
    write_png(tmp_path, "b.png")
    new, err = mm.apply_filename(a, "b.png")
    assert new is None and err is not None
    assert a.exists()


def test_apply_filename_refuses_distinct_case_variant(tmp_path, monkeypatch):
    """"Photo.png" as a genuinely distinct file must block the rename "photo.png" → "Photo.png"
       (real collision, different inode). The samefile guard only excuses the SAME entry, never
       a distinct same-name-different-case sibling. We force samefile False (distinct inode) so
       the case matters on any FS, not just a case-sensitive one; a case-INSENSITIVE host would
       otherwise fold the two writes into one entry (samefile True) and let the rename through.
       Runs before et_write, so no exiftool needed."""
    p = write_png(tmp_path, "photo.png")
    write_png(tmp_path, "Photo.png")                  # a real file at that name
    monkeypatch.setattr(mm.os.path, "samefile", lambda a, b: False)   # a genuinely distinct inode
    new, err = mm.apply_filename(p, "Photo.png")
    assert new is None and err == mm.tr("name_exists")   # refused: a real other file
    assert p.exists()


def test_apply_filename_allows_case_only_rename_same_file(tmp_path, monkeypatch):
    """REGRESSION (case-insensitive FS: APFS/macOS, NTFS/Windows): a case-only rename
       ("photo.png" → "Photo.png") must NOT be refused as a collision — target and source are
       the SAME directory entry. We simulate the folded lookup (a real target file + samefile
       True) and check the rename reaches et_write instead of the collision guard."""
    p = write_png(tmp_path, "photo.png")
    write_png(tmp_path, "Photo.png")                  # stands in for the case-folded same entry
    monkeypatch.setattr(mm.os.path, "samefile", lambda a, b: True)   # pretend same inode
    seen = {}
    def _fake_et_write(path, tag, value):
        seen["v"] = value
        return True
    monkeypatch.setattr(mm, "et_write", _fake_et_write)
    new, err = mm.apply_filename(p, "Photo.png")
    assert err is None and new is not None and new.name == "Photo.png"
    assert seen.get("v") == "Photo.png"               # reached the rename, not the collision guard


# ============================================================
#  External engines — integrity (exiftool scrub / et_run / mutagen)
# ============================================================

# --- _scrub no longer drops legitimate "0"s (only date sentinels) ---

def test_scrub_keeps_legit_zero_values():
    data = {"Title": "X", "Rating": 0, "TrackNumber": "0", "GPSAltitude": 0,
            "Compilation": False, "ExposureCompensation": 0.0,
            "CreateDate": "0000:00:00 00:00:00"}
    out = mm._scrub(dict(data))
    assert out["Rating"] == 0                      # rating 0 = legitimate
    assert out["TrackNumber"] == "0"               # track 0 = legitimate
    assert out["GPSAltitude"] == 0                 # sea level = legitimate
    assert out["Compilation"] is False            # boolean not confused with 0
    assert out["ExposureCompensation"] == 0.0     # zero float not confused
    assert "CreateDate" not in out                 # empty-date sentinel: removed
    assert out["Title"] == "X"


# --- exiftool success detection is not fooled by a warning ---

def test_et_ok_distinguishes_error_from_warning():
    # a benign warning containing the word "Error" must not fail a successful write
    assert mm._et_ok(0, "    1 image files updated\n",
                     "Warning: Error reading PreviewImage\n") is True
    # a real error (line starting with Error) does fail
    assert mm._et_ok(1, "", "Error: file not found\n") is False
    # "0 updated" without "unchanged" = failure (nothing written)
    assert mm._et_ok(0, "0 image files updated\n", "") is False
    # "0 updated" but already at the right value = success
    assert mm._et_ok(0, "0 image files updated\n1 image files unchanged\n", "") is True


# --- Minimal FLAC fixture (valid for mutagen), without ffmpeg ---

def _minimal_flac_bytes():
    streaminfo = bytearray(34)
    packed = (44100 << 44) | ((1 - 1) << 41) | ((16 - 1) << 36) | 0   # 44.1kHz mono 16 bits
    streaminfo[10:18] = packed.to_bytes(8, "big")
    header = bytes([0x80]) + (34).to_bytes(3, "big")                  # last block, type 0, len 34
    return b"fLaC" + header + bytes(streaminfo)


def make_flac(path):
    path.write_bytes(_minimal_flac_bytes())
    return path


# --- a name with a comma must NEVER be silently split ---

def test_mutagen_listfield_comma_name_not_split(tmp_path):
    # "Earth, Wind & Fire" entered into artist (a list field) must stay ONE single
    # value, never split into ["Earth", "Wind & Fire"] (invisible corruption: the
    # display would re-join them). Also holds for composer/albumartist…
    pytest.importorskip("mutagen")
    from mutagen.flac import FLAC
    p = make_flac(tmp_path / "a.flac")
    for band in ("Earth, Wind & Fire", "Crosby, Stills & Nash", "Tyler, the Creator"):
        assert mm.mg_write(p, "artist", band) is True
        assert FLAC(str(p))["artist"] == [band]
        assert mm.mg_read(p)["artist"] == band


def test_mutagen_multivalue_joins_for_display(tmp_path):
    # A truly multi-valued field is JOINED by ", " for display (read).
    pytest.importorskip("mutagen")
    from mutagen.flac import FLAC
    p = make_flac(tmp_path / "a.flac")
    f = FLAC(str(p)); f["artist"] = ["Artist A", "Artist B", "Artist C"]; f.save()
    assert mm.mg_read(p)["artist"] == "Artist A, Artist B, Artist C"


def test_mutagen_single_value_field_not_split(tmp_path):
    pytest.importorskip("mutagen")
    from mutagen.flac import FLAC
    p = make_flac(tmp_path / "a.flac")
    # a title (non-list field) containing "," must NOT be split
    assert mm.mg_write(p, "title", "Hello, World") is True
    assert FLAC(str(p))["title"] == ["Hello, World"]


# --- wiping a FLAC also removes the embedded cover art ---

def test_mutagen_wipe_removes_flac_picture(tmp_path):
    pytest.importorskip("mutagen")
    from mutagen.flac import FLAC, Picture
    p = make_flac(tmp_path / "a.flac")
    f = FLAC(str(p)); f["title"] = ["T"]
    pic = Picture(); pic.type = 3; pic.mime = "image/png"; pic.data = PNG_BYTES
    f.add_picture(pic); f.save()
    assert len(FLAC(str(p)).pictures) == 1
    assert mm.mg_wipe(p) is True
    g = FLAC(str(p))
    assert len(g.pictures) == 0
    assert list(g.keys()) == []


# --- the mutagen write is atomic (goes through a temporary) ---

def test_mutagen_write_is_atomic_no_leftover_tmp(tmp_path):
    pytest.importorskip("mutagen")
    from mutagen.flac import FLAC
    p = make_flac(tmp_path / "a.flac")
    assert mm.mg_write(p, "title", "Title") is True
    assert FLAC(str(p))["title"] == ["Title"]
    assert {f.name for f in tmp_path.iterdir()} == {"a.flac"}


# ============================================================
#  Form preservation: EML / playlists / JSON / renaming
# ============================================================

# --- EML — CRLF kept, long headers untouched, not folded/re-encoded ---

def test_eml_edit_preserves_crlf_and_untouched_headers(tmp_path):
    raw = (b"From: a@x.test\r\n"
           b"To: b@y.test\r\n"
           b"Subject: old\r\n"
           b"DKIM-Signature: v=1; a=rsa-sha256; d=example.org; s=sel; h=from:subject; b="
           + b"Zm9vYmFy" * 8 + b";\r\n"
           b"References: <" + b"x" * 70 + b"@example.org>\r\n"
           b"MIME-Version: 1.0\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
           b"Body.\r\n")
    p = tmp_path / "m.eml"
    p.write_bytes(raw)
    assert mm.eml_write(p, "Subject", "new") is True
    out = p.read_bytes()
    assert out.replace(b"\r\n", b"").count(b"\n") == 0          # no bare LF: CRLF preserved
    assert b"=?utf-8?" not in out                               # no header re-encoded
    assert (b"b=" + b"Zm9vYmFy" * 8) in out                     # DKIM signature intact (1 line)
    assert (b"<" + b"x" * 70 + b"@example.org>") in out         # References intact
    import email
    assert email.message_from_bytes(out)["Subject"] == "new"


def test_eml_read_still_decodes_for_display(tmp_path):
    # Reading must stay legible (RFC2047 decoding), even though writing preserves the byte.
    raw = (b"From: a@x.test\r\nSubject: =?utf-8?b?Q2Fmw6k=?=\r\n\r\nx\r\n")  # "Café"
    p = tmp_path / "m.eml"
    p.write_bytes(raw)
    assert mm.eml_read(p).get("Subject") == "Café"


# --- M3U/CUE playlists — encoding, BOM and line endings preserved ---

def test_m3u_preserves_latin1_and_crlf(tmp_path):
    p = tmp_path / "pl.m3u"
    p.write_bytes("#EXTM3U\r\nmusic/Café.mp3\r\n".encode("latin-1"))
    assert mm.m3u_write(p, "Title", "My List") is True
    raw = p.read_bytes()
    assert b"\r\n" in raw and raw.replace(b"\r\n", b"").count(b"\n") == 0   # CRLF kept
    assert "Café".encode("latin-1") in raw                                  # path stays latin-1


def test_m3u8_preserves_bom(tmp_path):
    p = tmp_path / "pl.m3u8"
    p.write_bytes(b"\xef\xbb\xbf#EXTM3U\r\nx.mp3\r\n")
    assert mm.m3u_write(p, "Title", "T") is True
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                       # BOM preserved
    assert b"\r\n" in raw


def test_cue_preserves_crlf(tmp_path):
    p = tmp_path / "d.cue"
    p.write_bytes(b'TITLE "old"\r\nFILE "a.wav" WAVE\r\n  TRACK 01 AUDIO\r\n')
    assert mm.cue_write(p, "Title", "new") is True
    raw = p.read_bytes()
    assert b"\r\n" in raw and raw.replace(b"\r\n", b"").count(b"\n") == 0
    assert b'"a.wav"' in raw                                     # track/file preserved


# --- JSON — the original indentation is preserved (no massive diff) ---

def test_geojson_preserves_indentation(tmp_path):
    p = tmp_path / "m.geojson"
    p.write_text('{\n  "type": "FeatureCollection",\n  "features": []\n}\n', encoding="utf-8")
    assert mm.geojson_write(p, "Name", "x") is True
    txt = p.read_text(encoding="utf-8")
    assert "\n  " in txt                                         # 2-space indentation kept


def test_geojson_compact_stays_compact(tmp_path):
    p = tmp_path / "m.geojson"
    p.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    assert mm.geojson_write(p, "Name", "x") is True
    assert p.read_text(encoding="utf-8").count("\n") <= 1        # stays compact


# --- renaming — Windows reserved names refused ---

def test_valid_new_name_rejects_windows_reserved():
    for bad in ("CON", "nul", "PRN", "AUX", "COM1", "LPT9", "nul.txt", "CON.jpg"):
        assert mm._valid_new_name(bad) is False, bad
    for ok in ("normal.txt", "console.txt", "com.png", "lpt.pdf"):
        assert mm._valid_new_name(ok) is True, ok


# ============================================================
#  Section 5 — OOXML engine (.docx, pure Python, no tool)
# ============================================================

def test_ooxml_read(tmp_path):
    p = make_docx(tmp_path / "doc.docx")
    data = mm.ooxml_read(p)
    assert data.get("Title") == "MyTitle"
    assert data.get("Creator") == "MyAuthor"
    assert data.get("Company") == "MyCompany"


def test_ooxml_write_no_corruption(tmp_path):
    p = make_docx(tmp_path / "doc.docx")
    before_members = _zip_names(p)
    assert mm.ooxml_write(p, "Title", "New") is True
    assert mm.ooxml_read(p).get("Title") == "New"
    # All the original members are still present (zip not corrupted)…
    assert before_members <= _zip_names(p)
    # …and the document body was not touched.
    with zipfile.ZipFile(p) as z:
        assert b"hello" in z.read("word/document.xml")


def test_ooxml_wipe_is_complete_and_undoable(tmp_path):
    # wipe erases ALL metadata — core.xml AND app.xml (company, template…) —
    # and the undo (snapshot of the raw members) restores them bit-for-bit.
    import zipfile, xml.etree.ElementTree as ET
    p = make_docx(tmp_path / "doc.docx")
    before = mm.ooxml_read(p)
    assert before.get("Title") and before.get("Company") == "MyCompany"   # data present
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        after = mm.ooxml_read(p)
        assert all(not v for v in after.values())                       # no metadata left
        with zipfile.ZipFile(p) as z:                                   # container still valid
            ET.fromstring(z.read("docProps/core.xml"))
            ET.fromstring(z.read("docProps/app.xml"))
        assert mm._UNDO.undo_last() is True
        assert mm.ooxml_read(p) == before                               # everything restored
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


# ============================================================
#  Section 6 — EPUB engine (.epub, pure Python)
# ============================================================

def test_epub_read_write(tmp_path):
    p = make_epub(tmp_path / "book.epub")
    assert mm.epub_read(p).get("Title") == "TheTitle"
    assert mm.epub_write(p, "Title", "AnotherTitle") is True
    assert mm.epub_read(p).get("Title") == "AnotherTitle"


# ============================================================
#  Section 7 — Mutagen engine (.mp3 generated by ffmpeg)
# ============================================================

@needs_ffmpeg
def test_mutagen_roundtrip(tmp_path):
    pytest.importorskip("mutagen")                   # skips if the module is not installed
    p = make_mp3(tmp_path)
    assert p is not None, "mp3 generation failed"
    assert mm.mg_write(p, "artist", "ZZARTIST") is True
    assert "ZZARTIST" in str(mm.mg_read(p).get("artist"))
    assert mm.mg_wipe(p) is True


# ============================================================
#  Section 8 — FFmpeg engine (.mkv generated by ffmpeg)
# ============================================================

@needs_ffmpeg
def test_ffmpeg_roundtrip(tmp_path):
    p = make_mkv(tmp_path)
    assert p is not None, "mkv generation failed"
    assert mm.ff_write(p, "title", "ZZTITLE") is True
    data = mm.ff_read(p) or {}
    assert "ZZTITLE" in str(data.get("title"))


@needs_ffmpeg
def test_ffmpeg_wipe_clears_tags(tmp_path):
    # SPEC §5 promises the wipe proof PER ENGINE — this is the ffmpeg one: tags gone,
    # file still readable by its own engine (not bricked).
    p = make_mkv(tmp_path)
    assert p is not None, "mkv generation failed"
    assert mm.ff_write(p, "title", "ZZTITLE") is True
    assert mm.ff_wipe(p) is True
    data = mm.ff_read(p)
    assert data is not None
    assert "ZZTITLE" not in str(data)


@needs_ffmpeg
def test_ff_wipe_fails_cleanly_when_the_replace_raises(tmp_path, monkeypatch):
    # REGRESSION: ff_wipe kept _replace_keep_mode OUTSIDE its try (ff_write has it inside).
    # A failing final rename (OSError) escaped the engine uncaught and orphaned the temp.
    # It must return False, leave the original intact, and clean the temp up.
    p = make_mkv(tmp_path)
    assert p is not None, "mkv generation failed"
    before = p.read_bytes()
    monkeypatch.setattr(mm, "_replace_keep_mode",
                        lambda *a: (_ for _ in ()).throw(OSError("rename failed")))
    assert mm.ff_wipe(p) is False
    assert p.read_bytes() == before                       # original untouched
    assert not list(tmp_path.glob("*metmux_tmp*"))        # no orphaned temp left behind


# ============================================================
#  Section 9 — Comic Book engine (.cbz, pure Python, no tool)
# ============================================================

_COMICINFO_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<ComicInfo>'
    '<Title>{title}</Title><Series>{series}</Series><Number>3</Number>'
    '<Writer>{writer}</Writer><Penciller>John Roe</Penciller>'
    '<Publisher>{publisher}</Publisher><Genre>{genre}</Genre>'
    '<Summary>{summary}</Summary>'
    '<Year>2021</Year><Month>6</Month><Day>9</Day>'
    '<LanguageISO>fr</LanguageISO><PageCount>2</PageCount>'
    '</ComicInfo>')


def make_cbz(path, title="TheAwakening", series="Adventures", writer="JaneDoe",
             publisher="TestPublisher", genre="SF", summary="ASummary",
             with_comicinfo=True):
    """Builds a real .cbz: a ZIP of 1×1 PNG pages, with (or without)
       a ComicInfo.xml at the root."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        if with_comicinfo:
            z.writestr("ComicInfo.xml", _COMICINFO_XML.format(
                title=title, series=series, writer=writer,
                publisher=publisher, genre=genre, summary=summary))
        z.writestr("page001.png", PNG_BYTES)
        z.writestr("page002.png", PNG_BYTES)
    return path


def test_cbz_read_semantic_fields(tmp_path):
    p = make_cbz(tmp_path / "comic.cbz")
    data = mm.cbz_read(p)
    assert data.get("Title") == "TheAwakening"
    assert data.get("Series") == "Adventures"
    assert data.get("Writer") == "JaneDoe"
    assert data.get("Publisher") == "TestPublisher"
    assert data.get("Genre") == "SF"
    assert data.get("Description") == "ASummary"      # Summary -> Description
    assert data.get("Language") == "fr"               # LanguageISO -> Language
    # Year/Month/Day assembled into a canonical date (colons).
    assert data.get("Date") == "2021:06:09 00:00:00"


def test_cbz_roundtrip_title(tmp_path):
    p = make_cbz(tmp_path / "comic.cbz")
    assert mm.cbz_write(p, "Title", "New Title") is True
    assert mm.cbz_read(p).get("Title") == "New Title"


def test_cbz_roundtrip_date(tmp_path):
    # The written Date (canonical) is recomposed into Year/Month/Day then read back
    # identically.
    p = make_cbz(tmp_path / "comic.cbz")
    assert mm.cbz_write(p, "Date", "2019:05:01 00:00:00") is True
    assert mm.cbz_read(p).get("Date") == "2019:05:01 00:00:00"


def test_cbz_erase_field(tmp_path):
    p = make_cbz(tmp_path / "comic.cbz")
    assert mm.cbz_write(p, "Genre", "") is True
    assert "Genre" not in mm.cbz_read(p)


def test_cbz_write_preserves_pages(tmp_path):
    # The page images and the zip structure stay intact after a write.
    p = make_cbz(tmp_path / "comic.cbz")
    before = _zip_names(p)
    assert mm.cbz_write(p, "Title", "X") is True
    names = _zip_names(p)
    assert before <= names
    with zipfile.ZipFile(p) as z:
        assert z.read("page001.png") == PNG_BYTES
        assert z.read("page002.png") == PNG_BYTES


def test_cbz_create_comicinfo_when_missing(tmp_path):
    # A .cbz without ComicInfo.xml: read returns {} (valid zip), and write creates
    # the ComicInfo.xml member without touching the pages.
    p = make_cbz(tmp_path / "nometa.cbz", with_comicinfo=False)
    assert mm.cbz_read(p) == {}
    assert mm.cbz_write(p, "Title", "Created") is True
    assert mm.cbz_read(p).get("Title") == "Created"
    names = _zip_names(p)
    assert "ComicInfo.xml" in names and "page001.png" in names


def test_cbz_wipe_clears_and_keeps_pages(tmp_path):
    p = make_cbz(tmp_path / "comic.cbz")
    assert mm.cbz_wipe(p) is True
    data = mm.cbz_read(p)
    assert data.get("Title") in (None, "")
    assert data.get("Series") in (None, "")
    assert data.get("Date") in (None, "")
    with zipfile.ZipFile(p) as z:                     # pages survive the wipe
        assert "page001.png" in set(z.namelist())


def test_cbz_garbage_fails_cleanly(tmp_path):
    # Non-zip / truncated / empty file: never a crash, clean failure.
    bad = tmp_path / "bad.cbz"
    bad.write_bytes(b"not a zip at all")
    assert mm.cbz_read(bad) is None
    assert mm.cbz_write(bad, "Title", "x") is False
    assert mm.cbz_wipe(bad) is False
    empty = tmp_path / "empty.cbz"
    empty.write_bytes(b"")
    assert mm.cbz_read(empty) is None


def test_cbz_year_overflow_fails_cleanly(tmp_path):
    # REGRESSION: a <Year> too big for datetime's C int raised OverflowError (NOT a
    # ValueError subclass) and killed the session on a mere read. The other fields
    # still read; an 11-digit "year" is not a date at all, so no Date is invented.
    p = tmp_path / "big.cbz"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("ComicInfo.xml",
                   "<ComicInfo><Title>T</Title><Year>99999999999</Year></ComicInfo>")
        z.writestr("page1.jpg", b"x")
    data = mm.cbz_read(p)
    assert data is not None and data.get("Title") == "T"
    assert "Date" not in data


# ============================================================
#  Section 10 — Playlist engine (.m3u / .m3u8, pure text, no tool)
# ============================================================

def make_m3u(path, header=True, with_playlist=True, title="My awesome playlist"):
    """Builds a real m3u/m3u8 playlist: optional #EXTM3U header, optional #PLAYLIST:
       directive, 2 #EXTINF entries (file + URL) plus an empty line and a bare media,
       to exercise preservation."""
    lines = []
    if header:
        lines.append("#EXTM3U")
    if with_playlist:
        lines.append(f"#PLAYLIST:{title}")
    lines += [
        "#EXTINF:123,Artist A - Title A",
        "media/a.mp3",
        "#EXTINF:200,Artist B - Title B",
        "http://example.com/b.mp3",
        "",
        "media/c.mp3",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_m3u_engine_routing():
    assert mm.engine_for(Path("x.m3u")) == "m3u"
    assert mm.engine_for(Path("x.m3u8")) == "m3u"


def test_m3u_read_semantic_fields(tmp_path):
    p = make_m3u(tmp_path / "pl.m3u8")
    data = mm.m3u_read(p)
    assert data.get("Title") == "My awesome playlist"
    assert data.get("TrackCount") == "2"


def test_m3u_read_counts_media_without_extinf(tmp_path):
    p = tmp_path / "plain.m3u"
    p.write_text("a.mp3\nb.mp3\nc.mp3\n", encoding="utf-8")
    data = mm.m3u_read(p)
    assert "Title" not in data
    assert data.get("TrackCount") == "3"


def test_m3u_roundtrip_title(tmp_path):
    p = make_m3u(tmp_path / "pl.m3u8")
    assert mm.m3u_write(p, "Title", "New Name") is True
    assert mm.m3u_read(p).get("Title") == "New Name"
    txt = p.read_text(encoding="utf-8")
    lines = txt.splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#PLAYLIST:New Name"
    assert txt.count("#PLAYLIST:") == 1
    for media in ("media/a.mp3", "http://example.com/b.mp3", "media/c.mp3"):
        assert media in txt


def test_m3u_erase_title(tmp_path):
    p = make_m3u(tmp_path / "pl.m3u")
    assert mm.m3u_write(p, "Title", "") is True
    assert "Title" not in mm.m3u_read(p)
    assert "#PLAYLIST:" not in p.read_text(encoding="utf-8")
    assert "media/a.mp3" in p.read_text(encoding="utf-8")  # media intact


def test_m3u_write_inserts_header_when_missing(tmp_path):
    p = make_m3u(tmp_path / "noheader.m3u", header=False, with_playlist=False)
    assert "Title" not in mm.m3u_read(p)
    assert mm.m3u_write(p, "Title", "X") is True
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1] == "#PLAYLIST:X"
    assert mm.m3u_read(p).get("Title") == "X"


def test_m3u_wipe_clears_title_and_extinf_titles(tmp_path):
    p = make_m3u(tmp_path / "pl.m3u8")
    assert mm.m3u_wipe(p) is True
    txt = p.read_text(encoding="utf-8")
    assert "#PLAYLIST:" not in txt
    assert "Title A" not in txt and "Title B" not in txt
    assert "#EXTINF:123," in txt
    for media in ("media/a.mp3", "http://example.com/b.mp3", "media/c.mp3"):
        assert media in txt
    data = mm.m3u_read(p)
    assert "Title" not in data
    assert data.get("TrackCount") == "2"                  # still 2 entries


def test_m3u_wipe_keeps_extinf_attributes_with_commas(tmp_path):
    # REGRESSION: extended IPTV #EXTINF attributes hold their own commas
    # (group-title="News, Sports"). A cut on the FIRST comma landed inside the attribute,
    # dropping tvg-logo/group-title and leaving an unbalanced quote. The title after the
    # LAST-comma-outside-quotes is what wipe removes, the attributes stay.
    p = tmp_path / "iptv.m3u8"
    p.write_text('#EXTM3U\n'
                 '#EXTINF:-1 tvg-id="fr1" tvg-logo="http://x/l.png" '
                 'group-title="News, Sports",France 24\n'
                 'http://server/fr24.m3u8\n', encoding="utf-8")
    assert mm.m3u_wipe(p) is True
    extinf = [l for l in p.read_text(encoding="utf-8").splitlines()
              if l.startswith("#EXTINF")][0]
    assert extinf.count('"') % 2 == 0                     # quotes balanced
    assert 'group-title="News, Sports"' in extinf         # attribute (with its comma) intact
    assert 'tvg-logo="http://x/l.png"' in extinf
    assert "France 24" not in extinf                      # the human title is gone
    assert extinf.endswith(",")


def test_m3u_read_handles_garbage_and_empty(tmp_path):
    empty = tmp_path / "empty.m3u"
    empty.write_text("", encoding="utf-8")
    assert mm.m3u_read(empty).get("TrackCount") == "0"
    junk = tmp_path / "junk.m3u"
    junk.write_bytes(b"\xff\xfe\x00\x01garbage\x80")       # binary: latin-1 fallback, no crash
    assert mm.m3u_read(junk) is not None


def test_m3u_writable(tmp_path):
    p = make_m3u(tmp_path / "pl.m3u")
    assert mm.m3u_writable(p) == {"Title"}
    assert mm.m3u_write(p, "TrackCount", "9") is False


def test_m3u_write_keeps_attributed_header(tmp_path):
    # REGRESSION: an IPTV header carrying attributes ("#EXTM3U url-tvg=…") was not
    # recognised as THE header — a second bare #EXTM3U was inserted above it, and
    # players that only read line 1 lost the tvg attributes.
    p = tmp_path / "tv.m3u"
    p.write_text('#EXTM3U url-tvg="http://g.tv/xml"\nhttp://host/ch1\n', encoding="utf-8")
    assert mm.m3u_write(p, "Title", "TV") is True
    lines = p.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '#EXTM3U url-tvg="http://g.tv/xml"'
    assert lines[1] == "#PLAYLIST:TV"
    assert sum(1 for l in lines if l.upper().startswith("#EXTM3U")) == 1


# ============================================================
#  Section 11 — Property List engine (.plist/.webloc/.mobileconfig)
# ============================================================
import plistlib


def make_plist(path, mapping):
    with open(path, "wb") as f:
        plistlib.dump(mapping, f)
    return path


def test_plist_read_generic(tmp_path):
    p = make_plist(tmp_path / "Info.plist", {
        "CFBundleName": "MyApp", "CFBundleIdentifier": "com.test.app",
        "CFBundleShortVersionString": "1.2.3", "NSHumanReadableCopyright": "© Me"})
    data = mm.plist_read(p)
    assert data.get("CFBundleName") == "MyApp"
    assert data.get("CFBundleIdentifier") == "com.test.app"   # read (read-only)
    assert data.get("NSHumanReadableCopyright") == "© Me"


def test_plist_webloc_roundtrip(tmp_path):
    p = make_plist(tmp_path / "link.webloc", {"URL": "https://example.com"})
    assert mm.plist_read(p).get("URL") == "https://example.com"
    assert mm.plist_write(p, "URL", "https://other.com") is True
    assert mm.plist_read(p).get("URL") == "https://other.com"


def test_plist_binary_format_preserved(tmp_path):
    p = tmp_path / "bin.plist"
    with open(p, "wb") as f:
        plistlib.dump({"Title": "Old"}, f, fmt=plistlib.FMT_BINARY)
    assert mm.plist_write(p, "Title", "New") is True
    assert p.read_bytes()[:8] == b"bplist00"               # still binary
    assert mm.plist_read(p).get("Title") == "New"


def test_plist_structural_keys_not_writable(tmp_path):
    p = make_plist(tmp_path / "x.mobileconfig",
                   {"PayloadType": "Configuration", "PayloadDisplayName": "Profile"})
    assert mm.plist_write(p, "PayloadType", "Hacked") is False
    assert mm.plist_read(p).get("PayloadType") == "Configuration"
    assert mm.plist_write(p, "PayloadDisplayName", "Renamed") is True
    assert mm.plist_read(p).get("PayloadDisplayName") == "Renamed"


def test_plist_wipe_keeps_structure(tmp_path):
    p = make_plist(tmp_path / "x.mobileconfig",
                   {"PayloadType": "Configuration", "PayloadDisplayName": "Profile",
                    "PayloadOrganization": "ACME"})
    assert mm.plist_wipe(p) is True
    data = mm.plist_read(p)
    assert data.get("PayloadDisplayName") in (None, "")
    assert data.get("PayloadType") == "Configuration"


def test_plist_garbage_fails_cleanly(tmp_path):
    bad = tmp_path / "bad.plist"
    bad.write_bytes(b"not a plist")
    assert mm.plist_read(bad) is None
    assert mm.plist_write(bad, "URL", "x") is False


# ============================================================
#  Section 12 — E-mail engine (.eml)
# ============================================================
import email.message


def make_eml(path, subject="Hello", frm="a@x.test", to="b@y.test", body="Message body"):
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    msg["Date"] = "Mon, 01 Jan 2024 12:00:00 +0000"
    msg.set_content(body)
    path.write_bytes(msg.as_bytes())
    return path


def test_eml_read(tmp_path):
    p = make_eml(tmp_path / "m.eml")
    data = mm.eml_read(p)
    assert data.get("Subject") == "Hello"
    assert data.get("From") == "a@x.test"
    assert data.get("To") == "b@y.test"


def test_eml_roundtrip_subject_keeps_body(tmp_path):
    p = make_eml(tmp_path / "m.eml", body="UNIQUE-BODY-123")
    assert mm.eml_write(p, "Subject", "New subject") is True
    assert mm.eml_read(p).get("Subject") == "New subject"
    assert b"UNIQUE-BODY-123" in p.read_bytes()


def test_eml_erase_header(tmp_path):
    p = make_eml(tmp_path / "m.eml")
    assert mm.eml_write(p, "Subject", "") is True
    assert "Subject" not in mm.eml_read(p)


def test_eml_wipe_removes_identifying_headers_and_undoes(tmp_path):
    # wipe removes ALL identifying headers (IP, Message-ID, Return-Path, DKIM…),
    # keeps the structural ones (Content-Type) and the body; the undo restores byte for byte.
    p = tmp_path / "m.eml"
    p.write_bytes(
        b"From: alice@example.com\r\nSubject: Secret\r\n"
        b"Received: from evil.example [203.0.113.55]\r\nX-Originating-IP: [203.0.113.55]\r\n"
        b"Message-ID: <abc@evil.example>\r\nReturn-Path: <leaker@evil.example>\r\n"
        b"DKIM-Signature: v=1; a=rsa-sha256; bh=xxx\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nBody.\r\n")
    original = p.read_bytes()
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        after = p.read_bytes().decode()
        for leak in ("alice@", "203.0.113.55", "abc@evil", "leaker@evil", "DKIM", "Subject:"):
            assert leak not in after                      # no identifying metadata left
        assert "Content-Type" in after and "Body." in after   # structure + body preserved
        assert mm._UNDO.undo_last() is True
        assert p.read_bytes() == original
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_eml_garbage_does_not_crash(tmp_path):
    bad = tmp_path / "bad.eml"
    bad.write_bytes(b"\xff\xfe not an email \x00")
    assert mm.eml_read(bad) is not None


@pytest.mark.parametrize("raw", [
    # Malformed structured headers (From/To/Date): policy.default parses them LAZILY
    # on read, outside the try of _eml_load. These 4 entries (found by fuzzing) raised
    # AttributeError / IndexError / TypeError / OverflowError and killed the session.
    # eml_read must return a dict without crashing.
    bytes.fromhex("46726f6d3a203a204d6f6e2c2030204a616e2015000000000000006d706c3b0e636f9a"
                  "0d0a546f3affffffffffffffffffff0534306572652e0d0a"),
    bytes.fromhex("46726f6d3a2061400d0a546f3a20233c62406578616d706c65706c46726f6d3a206e2c0027"),
    bytes.fromhex("46726f6d3a4d6f6e2c202e636f6d0d203c726f6d546f3a20dd4d"),
    bytes.fromhex("46726f6d3a206140656e20322030303a30303e546f3a2062406578616d706c652e636f6d"
                  "0d0a5375626a6563743a2048656c6c6f0d0a446174653a204d6f6e2c20333333333333"
                  "3333333333333333333331206a616e20323032342030303a30303a303020ffffffffff00ff00652e0d0a"),
])
def test_eml_malformed_headers_do_not_crash(tmp_path, raw):
    p = tmp_path / "bad.eml"
    p.write_bytes(raw)
    assert isinstance(mm.eml_read(p), dict)                   # lazy parse: no traceback


def test_eml_split_headers_picks_first_empty_line():
    # REGRESSION: separators were tried by TYPE (\r\n\r\n before \n\n), not by POSITION —
    # on LF headers with a \r\n\r\n further down in the body, the "header block" swallowed
    # part of the body, and the wipe-undo snapshot pasted that fragment back over the file.
    raw = b"From: a@x\nSubject: hi\n\nline1\nline2\r\n\r\nrest\n"
    headers, body = mm._eml_split_headers(raw)
    assert headers == b"From: a@x\nSubject: hi\n\n"
    assert body == b"line1\nline2\r\n\r\nrest\n"


# ============================================================
#  Section 13 — Mailbox engine (.mbox, read-only)
# ============================================================

def make_mbox(path, n=2):
    blocks = []
    for i in range(n):
        blocks.append(
            f"From sender{i}@x.test Mon Jan 01 12:0{i}:00 2024\n"
            f"From: sender{i}@x.test\nSubject: Message {i}\n"
            f"Date: Mon, 0{i+1} Jan 2024 12:00:00 +0000\n\nBody {i}\n")
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def test_mbox_read_counts_and_first(tmp_path):
    p = make_mbox(tmp_path / "box.mbox", n=3)
    data = mm.mbox_read(p)
    assert data.get("MessageCount") == "3"
    assert data.get("Subject") == "Message 0"


def test_mbox_is_read_only(tmp_path):
    p = make_mbox(tmp_path / "box.mbox")
    before = sha256(p)
    assert mm.mbox_writable(p) == set()
    assert mm.mbox_write(p, "Subject", "x") is False
    assert mm.mbox_wipe(p) is False
    assert sha256(p) == before


def test_mbox_empty(tmp_path):
    p = tmp_path / "empty.mbox"
    p.write_text("", encoding="utf-8")
    assert mm.mbox_read(p).get("MessageCount") == "0"


# ============================================================
#  Section 14 — Cue Sheet engine (.cue)
# ============================================================

def make_cue(path, title="My Album", performer="My Artist", date="2020", genre="Rock"):
    txt = (f'REM GENRE {genre}\nREM DATE {date}\n'
           f'PERFORMER "{performer}"\nTITLE "{title}"\n'
           'FILE "album.wav" WAVE\n'
           '  TRACK 01 AUDIO\n    TITLE "Track 1"\n    INDEX 01 00:00:00\n'
           '  TRACK 02 AUDIO\n    TITLE "Track 2"\n    INDEX 01 03:00:00\n')
    path.write_text(txt, encoding="utf-8")
    return path


def test_cue_read(tmp_path):
    p = make_cue(tmp_path / "a.cue")
    data = mm.cue_read(p)
    assert data.get("Title") == "My Album"
    assert data.get("Performer") == "My Artist"
    assert data.get("Date") == "2020"
    assert data.get("Genre") == "Rock"
    assert data.get("TrackCount") == "2"


def test_cue_roundtrip_keeps_tracks(tmp_path):
    p = make_cue(tmp_path / "a.cue")
    assert mm.cue_write(p, "Title", "New Title") is True
    assert mm.cue_read(p).get("Title") == "New Title"
    txt = p.read_text(encoding="utf-8")
    assert 'FILE "album.wav" WAVE' in txt
    assert txt.count("TRACK ") == 2
    assert 'TITLE "Track 1"' in txt


def test_cue_erase_and_wipe(tmp_path):
    p = make_cue(tmp_path / "a.cue")
    assert mm.cue_write(p, "Genre", "") is True
    assert "Genre" not in mm.cue_read(p)
    assert mm.cue_wipe(p) is True
    data = mm.cue_read(p)
    for f in ("Title", "Performer", "Date", "Genre"):
        assert f not in data
    assert mm.cue_read(p).get("TrackCount") == "2"


# ============================================================
#  Section 15 — GeoJSON engine (.geojson)
# ============================================================
import json as _json


def make_geojson(path, name="My map"):
    obj = {"type": "FeatureCollection", "name": name,
           "bbox": [0, 0, 1, 1],
           "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
           "features": [
               {"type": "Feature", "geometry": {"type": "Point", "coordinates": [0, 0]},
                "properties": {}},
               {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 1]},
                "properties": {}}]}
    path.write_text(_json.dumps(obj), encoding="utf-8")
    return path


def test_geojson_read(tmp_path):
    p = make_geojson(tmp_path / "c.geojson")
    data = mm.geojson_read(p)
    assert data.get("Name") == "My map"
    assert data.get("FeatureCount") == "2"
    assert data.get("CRS") == "EPSG:4326"


def test_geojson_roundtrip_keeps_features(tmp_path):
    p = make_geojson(tmp_path / "c.geojson")
    assert mm.geojson_write(p, "Name", "Renamed") is True
    obj = _json.loads(p.read_text(encoding="utf-8"))
    assert obj["name"] == "Renamed"
    assert len(obj["features"]) == 2


def test_geojson_wipe_and_garbage(tmp_path):
    p = make_geojson(tmp_path / "c.geojson")
    assert mm.geojson_wipe(p) is True
    assert "Name" not in mm.geojson_read(p)
    bad = tmp_path / "bad.geojson"
    bad.write_text("{not json", encoding="utf-8")
    assert mm.geojson_read(bad) is None


# ============================================================
#  Section 16 — HTTP Archive engine (.har)
# ============================================================

def make_har(path, creator="DevTools", comment=None):
    log = {"version": "1.2", "creator": {"name": creator, "version": "120"},
           "browser": {"name": "Firefox"},
           "pages": [{"id": "p1"}], "entries": [{}, {}, {}]}
    if comment is not None:
        log["comment"] = comment
    path.write_text(_json.dumps({"log": log}), encoding="utf-8")
    return path


def test_har_read(tmp_path):
    p = make_har(tmp_path / "c.har")
    data = mm.har_read(p)
    assert data.get("Creator") == "DevTools"
    assert data.get("Browser") == "Firefox"
    assert data.get("EntryCount") == "3"
    assert data.get("PageCount") == "1"


def test_har_comment_roundtrip(tmp_path):
    p = make_har(tmp_path / "c.har")
    assert mm.har_write(p, "Comment", "2024 capture") is True
    assert mm.har_read(p).get("Comment") == "2024 capture"
    assert mm.har_write(p, "Creator", "x") is False          # read-only field
    obj = _json.loads(p.read_text(encoding="utf-8"))
    assert len(obj["log"]["entries"]) == 3


# ============================================================
#  Section 17 — SQLite engine (.sqlite/.sqlite3/.db)
# ============================================================
import sqlite3 as _sqlite3


def make_sqlite(path, app_id=0, user_version=0):
    con = _sqlite3.connect(str(path))
    con.execute("CREATE TABLE t (id INTEGER, name TEXT)")
    con.execute("INSERT INTO t VALUES (1, 'a')")
    con.execute(f"PRAGMA application_id = {app_id}")
    con.execute(f"PRAGMA user_version = {user_version}")
    con.commit()
    con.close()
    return path


def test_sqlite_read(tmp_path):
    p = make_sqlite(tmp_path / "base.db", app_id=252006, user_version=7)
    data = mm.sqlite_read(p)
    assert data.get("ApplicationID") == "252006"
    assert data.get("UserVersion") == "7"
    assert data.get("TableCount") == "1"
    assert "SQLiteVersion" in data


def test_sqlite_write_app_id_preserves_data(tmp_path):
    p = make_sqlite(tmp_path / "base.db")
    assert mm.sqlite_write(p, "ApplicationID", "123456") is True
    assert mm.sqlite_read(p).get("ApplicationID") == "123456"
    con = _sqlite3.connect(str(p))
    assert con.execute("SELECT count(*) FROM t").fetchone()[0] == 1
    con.close()


def test_sqlite_write_rejects_non_int(tmp_path):
    p = make_sqlite(tmp_path / "base.db")
    assert mm.sqlite_write(p, "ApplicationID", "not a number") is False
    assert mm.sqlite_write(p, "UserVersion", "9999999999999") is False


def test_sqlite_garbage_fails_cleanly(tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite database")
    assert mm.sqlite_read(bad) is None
    assert mm.sqlite_write(bad, "ApplicationID", "1") is False


# ============================================================
#  Section 18 — KMZ engine (.kmz)
# ============================================================
_KML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
    '<name>{name}</name><description>{desc}</description>'
    '<Placemark><name>Point A</name>'
    '<Point><coordinates>0,0,0</coordinates></Point></Placemark>'
    '</Document></kml>')


def make_kmz(path, name="My map", desc="A description"):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("doc.kml", _KML.format(name=name, desc=desc))
    return path


def test_kmz_read(tmp_path):
    p = make_kmz(tmp_path / "c.kmz")
    data = mm.kmz_read(p)
    assert data.get("Title") == "My map"
    assert data.get("Description") == "A description"


def test_kmz_roundtrip_keeps_placemark(tmp_path):
    p = make_kmz(tmp_path / "c.kmz")
    assert mm.kmz_write(p, "Title", "New Name") is True
    assert mm.kmz_read(p).get("Title") == "New Name"
    with zipfile.ZipFile(p) as z:
        assert b"Point A" in z.read("doc.kml")
        assert b"0,0,0" in z.read("doc.kml")


def test_kmz_wipe_and_garbage(tmp_path):
    p = make_kmz(tmp_path / "c.kmz")
    assert mm.kmz_wipe(p) is True
    data = mm.kmz_read(p)
    assert data.get("Title") in (None, "")
    bad = tmp_path / "bad.kmz"
    bad.write_bytes(b"not a zip")
    assert mm.kmz_read(bad) is None


# ============================================================
#  Section 19 — MusicXML engine (.musicxml)
# ============================================================
_MUSICXML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<score-partwise version="4.0">'
    '<work><work-title>{title}</work-title></work>'
    '<identification><creator type="composer">{composer}</creator>'
    '<rights>{rights}</rights>'
    '<encoding><software>Finale</software>'
    '<encoding-date>2024-01-01</encoding-date></encoding>'
    '</identification>'
    '<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
    '<part id="P1"><measure number="1"/></part>'
    '</score-partwise>')


def make_musicxml(path, title="My Score", composer="J. Doe", rights="© 2024"):
    path.write_text(_MUSICXML.format(title=title, composer=composer, rights=rights),
                    encoding="utf-8")
    return path


def test_musicxml_read(tmp_path):
    p = make_musicxml(tmp_path / "p.musicxml")
    data = mm.musicxml_read(p)
    assert data.get("Title") == "My Score"
    assert data.get("Creator") == "J. Doe"
    assert data.get("Rights") == "© 2024"
    assert data.get("Software") == "Finale"
    assert data.get("EncodingDate") == "2024-01-01"


def test_musicxml_roundtrip_keeps_parts(tmp_path):
    p = make_musicxml(tmp_path / "p.musicxml")
    assert mm.musicxml_write(p, "Title", "New Title") is True
    assert mm.musicxml_read(p).get("Title") == "New Title"
    assert mm.musicxml_write(p, "Creator", "Another Composer") is True
    assert mm.musicxml_read(p).get("Creator") == "Another Composer"
    assert b"Piano" in p.read_bytes()


def test_musicxml_wipe_and_garbage(tmp_path):
    p = make_musicxml(tmp_path / "p.musicxml")
    assert mm.musicxml_wipe(p) is True
    data = mm.musicxml_read(p)
    assert data.get("Title") in (None, "")
    assert data.get("Creator") in (None, "")
    bad = tmp_path / "bad.musicxml"
    bad.write_text("<html>not musicxml</html>", encoding="utf-8")
    assert mm.musicxml_read(bad) is None


# ============================================================
#  ZIP/XML container integrity (anti loss/corruption)
# ============================================================

# --- _zip_replace preserves duplicate names + archive comment ---

@pytest.mark.filterwarnings("ignore::UserWarning")          # the duplicate name is INTENDED here
def test_zip_replace_preserves_duplicates_and_comment(tmp_path):
    p = tmp_path / "a.cbz"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("page1.png", b"FIRST-COPY")
        z.writestr("page1.png", b"SECOND-COPY")              # duplicate name (badly built CBZ)
        z.writestr("meta.txt", b"hello")
        z.comment = b"ARCHIVE-COMMENT"
    assert mm._zip_replace(p, "meta.txt", b"world") is True
    with zipfile.ZipFile(p) as z:
        infos = z.infolist()
        assert [i.filename for i in infos].count("page1.png") == 2   # both entries survive
        datas = [z.read(i) for i in infos if i.filename == "page1.png"]
        assert b"FIRST-COPY" in datas and b"SECOND-COPY" in datas    # no copy lost
        assert z.comment == b"ARCHIVE-COMMENT"                       # comment preserved
        assert z.read("meta.txt") == b"world"


# --- ElementTree no longer rewrites the namespace prefixes ---

def test_xml_write_preserves_default_and_known_prefixes(tmp_path):
    # KMZ: default namespace (kml) + gx prefix; after writing, gx: and the default
    # must remain (no ns0:/ns1:).
    p = tmp_path / "m.kmz"
    kml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<kml xmlns="http://www.opengis.net/kml/2.2" '
           'xmlns:gx="http://www.google.com/kml/ext/2.2">'
           '<Document><name>{}</name>'
           '<Placemark><gx:Track><gx:coord>1 2 3</gx:coord></gx:Track></Placemark>'
           '</Document></kml>')
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("doc.kml", kml.format("old"))
    assert mm.kmz_write(p, "Title", "new") is True           # KMZ Title -> <name>
    with zipfile.ZipFile(p) as z:
        out = z.read("doc.kml").decode("utf-8")
    assert "<gx:Track>" in out and "<gx:coord>" in out      # gx prefix kept
    assert "ns0:" not in out and "ns1:" not in out          # no invented prefixes
    assert "new" in out


# --- MusicXML keeps its DOCTYPE on write ---

_MUSICXML_DOCTYPE = (
    '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
    '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
    '"http://www.musicxml.org/dtds/partwise.dtd">\n'
    '<score-partwise version="4.0">'
    '<work><work-title>Title</work-title></work>'
    '<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
    '<part id="P1"><measure number="1"/></part>'
    '</score-partwise>')


def test_musicxml_write_keeps_doctype(tmp_path):
    p = tmp_path / "s.musicxml"
    p.write_text(_MUSICXML_DOCTYPE, encoding="utf-8")
    assert mm.musicxml_write(p, "Title", "New") is True
    raw = p.read_bytes()
    assert b"<!DOCTYPE score-partwise PUBLIC" in raw        # DOCTYPE preserved (otherwise DTD not validatable)
    assert b"partwise.dtd" in raw
    assert mm.musicxml_read(p).get("Title") == "New"


# --- EPUB — editing/clearing the identifier no longer breaks unique-identifier ---

_EPUB_OPF_WITH_ID = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:identifier id="bookid">urn:uuid:REAL-ID</dc:identifier>'
    '<dc:title>{title}</dc:title><dc:language>fr</dc:language>'
    '</metadata><manifest/><spine/></package>')


def make_epub_with_id(path, title="TheTitle"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", _EPUB_CONTAINER)
        z.writestr("content.opf", _EPUB_OPF_WITH_ID.format(title=title))
    return path


def _epub_identifier_ids(path):
    root = mm._xml_parse(path, "content.opf")
    uid = root.get("unique-identifier")
    ids = [e.get("id") for e in root.iter() if str(e.tag).endswith("}identifier")]
    return uid, ids, root


def test_epub_edit_identifier_keeps_id_and_reference(tmp_path):
    p = make_epub_with_id(tmp_path / "b.epub")
    assert mm.epub_write(p, "Identifier", "NEW-ID") is True
    uid, ids, root = _epub_identifier_ids(p)
    assert uid in ids                                       # reference not dangling
    match = [e for e in root.iter() if str(e.tag).endswith("}identifier") and e.get("id") == uid]
    assert len(match) == 1 and match[0].text == "NEW-ID"   # value changed, id kept


def test_epub_edit_title_keeps_identifier_id(tmp_path):
    p = make_epub_with_id(tmp_path / "b.epub")
    assert mm.epub_write(p, "Title", "Another") is True
    uid, ids, _ = _epub_identifier_ids(p)
    assert uid in ids                                       # editing another field does not touch the id


def test_epub_identifier_clear_is_refused_not_faked(tmp_path):
    """REGRESSION: clearing an EPUB's unique identifier is a no-op (a dangling
       unique-identifier reference would be an invalid EPUB) — but it used to return True,
       logging a phantom 'clear', stacking an undo step and reporting "Identifier → (empty)".
       It must now REFUSE (return False), leave the identifier intact, log nothing, and offer a
       specific message rather than a bare "Write failed."."""
    p = make_epub_with_id(tmp_path / "b.epub")
    mm._CHANGELOG.clear()
    assert mm.write(p, "Identifier", "") is False           # refused, not a phantom success
    assert not mm._CHANGELOG                                # nothing logged
    uid, ids, root = _epub_identifier_ids(p)
    match = [e for e in root.iter() if str(e.tag).endswith("}identifier") and e.get("id") == uid]
    assert len(match) == 1 and match[0].text == "urn:uuid:REAL-ID"   # value untouched
    assert mm.write_refusal_reason(p, "Identifier", "") == mm.tr("epub_id_protected")
    mm._CHANGELOG.clear()


def test_epub_wipe_keeps_valid_structure(tmp_path):
    # wipe removes ALL identifying metadata (dc:creator, dc:source, <meta name>
    # calibre…) but KEEPS the minimum of a valid EPUB (unique identifier, emptied
    # title/language, EPUB 3 <meta property>); the undo restores the raw OPF.
    import xml.etree.ElementTree as ET
    opf = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
           '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
           '<dc:identifier id="bookid">urn:uuid:REAL-ID</dc:identifier>'
           '<dc:title>TheTitle</dc:title><dc:language>fr</dc:language>'
           '<dc:creator>Secret Author</dc:creator>'
           '<dc:source>file:///home/me/book.docx</dc:source>'
           '<meta name="calibre:user_metadata" content="private"/>'
           '<meta property="dcterms:modified">2024-01-01T00:00:00Z</meta>'
           '</metadata><manifest/><spine/></package>')
    p = tmp_path / "b.epub"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", _EPUB_CONTAINER)
        z.writestr("content.opf", opf)
    before_raw = zipfile.ZipFile(p).read("content.opf")
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        after = zipfile.ZipFile(p).read("content.opf").decode()
        for leak in ("Secret Author", "book.docx", "calibre"):
            assert leak not in after                        # identifiers removed
        ET.fromstring(after)                                # OPF still valid
        uid, ids, root = _epub_identifier_ids(p)
        assert uid in ids                                   # unique identifier preserved
        tags = {str(e.tag).split("}")[-1] for e in root.iter()}
        assert "title" in tags and "language" in tags       # required elements present
        assert "dcterms:modified" in after                  # EPUB 3 <meta property> kept
        assert mm._UNDO.undo_last() is True
        assert zipfile.ZipFile(p).read("content.opf") == before_raw   # OPF restored bit-for-bit
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


# ============================================================
#  Section 20 — TCX engine (.tcx, read-only)
# ============================================================
_TCX = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
    '<Activities><Activity Sport="{sport}">'
    '<Id>{date}</Id>'
    '<Lap StartTime="{date}"><TotalTimeSeconds>600</TotalTimeSeconds></Lap>'
    '</Activity></Activities>'
    '<Author xsi:type="Application_t" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<Name>{device}</Name></Author>'
    '</TrainingCenterDatabase>')


def make_tcx(path, sport="Running", date="2024-03-15T08:00:00Z", device="Garmin Connect"):
    path.write_text(_TCX.format(sport=sport, date=date, device=device), encoding="utf-8")
    return path


def test_tcx_read(tmp_path):
    p = make_tcx(tmp_path / "run.tcx")
    data = mm.tcx_read(p)
    assert data.get("Sport") == "Running"
    assert data.get("Date") == "2024-03-15T08:00:00Z"
    assert data.get("Device") == "Garmin Connect"


def test_tcx_is_read_only(tmp_path):
    p = make_tcx(tmp_path / "run.tcx")
    before = sha256(p)
    assert mm.tcx_writable(p) == set()
    assert mm.tcx_write(p, "Sport", "Cycling") is False
    assert mm.tcx_wipe(p) is False
    assert sha256(p) == before
    bad = tmp_path / "bad.tcx"
    bad.write_text("<other/>", encoding="utf-8")
    assert mm.tcx_read(bad) is None


# ============================================================
#  Section 21 — Application packages engine (.jar/.xpi/.ipa, read-only)
# ============================================================

def make_jar(path):
    manifest = ("Manifest-Version: 1.0\r\n"
                "Created-By: 17.0 (Eclipse Adoptium)\r\n"
                "Implementation-Title: MyApp\r\n"
                "Implementation-Version: 2.5.0\r\n"
                "Implementation-Vendor: ACME Corp\r\n"
                "Main-Class: com.acme.Main\r\n\r\n")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("META-INF/MANIFEST.MF", manifest)
        z.writestr("com/acme/Main.class", b"\xca\xfe\xba\xbe")
    return path


def make_xpi(path):
    manifest = _json.dumps({"name": "My Extension", "version": "1.4.0",
                            "description": "A test extension",
                            "author": "Jane Dev"})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", manifest)
    return path


def make_ipa(path):
    info = plistlib.dumps({"CFBundleName": "MyAppiOS",
                           "CFBundleShortVersionString": "3.1",
                           "CFBundleIdentifier": "com.test.ios"})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Payload/MyApp.app/Info.plist", info)
    return path


def test_archive_jar_read(tmp_path):
    p = make_jar(tmp_path / "app.jar")
    data = mm.archive_read(p)
    assert data.get("Title") == "MyApp"
    assert data.get("Version") == "2.5.0"
    assert data.get("Vendor") == "ACME Corp"
    assert data.get("MainClass") == "com.acme.Main"


def test_archive_xpi_read(tmp_path):
    p = make_xpi(tmp_path / "ext.xpi")
    data = mm.archive_read(p)
    assert data.get("Title") == "My Extension"
    assert data.get("Version") == "1.4.0"
    assert data.get("Author") == "Jane Dev"


def test_archive_ipa_read(tmp_path):
    p = make_ipa(tmp_path / "app.ipa")
    data = mm.archive_read(p)
    assert data.get("Title") == "MyAppiOS"
    assert data.get("Version") == "3.1"


def test_archive_is_read_only(tmp_path):
    p = make_jar(tmp_path / "app.jar")
    before = sha256(p)
    assert mm.archive_writable(p) == set()
    assert mm.archive_write(p, "Title", "x") is False
    assert mm.archive_wipe(p) is False
    assert sha256(p) == before


def test_archive_garbage_fails_cleanly(tmp_path):
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"not a zip")
    assert mm.archive_read(bad) is None


# ============================================================
#  Section 22 — Pure invariants (path collection, wipe reporting)
# ============================================================

# --- collect_paths: dedup + non-recursivity + dotfiles ---

def test_collect_paths_dedups_same_source(tmp_path):
    a = write_png(tmp_path, "a.png")
    paths = mm.collect_paths([str(a), str(a)])                # same source 2×
    assert paths == [a]                                       # ignored the 2nd time


def test_collect_paths_non_recursive_and_dotfiles(tmp_path):
    (tmp_path / "visible.png").write_bytes(PNG_BYTES)
    (tmp_path / ".cache").write_text("x", encoding="utf-8")  # dotfile excluded
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "deep.png").write_bytes(PNG_BYTES)                 # not expanded (recursion)
    names = {p.name for p in mm.collect_paths([str(tmp_path)])}
    assert names == {"visible.png"}


# --- collect_paths ignores backups/temporaries and links ---

def test_collect_paths_skips_backups_and_temps(tmp_path):
    # A wipe/group on a FOLDER must never pick up a backup copy (.bak, left by another
    # tool) nor the metmux temporaries (.tmp).
    (tmp_path / "a.geojson").write_text("{}", encoding="utf-8")
    (tmp_path / "a.geojson.bak").write_text("{}", encoding="utf-8")
    (tmp_path / "a.geojson.tmp").write_text("{}", encoding="utf-8")
    (tmp_path / "video.exifedit_tmp.mkv").write_bytes(b"x")
    names = {p.name for p in mm.collect_paths([str(tmp_path)])}
    assert names == {"a.geojson"}


def test_collect_paths_skips_symlinks_out_of_dir(tmp_path):
    # A symbolic link in the target folder must not step out of scope.
    outside_dir = tmp_path / "out"; outside_dir.mkdir()
    target = outside_dir / "secret.geojson"; target.write_text("{}", encoding="utf-8")
    folder = tmp_path / "in"; folder.mkdir()
    (folder / "real.geojson").write_text("{}", encoding="utf-8")
    try:
        (folder / "link.geojson").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    names = {p.name for p in mm.collect_paths([str(folder)])}
    assert names == {"real.geojson"}


# --- wipe names the failed files (not just a count) ---

def test_session_wipe_reports_failed_names(tmp_path, monkeypatch, capsys):
    good = make_docx(tmp_path / "good.docx")
    bad = tmp_path / "readonly.tcx"                        # tcx = read-only → wipe fails
    bad.write_text('<TrainingCenterDatabase xmlns="http://www.garmin.com/'
                   'xmlschemas/TrainingCenterDatabase/v2"></TrainingCenterDatabase>',
                   encoding="utf-8")
    answers = iter(["y", ""])
    monkeypatch.setattr(mm, "ask", lambda *a, **k: next(answers, ""))
    mm._CHANGELOG.clear()
    mm.session_wipe([good, bad])
    out = capsys.readouterr().out
    assert "readonly.tcx" in out
    mm._CHANGELOG.clear()


# ============================================================
#  Section — End-of-session summary (_finish): collapse to "origin → final"
# ------------------------------------------------------------
#  Repeated edits of one field collapse into a single row showing its ORIGINAL value
#  and its FINAL value; a field brought back to its origin drops out (net no-op).
# ============================================================

def test_finish_collapses_repeated_edits_to_origin_and_final():
    # Editing one field several times shows ONE "origin → final" row, not each step.
    mm._CHANGELOG.clear()
    p = "invoice.pdf"
    mm._log_change("write", p, "Artist", "x",     old=mm._ABSENT)
    mm._log_change("write", p, "Artist", "yyy",   old="x")
    mm._log_change("write", p, "Artist", "Queen", old="yyy")
    rows = mm._collapse_changelog()
    assert len(rows) == 1
    assert rows[0]["tag"] == "Artist"
    assert rows[0]["origin_s"] == mm.tr("empty")       # started absent → "(vide)"/"(empty)"
    assert rows[0]["final_s"] == "Queen"               # only the final value survives
    mm._CHANGELOG.clear()


def test_finish_drops_field_returned_to_its_origin():
    # A field edited then brought back to its original value is a net no-op: no row at all.
    mm._CHANGELOG.clear()
    p = "invoice.pdf"
    mm._log_change("clear", p, "Subject", "",     old=mm._ABSENT)
    mm._log_change("write", p, "Subject", "kaka", old="")
    mm._log_change("clear", p, "Subject", "",     old="kaka")
    assert mm._collapse_changelog() == []
    mm._CHANGELOG.clear()


def test_finish_wipe_supersedes_prior_field_edits():
    # A whole-file wipe supersedes the field edits BEFORE it; a field edited AFTER stays.
    mm._CHANGELOG.clear()
    p = "doc.docx"
    mm._log_change("write", p, "Title", "X", old="orig")
    mm._log_change("wipe", p)
    mm._log_change("write", p, "Author", "Bob", old="")
    kinds = [(r["action"], r["tag"]) for r in mm._collapse_changelog()]
    assert ("wipe", "") in kinds                        # the purge is reported
    assert not any(tag == "Title" for _, tag in kinds)  # pre-wipe Title row dropped
    assert ("write", "Author") in kinds                 # post-wipe edit kept
    mm._CHANGELOG.clear()


def test_finish_rename_shows_old_and_new_name():
    # A rename reads as "old name → new name".
    mm._CHANGELOG.clear()
    mm._log_change("rename", "new.pdf", "FileName", "new.pdf", old="old.pdf")
    r = mm._collapse_changelog()[0]
    assert r["origin_s"] == "old.pdf" and r["final_s"] == "new.pdf"
    mm._CHANGELOG.clear()


def test_finish_output_counts_and_prints_net_changes_only(monkeypatch, capsys):
    # End to end: the summary counts NET changes (1, not 6) and never shows intermediates.
    mm._CHANGELOG.clear()
    p = "invoice.pdf"
    for val, old in [("", mm._ABSENT), ("a", ""), ("", "a"), ("kaka", ""), ("", "kaka")]:
        mm._log_change("clear" if val == "" else "write", p, "Subject", val, old=old)
    mm._log_change("write", p, "Artist", "Queen", old=mm._ABSENT)
    monkeypatch.setattr(mm, "ask", lambda *a, **k: "")
    monkeypatch.setattr(mm, "clear_screen", lambda: None)
    mm._finish()
    out = capsys.readouterr().out
    assert mm.tr("changes_made", n=1) in out
    assert "Queen" in out
    assert "kaka" not in out
    mm._CHANGELOG.clear()


def test_finish_captures_old_through_real_write(tmp_path, monkeypatch):
    # The origin comes from write()'s real capture: editing a docx field twice in a
    # session yields a single origin(original file value) → final(last value) row.
    p = make_docx(tmp_path / "d.docx", title="Original")
    _answers(monkeypatch, ["Title First", "Title Second", "q"])
    mm._CHANGELOG.clear()
    mm.session_single(p)
    rows = [r for r in mm._collapse_changelog() if r["tag"] == "Title"]
    assert len(rows) == 1
    assert rows[0]["origin_s"] == "Original" and rows[0]["final_s"] == "Second"
    mm._CHANGELOG.clear()


@needs_exiftool
def test_undo_removes_the_summary_rows_of_what_it_undid(tmp_path):
    """REGRESSION: the summary showed phantom rows for net no-ops — "wiped" plus one row
       per restored field after wipe+undo (the restore's own writes were logged with no
       origin), and two rename rows after rename+undo (each keyed under its target path).
       An undo logs nothing and removes the rows of the step it reverted; what was done
       BEFORE that step still reports."""
    import plistlib
    p = tmp_path / "t.plist"
    with p.open("wb") as f:
        plistlib.dump({"Title": "Hello", "Author": "Someone"}, f)
    mm._CHANGELOG.clear()
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) and mm._UNDO.undo_all() is True
        assert mm._collapse_changelog() == []
        renamed, err = mm.apply_filename(p, "renamed.plist")
        assert err is None and mm._UNDO.undo_all() is True
        assert mm._collapse_changelog() == []
        assert mm.write(p, "Title", "Edited")
        mm._UNDO._commit_batch()
        assert mm.wipe(p) and mm._UNDO.undo_last() is True    # undo the wipe only
        rows = mm._collapse_changelog()
        assert [(r["tag"], r["final"]) for r in rows] == [("Title", "Edited")]
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None
        mm._CHANGELOG.clear()
        mm._RENAMES.clear()


# --- Jupyter Notebook engine (.ipynb): round-trip + garbage ---

def make_ipynb(path, title="My Notebook", authors=("Ada", "Linus")):
    nb = {"cells": [{"cell_type": "code", "source": ["print(1)"], "metadata": {},
                     "outputs": [], "execution_count": None}],
          "metadata": {"title": title,
                       "authors": [{"name": n} for n in authors]},
          "nbformat": 4, "nbformat_minor": 5}
    path.write_text(_json.dumps(nb), encoding="utf-8")
    return path


def test_ipynb_read_semantic(tmp_path):
    p = make_ipynb(tmp_path / "n.ipynb")
    data = mm.ipynb_read(p)
    assert data.get("Title") == "My Notebook"
    assert data.get("Authors") == "Ada, Linus"


def test_ipynb_roundtrip_keeps_cells(tmp_path):
    p = make_ipynb(tmp_path / "n.ipynb")
    assert mm.ipynb_write(p, "Title", "New") is True
    assert mm.ipynb_read(p).get("Title") == "New"
    nb = _json.loads(p.read_text(encoding="utf-8"))
    assert nb["cells"][0]["source"] == ["print(1)"]
    assert nb["nbformat"] == 4


def test_ipynb_wipe_and_garbage(tmp_path):
    p = make_ipynb(tmp_path / "n.ipynb")
    assert mm.ipynb_wipe(p) is True
    data = mm.ipynb_read(p)
    assert data.get("Title") in (None, "")
    assert data.get("Authors") in (None, "")
    bad = tmp_path / "bad.ipynb"
    bad.write_text("{not json", encoding="utf-8")
    assert mm.ipynb_read(bad) is None


# --- ODF engine (.odt/.ods/.odp): semantic field, round-trip, garbage ---
_ODF_META = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<office:document-meta '
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<office:meta>'
    '<dc:title>{title}</dc:title>'
    '<meta:initial-creator>{creator}</meta:initial-creator>'
    '<dc:subject>{subject}</dc:subject>'
    '</office:meta></office:document-meta>')


def make_odt(path, title="MyTitleODF", creator="MeODF", subject="ASubject"):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("meta.xml", _ODF_META.format(title=title, creator=creator,
                                                subject=subject))
        z.writestr("content.xml",
                   '<?xml version="1.0"?><office:document-content '
                   'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"/>')
    return path


def test_odf_read_semantic(tmp_path):
    p = make_odt(tmp_path / "doc.odt")
    data = mm.odf_read(p)
    assert data.get("Title") == "MyTitleODF"
    assert data.get("Creator") == "MeODF"                    # semantic field, not the type
    assert data.get("Subject") == "ASubject"


def test_odf_roundtrip_and_erase(tmp_path):
    p = make_odt(tmp_path / "doc.odt")
    assert mm.odf_write(p, "Title", "AnotherTitle") is True
    assert mm.odf_read(p).get("Title") == "AnotherTitle"
    with zipfile.ZipFile(p) as z:
        assert "content.xml" in set(z.namelist())
    assert mm.odf_write(p, "Title", "") is True
    assert mm.odf_read(p).get("Title") in (None, "")


def test_odf_wipe_and_garbage(tmp_path):
    # wipe erases ALL <office:meta>, including dc:creator (last author),
    # meta:generator and meta:user-defined (outside ODF_TAGS); the undo restores meta.xml.
    import xml.etree.ElementTree as ET
    p = tmp_path / "doc.odt"
    meta = ('<?xml version="1.0" encoding="UTF-8"?><office:document-meta '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><office:meta>'
            '<dc:title>T</dc:title><dc:creator>LastAuthor</dc:creator>'
            '<meta:generator>SecretApp/1.0</meta:generator>'
            '<meta:user-defined meta:name="Client">ACME</meta:user-defined>'
            '</office:meta></office:document-meta>')
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.text",
                   compress_type=zipfile.ZIP_STORED)
        z.writestr("meta.xml", meta)
        z.writestr("content.xml", '<?xml version="1.0"?><office:document-content '
                   'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"/>')
    before_raw = zipfile.ZipFile(p).read("meta.xml")
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        after = zipfile.ZipFile(p).read("meta.xml").decode()
        for leak in ("LastAuthor", "SecretApp", "ACME", "<dc:title>"):
            assert leak not in after                       # NO metadata left
        ET.fromstring(after)                               # meta.xml still valid
        with zipfile.ZipFile(p) as z:                      # mimetype first + STORED preserved
            assert z.namelist()[0] == "mimetype"
            assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert mm._UNDO.undo_last() is True
        assert zipfile.ZipFile(p).read("meta.xml") == before_raw
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None
    bad = tmp_path / "bad.odt"
    bad.write_bytes(b"not an odf zip")
    assert mm.odf_read(bad) is None


# Minimal 16×16 JPEG encoded in base64: the fixture is built on the fly, so NO binary
# is versioned in the repo. A valid JFIF JPEG is enough for exiftool to write then
# re-read EXIF/XMP tags.
_SAMPLE_JPG_B64 = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEExNDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAAQABADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCpRRRXoHCf/9k="


def _write_sample_jpg(path):
    """Writes a small valid JPEG to `path` (throwaway fixture; no real file)."""
    path.write_bytes(base64.b64decode(_SAMPLE_JPG_B64))
    return path


# --- Missing "garbage" gaps: cue, har, tcx, mbox ---

def test_cue_garbage_fails_cleanly(tmp_path):
    bad = tmp_path / "bad.cue"
    bad.write_bytes(b"\xff\xfe\x00 binary \x80")             # latin-1 fallback, no crash
    assert mm.cue_read(bad) is not None


def test_har_garbage_fails_cleanly(tmp_path):
    bad = tmp_path / "bad.har"
    bad.write_text("{not json", encoding="utf-8")
    assert mm.har_read(bad) is None
    assert mm.har_write(bad, "Comment", "x") is False


def test_tcx_garbage_fails_cleanly(tmp_path):
    bad = tmp_path / "bad.tcx"
    bad.write_text("<html>not tcx</html>", encoding="utf-8")
    assert mm.tcx_read(bad) is None


def test_mbox_garbage_does_not_crash(tmp_path):
    bad = tmp_path / "bad.mbox"
    bad.write_bytes(b"\xff\xfe not an mbox \x00")
    assert mm.mbox_read(bad) is not None                      # never a crash


# --- Unified view (per-theme display, editable vs read-only) -----------

_SAMPLE = {
    "FileName": "x.png", "FileSize": "54 kB", "MIMEType": "image/png",
    "ImageWidth": 300, "ImageHeight": 316,          # technical (read-only)
    "Artist": "Ada", "Title": "T",                  # editable, filled in
}


def test_visible_tags_unified_vs_edit():
    """mode "all" = unified view (empty editable ones included + present technical);
       mode "edit" = only the editable fields. The unified view is a strict superset
       of the edit view."""
    w = mm.writable_from_data(mm.Path("x.png"), _SAMPLE)
    unified = mm.visible_tags(_SAMPLE, w, mode="all")
    edit = mm.visible_tags(_SAMPLE, w, mode="edit")

    assert set(edit).issubset(set(unified))
    assert all(t in w for t in edit)                       # edit = only editable ones
    assert "ImageWidth" in unified and "ImageWidth" not in edit   # technical: unified only
    assert "Artist" in edit                                # editable, filled in, present
    assert unified["Title"] == "T" and "Keywords" in edit  # editable absent → offered empty


def test_visible_tags_view_in():
    """mode "in" = only the fields actually present (non-empty value), editable OR
       not; no field offered empty, and it is a subset of the "all" view."""
    w = mm.writable_from_data(mm.Path("x.png"), _SAMPLE)
    present = mm.visible_tags(_SAMPLE, w, mode="in")
    unified = mm.visible_tags(_SAMPLE, w, mode="all")

    assert set(present) == {t for t, v in _SAMPLE.items() if v not in ("", None)}
    assert "ImageWidth" in present                         # technical present: visible
    assert "Artist" in present                             # editable present: visible
    assert "Keywords" not in present                       # editable absent: NOT offered empty
    assert set(present).issubset(set(unified))             # "in" ⊆ "all"
    assert all(v not in ("", None) for v in present.values())  # never an empty value


def test_themed_layout_order_and_editability():
    """The themes come out in the fixed order, with headers; each tag is classified
       into the right group."""
    w = mm.writable_from_data(mm.Path("x.png"), _SAMPLE)
    shown = mm.visible_tags(_SAMPLE, w, mode="all")
    layout = mm.themed_layout(shown.keys(), _SAMPLE)

    headers = [lbl for kind, lbl in layout if kind == "H"]
    assert headers == ["File", "Image", "Description",
                       "People & rights", "Location", "Dates"]
    def theme_of(tag):
        cur = None
        for kind, payload in layout:
            if kind == "H": cur = payload
            elif payload == tag: return cur
    assert theme_of("FileSize") == "File"
    assert theme_of("ImageWidth") == "Image"
    assert theme_of("Artist") == "People & rights"
    assert theme_of("Title") == "Description"
    # a GPS tag and a date fall into Location / Dates even without an explicit list
    assert mm.tag_theme("GPSLatitude") == "Location"
    assert mm.tag_theme("SomeWeirdDate") == "Dates"


# ──────────────────────────────────────────────────────────────────────────────
#  Command-bar anti-paste guard (regression: a big pasted text was executed line by
#  line and overwrote the fields in a loop — e.g. "a …" matched Author)
# ──────────────────────────────────────────────────────────────────────────────

def _feed(monkeypatch, lines):
    """Makes _read_raw() deliver the list `lines`, one entry per call."""
    it = iter(lines)
    monkeypatch.setattr(mm, "_read_raw", lambda *a, **k: next(it, None))


def test_ask_blocks_multiline_paste(monkeypatch):
    """Off the cbreak reader (canonical mode: Windows, a pipe), a bracketed paste of several
       lines reaches ask() already split: it returns NO command (empty string) and arms ONE
       warning for the whole block — not one per line, and not one per slice."""
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")
    _feed(monkeypatch, [
        mm.PASTE_BEGIN + "a beautiful sunset",
        "the author wrote about",
        "many things here" + mm.PASTE_END,
    ])
    assert mm.ask() == ""
    notice = mm._take_paste_notice()
    assert notice == mm.tr("paste_multiline")
    assert mm._take_paste_notice() is None


def test_ask_allows_single_line_paste(monkeypatch):
    """One-line paste: harmless → returned stripped of its markers, like a normal
       input, without a warning."""
    _feed(monkeypatch, [mm.PASTE_BEGIN + "My Clean Title" + mm.PASTE_END])
    assert mm.ask() == "My Clean Title"
    assert mm._take_paste_notice() is None


def test_ask_allows_normal_input(monkeypatch):
    """A typed line (without a marker) passes as-is, without a warning."""
    _feed(monkeypatch, ["Title My Book"])
    assert mm.ask() == "Title My Book"
    assert mm._take_paste_notice() is None


def test_multiline_paste_never_reaches_resolve(monkeypatch):
    """End to end: the string "a …" WOULD have matched Author (the danger was real),
       but ask() intercepts it before resolve() for a multi-line paste."""
    _feed(monkeypatch, [
        mm.PASTE_BEGIN + "a wonderful day",
        "the rest of the story" + mm.PASTE_END,
    ])
    assert mm.ask() == ""                         # the loop sees an empty line = no-op
    # proof that the danger existed: the same string, passed to resolve, targets Author
    tag, val = mm.resolve("a wonderful day", {"Author": "a"}, ["Author"], "fr")
    assert tag == "Author" and val == "wonderful day"


# --- Which line lets a paste in: the field must ALREADY be named (_value_prefix) ---

_PASTE_ALIASES = {"Comment": "c", "Artist": "a", "Title": "t", "Keywords": "k"}
_PASTE_TAGS = list(_PASTE_ALIASES)


def _ok(prefix):
    return mm._value_prefix(prefix, _PASTE_ALIASES, _PASTE_TAGS, "en")


def test_value_prefix_opens_only_on_a_named_field():
    assert _ok("c ")                     # alias + space: the value slot is open
    assert _ok("Comment ")               # full name, likewise
    assert _ok("Comment: ")              # explicit ":" form
    assert _ok("c Some start")           # a value already begun: still the same slot
    assert _ok("k +")                    # append form of a list field
    assert _ok("dates ")                 # the one command that takes a free value
    assert not _ok("c")                  # a field alone is a FOCUS request, not a value slot
    assert not _ok("")                   # a bare line names nothing
    assert not _ok("   ")
    assert not _ok("zz ")                # not a field
    assert not _ok("wipe ")              # a command is not a value slot


def test_paste_starting_with_a_field_alias_is_never_read_as_one(monkeypatch):
    """"a man, a long time ago…" pasted on a BARE line must not be read as the alias of
       Artist followed by its value: the rule looks at what was TYPED before the paste.
       The same text pasted AFTER "c " is the comment's value."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    text = b"a man, a long time ago\nlived by the sea\n"

    _prime_paste(monkeypatch, b"", text)                       # nothing typed first
    assert mm._read_line_raw(paste_ok=_ok) == ""               # no command reaches the loop
    assert mm._take_paste_notice() == mm.tr("paste_blocked")
    # proof the danger is real: that very first line, resolved, WOULD have targeted Artist
    tag, val = mm.resolve("a man, a long time ago", _PASTE_ALIASES, _PASTE_TAGS, "en")
    assert (tag, val) == ("Artist", "man, a long time ago")

    _prime_paste(monkeypatch, b"c ", text, after=b"\n")        # the field named first: allowed
    assert mm._read_line_raw(paste_ok=_ok) == "c a man, a long time ago lived by the sea"
    assert mm._take_paste_notice() is None


def test_accepted_paste_is_one_value_and_waits_for_enter(monkeypatch):
    """A pasted paragraph block becomes ONE line — its line breaks collapse to spaces — and it
       does not validate itself: without the trailing Enter, nothing comes back to the loop."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    para = b"First paragraph.\n\nSecond one,\nwrapped over two lines.\n"
    _prime_paste(monkeypatch, b"c ", para, after=b"\n")
    assert (mm._read_line_raw(paste_ok=_ok)
            == "c First paragraph. Second one, wrapped over two lines.")

    # …and none of those line breaks pressed Enter: a character typed AFTER the paste is still
    # on the same line, which it could not be if the block had submitted itself.
    _prime_paste(monkeypatch, b"c ", para, after=b"!\n")
    assert (mm._read_line_raw(paste_ok=_ok)
            == "c First paragraph. Second one, wrapped over two lines.!")


def test_one_paste_arms_one_notice_even_when_it_arrives_in_slices(monkeypatch):
    """The macOS symptom: a big paste crosses the pty in slices, and each slice used to raise its
       own "Pasted input ignored (N lines)". The burst is drained to the end (a slice arriving
       within the grace period still belongs to it), so ONE paste says ONE thing, once."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    slices = [b"first slice of it\n", b"second slice\n", b"third slice\n"]
    _prime_reader(monkeypatch, reads=slices)
    monkeypatch.setattr(mm.select, "select", lambda r, w, x, t=0: ([r], [], []))  # more is coming
    assert mm._read_line_raw(paste_ok=_ok) == ""
    assert mm._take_paste_notice() == mm.tr("paste_blocked")   # one notice, not three


def test_typing_is_never_taken_for_a_paste(monkeypatch):
    """The other side of the guard: characters that arrive one read at a time are typed, whatever
       their number, and Enter alone in its read is not a second arrival."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    _prime_raw(monkeypatch, b"c a long comment typed by hand\n")
    assert mm._read_raw() == "c a long comment typed by hand"
    assert mm._take_paste_notice() is None


def test_a_command_typed_ahead_is_kept_not_refused(monkeypatch):
    """Type-ahead (keys struck while metmux was busy) comes back in ONE read, exactly like a
       paste. A single unbracketed line is NOT refused: replayed into the line as typing,
       with only its Enter withheld, so nothing runs that the user cannot read first."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    _prime_paste(monkeypatch, b"", b"wipe\n", after=b"\n")     # "wipe" + Enter, typed ahead
    assert mm._read_line_raw(paste_ok=_ok) == "wipe"           # kept, and it took the user's Enter to run
    assert mm._take_paste_notice() is None

    # Two lines, though, is a paste: nobody types two commands blind inside one read.
    _prime_paste(monkeypatch, b"", b"wipe\nq\n")
    assert mm._read_line_raw(paste_ok=_ok) == ""
    assert mm._take_paste_notice() == mm.tr("paste_blocked")


def test_a_long_single_line_burst_is_a_paste_not_type_ahead(monkeypatch):
    """Above _TYPEAHEAD_MAX a one-line burst is a paste (nobody types 120 characters blind):
       refused on a bare line, taken as the value once the field is named."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    long_text = ("a man, a long time ago lived by the sea and wrote a book about it, "
                 "which nobody ever read, but the sea did, and it kept the story").encode()
    assert len(long_text) > mm._TYPEAHEAD_MAX

    _prime_paste(monkeypatch, b"", long_text)
    assert mm._read_line_raw(paste_ok=_ok) == ""
    assert mm._take_paste_notice() == mm.tr("paste_blocked")

    _prime_paste(monkeypatch, b"c ", long_text, after=b"\n")
    assert mm._read_line_raw(paste_ok=_ok) == "c " + long_text.decode()


def test_ctrl_u_wipes_the_line_in_one_stroke(monkeypatch):
    """Ctrl-U: reading the keyboard ourselves removed the terminal's own line-kill, so we owe
       it back (a long pasted value cannot be erased one backspace at a time)."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    monkeypatch.setattr(mm, "term_width", lambda default=80: 80)
    _prime_raw(monkeypatch, b"Wrong value\x15Right\n")     # \x15 = Ctrl-U
    assert mm._read_raw() == "Right"

    # It reaches back over a WRAP (a run of backspaces stops dead at the row above).
    out = []
    monkeypatch.setattr(mm.sys, "stdout", type("W", (), {"write": lambda s, t: out.append(t),
                                                         "flush": lambda s: None})())
    monkeypatch.setattr(mm, "term_width", lambda default=80: 20)
    _prime_paste(monkeypatch, b"c ", b"x" * 90, after=b"\x15\n")
    assert mm._read_raw(paste_ok=_ok) == ""                # the whole pasted value: gone
    assert "\033[4A" in "".join(out)                       # cursor walked back up the 4 wrapped rows
    assert "\033[J" in "".join(out)                        # …and erased from there


def test_bracketed_single_line_paste_is_refused_on_a_bare_line(monkeypatch):
    """Where the terminal marks its pastes, the guard needs no guessing and gets stricter: even a
       ONE-line paste is refused on a bare line (it could be "a man, a long time ago"), while the
       same block after "t " is that field's value."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    marked = (mm.PASTE_BEGIN + "a man, a long time ago" + mm.PASTE_END).encode()

    _prime_paste(monkeypatch, b"", marked)
    assert mm._read_line_raw(paste_ok=_ok) == ""
    assert mm._take_paste_notice() == mm.tr("paste_blocked")

    _prime_paste(monkeypatch, b"t ", marked, after=b"\n")
    assert mm._read_line_raw(paste_ok=_ok) == "t a man, a long time ago"
    assert mm._take_paste_notice() is None


# --- Windows: no cbreak reader, no bracketed paste — the console input buffer is the evidence ---

class _FakeMsvcrt:
    """The console keyboard buffer, as msvcrt exposes it: kbhit() says whether something is
       waiting, getwch() takes the next character.

       Built from ARRIVALS, because that is the only thing that tells a paste from typing: a
       struck key is an arrival of one (nothing is waiting, getwch blocks on it), a pasted block
       is ONE arrival of many (they are all already in the buffer, and kbhit sees them)."""
    def __init__(self, *arrivals, queued=False):
        self.left = [list(a) for a in arrivals if a]
        self.mid = queued                             # inside an arrival: the rest is waiting
        #  queued=True: it was ALL there before we came to look — what type-ahead looks like

    def kbhit(self):
        return self.mid and bool(self.left) and bool(self.left[0])

    def getwch(self):
        if not self.left:
            raise AssertionError("the reader asked for a key past the end of the script")
        if not self.mid:
            time.sleep(mm._KEY_GAP + 0.005)           # a key is STRUCK: a hand paused before it.
        cur = self.left[0]                            # (the rest of an arrival comes with no pause
        ch = cur.pop(0)                               #  at all — that is what makes it an arrival)
        self.mid = bool(cur)
        if not cur:
            self.left.pop(0)
        return ch


def _windows_raw(monkeypatch, *arrivals, queued=False):
    """metmux on the Windows console, reading it key by key (no input(), no line editor)."""
    fake = _FakeMsvcrt(*arrivals, queued=queued)
    monkeypatch.setattr(mm, "msvcrt", fake)
    monkeypatch.setattr(mm, "_chunk_idle", False)
    monkeypatch.setattr(mm, "_drop_until", 0.0)
    monkeypatch.setattr(mm, "_raw_mode", False)       # cbreak is POSIX-only…
    monkeypatch.setattr(mm, "_win_raw", True)         # …the console reader takes over there
    monkeypatch.setattr(mm, "_pending", b"")
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_typed", "")
    monkeypatch.setattr(mm, "_cursor", 0)
    monkeypatch.setattr(mm, "_in_paste", False)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")
    return fake


def test_windows_reader_types_a_line(monkeypatch):
    """Key by key, unechoed by the console: the reader echoes what it accepts, and Enter — a key
       like any other now — closes the line."""
    out = []
    monkeypatch.setattr(mm.sys, "stdout", type("W", (), {"write": lambda s, t: out.append(t),
                                                         "flush": lambda s: None})())
    _windows_raw(monkeypatch, "c", " ", "H", "i", "\r")
    assert mm._read_raw(paste_ok=_ok) == "c Hi"
    printed = "".join(out)
    assert printed.startswith("c Hi")                 # echoed by us, since the console does not
    assert "\n" not in printed and "\r" not in printed    # …and the Enter itself never is


def test_windows_reader_refuses_a_whole_pasted_block(monkeypatch):
    """REGRESSION (Windows): input() let the console echo a paste, cut it on its line breaks
       and serve it back one line at a time — metmux ran them as commands (a line beginning
       with a field name wrote it). Read key by key, the block arrives as ONE burst: refused
       whole, never echoed, never run."""
    out = []
    monkeypatch.setattr(mm.sys, "stdout", type("W", (), {"write": lambda s, t: out.append(t),
                                                         "flush": lambda s: None})())
    block = "a man, a long time ago\nc lived by the sea\nand wrote a book\n"   # a field name inside
    _windows_raw(monkeypatch, block)                  # one arrival: the whole clipboard
    assert mm._read_line_raw(paste_ok=_ok) == ""      # no command reaches the loop…
    assert mm._take_paste_notice() == mm.tr("paste_blocked")
    printed = "".join(out)
    assert "lived by the sea" not in printed          # …and not a word of it was echoed
    assert "wrote a book" not in printed


def test_windows_reader_takes_a_paste_after_a_field_name(monkeypatch):
    """Name the field, then paste: the block lands as that field's value, flattened to one
       line, and waits for Enter."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    _windows_raw(monkeypatch, "c", " ", "First line\nsecond line\n", "\r")
    assert mm._read_line_raw(paste_ok=_ok) == "c First line second line"
    assert mm._take_paste_notice() is None


class _SlowConsole:
    """An old Windows console: it does not hand a pasted block over in one go, it feeds it in
       slices, and it takes its time between them. Nothing is waiting when the prompt opens (the
       user pastes into a session that sits idle), so the reader BLOCKS on the first character —
       which is exactly what tells it that no command was typed ahead."""
    def __init__(self, slices, gap):
        self.slices = [list(s) for s in slices]
        self.gap, self.ready = gap, time.monotonic() + gap

    def kbhit(self):                                   # a slice, once delivered, sits waiting
        return bool(self.slices) and time.monotonic() >= self.ready

    def getwch(self):                                  # …and blocks the reader until it lands
        while self.slices and time.monotonic() < self.ready:
            time.sleep(0.005)
        if not self.slices:
            return "\r"                                # block in: the user presses Enter
        cur = self.slices[0]
        ch = cur.pop(0)
        if not cur:
            self.slices.pop(0)
            self.ready = time.monotonic() + self.gap   # the next slice takes a while to come
        return ch


def test_a_paste_fed_in_slow_slices_is_still_one_paste(monkeypatch):
    """REGRESSION (Windows freeze): a slice arriving after the short grace closed the burst,
       so each piece came back as its OWN refusal — and every refusal re-reads the file through
       exiftool and repaints. Once the burst can only be a paste, the wait grows: the slices
       are gathered into ONE block, refused once."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    monkeypatch.setattr(mm, "_PASTE_SLOW_GRACE", 0.3)     # (the real one is 1 s: keep the test short)
    monkeypatch.setattr(mm, "_PASTE_HINT_AFTER", 10)      # not the subject here
    slices = ["Lorem ipsum dolor sit amet, consectetur.\n",
              "Sed do eiusmod tempor incididunt ut labore.\n",
              "Ut enim ad minim veniam, quis nostrud.\n"]
    console = _SlowConsole(slices, gap=0.15)              # gap > the quick grace (0.06 s)
    monkeypatch.setattr(mm, "msvcrt", console)
    monkeypatch.setattr(mm, "_raw_mode", False)
    monkeypatch.setattr(mm, "_win_raw", True)
    monkeypatch.setattr(mm, "_pending", b"")
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_typed", "")
    monkeypatch.setattr(mm, "_cursor", 0)
    monkeypatch.setattr(mm, "_in_paste", False)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "_drop_until", 0.0)
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")

    assert mm._read_line_raw(paste_ok=_ok) == ""
    assert mm._take_paste_notice() == mm.tr("paste_blocked")   # ONE message for the three slices
    assert console.slices == []                                # …and every slice was swallowed


def _slow_paste(monkeypatch, typed, block, gap=0.1):
    """A block handed over slowly, with `typed` already on the line. Gives back what was printed."""
    out = []
    monkeypatch.setattr(mm.sys, "stdout", type("W", (), {"write": lambda s, t: out.append(t),
                                                         "flush": lambda s: None})())
    monkeypatch.setattr(mm, "_PASTE_SLOW_GRACE", 0.2)     # (the real one is 1 s: keep it short)
    monkeypatch.setattr(mm, "_PASTE_HINT_AFTER", 0.0)     # say it at the first gap
    monkeypatch.setattr(mm, "term_width", lambda default=80: 80)
    console = _SlowConsole([typed + block] if typed else [block], gap=gap)
    monkeypatch.setattr(mm, "msvcrt", console)
    monkeypatch.setattr(mm, "_raw_mode", False)
    monkeypatch.setattr(mm, "_win_raw", True)
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_pending", b"")
    monkeypatch.setattr(mm, "_typed", typed)              # …as if it had just been keyed in
    monkeypatch.setattr(mm, "_cursor", len(typed))
    monkeypatch.setattr(mm, "_in_paste", False)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "_chunk_idle", False)
    monkeypatch.setattr(mm, "_drop_until", 0.0)
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")
    mm._read_line_raw(paste_ok=_ok)
    return "".join(out)


def test_a_slow_paste_says_its_verdict_at_once(monkeypatch):
    """The verdict depends only on what was typed BEFORE the block, so it is known from the
       first character and shown at once on the prompt line (a slow delivery would otherwise
       look like a hang), then wiped once the block is in."""
    block = "Lorem ipsum dolor sit amet.\nSed do eiusmod tempor.\n"

    printed = _slow_paste(monkeypatch, "", block)         # nothing named: it is being dropped
    assert mm.tr("paste_dropping") in printed
    assert printed.rstrip().endswith(mm.PROMPT.rstrip())  # …then wiped, prompt painted back

    printed = _slow_paste(monkeypatch, "c ", block)       # a field named: it is being taken in
    assert mm.tr("paste_reading") in printed
    assert mm.tr("paste_dropping") not in printed


def test_a_bracketed_block_lands_in_one_go_however_it_is_sliced(monkeypatch):
    """REGRESSION (macOS): a big bracketed block crosses the pty in several reads, and each
       read used to be appended as its own little paste — the text appeared in the field
       section by section. An open bracket promises an end marker: the reader waits for it,
       so the block goes in ONCE."""
    writes = []
    monkeypatch.setattr(mm.sys, "stdout", type("W", (), {"write": lambda s, t: writes.append(t),
                                                         "flush": lambda s: None})())
    _prime_reader(monkeypatch, reads=[b"c", b" ",                     # "c " keyed in…
                                      mm.PASTE_BEGIN.encode() + b"First part,",   # …then the block,
                                      b" second part,",                            # in three reads
                                      b" third part" + mm.PASTE_END.encode(),
                                      b"\n"])
    monkeypatch.setattr(mm.select, "select", lambda r, w, x, t=0: ([r], [], []))   # more is coming

    assert mm._read_line_raw(paste_ok=_ok) == "c First part, second part, third part"
    # …and the proof it was ONE stroke: the whole value was echoed by a single write.
    assert any("First part, second part, third part" in w for w in writes)


def test_the_wait_is_bought_only_where_it_buys_something(monkeypatch):
    """The closing silence costs exactly what it lasts, so it is bought only where it buys
       something: a block being TAKEN IN must land in one stroke (long wait); a REFUSED block
       gains nothing from it (late slices are dropped in silence anyway) and the wait would
       cost the user their answer. Read off the timeouts the reader asks the console for."""
    waits = []
    real = mm._input_waiting

    def spy(timeout=0):
        if timeout and timeout != mm._KEY_GAP:        # (the look-ahead's own 15 ms: not the subject)
            waits.append(timeout)
        return real(timeout)

    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    monkeypatch.setattr(mm, "_input_waiting", spy)
    big, small = "Lorem ipsum dolor sit amet.\n" * 20, "Two words\nof it\n"

    _windows_raw(monkeypatch, small, "\r")                            # refused, and short…
    mm._read_line_raw(paste_ok=_ok)
    assert waits == [mm._PASTE_TAIL]                                  # …the short wait

    waits.clear()
    _windows_raw(monkeypatch, big, "\r")                              # refused, but big…
    mm._read_line_raw(paste_ok=_ok)
    assert waits == [mm._PASTE_TAIL]                                  # …the short wait all the same

    waits.clear()
    _windows_raw(monkeypatch, big, "\r")                              # same block, field named:
    monkeypatch.setattr(mm, "_typed", "c ")                           # taken in
    monkeypatch.setattr(mm, "_cursor", 2)
    mm._read_line_raw(paste_ok=_ok)
    assert waits == [mm._PASTE_SLOW_GRACE]


class _DripConsole:
    """The Windows console under a paste: characters handed over ONE at a time, the next always
       ~0.3 ms away, so kbhit() says "not yet" on the first look and "here" a moment later."""
    STEP = 0.0003

    def __init__(self, text):
        self.left = list(text)
        self.ready_at = time.monotonic() + self.STEP

    def kbhit(self):
        return bool(self.left) and time.monotonic() >= self.ready_at

    def getwch(self):
        while self.left and time.monotonic() < self.ready_at:
            pass
        if not self.left:
            return "\r"
        self.ready_at = time.monotonic() + self.STEP
        return self.left.pop(0)


def test_a_console_that_drips_a_paste_is_still_a_paste(monkeypatch):
    """REGRESSION (the original catastrophe reopened): a console that drips a paste one
       character at a time defeats kbhit alone — nothing is ever "waiting" between two of them,
       so the paste passed for typing and its lines ran as commands. Only the CLOCK separates
       them: 0.3 ms between two characters is a machine, 100 ms is a hand."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    monkeypatch.setattr(mm, "_last_key", 0.0)
    block = "a man, a long time ago\nlived by the sea\n"       # "a " = the alias of Artist
    monkeypatch.setattr(mm, "msvcrt", _DripConsole(block))
    monkeypatch.setattr(mm, "_win_raw", True)
    monkeypatch.setattr(mm, "_raw_mode", False)
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_pending", b"")
    monkeypatch.setattr(mm, "_typed", "")
    monkeypatch.setattr(mm, "_cursor", 0)
    monkeypatch.setattr(mm, "_in_paste", False)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "_drop_until", 0.0)
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")

    assert mm._read_line_raw(paste_ok=_ok) == ""              # no command comes out of it…
    assert mm._take_paste_notice() == mm.tr("paste_blocked")  # …it is a paste, and it says so


def test_a_dripped_paste_is_caught_on_its_first_character(monkeypatch):
    """When a character lands alone, the reader waits _KEY_GAP for a second one (a machine
       always has one, a hand never does). Asked FORWARD, the question covers the first
       character of the block, so nothing pasted is ever echoed; asked backward ("did the
       previous one come fast?") the first character was unjudgeable."""
    out = []
    monkeypatch.setattr(mm.sys, "stdout", type("W", (), {"write": lambda s, t: out.append(t),
                                                         "flush": lambda s: None})())
    monkeypatch.setattr(mm, "_last_key", 0.0)
    monkeypatch.setattr(mm, "msvcrt", _DripConsole("Zorglub the block\nand its second line\n"))
    monkeypatch.setattr(mm, "_win_raw", True)
    monkeypatch.setattr(mm, "_raw_mode", False)
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_pending", b"")
    monkeypatch.setattr(mm, "_typed", "")
    monkeypatch.setattr(mm, "_cursor", 0)
    monkeypatch.setattr(mm, "_in_paste", False)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "_drop_until", 0.0)
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")

    assert mm._read_line_raw(paste_ok=_ok) == ""
    printed = "".join(out)
    assert "Z" not in printed                                 # not even its first letter…
    assert "\b" not in printed                                # …so nothing to take back either


class _BulkConsole:
    """A console read in BULK: one call brings back everything it is holding. Each argument is one
       read — a struck key comes back alone, a pasted block comes back whole."""
    def __init__(self, *reads):
        self.reads = [r for r in reads if r]

    def kbhit(self):
        return False                                  # we always block on the read itself

    def read_keys(self):
        if not self.reads:
            raise AssertionError("the reader asked for keys past the end of the script")
        time.sleep(mm._KEY_GAP + 0.005)               # what the console made us wait for
        return self.reads.pop(0)


def _bulk(monkeypatch, *reads):
    console = _BulkConsole(*reads)
    monkeypatch.setattr(mm, "msvcrt", console)
    monkeypatch.setattr(mm, "_win_read_keys", console.read_keys)
    monkeypatch.setattr(mm, "_win_raw", True)
    monkeypatch.setattr(mm, "_raw_mode", False)
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_pending", b"")
    monkeypatch.setattr(mm, "_typed", "")
    monkeypatch.setattr(mm, "_cursor", 0)
    monkeypatch.setattr(mm, "_in_paste", False)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "_drop_until", 0.0)
    monkeypatch.setattr(mm, "_last_key", 0.0)
    monkeypatch.setattr(mm, "_win_flush_input", lambda: False)     # force the reading path
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")
    return console


def test_a_block_read_in_bulk_is_a_burst_and_typing_is_not(monkeypatch):
    """Characters that come out of a SINGLE bulk read arrived together — the structural signal
       POSIX gets from os.read(), no clock needed. A key struck on its own comes back on its
       own: typed."""
    out = []
    monkeypatch.setattr(mm.sys, "stdout", type("W", (), {"write": lambda s, t: out.append(t),
                                                         "flush": lambda s: None})())
    _bulk(monkeypatch, "a man, a long time ago\nlived by the sea\n")     # one read: the block
    assert mm._read_line_raw(paste_ok=_ok) == ""
    assert mm._take_paste_notice() == mm.tr("paste_blocked")
    assert "a man" not in "".join(out)                # not a character of it on the screen

    out.clear()
    _bulk(monkeypatch, "q", "\r")                     # one read per key: struck, and taken as such
    assert mm._read_line_raw(paste_ok=_ok) == "q"
    assert mm._take_paste_notice() is None
    assert "q" in "".join(out)                        # …and echoed, as typing must be


def test_a_refused_block_is_thrown_away_unread(monkeypatch):
    """A block that will be refused whatever it says (the verdict depends only on what was
       typed before it) is not READ: the console buffer is emptied in one call instead of
       dripping three pages at a few milliseconds per character."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    console = _DripConsole("Lorem ipsum dolor.\nsit amet consectetur.\n" * 40)   # a long block
    monkeypatch.setattr(mm, "msvcrt", console)
    monkeypatch.setattr(mm, "_win_raw", True)
    monkeypatch.setattr(mm, "_raw_mode", False)
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_pending", b"")
    monkeypatch.setattr(mm, "_typed", "")
    monkeypatch.setattr(mm, "_cursor", 0)
    monkeypatch.setattr(mm, "_in_paste", False)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "_drop_until", 0.0)
    monkeypatch.setattr(mm, "_last_key", 0.0)
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")
    # The console CAN be emptied (on Windows this is one Win32 call; here we stand in for it).
    flushed = []
    monkeypatch.setattr(mm, "_win_flush_input",
                        lambda: (flushed.append(len(console.left)), console.left.clear(), True)[-1])
    read = []
    inner_getwch = console.getwch
    console.getwch = lambda: (read.append(1), inner_getwch())[-1]

    assert mm._read_line_raw(paste_ok=_ok) == ""
    assert mm._take_paste_notice() == mm.tr("paste_blocked")
    assert flushed and console.left == []                 # emptied by the flush call…
    assert len(read) < 10                                 # …not by reading 1600 characters through


def test_reading_a_pasted_block_costs_no_waiting(monkeypatch):
    """REGRESSION (Windows): the console gives its characters one at a time, so the poll found
       nothing on nearly every look and slept a flat 5 ms before looking again — 5 ms × 1400
       characters = 7 s of sleeping on text already on its way. The poll now spins before it
       sleeps."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    block = "Lorem ipsum dolor sit amet, consectetur adipiscing elit.\n" * 18   # ~1000 chars
    monkeypatch.setattr(mm, "msvcrt", _DripConsole(block))
    monkeypatch.setattr(mm, "_win_raw", True)
    monkeypatch.setattr(mm, "_raw_mode", False)
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_pending", b"")
    monkeypatch.setattr(mm, "_typed", "c ")
    monkeypatch.setattr(mm, "_cursor", 2)
    monkeypatch.setattr(mm, "_in_paste", False)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "_drop_until", 0.0)
    monkeypatch.setattr(mm, "_PASTE_SLOW_GRACE", 0.1)     # the closing wait is not what we measure
    monkeypatch.setattr(mm, "_PASTE_TAIL", 0.1)

    started = time.monotonic()
    raw, _ = mm._collect_paste("L", certain=True)
    took = time.monotonic() - started

    assert len(raw) > 900                                 # the whole block came in…
    assert took < 2.0, f"{len(raw)} characters took {took:.1f}s"   # …and reading it cost nothing
    # Measured on this very block: 6.40 s with the flat nap, 0.58 s without it — and most of THAT
    # is the console's own drip (1000 × 0.3 ms). The bound is loose on purpose: it is there to
    # catch a return to sleeping per character, not to police a tenth of a second.


def test_the_console_poll_looks_again_before_it_sleeps(monkeypatch):
    """The first looks at the console cost no sleep at all; the nap only grows once the wait
       is real."""
    naps = []
    monkeypatch.setattr(mm.time, "sleep", lambda t: naps.append(t))

    class _Console:
        def __init__(self):
            self.looks = 0

        def kbhit(self):
            self.looks += 1
            return self.looks > 4                     # it lands on the fifth look

    monkeypatch.setattr(mm, "msvcrt", _Console())
    monkeypatch.setattr(mm, "_win_raw", True)
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_pending", b"")

    assert mm._input_waiting(0.5) is True
    assert naps[0] == 0.0                             # looked again at once, slept not at all…
    assert max(naps) <= mm._POLL_NAP                  # …and never sleeps long, even later on


def test_not_one_character_of_a_pasted_block_reaches_the_screen(monkeypatch):
    """kbhit says up front whether an arrival carried more than one character, so a paste is
       caught on its FIRST, before any echo — not even the flicker of a letter appearing then
       being taken back."""
    out = []
    monkeypatch.setattr(mm.sys, "stdout", type("W", (), {"write": lambda s, t: out.append(t),
                                                         "flush": lambda s: None})())
    _windows_raw(monkeypatch, "Zorglub the block\nand its second line\n", "\r")
    assert mm._read_line_raw(paste_ok=_ok) == ""
    printed = "".join(out)
    assert "Z" not in printed                             # not even its first letter…
    assert "\b" not in printed                            # …so nothing to take back either


def test_the_late_slices_of_a_refused_block_are_dropped_in_silence(monkeypatch):
    """What lets the wait stay short: slices dribbling in AFTER a refusal used to raise the
       message again, once per slice, each one sending metmux round its loop and through
       exiftool (the freeze). They are now swallowed without a word."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    _windows_raw(monkeypatch, "Lorem ipsum dolor.\nsit amet.\n",      # the block: refused…
                 "consectetur adipiscing.\nelit sed.\n",              # …its tail, arriving late
                 "q", "\r")                                           # …and then the user types

    assert mm._read_line_raw(paste_ok=_ok) == ""                      # refused, once
    assert mm._take_paste_notice() == mm.tr("paste_blocked")

    assert mm._read_line_raw(paste_ok=_ok) == "q"                     # the tail: gone, in silence
    assert mm._take_paste_notice() is None                            # …not a second message
    # The window dies with that keystroke: a dropping window that outlived the command would
    # swallow a deliberate paste made moments later, without a word.
    assert mm._drop_until == 0.0


def test_asking_the_console_for_markers_is_a_no_op_off_windows(monkeypatch):
    """enable_windows_vt_input() is best-effort and nothing is built on it: asked off Windows,
       or refused, it must do nothing and leave the reader to its arrival rule."""
    monkeypatch.setattr(mm, "_WIN_CONSOLE", False)
    assert mm.enable_windows_vt_input() is None
    mm.restore_windows_console_mode(None)                 # …and restoring nothing never raises


def test_windows_keeps_a_command_typed_ahead(monkeypatch):
    """Keys struck while metmux was busy are ALREADY in the buffer when the prompt opens (the
       read does not catch us waiting): judged on shape (short, one line), replayed as typing,
       Enter withheld."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    _windows_raw(monkeypatch, "wipe\n", "\r", queued=True)     # typed while metmux was working
    assert mm._read_line_raw(paste_ok=_ok) == "wipe"           # kept, and it took the user's Enter to run
    assert mm._take_paste_notice() is None


def test_windows_reader_maps_the_arrow_keys(monkeypatch):
    """Windows announces a special key as \\xe0 + a scan code, not as a VT escape: translated at
       the door, so the caret keys work there with the same handlers as on POSIX."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    _windows_raw(monkeypatch, "a", "b", "\xe0K", "X", "\r")     # ← then 'X' inserted before 'b'
    assert mm._read_raw() == "aXb"


class _FakeBuffer:
    """The console buffer of the input() FALLBACK path (a console we could not take over): the
       first line of the paste is already gone into input(), the rest is sitting here, waiting."""
    def __init__(self, waiting=""):
        self.left = list(waiting)

    def kbhit(self):
        return bool(self.left)

    def getwch(self):
        return self.left.pop(0)


def _windows(monkeypatch, waiting=""):
    fake = _FakeBuffer(waiting)
    monkeypatch.setattr(mm, "msvcrt", fake)               # POSIX imports it as None
    monkeypatch.setattr(mm, "_raw_mode", False)           # neither reader: input() owns the line
    monkeypatch.setattr(mm, "_win_raw", False)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")

    class _Stdin:
        def isatty(self): return True
        def fileno(self): return 0
    monkeypatch.setattr(mm.sys, "stdin", _Stdin())
    return fake


def test_windows_multiline_paste_is_refused_whole(monkeypatch):
    """REGRESSION (Windows, input() fallback): input() gave back the paste's first line and the
       console kept the rest, which then ran as a burst of commands. The rest is now drained
       from the console buffer, and nothing runs."""
    fake = _windows(monkeypatch, waiting="lived by the sea\nand wrote it down\n")
    monkeypatch.setattr("builtins.input", lambda: "a man, a long time ago")
    assert mm.ask() == ""                                 # the first line does not run either…
    assert mm._take_paste_notice() == mm.tr("paste_multiline")
    assert fake.left == []                                # …and the rest never reaches the loop


def test_windows_lets_a_typed_command_through(monkeypatch):
    """Nothing waiting in the console buffer = nothing was pasted: the line runs, as always."""
    _windows(monkeypatch)                                 # empty console buffer
    monkeypatch.setattr("builtins.input", lambda: "c My typed comment")
    assert mm.ask() == "c My typed comment"
    assert mm._take_paste_notice() is None


# ──────────────────────────────────────────────────────────────────────────────
#  Regressions (input/dates/formats layer)
# ──────────────────────────────────────────────────────────────────────────────

def test_resolve_colon_in_value_is_not_a_separator():
    """"Field value" whose value contains a ":" (time 14:00, URL, text) must NOT be
       taken for the explicit form "Field : value": the field stays recognised and the
       value comes back whole."""
    tags = ["DateTimeOriginal", "URL", "Comment", "Title"]
    al = mm.aliases_of(tags)
    assert mm.resolve("DateTimeOriginal 25/12/2024 14:00", al, tags, "fr") \
        == ("DateTimeOriginal", "25/12/2024 14:00")
    assert mm.resolve("URL https://example.com", al, tags, "fr") == ("URL", "https://example.com")
    assert mm.resolve("Comment see: here", al, tags, "fr") == ("Comment", "see: here")
    # The explicit form stays intact when the left part IS a known field.
    assert mm.resolve("DateTimeOriginal : 25/12/2024 14:00", al, tags, "fr") \
        == ("DateTimeOriginal", "25/12/2024 14:00")
    # An unknown field stays rejected (no false positive).
    assert mm.resolve("Nonexistent 14:00", al, tags, "fr")[0] is None


def test_to_exif_empty_date_is_erase_not_illegible():
    """Clearing a DATE field from the UI: the empty value is a clear (""), not an
       "unreadable date" (None). A real unreadable date stays None."""
    assert mm.to_exif("", "DateTimeOriginal") == ""
    assert mm.to_exif("", "CreateDate") == ""
    assert mm.to_exif("", "Title") == ""                       # unchanged for non-dates
    assert mm.to_exif("not a date", "DateTimeOriginal") is None
    # Full UI path: "DateTimeOriginal␣" (trailing space) = clear.
    tags = ["DateTimeOriginal", "Title"]
    al = mm.aliases_of(tags)
    tag, val = mm.resolve("DateTimeOriginal ", al, tags, "fr")
    assert (tag, val) == ("DateTimeOriginal", "")
    assert mm.to_exif(val, tag) == ""                          # will write "" = clear


def test_cbz_date_accepts_permissive_input_formats(tmp_path):
    """The Date field of a .cbz accepts the documented input formats (25/12/2024,
       25-12-2024 14h00), not just the canonical colon form."""
    p = make_cbz(tmp_path / "comic.cbz")
    assert mm.cbz_write(p, "Date", "25/12/2024") is True
    assert mm.cbz_read(p).get("Date") == "2024:12:25 00:00:00"
    assert mm.cbz_write(p, "Date", "25-12-2024 14h00") is True
    assert mm.cbz_read(p).get("Date") == "2024:12:25 00:00:00"  # .cbz only stores Y/M/D
    # The already-stored canonical form still works; an unreadable date is refused cleanly.
    assert mm.cbz_write(p, "Date", "2019:05:01 00:00:00") is True
    assert mm.cbz_write(p, "Date", "not a date") is False


def test_cbz_date_preserves_granularity(tmp_path):
    """REGRESSION: a .cbz <Date> is a SEMANTIC_DATE_TAG — a bare year must NOT be padded with
       an invented Month=1/Day=1. Writing a year-only (or year+month) Date must store only the
       components given, and reading them back must not fabricate the missing ones. A later
       full date, then a bare year again, must drop the stale Month/Day."""
    p = make_cbz(tmp_path / "comic.cbz")
    # Bare year: only <Year> is written; Month/Day are absent, and the read stays year-only.
    assert mm.cbz_write(p, "Date", "1982") is True
    root = mm._xml_parse(p, "ComicInfo.xml")
    assert root.find("Year").text == "1982"
    assert root.find("Month") is None and root.find("Day") is None   # no invented precision
    assert mm.cbz_read(p).get("Date") == "1982"

    # Year+month keeps the day out.
    assert mm.cbz_write(p, "Date", "03/1982") is True
    assert mm.cbz_read(p).get("Date") == "1982:03"

    # A full date still round-trips fully…
    assert mm.cbz_write(p, "Date", "15/06/1982") is True
    assert mm.cbz_read(p).get("Date") == "1982:06:15 00:00:00"
    # …and going back to a bare year drops the now-stale Month/Day.
    assert mm.cbz_write(p, "Date", "1999") is True
    root = mm._xml_parse(p, "ComicInfo.xml")
    assert root.find("Month") is None and root.find("Day") is None
    assert mm.cbz_read(p).get("Date") == "1999"


_MUSICXML_NO_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<score-partwise version="4.0">'
    '<movement-title>Old</movement-title>'
    '<part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>'
    '<part id="P1"><measure number="1"/></part>'
    '</score-partwise>')


def test_musicxml_write_keeps_header_order_and_no_dup_title(tmp_path):
    """Creating <work>/<identification> on a score that has none inserts them AT THE
       RIGHT RANK (before part-list/part, work before identification) and keeps the
       <movement-title> consistent (no two divergent titles)."""
    import xml.etree.ElementTree as ET
    p = tmp_path / "p.musicxml"
    p.write_text(_MUSICXML_NO_HEADER, encoding="utf-8")
    assert mm.musicxml_write(p, "Title", "New") is True
    assert mm.musicxml_write(p, "Creator", "Bach") is True
    children = [c.tag for c in ET.parse(p).getroot()]
    assert children.index("work") < children.index("identification") < children.index("part-list")
    assert children.index("part-list") < children.index("part")
    root = ET.parse(p).getroot()
    assert root.find("work/work-title").text == "New"
    assert root.find("movement-title").text == "New"        # no divergent ghost title
    assert mm.musicxml_read(p).get("Title") == "New"
    assert b"Piano" in p.read_bytes()


# ============================================================
#  Section — non-regression
# ============================================================

# --- --mode recognised wherever it is in argv ---

def test_cli_options_order_independent():
    # "file --mode=bogus" must READ the mode (and declare it invalid), not silently
    # fall back to single.
    r = subprocess.run([sys.executable, str(SCRIPT), "file.txt", "--mode=bogus"],
                       capture_output=True, text=True, input="")
    assert r.returncode == 1
    # The offending mode value is echoed in every language: proof it was READ.
    assert "bogus" in r.stdout


# --- ipynb_read does not crash on non-object metadata/root ---

def test_ipynb_read_survives_non_dict_metadata(tmp_path):
    for bad in ("42", "[1,2]", '"x"', "null"):
        p = tmp_path / "n.ipynb"
        p.write_text('{"cells":[],"metadata":%s,"nbformat":4,"nbformat_minor":5}' % bad,
                     encoding="utf-8")
        assert mm.ipynb_read(p) == {}


def test_ipynb_read_survives_non_dict_root(tmp_path):
    p = tmp_path / "n.ipynb"
    p.write_text("[1,2,3]", encoding="utf-8")
    assert mm.ipynb_read(p) == {}


def test_ipynb_write_and_wipe_survive_non_dict_root(tmp_path):
    # REGRESSION: read() opens a non-object-root .ipynb ([1,2,3]) as editable (returns {}),
    # so write()/wipe() were reached with a list and crashed on .setdefault()/.get() —
    # an AttributeError that no session try/except catches, killing the whole program.
    for root in ("[1,2,3]", "42", '"x"', "null"):
        p = tmp_path / "n.ipynb"
        p.write_text(root, encoding="utf-8")
        assert mm.write(p, "Title", "Hello") is False
        assert mm.wipe(p) is False
        assert p.read_text(encoding="utf-8") == root      # file untouched on the refusal


# --- an oversized zip member is refused (anti decompression-bomb) ---

def test_zip_member_size_bound(tmp_path, monkeypatch):
    p = tmp_path / "c.cbz"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ComicInfo.xml", "<ComicInfo><Title>" + "A" * 500 + "</Title></ComicInfo>")
    assert mm._xml_parse(p, "ComicInfo.xml") is not None
    monkeypatch.setattr(mm, "MAX_XML_MEMBER", 10)
    assert mm._xml_parse(p, "ComicInfo.xml") is None


# --- refusal of XML entities (anti billion-laughs / XXE), simple DOCTYPE tolerated ---

def test_xml_reject_entities_unit():
    with pytest.raises(ValueError):
        mm._xml_reject_entities(
            b'<?xml version="1.0"?><!DOCTYPE x [ <!ENTITY a "bomb"> ]><x>&a;</x>')
    # a DOCTYPE WITHOUT an entity (MusicXML, external DTD) must NOT be rejected:
    mm._xml_reject_entities(_MUSICXML_DOCTYPE.encode("utf-8"))


def test_musicxml_billion_laughs_blocked(tmp_path):
    bomb = ('<?xml version="1.0"?>\n<!DOCTYPE score-partwise [\n'
            '<!ENTITY a "AAAAAAAAAA">\n<!ENTITY b "&a;&a;&a;&a;&a;">\n]>\n'
            '<score-partwise><work><work-title>&b;</work-title></work></score-partwise>')
    p = tmp_path / "bomb.musicxml"
    p.write_text(bomb, encoding="utf-8")
    assert mm.musicxml_read(p) in (None, {})


# --- editable date fields normalised; "year only" stored in 4 digits ---

def test_date_tags_completed():
    for t in ("originaldate", "Date", "DigitalCreationDate", "year", "Year"):
        assert t in mm.DATE_TAGS
    # originaldate (mutagen): "25/12/2024" is no longer stored raw but normalised. It is
    # a content date (SEMANTIC_DATE_TAGS): the precision typed is kept (a full date here,
    # so a full date — but no invented 00:00:00 time is appended).
    assert mm.to_exif("25/12/2024", "originaldate") == "2024:12:25"


def test_year_tag_stays_four_digits():
    assert mm._year_str("2024:12:25 00:00:00") == "2024"
    assert mm.format_date("2024", "mutagen") == "2024"           # no invented January 1st
    assert mm.format_date("2019:03:15", "mutagen") == "2019-03-15"
    assert mm.format_date("2019:03:15", "exiftool") == "2019:03:15"


def test_year_field_write_keeps_year_only(tmp_path):
    pytest.importorskip("mutagen")
    from mutagen.flac import FLAC
    p = make_flac(tmp_path / "a.flac")
    canon = mm.to_exif("25/12/2024", "year")                     # like the session: parse first
    assert mm.write(p, "year", canon) is True
    assert FLAC(str(p))["year"] == ["2024"]                      # year only, not "2024-12-25"


# --- content dates (album/film/book) keep the precision typed, never padded ---

def test_semantic_dates_preserve_granularity():
    # A release year stays a year: "1982" must NOT become "1982:01:01 00:00:00" (which
    # format_date would then render "1982-01-01"). Technical timestamps are unaffected.
    for tag in ("date", "Date", "originaldate", "RecordingTime"):
        assert tag in mm.SEMANTIC_DATE_TAGS
        assert mm.to_exif("1982", tag) == "1982"                 # year only, kept
        assert mm.to_exif("03/1982", tag) == "1982:03"           # year+month, kept
        assert mm.to_exif("15/03/1982", tag) == "1982:03:15"     # full date, no invented time
        assert mm.to_exif("15/03/1982 14:30", tag) == "1982:03:15 14:30:00"
    # A technical timestamp keeps the historical full-canonical padding.
    assert mm.to_exif("1982", "DateTimeOriginal") == "1982:01:01 00:00:00"
    assert mm.to_exif("1982", "CreateDate") == "1982:01:01 00:00:00"


def test_parse_date_granular_flag():
    # granular=False (default, technical dates) is unchanged; granular=True truncates.
    assert mm.parse_date("1982") == "1982:01:01 00:00:00"
    assert mm.parse_date("1982", granular=True) == "1982"
    assert mm.parse_date("198203", granular=True) == "1982:03"   # YYYYMM
    assert mm.parse_date("19820315", granular=True) == "1982:03:15"
    assert mm.parse_date("bad", granular=True) is None
    # calendar is still validated in full even when only the year is kept
    assert mm.parse_date("30/02/1982", granular=True) is None    # Feb 30 rejected


def test_format_date_year_month():
    assert mm.format_date("1982:03", "exiftool") == "1982:03"
    assert mm.format_date("1982:03", "mutagen") == "1982-03"
    assert mm.format_date("1982:03", "ffmpeg") == "1982-03"


def test_semantic_date_write_keeps_year_only(tmp_path):
    pytest.importorskip("mutagen")
    from mutagen.flac import FLAC
    p = make_flac(tmp_path / "a.flac")
    assert mm.write(p, "date", mm.to_exif("1982", "date")) is True
    assert FLAC(str(p))["date"] == ["1982"]                      # not "1982-01-01"


def test_bulk_dates_skips_content_dates(monkeypatch):
    # The "dates" command shifts/overwrites the user's technical timestamps only. An
    # album's release year (date, originaldate) is carved in stone: never targeted.
    fixed = {
        "date": "1975", "originaldate": "1999",         # content: must stay untouched
        "CreateDate": "2020:01:01 00:00:00",            # technical: shiftable
        "FileModifyDate": "2020:01:01 00:00:00",        # mtime: shiftable
    }
    monkeypatch.setattr(mm, "read", lambda path, raw=False: dict(fixed))
    monkeypatch.setattr(mm, "writable", lambda path: set(fixed) | set(mm.FILE_BASE_TAGS))
    attempted = []
    monkeypatch.setattr(mm, "write", lambda path, tag, value: (attempted.append(tag) or True))

    mm.cmd_dates(Path("x"), "+2h")
    assert "date" not in attempted and "originaldate" not in attempted
    assert "CreateDate" in attempted
    attempted.clear()
    mm.cmd_dates(Path("x"), "2024")
    assert "date" not in attempted and "originaldate" not in attempted
    assert "CreateDate" in attempted


def test_file_create_date_editability_follows_os_capability():
    # The creation date (btime) is editable only where metmux can actually write it — macOS
    # (setattrlist) and Windows (SetFileTime). On Linux there is no userspace API, so it is
    # read-only. Either way it is never written through exiftool, and the bulk "dates" command
    # never targets it (it is the file's birth, set deliberately).
    writable = mm._FILE_CREATE_WRITABLE
    assert ("FileCreateDate" in mm.FILE_BASE_TAGS) == writable
    assert ("FileCreateDate" in mm.FILE_EXTRA_TAGS) == (not writable)
    assert "FileCreateDate" not in mm.FILE_DATE_TAGS            # never via exiftool
    assert "FileCreateDate" in mm.BULK_DATE_SKIP               # never bulk-shifted
    assert "FileModifyDate" in mm.FILE_DATE_TAGS               # mtime stays editable


def test_set_os_create_date_validates_input():
    # Empty (a file always has a birth time — nothing to clear) and unreadable dates are
    # refused without touching anything.
    assert mm._set_os_create_date(Path("x"), "") is False
    assert mm._set_os_create_date(Path("x"), "pas une date") is False


def test_set_os_create_date_routes_by_platform(monkeypatch):
    # macOS → setattrlist, Windows → SetFileTime, anything else (Linux) → unsupported.
    seen = []
    monkeypatch.setattr(mm, "_set_btime_darwin", lambda p, dt: (seen.append(("mac", dt)), True)[1])
    monkeypatch.setattr(mm, "_set_btime_windows", lambda p, dt: (seen.append(("win", dt)), True)[1])
    monkeypatch.setattr(mm.platform, "system", lambda: "Darwin")
    assert mm._set_os_create_date(Path("x"), "2001:02:03 04:05:06") is True
    assert seen[-1][0] == "mac"
    monkeypatch.setattr(mm.platform, "system", lambda: "Windows")
    assert mm._set_os_create_date(Path("x"), "2001:02:03 04:05:06") is True
    assert seen[-1][0] == "win"
    monkeypatch.setattr(mm.platform, "system", lambda: "Linux")
    assert mm._set_os_create_date(Path("x"), "2001:02:03 04:05:06") is False


def test_set_os_create_date_honours_explicit_offset(monkeypatch):
    # A pasted "...+02:00" fixes the ABSOLUTE instant regardless of the machine's own
    # timezone: 23:15:00+02:00 is 21:15:00 UTC, whatever localtime the runner sits in.
    # (Copying FileModifyDate into FileCreateDate must land the same instant.)
    import datetime
    seen = []
    monkeypatch.setattr(mm, "_set_btime_darwin", lambda p, dt: (seen.append(dt), True)[1])
    monkeypatch.setattr(mm.platform, "system", lambda: "Darwin")
    assert mm._set_os_create_date(Path("x"), "2021:05:09 23:15:00+02:00") is True
    assert seen[-1].timestamp() == datetime.datetime(
        2021, 5, 9, 21, 15, 0, tzinfo=datetime.timezone.utc).timestamp()
    # 'Z' is honoured too, and a value with no offset stays naive (machine-local).
    assert mm._set_os_create_date(Path("x"), "2021:05:09 21:15:00Z") is True
    assert seen[-1].tzinfo == datetime.timezone.utc
    assert mm._set_os_create_date(Path("x"), "2021:05:09 21:15:00") is True
    assert seen[-1].tzinfo is None


def test_to_exif_preserves_offset_on_file_dates_only():
    # A typed UTC offset survives on the absolute-instant file dates (mtime/btime) so it can
    # move the instant; on a naive metadata date it is dropped as before.
    for tag in ("FileCreateDate", "FileModifyDate"):
        assert mm.to_exif("2021:05:09 23:15:00+03:00", tag) == "2021:05:09 23:15:00+03:00"
        assert mm.to_exif("09/05/2021 23:15:00+03:00", tag) == "2021:05:09 23:15:00+03:00"
        assert mm.to_exif("2021:05:09 23:15:00Z", tag) == "2021:05:09 23:15:00+00:00"
        assert mm.to_exif("2021:05:09 23:15:00", tag) == "2021:05:09 23:15:00"   # none typed
        assert mm.to_exif("09/05/2021", tag) == "2021:05:09 00:00:00"            # date, no tz
    assert mm.to_exif("2021:05:09 23:15:00+03:00", "DateTimeOriginal") == "2021:05:09 23:15:00"


def test_typed_offset_reaches_btime_writer(monkeypatch, tmp_path):
    # END-TO-END for the user scenario: typing "fcd ...+03:00" must land the file's birth
    # time at the instant that offset denotes (23:15+03:00 = 20:15 UTC), whatever the runner's
    # own timezone — the offset is no longer silently dropped between typing and the syscall.
    import datetime
    seen = []
    monkeypatch.setattr(mm, "_set_btime_darwin", lambda p, dt: (seen.append(dt), True)[1])
    monkeypatch.setattr(mm.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(mm, "_undo_active", lambda: False)
    f = tmp_path / "photo.jpg"
    f.write_bytes(b"x")
    new_val = mm.to_exif("09/05/2021 23:15:00+03:00", "FileCreateDate")   # the edit handler
    assert mm.write(f, "FileCreateDate", new_val) is True
    assert seen[-1].timestamp() == datetime.datetime(
        2021, 5, 9, 20, 15, 0, tzinfo=datetime.timezone.utc).timestamp()


def test_set_os_create_date_swallows_oserror(monkeypatch):
    # A syscall failure is reported as False, never a traceback that would kill the session.
    def boom(p, dt):
        raise OSError(1, "nope")
    monkeypatch.setattr(mm, "_set_btime_darwin", boom)
    monkeypatch.setattr(mm.platform, "system", lambda: "Darwin")
    assert mm._set_os_create_date(Path("x"), "2001:02:03 04:05:06") is False


def test_write_routes_create_date_to_os_not_exiftool(monkeypatch, tmp_path):
    # A write to FileCreateDate goes to the OS writer, never to exiftool.
    seen = {}
    monkeypatch.setattr(mm, "_set_os_create_date", lambda p, v: (seen.__setitem__("os", (str(p), v)), True)[1])
    monkeypatch.setattr(mm, "et_write", lambda p, t, v: (seen.__setitem__("et", (t, v)), True)[1])
    monkeypatch.setattr(mm, "_undo_active", lambda: False)
    f = tmp_path / "x.jpg"
    f.write_bytes(b"x")
    assert mm.write(f, "FileCreateDate", "2001:02:03 04:05:06") is True
    assert "os" in seen and "et" not in seen
    assert seen["os"][1] == "2001:02:03 04:05:06"


def _stat_for(monkeypatch, marker, ns):
    # Patch os.stat to return a crafted stat for paths ending in `marker`, real otherwise
    # (so pytest's own os.stat calls keep working).
    real_stat = mm.os.stat
    monkeypatch.setattr(mm.os, "stat",
                        lambda p, *a, **k: ns if str(p).endswith(marker)
                        else real_stat(p, *a, **k))


def test_os_create_date_read_from_stat(monkeypatch):
    # metmux reads the creation date from the OS (os.stat().st_birthtime), NOT from exiftool
    # — exiftool does not expose it on macOS. A fixed birth time renders as the local wall
    # clock WITH the local UTC offset, exactly like exiftool's FileModifyDate.
    import datetime, types
    local = datetime.datetime(2026, 6, 21, 22, 14, 9)
    z = local.astimezone().strftime("%z")                        # +0200 / -0500 / +0000
    expected = "2026:06:21 22:14:09" + (f"{z[:3]}:{z[3:]}" if z else "")
    _stat_for(monkeypatch, "birth", types.SimpleNamespace(st_birthtime=local.timestamp()))
    assert mm._os_create_date(Path("f_birth")) == expected


def test_os_create_date_absent_when_unsupported(monkeypatch):
    # No st_birthtime (Linux < Python 3.12, or a filesystem without birth time), or a zero
    # btime: no creation date, so metmux omits the field rather than a dead "(empty)".
    import types
    _stat_for(monkeypatch, "none", types.SimpleNamespace())          # no st_birthtime attr
    assert mm._os_create_date(Path("f_none")) is None
    _stat_for(monkeypatch, "zero", types.SimpleNamespace(st_birthtime=0))
    assert mm._os_create_date(Path("f_zero")) is None


def test_inject_create_date_fills_and_preserves(monkeypatch):
    # Injection surfaces the OS btime as read-only FileCreateDate when absent, and never
    # overrides a value exiftool already provided (the Windows case).
    import datetime, types
    local = datetime.datetime(2021, 9, 5, 21, 15, 0)
    z = local.astimezone().strftime("%z")
    expected = "2021:09:05 21:15:00" + (f"{z[:3]}:{z[3:]}" if z else "")
    _stat_for(monkeypatch, "here", types.SimpleNamespace(st_birthtime=local.timestamp()))
    filled = mm._inject_create_date(Path("f_here"), {"FileModifyDate": "2022:01:01 00:00:00"})
    assert filled["FileCreateDate"] == expected
    kept = mm._inject_create_date(Path("f_here"), {"FileCreateDate": "1999:12:31 00:00:00"})
    assert kept["FileCreateDate"] == "1999:12:31 00:00:00"


# --- et_write passes -sep for list fields (without exiftool installed) ---

def test_et_write_uses_sep_for_list_fields(monkeypatch, tmp_path):
    seen = {}
    def fake_run(*args):
        seen["args"] = args
        return "", True
    monkeypatch.setattr(mm, "et_run", fake_run)
    mm.et_write(tmp_path / "x.jpg", "Keywords", "cat, dog")
    assert "-sep" in seen["args"]
    assert seen["args"][seen["args"].index("-sep") + 1] == ", "
    mm.et_write(tmp_path / "x.jpg", "Title", "Hello")            # non-list field: no -sep
    assert "-sep" not in seen["args"]


@needs_exiftool
def test_cmd_dates_absolute_keeps_typed_offset_for_file_dates(monkeypatch):
    # "dates 25/12/2024 14:00:00+03:00": the typed UTC offset fixes the INSTANT of the
    # file dates (mtime) and must reach their writer — while the naive metadata dates
    # keep the offset-less canonical form. Same rule as to_exif on the per-field path.
    calls = {}

    def fake_write(p, tag, v):
        calls[tag] = v
        return True

    monkeypatch.setattr(mm, "read", lambda p, raw=False: {
        "FileModifyDate": "2024:01:01 00:00:00+00:00", "CreateDate": "2024:01:01 00:00:00"})
    monkeypatch.setattr(mm, "writable", lambda p: {"FileModifyDate", "CreateDate"})
    monkeypatch.setattr(mm, "write", fake_write)
    touched, errors, present = mm.cmd_dates(Path("x.png"), "25/12/2024 14:00:00+03:00")
    assert (touched, errors, present) == (2, 0, 2)
    assert calls["FileModifyDate"] == "2024:12:25 14:00:00+03:00"
    assert calls["CreateDate"] == "2024:12:25 14:00:00"


@needs_exiftool
def test_undo_preserves_list_field_entry_containing_comma(tmp_path):
    # Regression: a pre-existing keyword containing "," ("Earth, Wind & Fire", ONE
    # entry set by another tool) must come back IDENTICAL after an edit then undo.
    # Without care, the capture would flatten the list and et_write would re-split it
    # on "," → 1 entry would become 2, the undo corrupting an untargeted piece of data.
    # The exact list is captured then restored as-is.
    import subprocess
    p = write_png(tmp_path)
    subprocess.run([mm.EXIFTOOL, "-Keywords=Earth, Wind & Fire",
                    "-overwrite_original", str(p)], capture_output=True)
    before = mm.read(p, raw=True).get("Keywords")
    assert before == "Earth, Wind & Fire"                       # indeed ONE entry to start
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.write(p, "Keywords", "Rock") is True
        assert mm._UNDO.undo_last() is True
        assert mm.read(p, raw=True).get("Keywords") == before
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


@needs_exiftool
def test_list_field_append_preserves_comma_entry(tmp_path):
    # REGRESSION: appending a keyword ("Keywords +x") must not split an EXISTING
    # keyword containing a comma. The append builds the exact list instead of re-joining
    # then letting et_write re-split on ",".
    import subprocess
    p = write_png(tmp_path)
    subprocess.run([mm.EXIFTOOL, "-Keywords=alpha", "-Keywords=Paris, France",
                    "-overwrite_original", str(p)], capture_output=True)
    subprocess.run([sys.executable, str(SCRIPT), str(p)],
                   capture_output=True, text=True, input="Keywords +beach\nq\n")
    assert mm.read(p, raw=True).get("Keywords") == ["alpha", "Paris, France", "beach"]


@needs_exiftool
def test_list_field_empty_element_does_not_wipe_others(tmp_path):
    # REGRESSION: an element reduced to "" by _strip_ctrl (a keyword all in control
    # chars, or a "Keywords +<ESC>" append) injected a "-Keywords=" in the middle of the
    # exiftool command, which ERASED the already-set keywords — appending a keyword lost
    # "cat, dog". write() now removes the empty ones.
    p = write_png(tmp_path)
    assert mm.write(p, "Keywords", ["cat", "dog"]) is True
    assert mm.write(p, "Keywords", ["cat", "dog", "\x1b"]) is True    # ESC → "" after strip
    assert mm.read(p, raw=True).get("Keywords") == ["cat", "dog"]
    assert mm.write(p, "Keywords", ["a", "", "b"]) is True
    assert mm.read(p, raw=True).get("Keywords") == ["a", "b"]


def test_list_field_append_on_stdlib_engine_joins_not_crashes(tmp_path):
    # REGRESSION: the append builds a list ([items, new]); a stdlib engine (OOXML…)
    # writes a SINGLE value → write() must JOIN the list, not pass it as-is to
    # ElementTree (.text = list would raise a TypeError and kill the session).
    p = make_docx(tmp_path / "d.docx")
    assert mm.ooxml_write(p, "Subject", "old") is True
    assert mm.write(p, "Subject", ["old", "report"]) is True
    assert mm.ooxml_read(p).get("Subject") == "old, report"


def test_write_strips_control_chars_keeps_xml_valid(tmp_path):
    # REGRESSION: an XML 1.0-illegal control character in a value (pasted from a
    # PDF/terminal) made ElementTree write a non-re-parsable XML — write() returned True,
    # the container was broken and the original data lost. write() now removes these
    # characters before writing.
    import xml.etree.ElementTree as ET, zipfile
    p = make_docx(tmp_path / "d.docx")
    # \x0b (C0 control) AND U+FFFF (BMP non-character) are both illegal in XML 1.0.
    assert mm.write(p, "Title", "AAA\x0bB￿BB") is True
    ET.fromstring(zipfile.ZipFile(p).read("docProps/core.xml"))   # does not raise: valid XML
    assert mm.ooxml_read(p).get("Title") == "AAABBB"
    # a legitimate character of the E000-FFFD range (ligature) is NOT removed
    assert mm._strip_ctrl("aﬁb") == "aﬁb"


@needs_exiftool
def test_write_strips_nul_no_exiftool_crash(tmp_path):
    # REGRESSION: a NUL byte in a value made subprocess raise "ValueError: embedded
    # null byte" uncaught → the session crashed.
    p = write_png(tmp_path)
    assert mm.write(p, "Title", "a\x00b") is True
    assert mm.et_read(p).get("Title") == "ab"


def test_cue_preserves_embedded_quotes_roundtrip(tmp_path):
    # REGRESSION: cue_read did .strip('"') and ate a quote belonging to the value
    # ("say \"hi\"" → "say \"hi"). We only remove ONE wrapping pair; a value without a
    # quote is not touched.
    p = tmp_path / "t.cue"
    p.write_text('FILE "a.wav" WAVE\r\n  TRACK 01 AUDIO\r\n', encoding="utf-8")
    assert mm.cue_write(p, "Title", 'say "hi"') is True
    assert mm.cue_read(p).get("Title") == 'say "hi"'
    assert mm.cue_write(p, "Performer", "Orchestra") is True
    assert mm.cue_read(p).get("Performer") == "Orchestra"


@needs_exiftool
def test_file_access_date_is_read_only_and_visible(tmp_path):
    # FileAccessDate (atime) is never durably writable (exiftool fails) — we SHOW it
    # read-only instead of presenting it editable then failing.
    p = write_png(tmp_path)
    assert "FileAccessDate" not in mm.writable(p)             # read-only
    assert "FileAccessDate" in (mm.read(p, raw=True) or {})   # but visible
    assert "FileModifyDate" in mm.writable(p)                 # the other dates stay editable


# --- _xml_file_save keeps standalone="no" of the MusicXML ---

def test_musicxml_write_preserves_standalone(tmp_path):
    p = tmp_path / "s.musicxml"
    p.write_text(_MUSICXML_DOCTYPE, encoding="utf-8")            # declares standalone="no"
    assert mm.musicxml_write(p, "Title", "New") is True
    raw = p.read_bytes()
    assert b'standalone="no"' in raw                            # preserved
    assert b"<!DOCTYPE score-partwise PUBLIC" in raw            # DOCTYPE still there


# --- in degraded group mode (dedicated engine absent), the audio/video
#           fields are no longer presented as editable ---

def test_group_degraded_hides_dedicated_fields(tmp_path, monkeypatch):
    p = make_flac(tmp_path / "a.flac")                          # routed to mutagen
    monkeypatch.setattr(mm, "engine_available", lambda eng: False)
    assert mm.writable_from_data(p, {}) == set(mm.FILE_BASE_TAGS)


# --- parse_date accepts old dates (< 1900) ---

def test_parse_date_accepts_old_years():
    assert mm.parse_date("15/06/1850") == "1850:06:15 00:00:00"
    assert mm.parse_date("1899") == "1899:01:01 00:00:00"


# --- FR labels disambiguated (no more routing to the wrong field) ---

def test_fr_labels_disambiguated_for_colliding_tags():
    pairs = [("Publisher", "Vendor"), ("Model", "Template"),
             ("organization", "PayloadOrganization"),
             ("AudioChannels", "ChannelMode"),
             ("DateCreated", "CreationDate"),
             ("GPSDateTime", "GPSDateStamp"),
             ("Version", "CFBundleShortVersionString")]
    for a, b in pairs:
        assert mm.FR.get(a) != mm.FR.get(b), f"{a} and {b} still share an FR label"
    # routing by label becomes deterministic ("Organisation" -> organization, not Payload…)
    assert mm._match_tag("Organisation", {}, ["organization", "PayloadOrganization"], "fr") == "organization"


# --- a successful rename logs and returns the new path ---

def test_apply_filename_renames_and_logs(tmp_path, monkeypatch):
    src = tmp_path / "old.txt"
    src.write_text("v2", encoding="utf-8")
    def fake_et_write(path, tag, value):                             # simulates exiftool -FileName=
        if tag == "FileName":
            Path(path).rename(Path(path).with_name(value))
            return True
        return False
    monkeypatch.setattr(mm, "et_write", fake_et_write)
    mm._CHANGELOG.clear()
    new_path, err = mm.apply_filename(src, "new.txt")
    assert err is None and new_path.name == "new.txt"
    assert new_path.exists() and not src.exists()
    assert any(action == "rename" for action, *_ in mm._CHANGELOG)
    mm._CHANGELOG.clear()


# ============================================================
#  Section — Undo (undo / u, ua)
# ------------------------------------------------------------
#  The undo captures the METADATA STATE from before (never a copy of the file):
#  no .bak, cost independent of the file size, bounded to the session.
# ============================================================

def _editable(p):
    """Editable fields present (excluding system dates), to compare a state."""
    data = mm.read(p) or {}
    w = mm.writable(p)
    return {t: v for t, v in data.items()
            if t in w and t not in mm.FILE_BASE_TAGS}


def _answers(monkeypatch, seq):
    """Drives a session: ask() delivers the given sequence (default "q" = quit)."""
    it = iter(seq)
    monkeypatch.setattr(mm, "ask", lambda *a, **k: next(it, "q"))


def test_undo_single_field_roundtrip(tmp_path):
    # Writing a field then u returns EXACTLY the previous value (round-trip).
    p = make_docx(tmp_path / "d.docx")                     # OOXML: pure stdlib
    before = mm.read(p)["Title"]
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.write(p, "Title", "New Title") is True
        assert mm.read(p)["Title"] == "New Title"
        assert mm._UNDO.undo_last() is True
        assert mm.read(p)["Title"] == before
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_restores_absent_field_as_absent(tmp_path):
    # Field ABSENT before -> u re-deletes it (not an empty value).
    p = make_docx(tmp_path / "d.docx")
    assert "Description" not in (mm.read(p) or {})
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.write(p, "Description", "present") is True
        assert mm.read(p).get("Description") == "present"
        assert mm._UNDO.undo_last() is True
        assert "Description" not in (mm.read(p) or {})     # absent, not ""
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_all_restores_initial_state(tmp_path):
    # After N changes, ua restores the initial state of ALL the touched fields.
    p = make_docx(tmp_path / "d.docx")
    initial = _editable(p)
    mm._UNDO = mm.SessionUndo()
    try:
        mm.write(p, "Title", "T2")
        mm.write(p, "Creator", "C2")
        mm.write(p, "Subject", "added")                    # absent to start
        assert _editable(p) != initial
        assert mm._UNDO.undo_all() is True
        assert _editable(p) == initial
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_creates_no_bak_file(tmp_path):
    # No .bak copy (nor sibling file) created next to the file.
    p = make_docx(tmp_path / "d.docx")
    before = {q.name for q in tmp_path.iterdir()}
    mm._UNDO = mm.SessionUndo()
    try:
        mm.write(p, "Title", "X")
        mm._UNDO.undo_last()
        assert {q.name for q in tmp_path.iterdir()} == before
        assert not list(tmp_path.glob("*.bak"))
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_snapshot_cleaned_on_exit(tmp_path):
    # The snapshot lives in a temporary folder OUTSIDE the file, erased on close.
    p = make_docx(tmp_path / "d.docx")
    u = mm.SessionUndo()
    mm._UNDO = u
    try:
        mm.write(p, "Title", "X")
        assert u.dir is not None and u.dir.exists()
        snap_dir = u.dir
        assert tmp_path.resolve() not in snap_dir.resolve().parents
    finally:
        mm._UNDO = None
    u.cleanup()
    assert not snap_dir.exists()


def test_undo_wipe_restores_full_metadata(tmp_path):
    # ua of a wipe restores the previous editable metadata.
    # OOXML pure stdlib, no blob -> complete restoration of the core fields.
    p = make_docx(tmp_path / "d.docx")
    mm.write(p, "Creator", "Alice")
    mm.write(p, "Subject", "Subject")
    before = _editable(p)
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        assert _editable(p) == {}
        assert mm._UNDO.undo_last() is True
        assert _editable(p) == before
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


@needs_exiftool
def test_undo_wipe_restores_full_metadata_exiftool(tmp_path):
    # exiftool engine: the core fields come back (documented imperfect blobs).
    p = _write_sample_jpg(tmp_path / "i.jpg")
    mm.write(p, "Artist", "Ansel")
    mm.write(p, "Copyright", "(C) 2024")
    before = {t: mm.read(p).get(t) for t in ("Artist", "Copyright")}
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        assert (mm.read(p) or {}).get("Artist") in (None, "")
        assert mm._UNDO.undo_last() is True
        data = mm.read(p) or {}
        assert data.get("Artist") == before["Artist"]
        assert data.get("Copyright") == before["Copyright"]
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_filename_restored(tmp_path, monkeypatch):
    # A rename stays undoable (the name is a "previous value").
    # We simulate exiftool -FileName= with a real rename: the proof holds without exiftool.
    def fake_et_write(path, tag, value):
        if tag == "FileName":
            Path(path).rename(Path(path).with_name(value))
            return True
        return False
    monkeypatch.setattr(mm, "et_write", fake_et_write)
    p = tmp_path / "original.txt"
    p.write_text("x", encoding="utf-8")
    mm._UNDO = mm.SessionUndo()
    try:
        new, err = mm.apply_filename(p, "renamed.txt")
        assert err is None and new.name == "renamed.txt"
        assert new.exists() and not p.exists()
        assert mm._UNDO.undo_last() is True
        assert p.exists() and not new.exists()
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_available_in_all_modes(tmp_path, monkeypatch):
    # undo/ua are available AND functional in all 3 modes (single/group/wipe).
    mm._CHANGELOG.clear()

    # --- single: field written then undone via "u" in the REPL ---
    p = make_docx(tmp_path / "single.docx")
    before = mm.read(p)["Title"]
    _answers(monkeypatch, ["Title CHANGED", "u", "q"])
    mm.session_single(p)
    assert mm.read(p)["Title"] == before

    # --- group: same field on a batch, undone with a single "u" ---
    g1 = make_docx(tmp_path / "g1.docx")
    g2 = make_docx(tmp_path / "g2.docx")
    b1, b2 = mm.read(g1)["Title"], mm.read(g2)["Title"]
    _answers(monkeypatch, ["Title CHANGED", "u", "q"])
    mm.session_group([g1, g2])
    assert mm.read(g1)["Title"] == b1 and mm.read(g2)["Title"] == b2

    # --- wipe: confirmed wipe then undone at the result screen ---
    w = make_docx(tmp_path / "w.docx")
    edit_before = _editable(w)
    _answers(monkeypatch, ["y", "u", ""])
    mm.session_wipe([w])
    assert _editable(w) == edit_before

    mm._CHANGELOG.clear()


def test_undo_all_alias(tmp_path, monkeypatch):
    # "undo all" is a synonym of "ua": it undoes ALL the changes, in the single REPL
    # as in group, and at the result screen of the one-shot wipe.
    mm._CHANGELOG.clear()

    # single: two writes of the same field; "undo all" goes back to the initial state
    # (a plain "u" would have undone only the second).
    p = make_docx(tmp_path / "single.docx")
    before = _editable(p)
    _answers(monkeypatch, ["Title FIRST", "Title SECOND", "undo all", "q"])
    mm.session_single(p)
    assert _editable(p) == before

    g1 = make_docx(tmp_path / "g1.docx")
    g2 = make_docx(tmp_path / "g2.docx")
    b1, b2 = _editable(g1), _editable(g2)
    _answers(monkeypatch, ["Title CHANGED", "undo all", "q"])
    mm.session_group([g1, g2])
    assert _editable(g1) == b1 and _editable(g2) == b2

    w = make_docx(tmp_path / "w.docx")
    edit_before = _editable(w)
    _answers(monkeypatch, ["y", "undo all", ""])
    mm.session_wipe([w])
    assert _editable(w) == edit_before

    mm._CHANGELOG.clear()


def test_wipe_command_in_single_and_group(tmp_path, monkeypatch):
    # "wipe" is a command typed in a single/group session, not only a --mode in the
    # terminal; it stays undoable with "u".
    mm._CHANGELOG.clear()

    p = make_docx(tmp_path / "s.docx")
    before = _editable(p)
    assert before
    _answers(monkeypatch, ["wipe", "u", "q"])
    mm.session_single(p)
    assert _editable(p) == before

    # single, without undoing: "wipe" alone does erase the core (core.xml).
    p2 = make_docx(tmp_path / "s2.docx")
    assert "Title" in (mm.read(p2) or {})
    _answers(monkeypatch, ["wipe", "q"])
    mm.session_single(p2)
    assert "Title" not in (mm.read(p2) or {})

    g1 = make_docx(tmp_path / "g1.docx")
    g2 = make_docx(tmp_path / "g2.docx")
    b1, b2 = _editable(g1), _editable(g2)
    _answers(monkeypatch, ["wipe", "y", "u", "q"])
    mm.session_group([g1, g2])
    assert _editable(g1) == b1 and _editable(g2) == b2

    h1 = make_docx(tmp_path / "h1.docx")
    keep = _editable(h1)
    _answers(monkeypatch, ["wipe", "n", "q"])
    mm.session_group([h1])
    assert _editable(h1) == keep

    mm._CHANGELOG.clear()


def test_single_command_switches_group_to_sequential(tmp_path, monkeypatch):
    # "single" in group mode (≥2 files) requests the switch to one-by-one sessions:
    # session_group signals it through its return value, on which main() chains a single
    # session per file (cf. main()).
    g1 = make_docx(tmp_path / "g1.docx")
    g2 = make_docx(tmp_path / "g2.docx")
    _answers(monkeypatch, ["single"])
    assert mm.session_group([g1, g2]) == "single"


def test_single_command_disabled_on_one_file(tmp_path, monkeypatch):
    # On a single file, "single" is pointless: no switch (returns None, not "single"),
    # the session continues until "q", and the file stays intact.
    p = make_docx(tmp_path / "solo.docx")
    keep = _editable(p)
    _answers(monkeypatch, ["single", "q"])
    assert mm.session_group([p]) is None
    assert _editable(p) == keep


def test_relative_shift_on_one_field_single(tmp_path, monkeypatch):
    # "CreateDate +2h" typed in a session shifts THAT stored date only (style and
    # timezone kept, ModifyDate untouched) — and "u" undoes it like any other write.
    p = make_docx(tmp_path / "s.docx")                       # dcterms:created = 2020-01-01T00:00:00Z
    assert mm.write(p, "ModifyDate", "2022:05:05 05:00:00")
    _answers(monkeypatch, ["CreateDate +2h", "q"])
    mm.session_single(p)
    data = mm.read(p) or {}
    assert data.get("CreateDate") == "2020-01-01T02:00:00Z"
    assert data.get("ModifyDate") == "2022-05-05T05:00:00"
    _answers(monkeypatch, ["CreateDate +2h", "u", "q"])      # shift then undo: back to +2h state
    mm.session_single(p)
    assert (mm.read(p) or {}).get("CreateDate") == "2020-01-01T02:00:00Z"
    mm._CHANGELOG.clear()


def test_relative_shift_on_one_field_group(tmp_path, monkeypatch):
    # In group mode, "CreateDate +1d" shifts EACH file from its OWN stored value —
    # the very case the merged "***" display cannot express as one absolute date.
    g1 = make_docx(tmp_path / "g1.docx")
    g2 = make_docx(tmp_path / "g2.docx")
    assert mm.write(g2, "CreateDate", "2021:06:15 08:00:00")
    _answers(monkeypatch, ["CreateDate +1d", "q"])
    mm.session_group([g1, g2])
    assert (mm.read(g1) or {}).get("CreateDate") == "2020-01-02T00:00:00Z"
    assert (mm.read(g2) or {}).get("CreateDate") == "2021-06-16T08:00:00"
    mm._CHANGELOG.clear()


@needs_exiftool
def test_group_view_stars_and_joins_lists(tmp_path, monkeypatch, capsys):
    # SPEC §2 (group): a field whose value differs across files shows "***"; a
    # multi-value field shows joined by ", " — never a Python list repr.
    p1, p2 = write_png(tmp_path, "a.png"), write_png(tmp_path, "b.png")
    assert mm.write(p1, "Title", "Alpha") and mm.write(p2, "Title", "Beta")
    for p in (p1, p2):
        assert mm.write(p, "Keywords", "cat, dog")           # two IPTC keywords (-sep)
    _answers(monkeypatch, ["q"])
    mm.session_group([p1, p2])
    out = capsys.readouterr().out
    assert "***" in out
    assert "cat, dog" in out
    assert "['" not in out
    mm._CHANGELOG.clear()


def test_focus_shows_full_value_untruncated(tmp_path, monkeypatch, capsys):
    # The focus view exists to READ a value the list view truncates (> MAX_LEN):
    # it must print the whole value, never the "[text, N chars]" placeholder again.
    long_title = "x" * 200
    p = make_docx(tmp_path / "f.docx", title=long_title)
    _answers(monkeypatch, ["Title", "", "q"])                # focus, back, quit
    mm.session_single(p)
    assert long_title in capsys.readouterr().out


def test_show_help_is_one_panel_whatever_the_mode(monkeypatch, capsys):
    # ONE panel, the same everywhere: nothing appears or disappears with the mode. The
    # commands that only live in a batch are all there, and the heading of their section
    # carries the condition instead — so what is read once stays true.
    _answers(monkeypatch, [""])
    mm.show_help()
    out = _plain(capsys.readouterr().out)
    for row in ("g | group", "s | single", "→ | n", "← | p", "Ctrl-U",
                "wipe", "u | undo", "ua | undo all", "fr | en", "eu | us", "q | quit"):
        assert row in out
    assert mm.tr("help_sec_nav_if") in out                  # "several files", on the heading
    assert mm.tr("help_paste") in out                       # how a pasted block gets in
    assert mm.tr("help_killline") in out                    # …and how to get rid of it
    # Every section is drawn, each behind its rule.
    for key in ("help_sec_fields", "help_sec_dates", "help_sec_views", "help_sec_nav",
                "help_sec_undo", "help_sec_conf", "help_sec_session"):
        assert f"{mm.tr(key)} ─" in out or f"{mm.tr(key)} (" in out
    # The typed forms carry the localised placeholders, not an English stand-in.
    assert f"dates {mm.tr('help_value')}" in out
    assert f"{mm.tr('help_field')} : {mm.tr('help_value')}" in out


def test_show_help_dates_examples_follow_the_configured_order(monkeypatch, capsys,
                                                              restore_config_globals):
    # The examples are dates: they are written in the order the user reads dates in
    # (config eu/us), like every date on screen. A "25/12/2024" under a US config would
    # teach a form metmux itself would reject.
    monkeypatch.setattr(mm, "DEFAULT_DATE_ORDER", "DMY")
    _answers(monkeypatch, [""])
    mm.show_help()
    out = _plain(capsys.readouterr().out)
    assert "25/12/2024" in out and "12/25/2024" not in out
    monkeypatch.setattr(mm, "DEFAULT_DATE_ORDER", "MDY")
    _answers(monkeypatch, [""])
    mm.show_help()
    out = _plain(capsys.readouterr().out)
    assert "12/25/2024" in out and "25/12/2024" not in out


@pytest.mark.parametrize("lang", ["fr", "en"])
@pytest.mark.parametrize("w", [40, 72, 80, 100, 200])
def test_help_rules_reach_the_panel_edge_and_never_wrap(monkeypatch, capsys, lang, w):
    """The section rules end where the panel's text ends. They used to stop at a hardcoded
       72 columns while the widest line ran to 78 (FR), which left every rule visibly short
       of the text it headed. Now they are measured: they span the panel (all of them
       ending on the SAME column), never overrun the window (one spare column, so they
       never wrap), and clamp to the window when it is narrower than the text."""
    monkeypatch.setattr(mm, "DEFAULT_LANG", lang)
    monkeypatch.setattr(mm, "term_width", lambda default=80, _w=w: _w)
    _answers(monkeypatch, [""])
    mm.show_help()
    lines = _plain(capsys.readouterr().out).split("\n")
    rules = [len(line) for line in lines if "─" in line]
    body = [len(line) for line in lines if line.startswith("  ")]   # the rows and the notes
    assert rules and body
    assert len(set(rules)) == 1                   # every rule ends on the same column
    end = rules[0]
    assert end <= w - 1                           # one spare column: a rule never wraps
    assert end >= min(w - 1, max(body))           # and is never short of the text it heads


def test_view_footer_reads_as_two_labelled_lines():
    # The footer is a small two-line table: what is LOOKED AT on one line (the views, plus
    # g/i on a batch: the two ways to look at it), what WALKS the batch on the next, each
    # behind its own label, both bodies starting on the SAME column.
    nav = mm.tr("nav_footer_arrows")
    footer = mm.view_footer("in", nav, batch="single")
    lines = _plain(footer).splitlines()
    assert len(lines) == 2
    assert lines[0].startswith(mm.tr("footer_view")) and lines[0].endswith("single (s)")
    assert " · ".join(mm.VIEWS) in lines[0]
    assert lines[1].startswith(mm.tr("footer_nav")) and lines[1].endswith(nav)
    assert lines[0].index(mm.VIEWS[0]) == lines[1].index(nav[0])       # aligned label column
    # Two registers, one underline each: the current view, and the current view OF THE BATCH.
    assert f"{mm.UNDERLINE}in{mm.RESET}" in footer
    assert f"{mm.UNDERLINE}single{mm.RESET}" in footer
    assert f"{mm.UNDERLINE}group{mm.RESET}" not in footer
    assert f"{mm.UNDERLINE}group{mm.RESET}" in mm.view_footer("edit", batch="group")
    # No batch (a lone file): neither the g/i pair nor the nav line has anything to say.
    lone = mm.view_footer("edit")
    assert len(_plain(lone).splitlines()) == 1 and "(i)" not in _plain(lone)


def test_walk_single_navigates_both_directions(monkeypatch):
    # walk_single drives an index: "next" advances, "prev" goes back, and BOTH ends
    # clamp — "next" on the last file stays on it (an arrow held one press too long
    # must not dump the user out of the batch); only "quit" leaves. We replace
    # session_single with a script of signals and check the ORDER of the visited
    # files AND the position indicator "i/N".
    visited = []
    signals = iter(["next", "next", "prev", "next", "next", "quit"])

    def fake_session(path, position=None):
        visited.append((path, position))
        return next(signals)

    monkeypatch.setattr(mm, "session_single", fake_session)
    mm.walk_single(["A", "B", "C"])
    assert visited == [
        ("A", (1, 3)),
        ("B", (2, 3)),
        ("C", (3, 3)),
        ("B", (2, 3)),
        ("C", (3, 3)),
        ("C", (3, 3)),
    ]


def test_walk_single_prev_on_first_file_stays(monkeypatch):
    # "prev" on the first file does not exit the batch: we stay on it (max(0, i-1)).
    visited = []
    signals = iter(["prev", "next", "quit"])

    def fake_session(path, position=None):
        visited.append(path)
        return next(signals)

    monkeypatch.setattr(mm, "session_single", fake_session)
    mm.walk_single(["A", "B"])
    assert visited == ["A", "A", "B"]


def test_walk_single_skip_walks_off_the_end(monkeypatch):
    # "skip" (unreadable file) auto-advances WITHOUT clamping: a batch ending in
    # unreadable files exits instead of looping forever on the last one.
    visited = []

    def fake_session(path, position=None):
        visited.append(path)
        return "skip"

    monkeypatch.setattr(mm, "session_single", fake_session)
    assert mm.walk_single(["A", "B"]) is None
    assert visited == ["A", "B"]


def test_undo_reaches_back_across_file_changes(tmp_path, monkeypatch):
    # The undo stack now spans the whole run (run_sessions owns it, _with_undo is
    # reentrant): a change made on file 1 is undone with "u" typed on file 2.
    mm._CHANGELOG.clear()
    f1 = make_docx(tmp_path / "f1.docx")
    f2 = make_docx(tmp_path / "f2.docx")
    before = mm.read(f1)["Title"]
    _answers(monkeypatch, ["Title CHANGED", "n", "u", "q"])
    mm.run_sessions("single", [f1, f2])
    assert mm.read(f1)["Title"] == before
    mm._CHANGELOG.clear()


def test_undo_survives_group_to_single_flip(tmp_path, monkeypatch):
    # A group write is still undoable after flipping to the file-by-file walk ("i"):
    # same stack, "u" pops the whole group batch.
    mm._CHANGELOG.clear()
    g1 = make_docx(tmp_path / "g1.docx")
    g2 = make_docx(tmp_path / "g2.docx")
    b1, b2 = mm.read(g1)["Title"], mm.read(g2)["Title"]
    _answers(monkeypatch, ["Title CHANGED", "s", "u", "q"])
    mm.run_sessions("group", [g1, g2])
    assert mm.read(g1)["Title"] == b1 and mm.read(g2)["Title"] == b2
    mm._CHANGELOG.clear()


@needs_exiftool
def test_commands_ignore_the_case_but_values_never_do(tmp_path, monkeypatch):
    # A command is matched case-insensitively — like the opening screen, the y/N prompts and
    # every field name already were. What is DATA keeps its case: the value written must come
    # back exactly as typed, capitals included.
    p = make_docx(tmp_path / "a.docx")
    _answers(monkeypatch, ["Titre : MacBook Air M3", "ALL", "DATES 25/12/2024 14H00", "Q"])
    monkeypatch.setattr(mm, "DEFAULT_LANG", "fr")
    mm.session_single(p)                                  # "Q" quits: no hang, no backstop needed
    data = mm.read(p)
    assert data["Title"] == "MacBook Air M3"              # value untouched by the folding
    assert data["FileModifyDate"].startswith("2024:12:25 14:00")
    # The batch keys too: "S" flips a group session to the file-by-file walk.
    q1, q2 = make_docx(tmp_path / "q1.docx"), make_docx(tmp_path / "q2.docx")
    _answers(monkeypatch, ["S"])
    assert mm.session_group([q1, q2]) == "single"


def test_g_and_s_shortcuts_switch_modes(tmp_path, monkeypatch):
    # "g" mirrors "group" in a batched single session; "s" mirrors "single" in group —
    # the letter follows the word it stands for. Both are reserved, so no field alias can
    # ever shadow them; "i", which used to be the key, is a plain alias candidate again.
    p = make_docx(tmp_path / "a.docx")
    _answers(monkeypatch, ["g"])
    assert mm.session_single(p, position=(1, 2)) == "group"
    q1 = make_docx(tmp_path / "q1.docx")
    q2 = make_docx(tmp_path / "q2.docx")
    _answers(monkeypatch, ["s"])
    assert mm.session_group([q1, q2]) == "single"
    assert "g" in mm._RESERVED_ALIASES and "s" in mm._RESERVED_ALIASES
    assert "i" not in mm._RESERVED_ALIASES


def test_purged_synonyms_stay_dead_and_exit_still_quits(tmp_path, monkeypatch):
    """The ghost commands purged from the sessions (accepted but shown nowhere) must stay
       dead: typed, they are a failed field lookup, never a command. "exit" is the one
       tolerated survivor — the READMEs document it."""
    p = make_docx(tmp_path / "a.docx")
    _answers(monkeypatch, ["q"])
    single_quit = mm.session_single(p, position=(1, 2))       # what "q" alone returns
    for ghost in ("groupe", "next", "prev", "previous", "individuel", "individual"):
        _answers(monkeypatch, [ghost, "q"])
        assert mm.session_single(p, position=(1, 2)) == single_quit, ghost
    q1, q2 = make_docx(tmp_path / "q1.docx"), make_docx(tmp_path / "q2.docx")
    _answers(monkeypatch, ["q"])
    group_quit = mm.session_group([q1, q2])
    for ghost in ("groupe", "individuel", "individual"):
        _answers(monkeypatch, [ghost, "q"])
        assert mm.session_group([q1, q2]) == group_quit, ghost
    _answers(monkeypatch, ["exit"])
    assert mm.session_single(p, position=(1, 2)) == single_quit
    _answers(monkeypatch, ["exit"])
    assert mm.session_group([q1, q2]) == group_quit


@pytest.mark.parametrize("session", ["single", "group"])
def test_the_shared_commands_land_in_both_sessions(tmp_path, monkeypatch, capsys, session):
    """help, the views, the language and the date order are answered by ONE piece of code
       (_shared_command) that both sessions call — they used to be copied into each, with
       nothing keeping the copies in step. The same commands must land in both sessions and
       be SEEN to land: the view is checked on the SCREEN, not merely accepted."""
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")
    monkeypatch.setattr(mm, "DEFAULT_DATE_ORDER", "DMY")
    saves, helped = [], []
    monkeypatch.setattr(mm, "save_config", lambda *a, **k: saves.append(1) or True)
    monkeypatch.setattr(mm, "show_help", lambda: helped.append(1))
    p1, p2 = make_docx(tmp_path / "a.docx"), make_docx(tmp_path / "b.docx")

    _answers(monkeypatch, ["help", "all", "fr", "us", "q"])
    if session == "single":
        assert mm.session_single(p1) == "quit"
    else:
        assert mm.session_group([p1, p2]) is None
    out = capsys.readouterr().out

    assert helped == [1]                      # the panel was shown
    assert mm.DEFAULT_LANG == "fr"            # the language switched…
    assert mm.DEFAULT_DATE_ORDER == "MDY"     # …and so did the date order
    assert len(saves) == 2                    # each of the two was persisted to config.json
    # The footer underlines the current view: an "all" that never made it into the session
    # leaves that underline unwritten.
    assert f"{mm.UNDERLINE}all{mm.RESET}" in out
    assert f"{mm.UNDERLINE}edit{mm.RESET}" in out          # the opening view, before "all"
    # Labels repainted in the new language: "fr" changing only the global (frame still in
    # English) is the bug a local copy of the language used to allow.
    assert mm.label_of("Title", "fr") in out               # "Titre", after the switch
    assert mm.label_of("Title", "en") in out               # "Title", on the frames before it


def test_live_path_follows_renames(tmp_path):
    # _live_path resolves a stale batch entry through the session's renames, stops
    # as soon as the recorded name exists again (undo), and never loops on a cycle.
    old = tmp_path / "old.docx"
    new = make_docx(tmp_path / "new.docx")
    try:
        mm._RENAMES[str(old)] = new
        assert mm._live_path(old) == new              # old is gone: follow the rename
        make_docx(old)
        assert mm._live_path(old) == old              # old exists again (undone): stay
        old.unlink()
        mm._RENAMES[str(new)] = old                   # cycle old→new→old, neither… new exists
        new.unlink()
        assert mm._live_path(old) in (old, new)       # cycle: terminates, no hang
    finally:
        mm._RENAMES.clear()


@needs_exiftool
def test_walk_reopens_a_renamed_file(tmp_path, monkeypatch, capsys):
    # Rename file 1, go next, come back: the walk follows the rename instead of
    # failing on the stale path ("cannot read" must not appear).
    mm._CHANGELOG.clear()
    f1 = make_docx(tmp_path / "before.docx")
    f2 = make_docx(tmp_path / "other.docx")
    _answers(monkeypatch, ["FileName: after.docx", "n", "p", "q"])
    mm.run_sessions("single", [f1, f2])
    out = _plain(capsys.readouterr().out)
    assert (tmp_path / "after.docx").exists()
    assert mm.tr("cannot_read_file") not in out
    assert "after.docx" in out                        # the renamed file was re-opened
    mm._CHANGELOG.clear()
    mm._RENAMES.clear()


def test_clear_screen_drops_scrollback():
    # REGRESSION: every screen drawn outside render() (help, focus, choice, wipe)
    # must also drop the scrollback (\x1b[3J) — Terminal.app pushes cleared content
    # there, leaving the previous frame readable by scrolling up.
    assert "\x1b[3J" in mm.CLEAR


def test_group_command_switches_single_to_group(tmp_path, monkeypatch):
    # "group" typed in a batched single session asks for the whole-batch view: the
    # session returns the signal (run_sessions re-enters session_group on it) —
    # the mirror of the "single" command in group mode.
    p = make_docx(tmp_path / "g.docx")
    _answers(monkeypatch, ["group"])
    assert mm.session_single(p, position=(1, 2)) == "group"


def test_group_command_disabled_without_batch(tmp_path, monkeypatch):
    # Outside a batch (no position), "group" has nothing to switch to: the session
    # says so and continues (here until "q"), and the file stays intact.
    p = make_docx(tmp_path / "solo.docx")
    keep = _editable(p)
    _answers(monkeypatch, ["group", "q"])
    assert mm.session_single(p) == "quit"
    assert _editable(p) == keep


def test_group_and_single_are_reserved_command_words():
    # The mode-switch commands can never be hijacked by a generated field alias.
    assert "group" in mm._RESERVED_ALIASES
    assert "single" in mm._RESERVED_ALIASES


def test_walk_single_propagates_group_switch(monkeypatch):
    # A "group" signal from any file of the walk stops it and bubbles up to
    # run_sessions, which re-enters the group view on the SAME batch.
    visited = []
    signals = iter(["next", "group"])

    def fake_session(path, position=None):
        visited.append(path)
        return next(signals)

    monkeypatch.setattr(mm, "session_single", fake_session)
    assert mm.walk_single(["A", "B", "C"]) == "group"
    assert visited == ["A", "B"]


@pytest.mark.parametrize("answers,expected", [
    (["g"], "group"),
    (["group"], "group"),
    (["s"], "single"),
    (["single"], "single"),
    (["q"], None),
    (["individuel"], None),      # not a command: re-asks, and the "q" backstop closes
    (["junk", "", "G"], "group"),          # junk re-asks; case-insensitive
])
def test_choose_session_mode_answers(monkeypatch, capsys, answers, expected):
    # The opening choice of --mode=ask accepts EXACTLY what the panel shows (the key and
    # its word). Anything else re-asks, "q" (or EOF) gives up.
    paths = [Path("a.jpg"), Path("b.jpg"), Path("c.jpg")]
    _answers(monkeypatch, answers + ["q"])           # "q" backstop: never hang
    assert mm.choose_session_mode(paths) == expected
    out = _plain(capsys.readouterr().out)
    assert "a.jpg" in out
    for option_line in ("\n  g | group", "\n  s | single", "\n  q | quit"):
        assert option_line in out                    # one option per line, "key | word — label"


def test_choose_panel_answers_one_question_in_the_help_grammar(monkeypatch, capsys):
    # The panel asks ONE thing: whole batch, or file by file. Its rows carry the very
    # commands the session takes; how to WALK the batch (arrows, n/p) lives in the help.
    paths = [Path("a.jpg"), Path("b.jpg")]
    for raw in (True, False):                       # identical panel, terminal or not
        monkeypatch.setattr(mm, "_raw_mode", raw)
        _answers(monkeypatch, ["q"])
        assert mm.choose_session_mode(paths) is None
        out = _plain(capsys.readouterr().out)
        for row in ("g | group", "s | single", "q | quit"):
            assert row in out
        assert "a.jpg" in out and "b.jpg" in out
        assert mm.tr("choose_switch_note") in out   # the choice binds no one
        assert "←" not in out and "n/p" not in out


@pytest.mark.parametrize("lang", ["fr", "en"])
@pytest.mark.parametrize("w", [40, 72, 80, 100, 200])
def test_choose_panel_rule_reaches_the_panel_edge_and_never_wraps(monkeypatch, capsys, lang, w):
    """Same guard as the help panel, on the OTHER panel drawn outside the main view (the
       group/single question): its rule is measured from the formulas that print the rows,
       so a return of the hardcoded-width bug would leave it short of the text it heads."""
    monkeypatch.setattr(mm, "DEFAULT_LANG", lang)
    monkeypatch.setattr(mm, "term_width", lambda default=80, _w=w: _w)
    _answers(monkeypatch, ["q"])
    assert mm.choose_session_mode([Path("a.jpg"), Path("b.jpg")]) is None
    lines = _plain(capsys.readouterr().out).split("\n")
    rules = [len(line) for line in lines if "─" in line]
    panel = [len(line) for line in lines if line.startswith("  ") and ".jpg" not in line]
    assert len(rules) == 1                            # one heading, one rule
    end = rules[0]
    assert end <= w - 1                               # one spare column: the rule never wraps
    assert end >= min(w - 1, max(panel))              # and is never short of the text it heads


def test_choose_session_mode_eof_quits(monkeypatch):
    monkeypatch.setattr(mm, "ask", lambda *a, **k: None)
    assert mm.choose_session_mode([Path("a.jpg"), Path("b.jpg")]) is None


def test_run_sessions_ask_lets_the_user_flip_between_modes(monkeypatch):
    # The dispatch loop: "ask" resolves via the choice screen, then the user can
    # ping-pong — group asks for "single", the walk asks for "group", and the
    # loop only ends when a session returns None. _finish closes the run.
    calls = []
    group_signals = iter(["single", None])
    monkeypatch.setattr(mm, "choose_session_mode", lambda paths: "group")
    monkeypatch.setattr(mm, "session_group",
                        lambda paths: (calls.append("group"), next(group_signals))[1])
    monkeypatch.setattr(mm, "walk_single",
                        lambda paths: (calls.append("single"), "group")[1])
    monkeypatch.setattr(mm, "_finish", lambda: calls.append("finish"))
    mm.run_sessions("ask", [Path("a"), Path("b")])
    assert calls == ["group", "single", "group", "finish"]


def test_run_sessions_ask_single_file_skips_question(monkeypatch):
    # One file: no question, straight to the single session.
    calls = []
    monkeypatch.setattr(mm, "choose_session_mode",
                        lambda paths: calls.append("asked"))
    monkeypatch.setattr(mm, "walk_single",
                        lambda paths: (calls.append("single"), None)[1])
    monkeypatch.setattr(mm, "_finish", lambda: None)
    mm.run_sessions("ask", [Path("a")])
    assert calls == ["single"]


def test_run_sessions_ask_quit_at_question_opens_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(mm, "choose_session_mode", lambda paths: None)
    monkeypatch.setattr(mm, "session_group",
                        lambda paths: calls.append("group"))
    monkeypatch.setattr(mm, "walk_single",
                        lambda paths: calls.append("single"))
    monkeypatch.setattr(mm, "_finish", lambda: calls.append("finish"))
    mm.run_sessions("ask", [Path("a"), Path("b")])
    assert calls == ["finish"]                       # no session was opened


def test_run_sessions_wipe_is_untouched_by_the_loop(monkeypatch):
    calls = []
    monkeypatch.setattr(mm, "session_wipe", lambda paths: calls.append("wipe"))
    monkeypatch.setattr(mm, "_finish", lambda: calls.append("finish"))
    mm.run_sessions("wipe", [Path("a")])
    assert calls == ["wipe"]                         # wipe has its own closing screen


# --- --gather: rendezvous of simultaneous launches (Windows context menu) ---

def test_gather_alone_leads_and_cleans_up(tmp_path):
    # No other instance: the launch leads, waits out the quiet window, and comes
    # back with its own args — leaving no rendezvous directory behind.
    rdv = tmp_path / "rdv"
    assert mm.gather_args(["a.jpg"], rdv=rdv, quiet=0.05, cap=1.0) == ["a.jpg"]
    assert not rdv.exists()


def test_gather_merges_concurrent_launches(tmp_path):
    # Three launches within the quiet window end up in ONE session, in arrival
    # order; the followers hand off (None) and the leader carries the batch.
    rdv = tmp_path / "rdv"
    res = {}

    def lead():
        res["leader"] = mm.gather_args(["a.jpg"], rdv=rdv, quiet=0.4, cap=5.0)

    t = threading.Thread(target=lead)
    t.start()
    deadline = time.monotonic() + 5.0                # let the leader win the mkdir — waiting
    while not rdv.exists() and time.monotonic() < deadline:   # on the fact, not on a fixed nap
        time.sleep(0.01)                             # (a loaded CI outlived the old 0.1 s sleep)
    assert rdv.exists()
    res["f1"] = mm.gather_args(["b.jpg"], rdv=rdv, quiet=0.4, cap=5.0)
    res["f2"] = mm.gather_args(["c.jpg", "d.jpg"], rdv=rdv, quiet=0.4, cap=5.0)
    t.join(timeout=10)
    assert res["leader"] == ["a.jpg", "b.jpg", "c.jpg", "d.jpg"]
    assert res["f1"] is None and res["f2"] is None
    assert not rdv.exists()


def test_gather_follower_drops_json_paths(tmp_path):
    # An open rendezvous (fresh directory) makes the launch a follower: it returns
    # None and its args land as ONE json drop the leader will read.
    rdv = tmp_path / "rdv"
    rdv.mkdir()
    assert mm.gather_args(["x x.jpg", "é.png"], rdv=rdv, quiet=0.05, cap=1.0) is None
    drops = list(rdv.glob("*.paths"))
    assert len(drops) == 1
    assert json.loads(drops[0].read_text(encoding="utf-8")) == ["x x.jpg", "é.png"]


def test_gather_stale_rendezvous_is_reclaimed(tmp_path):
    # A directory left by a crashed leader must not swallow drops forever: too old,
    # it is cleared and the new launch takes the lead (nothing is lost in it: a
    # live rendezvous is always younger than the staleness bound).
    rdv = tmp_path / "rdv"
    rdv.mkdir()
    old = time.time() - mm.GATHER_STALE - 5
    os.utime(rdv, (old, old))
    assert mm.gather_args(["a.jpg"], rdv=rdv, quiet=0.05, cap=1.0) == ["a.jpg"]
    assert not rdv.exists()


def test_gather_lead_orders_drops_by_arrival(tmp_path):
    # The leader's own args come first (it was launched first), then the drops in
    # file-name order — the names start with time_ns, so arrival order.
    rdv = tmp_path / "rdv"
    rdv.mkdir()
    (rdv / "00000000000000000002-9.paths").write_text('["late.jpg"]', encoding="utf-8")
    (rdv / "00000000000000000001-9.paths").write_text('["early.jpg"]', encoding="utf-8")
    merged = mm._gather_lead(rdv, ["lead.jpg"], quiet=0.05, cap=1.0)
    assert merged == ["lead.jpg", "early.jpg", "late.jpg"]
    assert not rdv.exists()


def test_undo_preserves_mutagen_multivalue(tmp_path):
    # REGRESSION: the undo must NOT flatten a mutagen multi-value field.
    # mg_write is not allowed to re-split "," -> the capture keeps the native list.
    p = make_flac(tmp_path / "a.flac")
    f = mm.mg_load(p)
    f["artist"] = ["Artist A", "Bob, Jr.", "Artist C"]      # 3 values, one with a comma
    f.save()
    before = list(mm.mg_load(p)["artist"])
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.write(p, "artist", "Solo") is True
        assert list(mm.mg_load(p)["artist"]) == ["Solo"]
        assert mm._UNDO.undo_last() is True
        assert list(mm.mg_load(p)["artist"]) == before       # 3 values intact, not flattened
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_wipe_preserves_mutagen_multivalue(tmp_path):
    # REGRESSION: the wipe-undo restores the multi-value without flattening it.
    p = make_flac(tmp_path / "a.flac")
    f = mm.mg_load(p)
    f["artist"] = ["A", "B", "C"]
    f["genre"] = ["Rock", "Jazz"]
    f.save()
    before_artist = list(mm.mg_load(p)["artist"])
    before_genre = list(mm.mg_load(p)["genre"])
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        assert "artist" not in (mm.mg_load(p) or {})
        assert mm._UNDO.undo_last() is True
        assert list(mm.mg_load(p)["artist"]) == before_artist
        assert list(mm.mg_load(p)["genre"]) == before_genre
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_wipe_restores_non_editable_fields_mutagen(tmp_path):
    # REGRESSION: the wipe-undo ALSO restores the fields outside the edit whitelist
    # (mood, engineer…) that mutagen knows how to rewrite — not just the ~30 "offered"
    # fields. Otherwise "Wipe cancelled" would be a complete lie.
    p = make_flac(tmp_path / "a.flac")
    f = mm.mg_load(p)
    f["title"] = ["T"]                 # whitelisted
    f["mood"] = ["Melancholic"]        # outside the edit whitelist
    f["engineer"] = ["Bob, Jr."]       # outside the whitelist + value with a comma
    f.save()
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        assert "mood" not in (mm.mg_load(p) or {})
        assert mm._UNDO.undo_last() is True
        g = mm.mg_load(p)
        assert list(g["title"]) == ["T"]
        assert list(g["mood"]) == ["Melancholic"]
        assert list(g["engineer"]) == ["Bob, Jr."]
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


@needs_exiftool
def test_undo_wipe_restores_non_editable_fields_exiftool(tmp_path):
    # REGRESSION: on the exiftool side, the wipe-undo restores the technical EXIF
    # (Make/Model/ISO) outside the edit whitelist, via "exiftool -json=".
    p = _write_sample_jpg(tmp_path / "i.jpg")
    mm.et_run("-Make=Canon", "-Model=EOS 5D", "-ISO=400", "-Artist=Ansel",
              "-overwrite_original", str(p))
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        assert (mm.read(p, raw=True) or {}).get("Make") in (None, "")
        assert mm._UNDO.undo_last() is True
        a = mm.read(p, raw=True)
        assert a.get("Make") == "Canon"
        assert a.get("Model") == "EOS 5D"
        assert str(a.get("ISO")) == "400"
        assert a.get("Artist") == "Ansel"
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_rename_keeps_session_alive(tmp_path, monkeypatch):
    # REGRESSION: undoing a rename in a session restores both the file AND the followed
    # path — the session continues (does not exit on "Cannot read file" of a stale path).
    # The command AFTER the undo must execute.
    def fake_et_write(path, tag, value):
        if tag == "FileName":
            Path(path).rename(Path(path).with_name(value))
            return True
        return False
    monkeypatch.setattr(mm, "et_write", fake_et_write)
    p = make_docx(tmp_path / "d.docx")
    answers = iter(["Title X", "FileName renamed.docx", "u", "Creator AfterUndo", "q"])
    monkeypatch.setattr(mm, "ask", lambda *a, **k: next(answers, "q"))
    mm.session_single(p)
    assert (tmp_path / "d.docx").exists()
    assert not (tmp_path / "renamed.docx").exists()
    assert mm.read(tmp_path / "d.docx").get("Creator") == "AfterUndo"
    assert mm.read(tmp_path / "d.docx").get("Title") == "X"


def test_undo_date_field_mutagen_roundtrip(tmp_path):
    # REGRESSION: undo of a DATE field on the mutagen engine does not crash. The capture
    # returns a list (['2019']); write() passed it to format_date() which expects a
    # string -> TypeError. Must round-trip without raising.
    p = make_flac(tmp_path / "a.flac")
    f = mm.mg_load(p)
    f["date"] = ["2019"]
    f.save()
    before = mm.read(p).get("date")
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.write(p, "date", "2024") is True
        assert mm._UNDO.undo_last() is True
        assert mm.read(p).get("date") == before        # 2019 restored
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


def test_undo_dates_command_mutagen(tmp_path, monkeypatch):
    # REGRESSION: "dates 2024" then "u" on a mutagen audio carrying a date — the session
    # must not crash and the date comes back.
    p = make_flac(tmp_path / "a.flac")
    f = mm.mg_load(p)
    f["date"] = ["2019"]
    f.save()
    before = mm.read(p).get("date")
    answers = iter(["dates 2024", "u", "q"])
    monkeypatch.setattr(mm, "ask", lambda *a, **k: next(answers, "q"))
    mm.session_single(p)
    assert mm.read(p).get("date") == before


def test_dates_command_keeps_the_value_case(tmp_path, monkeypatch):
    # REGRESSION: the batch "dates" command reused the case-folded line as its VALUE, so a
    # typed "+6M" reached parse_offset as "+6m" and shifted the file mtime by 6 MINUTES — the
    # exact ambiguity (month vs minute) the M/m guard rejects. The value must keep its case:
    # "+6M" is refused, nothing is written, like the field-by-field path already does.
    p = make_flac(tmp_path / "a.flac")
    fixed = 1546340400.0                                # 2019-01-01, a stable known mtime
    os.utime(p, (fixed, fixed))
    answers = iter(["dates +6M", "q"])
    monkeypatch.setattr(mm, "ask", lambda *a, **k: next(answers, "q"))
    mm.session_single(p)
    assert abs(os.stat(p).st_mtime - fixed) < 1         # +6M rejected: mtime not shifted 6 min


def test_group_session_survives_a_rename_undone_from_single(tmp_path, monkeypatch):
    # REGRESSION: session_group never refreshed its paths through _live_path (walk_single and
    # run_sessions both do). Rename a file in the single view, flip to group, undo there: the
    # undo renames the file back on disk, but group still held the stale path, read_many hit a
    # missing file and ejected the whole session. The command after the undo must still run.
    def fake_et_write(path, tag, value):
        if tag == "FileName":
            Path(path).rename(Path(path).with_name(value))
            return True
        return False
    monkeypatch.setattr(mm, "et_write", fake_et_write)
    a = make_docx(tmp_path / "a.docx")
    b = make_docx(tmp_path / "b.docx")
    # single: rename a -> renamed, flip to group; group: undo the rename, set a Title, quit.
    answers = iter(["FileName renamed.docx", "g", "u", "Title AfterUndo", "q"])
    monkeypatch.setattr(mm, "ask", lambda *a, **k: next(answers, "q"))
    mm.run_sessions("single", [a, b])
    assert (tmp_path / "a.docx").exists()
    assert not (tmp_path / "renamed.docx").exists()
    assert mm.read(tmp_path / "a.docx").get("Title") == "AfterUndo"   # session lived on
    assert mm.read(tmp_path / "b.docx").get("Title") == "AfterUndo"


# ============================================================
#  Section 23 — Property-based fuzzing (Hypothesis)
# ============================================================
# Instead of hand-written examples, Hypothesis generates hundreds of tortured inputs
# (unicode, control characters, empty or oversized strings) and checks a CONTRACT.
# The edge cases it surfaces are frozen as deterministic regressions above. The FUZZ
# environment variable sets the depth:
#   - absent     : fast search, 100% pure Python (parsers, readers, stdlib writes) —
#                  a few seconds, run on every `pytest`;
#   - FUZZ=N     : at least N examples per test (each keeps its own floor: 200 fast,
#                  80 fast-io, 40/8 slow) AND enables the end-to-end write campaign
#                  via exiftool/ffmpeg/mutagen (slower). E.g.: FUZZ=300 pytest.

import os as _os
import datetime as _dt
from hypothesis import given, strategies as st, settings, HealthCheck, example

_FUZZ = int(_os.environ.get("FUZZ", "0"))
needs_fuzz = pytest.mark.skipif(
    _FUZZ <= 0, reason="end-to-end write campaign (set FUZZ=N to enable)")

# "Nasty" text: the whole multilingual plane + control characters, but without the
# lone surrogates U+D800..U+DFFF (impossible to type on a keyboard; they would cause a
# false positive at the UTF-8 encoding of a file). We exclude them by codepoint range
# rather than by Unicode category, to stay portable across Hypothesis versions.
_FUZZ_CHARS = st.one_of(st.characters(min_codepoint=0, max_codepoint=0xD7FF),
                        st.characters(min_codepoint=0xE000, max_codepoint=0x2FFFF))
_NASTY = st.text(_FUZZ_CHARS, max_size=120)

_SUPPRESS = [HealthCheck.too_slow, HealthCheck.function_scoped_fixture,
             HealthCheck.filter_too_much]
_fast = settings(max_examples=max(200, _FUZZ), deadline=None, suppress_health_check=_SUPPRESS)
_fast_io = settings(max_examples=max(80, _FUZZ), deadline=None, suppress_health_check=_SUPPRESS)
_slow = settings(max_examples=max(40, _FUZZ), deadline=None, suppress_health_check=list(HealthCheck))


# ── Value parsers: return a value or None, NEVER an exception ─────
@_fast
@given(s=_NASTY)
@example(s="1/99999999999/2024")               # OverflowError (_try_make) — found by fuzzing
@example(s="3333333333333333s3333")
def test_fuzz_value_parsers_never_raise(s):
    mm.parse_date(s); mm.parse_date(s, order="MDY"); mm.parse_date(s, order="DMY")
    mm.parse_offset(s); mm.parse_stored_dt(s); mm.to_exif(s, "DateTimeOriginal")
    for eng in ("exiftool", "mutagen", "odf", "epub", "ffmpeg", "ooxml"):
        mm.format_date(s, eng)
    for off in (_dt.timedelta(days=1), _dt.timedelta(days=3_650_000)):
        mm._shift_stored_date(s, off)


# ── stdlib readers: on a malformed file, return dict|None, never raise ─
_STDLIB_READERS = ["m3u", "cue", "geojson", "har", "ipynb", "plist",
                   "eml", "mbox", "musicxml", "tcx", "sqlite"]

@_fast
@given(name=st.sampled_from(_STDLIB_READERS), data=st.binary(max_size=400))
def test_fuzz_stdlib_readers_never_raise(tmp_path, name, data):
    p = tmp_path / f"f.{name}"
    p.write_bytes(data)
    out = getattr(mm, name + "_read")(p)
    assert out is None or isinstance(out, dict)


# ── stdlib writes (zip/text, no external tool): does not raise, file re-readable ─
@_fast_io
@given(raw=_NASTY)
def test_fuzz_stdlib_writers_never_raise(tmp_path, raw):
    for tag, p, reader in (
        ("Subject", make_odt(tmp_path / "d.odt"), mm.odf_read),
        ("Subject", make_docx(tmp_path / "d.docx"), mm.ooxml_read),
        ("Title", make_m3u(tmp_path / "p.m3u"), mm.m3u_read),
        ("Title", make_cue(tmp_path / "c.cue"), mm.cue_read),
    ):
        mm.write(p, tag, raw)                  # must not raise
        assert reader(p) is not None           # file not bricked


# ── END-TO-END write campaign (FUZZ=N): replays the real edit handler ──
def _edit_roundtrip(path, tag, raw_val):
    """Replays metmux.py's edit handler (append "+value" + to_exif + write) then re-reads.
       Contract: no exception, and the file stays readable (never bricked). The re-read
       goes through the file's OWN engine — mm.read() would fall back to the lenient
       exiftool read (name/dates of ANY byte soup) and mask a destroyed file."""
    shown = mm.read(path) or {}
    append = (tag in mm.LIST_FIELDS and isinstance(raw_val, str)
              and raw_val.startswith("+") and len(raw_val) > 1)
    val = raw_val[1:].lstrip() if append else raw_val
    new_val = mm.to_exif(val, tag)
    if new_val is None:                        # "Unreadable date.": nothing written
        return
    if append:
        cur = shown.get(tag)
        items = (list(cur) if isinstance(cur, list)
                 else [cur] if cur not in (None, "") else [])
        new_val = [str(x) for x in items if str(x).strip()] + [new_val]
    mm.write(path, tag, new_val)               # must not raise
    assert mm.ENGINES[mm.engine_for(path)][0](path) is not None   # file not bricked


@needs_fuzz
@needs_exiftool
@_slow
@given(tag=st.sampled_from(["Artist", "Caption", "Copyright",
                            "Keywords", "Subject", "Category", "CreateDate"]),
       raw=_NASTY)
def test_fuzz_write_exiftool_end_to_end(tmp_path, tag, raw):
    _edit_roundtrip(write_png(tmp_path), tag, raw)


@needs_fuzz
@needs_ffmpeg                                  # make_mp3 builds the file via ffmpeg
@_slow
@given(tag=st.sampled_from(["artist", "album", "title", "genre", "comment", "date"]),
       raw=_NASTY)
def test_fuzz_write_mutagen_end_to_end(tmp_path, tag, raw):
    p = make_mp3(tmp_path)
    if p is None:
        pytest.skip("ffmpeg could not build the mp3")
    _edit_roundtrip(p, tag, raw)


@needs_fuzz
@needs_ffmpeg
@settings(max_examples=max(8, _FUZZ), deadline=None, suppress_health_check=list(HealthCheck))
@given(tag=st.sampled_from(["title", "artist", "comment", "date"]), raw=_NASTY)
def test_fuzz_write_ffmpeg_end_to_end(tmp_path, tag, raw):
    p = make_mkv(tmp_path)
    if p is None:
        pytest.skip("ffmpeg could not build the mkv")
    _edit_roundtrip(p, tag, raw)


# ============================================================
#  Section — v1.0.0 release-audit non-regressions
# ============================================================
# 1) Permissions preserved by the atomic rewrites (tmp + replace): a private file
#    (0600) must NOT become world-readable (0644) on a mere metadata edit.

@pytest.mark.skipif(_os.name == "nt", reason="POSIX permission bits")
def test_write_preserves_permissions_stdlib_engines(tmp_path):
    cases = [
        (make_docx(tmp_path / "d.docx"), "Title"),          # _zip_replace
        (make_m3u(tmp_path / "p.m3u"), "Title"),            # _save_text_lines
        (make_cue(tmp_path / "c.cue"), "Title"),            # _save_text_lines
        (make_plist(tmp_path / "p.plist", {"Title": "old"}), "Title"),   # _plist_save
        (make_eml(tmp_path / "m.eml"), "Subject"),          # _eml_save
        (make_geojson(tmp_path / "g.geojson"), "Name"),     # _geojson_save
        (make_musicxml(tmp_path / "s.musicxml"), "Title"),  # _xml_file_save
    ]
    ipynb = tmp_path / "n.ipynb"                            # ipynb_save
    ipynb.write_text('{"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}',
                     encoding="utf-8")
    cases.append((ipynb, "Title"))
    for p, tag in cases:
        _os.chmod(p, 0o600)
        assert mm.write(p, tag, "New"), p.name
        assert (_os.stat(p).st_mode & 0o777) == 0o600, p.name


@pytest.mark.skipif(_os.name == "nt", reason="POSIX permission bits")
@needs_ffmpeg
def test_write_preserves_permissions_ffmpeg(tmp_path):
    p = make_mkv(tmp_path)
    if p is None:
        pytest.skip("ffmpeg could not build the mkv")
    _os.chmod(p, 0o600)
    assert mm.write(p, "title", "New")
    assert (_os.stat(p).st_mode & 0o777) == 0o600


# 1bis) mtime preserved too: metmux exposes FileModifyDate as a field, so editing
#       ANOTHER field must not stamp the file to "now" (exiftool via -P; the
#       tmp+replace engines capture the old mtime and put it back).

def test_write_preserves_mtime_stdlib_engines(tmp_path):
    cases = [
        (make_docx(tmp_path / "d.docx"), "Title"),          # _zip_replace
        (make_m3u(tmp_path / "p.m3u"), "Title"),            # _save_text_lines
        (make_geojson(tmp_path / "g.geojson"), "Name"),     # _geojson_save
    ]
    old = 946684800                                         # 2000-01-01 00:00:00 UTC
    for p, tag in cases:
        _os.utime(p, (old, old))
        assert mm.write(p, tag, "New"), p.name
        assert int(_os.stat(p).st_mtime) == old, p.name


@needs_exiftool
def test_write_preserves_mtime_exiftool(tmp_path):
    p = write_png(tmp_path)
    old = 946684800
    _os.utime(p, (old, old))
    assert mm.write(p, "Title", "New")
    assert int(_os.stat(p).st_mtime) == old                 # -P: mtime untouched
    # Writing the mtime FIELD itself is the deliberate exception.
    assert mm.write(p, "FileModifyDate", "2021:05:09 10:00:00")
    assert int(_os.stat(p).st_mtime) != old


# 1ter) The birth date is preserved too, and for the very same reason. Every engine rewrites the
#       file as a temporary sibling and renames it over the original, so the file that survives is
#       the TEMPORARY — born at the moment of the edit. Windows and macOS keep a btime and metmux
#       shows it as FileCreateDate; the Linux bench keeps none, so a filesystem that does is stood
#       in for below. Only the btime is stood in for: the engines run for real, and the rename
#       that causes the damage is the real one.

class _Birth:
    """A stat result carrying a creation date (os.stat_result is immutable, and has none here)."""

    def __init__(self, st, bt):
        self._st, self.st_birthtime = st, bt

    def __getattr__(self, name):
        return getattr(self._st, name)


class _BirthFS:
    """A filesystem that keeps a creation date, standing in for Windows/macOS. The btime belongs
       to the INODE, as it does there: rename a temporary over a file and the birth date the file
       had is gone with the inode it was on — the survivor is newborn."""

    def __init__(self, monkeypatch, root, born):
        self.births = {}                    # inode -> btime; an inode never seen before is newborn
        self.born = born                    # the birth stamped on the file under test
        self.now = born + 10_000_000        # …and the one a brand-new inode is given: much later
        real_stat = mm.os.stat

        def fake_stat(p, *a, **k):
            st = real_stat(p, *a, **k)
            if not str(p).startswith(str(root)):        # leave the rest of the world alone
                return st
            return _Birth(st, self.births.setdefault(st.st_ino, self.now))

        monkeypatch.setattr(mm.os, "stat", fake_stat)
        monkeypatch.setattr(mm.platform, "system", lambda: "Windows")
        monkeypatch.setattr(mm, "_set_btime_windows", self.set)      # stands in for SetFileTime
        monkeypatch.setattr(mm, "_undo_active", lambda: False)

    def set(self, path, dt):
        self.births[mm.os.stat(path).st_ino] = dt.timestamp()
        return True

    def adopt(self, path):
        self.births[mm.os.stat(path).st_ino] = self.born             # the file was born long ago

    def of(self, path):
        return mm.os.stat(path).st_birthtime


def test_write_preserves_the_creation_date(tmp_path, monkeypatch):
    """REGRESSION (reported on Windows): editing ANY field moved FileCreateDate to the moment
       of the edit, while FileModifyDate stayed put — exactly backwards. The birth date is now
       captured before the rewrite and put back after it."""
    fs = _BirthFS(monkeypatch, tmp_path, born=946684800.0)           # 2000-01-01 00:00:00 UTC
    cases = [
        (make_docx(tmp_path / "d.docx"), "Title"),                   # _zip_replace
        (make_geojson(tmp_path / "g.geojson"), "Name"),              # _geojson_save
        (make_m3u(tmp_path / "p.m3u"), "Title"),                     # _save_text_lines
    ]
    for p, tag in cases:
        fs.adopt(p)
        ino = mm.os.stat(p).st_ino
        assert mm.write(p, tag, "New"), p.name
        assert mm.os.stat(p).st_ino != ino, p.name        # the file really was rewritten…
        assert fs.of(p) == fs.born, p.name                # …and it kept the birth date it had


@needs_exiftool
def test_write_preserves_the_creation_date_exiftool(tmp_path, monkeypatch):
    """exiftool renames its own temporary over the original (-overwrite_original), so a photo
       is reborn just like the rest. -P preserves the mtime; nothing preserved this."""
    fs = _BirthFS(monkeypatch, tmp_path, born=946684800.0)
    p = write_png(tmp_path)
    fs.adopt(p)
    ino = mm.os.stat(p).st_ino
    assert mm.write(p, "Title", "New")
    assert mm.os.stat(p).st_ino != ino
    assert fs.of(p) == fs.born


def test_writing_the_creation_date_still_moves_it(tmp_path, monkeypatch):
    """The deliberate write stays the exception: asking for a new FileCreateDate must SET it. The
       net above must not swallow the very field it protects."""
    fs = _BirthFS(monkeypatch, tmp_path, born=946684800.0)
    p = make_geojson(tmp_path / "g.geojson")
    fs.adopt(p)
    assert mm.write(p, "FileCreateDate", "2021:05:09 10:00:00")
    assert fs.of(p) != fs.born
    assert mm._os_create_date(p).startswith("2021:05:09 10:00:00")


def test_wipe_preserves_the_creation_date(tmp_path, monkeypatch):
    """Clearing a file's metadata does not make the file be born again. (Its mtime does move — a
       wipe rewrites the bytes — but its birth date has no reason to.)"""
    fs = _BirthFS(monkeypatch, tmp_path, born=946684800.0)
    p = make_geojson(tmp_path / "g.geojson")
    fs.adopt(p)
    ino = mm.os.stat(p).st_ino
    assert mm.wipe(p)
    assert mm.os.stat(p).st_ino != ino
    assert fs.of(p) == fs.born


def test_wipe_undo_preserves_the_creation_date(tmp_path, monkeypatch):
    """The undo of a wipe rewrites through the engine paths, which also rename a temporary
       over the original: without the same net as write()/wipe(), the repair re-dated the
       very birth date the wipe had just preserved."""
    fs = _BirthFS(monkeypatch, tmp_path, born=946684800.0)
    monkeypatch.setattr(mm, "_undo_active",
                        lambda: mm._UNDO is not None and not mm._UNDO_RESTORING)
    calls = []                                        # instants stamped back on the file
    real_set = fs.set
    monkeypatch.setattr(mm, "_set_btime_windows",
                        lambda pth, dt: (calls.append(dt.timestamp()), real_set(pth, dt))[1])
    p = make_docx(tmp_path / "d.docx", title="Kept")
    fs.adopt(p)
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p)
        stamps = len(calls)                           # the wipe's own net
        assert mm._UNDO.undo_all() is True
        # (No inode assertion: ext4 recycles the inode the wipe freed, so the repair can
        # land back on it and st_ino proves nothing here.)
        assert len(calls) > stamps                    # the repair re-stamped the birth…
        assert calls[-1] == fs.born                   # …with the instant the file had
        assert fs.of(p) == fs.born
        assert mm.read(p).get("Title") == "Kept"      # the repair itself did repair
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None
        mm._CHANGELOG.clear()


def test_keeping_a_creation_date_never_fails_the_write(monkeypatch):
    """Best-effort by design: the write has already succeeded by the time the birth date is put
       back, so a filesystem with no btime (Linux) or a failing OS writer must not turn a good
       write into a failed one — it can only leave the birth date drifted."""
    assert mm._keep_create_date(Path("x"), None) is False        # nothing captured, nothing to do
    assert mm._keep_create_date(Path("x"), 0) is False           # no real birth time either
    monkeypatch.setattr(mm.platform, "system", lambda: "Linux")
    assert mm._keep_create_date(Path("x"), 946684800.0) is False  # no btime to write on Linux
    seen = []
    monkeypatch.setattr(mm.platform, "system", lambda: "Windows")
    monkeypatch.setattr(mm, "_set_btime_windows", lambda p, dt: (seen.append(dt), True)[1])
    assert mm._keep_create_date(Path("x"), 946684800.0) is True
    assert seen[0].timestamp() == 946684800.0                    # the instant goes back as it came


def test_windows_reads_the_creation_time_from_ctime_before_312(tmp_path, monkeypatch):
    """Windows only grew st_birthtime in Python 3.12 (gh-99726); before that the creation
       time is st_ctime. Without the fallback the whole btime net was a no-op on Windows
       with Python 3.8-3.11 — every edit re-dated the file's birth, the very bug the net
       exists to stop. POSIX never falls back: st_ctime is the inode-change time there."""
    p = tmp_path / "x.txt"
    p.write_text("x")
    class _NTStat:
        st_ctime = 1234567890.0                       # and no st_birthtime attribute
    real_stat = mm.os.stat
    monkeypatch.setattr(mm.os, "stat",
                        lambda q, *a, **k: _NTStat() if str(q) == str(p)
                        else real_stat(q, *a, **k))
    real_name = mm.os.name
    monkeypatch.setattr(mm.os, "name", "nt")
    got_nt = mm._os_create_ts(p)
    monkeypatch.setattr(mm.os, "name", real_name)     # back before asserting: pytest's own
    assert got_nt == 1234567890.0                     # failure report instantiates Path()
    assert mm._os_create_ts(p) is None                # POSIX never falls back to st_ctime


# 2) Real line endings only: a 0x85 byte ("…" in cp1252, U+0085 once read as
#    latin-1) INSIDE an m3u media path or a cue FILE line must not be split in two
#    by the loader (str.splitlines() did), mutilating the entry at the next save.

def test_m3u_path_with_nel_byte_survives_title_edit(tmp_path):
    p = tmp_path / "pl.m3u"
    p.write_bytes(b"#EXTM3U\r\nmy song\x85final.mp3\r\n")
    assert mm.m3u_write(p, "Title", "New")
    assert b"my song\x85final.mp3" in p.read_bytes()


def test_cue_file_line_with_nel_byte_survives_title_edit(tmp_path):
    p = tmp_path / "c.cue"
    p.write_bytes(b'FILE "my album\x85 disc.wav" WAVE\r\n  TRACK 01 AUDIO\r\n')
    assert mm.cue_write(p, "Title", "New Album")
    assert b'FILE "my album\x85 disc.wav" WAVE' in p.read_bytes()


# 3) The wipe caveat must cover images: the undo snapshot cannot round-trip the
#    binary blobs (IFD1 thumbnail, MakerNotes) — the user is told at wipe time,
#    exactly as for audio/video and PDF.

def test_wipe_caveat_covers_images():
    note = mm._wipe_caveat([Path("photo.jpg")])
    assert mm.tr("caveat_img") in note
    assert mm._wipe_caveat([Path("doc.txt")]) == ""
    # Same honesty for the other lossy cases: PDF metadata stays technically
    # recoverable (exiftool's incremental update), and the audio/video undo cannot
    # restore cover art, per-track metadata or chapters.
    assert mm.tr("caveat_pdf") in mm._wipe_caveat([Path("doc.pdf")])
    assert mm.tr("caveat_av") in mm._wipe_caveat([Path("movie.mkv")])
    assert mm.tr("caveat_av") in mm._wipe_caveat([Path("song.mp3")])


# 4) A corrupted .docx (not even a zip) must be REFUSED by the ooxml engine
#    (None -> external-data fallback), not opened as an empty editable session.
#    A readable zip without docProps/core.xml stays a valid container ({}).

def test_ooxml_read_refuses_non_zip(tmp_path):
    p = tmp_path / "garbage.docx"
    p.write_bytes(b"\x00\x01NOTAZIP" * 40)
    assert mm.ooxml_read(p) is None


def test_ooxml_read_zip_without_core_is_empty_not_refused(tmp_path):
    p = tmp_path / "bare.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    assert mm.ooxml_read(p) == {}


# 5) Mutagen/ASF hardening: .wma tags must be READABLE (attribute objects were
#    crashing mg_read into {}), and wma/aiff/aif content stays read-only (easy keys
#    land in non-standard ASF attributes / raw ID3 frames — silent-loss writes).

def _make_wma(tmp_path):
    out = tmp_path / "audio.wma"
    r = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=8000",
         "-t", "0.1", "-c:a", "wmav2", "-metadata", "title=Real Title", "-y", str(out)],
        capture_output=True)
    return out if r.returncode == 0 and out.exists() else None


@needs_ffmpeg
def test_mg_read_wma_tags_visible(tmp_path):
    p = _make_wma(tmp_path)
    if p is None:
        pytest.skip("ffmpeg could not build the wma")
    data = mm.mg_read(p)
    assert data, "ASF read must not degrade to {}"
    assert any("Real Title" in v for v in data.values())


def test_wma_aiff_content_is_readonly():
    for name in ("x.wma", "x.aiff", "x.aif"):
        assert mm.mg_writable(Path(name)) == set()
        assert mm.mg_write(Path(name), "title", "X") is False
        assert mm.mg_wipe(Path(name)) is False


# 6) Undo snapshots must stay JSON-serializable: APEv2/ASF values are attribute
#    OBJECTS; the wipe of a .wv crashed inside dump_snapshot (TypeError), killing
#    the session mid-batch with no undo prompt.

def _make_wv(tmp_path):
    out = tmp_path / "audio.wv"
    r = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=8000",
         "-t", "0.1", "-c:a", "wavpack", "-metadata", "title=WV Title", "-y", str(out)],
        capture_output=True)
    return out if r.returncode == 0 and out.exists() else None


@needs_ffmpeg
def test_wipe_undo_wv_snapshot_serializable(tmp_path):
    p = _make_wv(tmp_path)
    if p is None:
        pytest.skip("ffmpeg could not build the wv")
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True                  # crashed before the fix
        assert not (mm.mg_read(p) or {})
        mm._UNDO.undo_all()
        data = mm.mg_read(p) or {}
        assert any("WV Title" in v for v in data.values())
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None


# 7) CBZ wipe→undo must restore ComicInfo.xml BIT-FOR-BIT: fields outside CBZ_TAGS
#    (Colorist, <Pages>…) were lost forever (snapshot only captured the whitelist).

_RICH_COMICINFO = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<ComicInfo><Title>T</Title><Series>S</Series>"
    "<Colorist>Gregory Wright</Colorist><Inker>Tim Sale</Inker>"
    "<Notes>Scanned by ACME</Notes><PageCount>2</PageCount>"
    '<Pages><Page Image="0" Type="FrontCover"/><Page Image="1"/></Pages>'
    "</ComicInfo>")


def test_cbz_wipe_undo_restores_comicinfo_bit_for_bit(tmp_path):
    p = tmp_path / "comic.cbz"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("ComicInfo.xml", _RICH_COMICINFO)
        z.writestr("page001.png", PNG_BYTES)
    with zipfile.ZipFile(p) as z:
        before = z.read("ComicInfo.xml")
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        with zipfile.ZipFile(p) as z:
            assert b"Colorist" not in z.read("ComicInfo.xml")
        mm._UNDO.undo_all()
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None
    with zipfile.ZipFile(p) as z:
        assert z.read("ComicInfo.xml") == before
        assert z.read("page001.png") == PNG_BYTES


# 8) OOXML wipe must also clear docProps/custom.xml (custom document properties
#    silently survived), and the undo must bring it back bit-for-bit.

_CUSTOM_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"'
    ' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
    '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="ClientCode">'
    "<vt:lpwstr>ACME-42</vt:lpwstr></property></Properties>")


def test_ooxml_wipe_clears_custom_properties_and_undo_restores(tmp_path):
    p = make_docx(tmp_path / "d.docx")
    assert mm._zip_replace(p, "docProps/custom.xml", _CUSTOM_XML.encode())
    with zipfile.ZipFile(p) as z:
        before = z.read("docProps/custom.xml")
    mm._UNDO = mm.SessionUndo()
    try:
        assert mm.wipe(p) is True
        with zipfile.ZipFile(p) as z:
            assert b"ClientCode" not in z.read("docProps/custom.xml")
        mm._UNDO.undo_all()
    finally:
        mm._UNDO.cleanup()
        mm._UNDO = None
    with zipfile.ZipFile(p) as z:
        assert z.read("docProps/custom.xml") == before


# 9) Writing must never inflate a whole zip member in RAM: a trapped member of a
#    few hundred KB on disk announcing hundreds of MB exhausted the memory. The
#    copy now streams; peak RSS of a fresh process doing the write stays bounded.

@pytest.mark.skipif(_os.name == "nt", reason="the resource module is POSIX-only")
def test_zip_write_memory_bounded_on_trapped_member(tmp_path):
    p = make_docx(tmp_path / "trap.docx")
    chunk = b"\0" * (1024 * 1024)
    with zipfile.ZipFile(p, "a", zipfile.ZIP_DEFLATED) as z:
        with z.open(zipfile.ZipInfo("word/media/trap.bin"), mode="w",
                    force_zip64=True) as dst:
            for _ in range(384):                    # announces 384 MiB, ~400 KB on disk
                dst.write(chunk)
    child = (
        "import resource, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(mm.__file__).parent)!r})\n"
        "import metmux\n"
        f"ok = metmux.ooxml_write(Path({str(p)!r}), 'Title', 'x')\n"
        # getrusage's ru_maxrss unit differs by OS: BYTES on macOS, KiB on Linux —
        # normalise to MiB in the child so the assertion holds on both.
        "peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss\n"
        "mib = peak / (1024 * 1024) if sys.platform == 'darwin' else peak / 1024\n"
        "print('OK' if ok else 'FAIL', mib)\n")
    # -B on the CHILD too: the parent's -B is a flag of the parent process, it does not cross
    # into a subprocess. Without it this child imports metmux as a module and drops a
    # __pycache__/metmux.*.pyc in the repository — the bench must leave no trace on the disk.
    r = subprocess.run([sys.executable, "-B", "-c", child], capture_output=True, text=True)
    status, peak_mib = r.stdout.split()
    assert status == "OK", r.stderr
    assert float(peak_mib) < 200, f"peak RSS {peak_mib} MiB: member inflated in RAM"
    with zipfile.ZipFile(p) as z:                  # trap member copied intact
        assert z.testzip() is None
        assert z.getinfo("word/media/trap.bin").file_size == 384 * 1024 * 1024
    assert mm.ooxml_read(p).get("Title") == "x"


# 10) A pre-existing USER file named "X.ext.tmp" must survive an edit of X.ext —
#     both a successful write and a FAILED one (the failure path deleted it).

def test_user_tmp_sibling_survives_write(tmp_path):
    p = make_docx(tmp_path / "report.docx")
    user_tmp = tmp_path / "report.docx.tmp"
    user_tmp.write_bytes(b"user data, do not destroy")
    assert mm.write(p, "Title", "New") is True
    assert user_tmp.read_bytes() == b"user data, do not destroy"

    corrupt = tmp_path / "corrupt.docx"
    corrupt.write_bytes(b"NOT A ZIP")
    user_tmp2 = tmp_path / "corrupt.docx.tmp"
    user_tmp2.write_bytes(b"precious")
    assert mm.ooxml_write(corrupt, "Title", "x") is False
    assert user_tmp2.read_bytes() == b"precious"


# 11) ooxml_wipe must FAIL on a non-zip file: returning True was a false "cleaned"
#     assurance (a mis-named .docx whose real metadata survives), unlike its zip
#     siblings (cbz/odf/epub/kmz) which all return False.

def test_ooxml_wipe_refuses_non_zip(tmp_path):
    p = tmp_path / "fake.docx"
    p.write_bytes(b"{\\rtf1 actually an RTF, not a zip}" * 4)
    before = p.read_bytes()
    assert mm.ooxml_wipe(p) is False
    assert p.read_bytes() == before                # untouched, no false success


def test_ooxml_wipe_succeeds_on_real_docx(tmp_path):
    p = make_docx(tmp_path / "real.docx")
    assert mm.ooxml_wipe(p) is True                # regression guard: valid docx still wiped
    assert mm.ooxml_read(p) == {}


# ──────────────────────────────────────────────────────────────────────────────
#  Stable, resize-responsive display (SPEC §2 "Affichage stable et réactif au
#  redimensionnement" + §4 seam): rendering, resize, and the raw cbreak reader.
# ──────────────────────────────────────────────────────────────────────────────

import codecs as _codecs_rt


def _prime_reader(monkeypatch, pending=b"", reads=()):
    """Arm the raw reader: `pending` sits in the byte buffer (as if it had arrived in ONE read),
       then os.read() hands back the frames of `reads`, then EOF."""
    monkeypatch.setattr(mm, "_raw_mode", True)
    monkeypatch.setattr(mm, "_pending", pending)
    monkeypatch.setattr(mm, "_char_buf", "")
    monkeypatch.setattr(mm, "_in_paste", False)
    monkeypatch.setattr(mm, "_typed", "")
    monkeypatch.setattr(mm, "_cursor", 0)
    monkeypatch.setattr(mm, "_paste_notice", None)
    monkeypatch.setattr(mm, "_drop_until", 0.0)
    monkeypatch.setattr(mm, "_decoder", _codecs_rt.getincrementaldecoder("utf-8")(errors="surrogateescape"))
    seq = iter(reads)
    monkeypatch.setattr(mm.os, "read", lambda fd, n: next(seq, b""))
    monkeypatch.setattr(mm.select, "select", lambda r, w, x, t=0: ([], [], []))   # tty silent


def _prime_raw(monkeypatch, data, reads=None):
    """TYPING: every byte of `data` comes back from its OWN os.read(), which is what a terminal
       does between keystrokes (the reader sits blocked in read()). It matters: two characters
       out of one read is precisely how the paste guard recognises a paste, so priming a whole
       line in one frame would now (rightly) be refused as one. `reads` appends extra frames —
       used to split a multibyte character across two reads."""
    frames = [data[i:i + 1] for i in range(len(data))] + list(reads or [])
    _prime_reader(monkeypatch, b"", frames)


def _prime_paste(monkeypatch, typed, pasted, after=b""):
    """A PASTE: `typed` is keyed in character by character, then `pasted` lands whole, in one
       read — the burst a terminal delivers when the clipboard is dropped into the prompt —
       then `after` is keyed in, character by character again (the Enter that validates it)."""
    frames = ([typed[i:i + 1] for i in range(len(typed))] + [pasted]
              + [after[i:i + 1] for i in range(len(after))])
    _prime_reader(monkeypatch, b"", frames)


class _Sink:                                     # swallow the reader's echo (it writes to stdout)
    def write(self, s): return len(s)
    def flush(self): pass


def _reset_frame(monkeypatch):
    monkeypatch.setattr(mm, "_IN_ALT", False)
    monkeypatch.setattr(mm, "_LAST_FRAME", None)
    monkeypatch.setattr(mm, "_rendering", False)
    monkeypatch.setattr(mm, "_typed", "")
    monkeypatch.setattr(mm, "_edit_scroll", 0)
    monkeypatch.setattr(mm, "_edit_max_scroll", 0)
    monkeypatch.setattr(mm, "_edit_page", 1)
    monkeypatch.setattr(mm, "_mouse_on", False)


def test_term_width_live(monkeypatch):
    """On a resized pty (TIOCSWINSZ), term_width() reflects the NEW width live and ignores
       a stale COLUMNS env var (a running process keeps COLUMNS at its launch value)."""
    import os
    import termios
    import fcntl
    import struct
    if os.name != "posix" or not hasattr(termios, "TIOCSWINSZ"):
        pytest.skip("no pty / TIOCSWINSZ on this platform")
    master, slave = os.openpty()
    try:
        class _Stream:                                # a stream whose fileno() is the pty
            def __init__(self, fd): self._fd = fd
            def fileno(self): return self._fd
        monkeypatch.setattr(mm.sys, "stdout", _Stream(slave))
        monkeypatch.setenv("COLUMNS", "9999")         # stale: term_width must ignore it

        def resize(cols, rows=24):
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

        resize(123)
        assert mm.term_width() == 123                 # live, from the tty (not COLUMNS=9999)
        resize(66)
        assert mm.term_width() == 66                  # a second resize shows up immediately
    finally:
        os.close(master)
        os.close(slave)


def test_rule_never_wraps(capsys, monkeypatch):
    """For a range of widths (40→200) and category names (short + the longest,
       "Personnes & droits"), the category rule always fits on ONE line with at least one
       spare column on the right — it never wraps onto a second line."""
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")     # head == the name as given (no HEADER_FR remap)
    _reset_frame(monkeypatch)
    names = ["Fichier", "Personnes & droits"]         # a short label and the longest one
    for w in range(40, 201, 8):
        monkeypatch.setattr(mm, "term_width", lambda default=80, _w=w: _w)
        for name in names:
            monkeypatch.setattr(mm, "_IN_ALT", False)
            mm.render("T", "s", [(None, name, None, None)], None)
            out = capsys.readouterr().out
            rule_line = next(line for line in out.split("\n") if name in line)
            rule_len = rule_line.count("─")
            # visible width of the rule line = len(name) + 1 space + rule; must be <= w-1
            assert rule_len == 0 or len(name) + 1 + rule_len <= w - 1


def test_render_edit_view_resets_once_then_paints_in_place(capsys, monkeypatch):
    """The edit view runs in the NORMAL screen buffer (the alternate buffer stranded the frame
       far down the page on macOS Terminal.app). The FIRST edit frame resets the terminal
       (\\033c) so it lands at the top; a same-view redraw does NOT reset again and never
       switches to the alternate screen buffer (\\033[?1049h/l)."""
    _reset_frame(monkeypatch)
    mm.render("T", "s", [], None)
    first = capsys.readouterr().out
    assert "\033c" in first                            # reset once, so the frame lands at the top
    assert "\033[?1049h" not in first                  # never the alternate screen buffer
    mm.render("T", "s", [], None)
    second = capsys.readouterr().out
    assert "\033c" not in second                       # no second reset on a same-view redraw
    assert "\033[?1049h" not in second and "\033[?1049l" not in second


def test_terminal_reset_re_arms_bracketed_paste(capsys, monkeypatch):
    """REGRESSION (root cause of the paste mess): \\033c is a FULL terminal reset, so it also
       turned bracketed paste back OFF — metmux killed its own paste detection on the first
       frame it drew, on every platform. The reset must re-assert the mode."""
    _reset_frame(monkeypatch)
    mm.render("T", "s", [], None)
    first = capsys.readouterr().out
    assert first.index("\033c") < first.index(mm.PASTE_ON)   # re-armed after the reset, not before


def test_render_repaints_in_place_no_screen_wipe(capsys, monkeypatch):
    """A same-view redraw must NOT wipe the VISIBLE screen (\\033[2J): that full erase blanks the
       display for a beat before repainting — the "jump" where the top line vanishes on every
       validated edit. Instead it homes the cursor (\\033[H) and overwrites in place, erasing
       each line's stale tail (\\033[K) and the leftover rows below (\\033[J). It DOES drop the
       saved scrollback (\\033[3J) so a resize cannot spill a copy of the frame into the history —
       that is invisible and causes no flash."""
    _reset_frame(monkeypatch)
    rows = [(None, "Description", None, None), ("Title", "ti", "Hello", True)]
    mm.render("Doc", "sub", rows, None)               # first frame (enters the edit view)
    capsys.readouterr()                               # discard it
    mm.render("Doc", "sub", rows, None)               # same view again: the redraw under test
    second = capsys.readouterr().out
    assert "\033[2J" not in second                    # no VISIBLE wipe → no flash
    assert "\033[3J" in second                         # saved scrollback dropped → a resize can't stack a copy
    assert "\033[H" in second                         # home, then overwrite in place
    assert "\033[K" in second                         # per-line erase (a shrunk value leaves no tail)
    assert "\033[J" in second                         # drop leftover rows of a taller previous frame


def test_render_clamps_tall_frame_to_height(capsys, monkeypatch):
    """A frame taller than the terminal is clamped to term_height(): painting more lines than
       the screen has rows scrolls it, \\033[H drifts off the top and every redraw stacks
       another copy into the scrollback (the runaway bug). The clamped frame keeps the title
       on top, the prompt on the bottom, and shows a window over the fields — no scroll
       indicator, the bottom fields are simply off-window."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    rows = [(None, "Cat", None, None)] + [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]

    def visual_lines(out):                                # \n only comes from the "\033[K\n" join
        return out.count("\n") + 1

    mm.render("Doc", "sub", rows, None)
    first = capsys.readouterr().out
    assert visual_lines(first) <= 10                      # fits the screen: no scroll, no stacking
    assert "Doc" in first                                 # title kept on top…
    assert f"{mm.BOLD}>{mm.RESET} " in first               # …prompt kept at the bottom
    assert "Field0" in first                              # window starts at the top of the list…
    assert "Field29" not in first                         # …the bottom fields are off-window
    assert "▲" not in first and "▼" not in first          # no scroll indicators

    # A second same-view redraw is byte-identical in shape: it does NOT grow (no stacking)
    # and still paints in place (home, no full-screen wipe).
    mm.render("Doc", "sub", rows, None)
    second = capsys.readouterr().out
    assert visual_lines(second) == visual_lines(first)
    assert "\033[H" in second and "\033[2J" not in second


def test_render_edit_uses_full_height_with_a_raw_reader(capsys, monkeypatch):
    """The raw readers no longer echo Enter, so no newline scrolls the frame and the edit view
       uses the FULL height. Only the input() fallback, where the terminal echoes the newline
       itself, still budgets one row fewer (its own test below)."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    rows = [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]

    monkeypatch.setattr(mm, "_raw_mode", True)            # a raw reader: no echo, so no reserved row
    mm.render("Doc", "sub", rows, None)
    raw = capsys.readouterr().out
    assert raw.count("\n") + 1 <= 10                      # up to the full height…
    assert raw.count("\n") + 1 > 9                        # …strictly more than a height-1 budget
    assert f"{mm.BOLD}>{mm.RESET} " in raw                # prompt still shown, on the last row now

    monkeypatch.setattr(mm, "_raw_mode", False)           # off a terminal: full height too
    monkeypatch.setattr(mm, "_edit_scroll", 0)
    mm.render("Doc", "sub", rows, None)
    piped = capsys.readouterr().out
    assert piped.count("\n") + 1 <= 10
    assert piped.count("\n") + 1 > 9


def test_render_edit_reserves_bottom_row_on_a_tty_without_cbreak(monkeypatch):
    """REGRESSION (Windows): without cbreak, input() reads and the CONSOLE echoes Enter itself;
       with the prompt on the last row that newline scrolled the frame up before the redraw
       (title vanishing for a beat on every validated edit). The bottom row must stay free on
       ANY interactive terminal without a raw reader, not only under cbreak."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    monkeypatch.setattr(mm, "_raw_mode", False)           # Windows: no cbreak reader, input() reads

    class _Tty:                                           # a console: isatty() True, so Enter echoes
        def __init__(self): self.buf = []
        def write(self, s): self.buf.append(s); return len(s)
        def flush(self): pass
        def isatty(self): return True

    out = _Tty()
    monkeypatch.setattr(mm.sys, "stdout", out)
    monkeypatch.setattr(mm.sys, "stdin", _Tty())
    rows = [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]
    mm.render("Doc", "sub", rows, None)
    painted = "".join(out.buf)
    assert painted.count("\n") + 1 <= 9                   # height-1: Enter lands on the free row
    assert f"{mm.BOLD}>{mm.RESET} " in painted            # prompt still on screen to type into
    assert "Doc" in painted                               # title still on top, and it stays there


def test_footer_underline_does_not_bleed_to_the_right_on_windows(capsys, monkeypatch):
    """REGRESSION (Windows): on a resize the console pads every line with the attributes of its
       last NON-BLANK character, ignoring the reset after it (microsoft/terminal#75, closed
       without a fix) — the underlined "edit" ending the footer stretched into a rule to the
       right edge. Such a line is closed with a non-breaking space (blank to the eye, non-blank
       to the console). macOS and Linux must not see one byte of it."""
    footer = mm.view_footer("edit")                       # current view = the LAST word of the line
    assert footer.endswith(f"{mm.UNDERLINE}edit{mm.RESET}{mm.DIM}")   # …and it carries the underline
    rows = [("Title", "ti", "Hello", True)]

    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "_WIN_CONSOLE", True)
    mm.render("Doc", "sub", rows, None, footer)
    win_lines = [ln for ln in capsys.readouterr().out.split("\n") if mm.UNDERLINE in ln]
    assert win_lines                                      # the underlined footer line is there…
    for ln in win_lines:                                  # …and no longer ENDS on the underlined word
        assert ln.endswith(mm.NBSP + "\033[K")

    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "_WIN_CONSOLE", False)
    mm.render("Doc", "sub", rows, None, footer)
    assert mm.NBSP not in capsys.readouterr().out         # macOS/Linux: frame unchanged, to the byte


def test_resize_tick_redraws_the_frame_only_when_the_size_changed(monkeypatch):
    """Windows has no SIGWINCH, so a daemon thread polls the size and redraws through the same
       _on_winch path. The tick must redraw when the size changed, and stay silent when it did
       not — else the frame would be repainted several times a second for nothing."""
    calls = []
    monkeypatch.setattr(mm, "_LAST_FRAME", ("Doc", "sub", [], None, None, False))
    monkeypatch.setattr(mm, "_rendering", False)
    monkeypatch.setattr(mm, "_typed", "")
    monkeypatch.setattr(mm, "render", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(mm, "term_width", lambda default=80: 80)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 24)

    assert mm._resize_tick((80, 24)) == (80, 24)          # same window: nothing to repaint
    assert calls == []

    monkeypatch.setattr(mm, "term_width", lambda default=80: 60)   # the window was dragged narrower
    assert mm._resize_tick((80, 24)) == (60, 24)
    assert len(calls) == 1                                # repainted once, at the new width


def test_resize_watcher_speeds_up_while_the_window_is_being_dragged(monkeypatch):
    """A drag changes the width many times a second: the watcher switches to the fast cadence
       as soon as the size moves, and falls back to the resting one once the window settles."""
    widths = iter([80, 80, 70, 60, 60, 60])               # at rest, then dragged, then still
    naps = []
    monkeypatch.setattr(mm, "term_width", lambda default=80: next(widths))
    monkeypatch.setattr(mm, "term_height", lambda default=24: 24)
    monkeypatch.setattr(mm, "_on_winch", lambda *a: None)

    def _sleep(t):
        naps.append(t)
        if len(naps) == 5:
            raise KeyboardInterrupt                       # stop the daemon loop inside the test
    monkeypatch.setattr(mm.time, "sleep", _sleep)
    with pytest.raises(KeyboardInterrupt):
        mm._watch_terminal_size()

    assert naps[0] == mm._RESIZE_POLL                     # window at rest: the slow cadence
    assert naps[1] == mm._RESIZE_POLL                     # still nothing: still slow
    assert naps[2] == mm._RESIZE_POLL_FAST               # 80 → 70: the drag is on
    assert naps[3] == mm._RESIZE_POLL_FAST               # 70 → 60: still tracking it
    assert mm._RESIZE_POLL_FAST < mm._RESIZE_POLL


def test_render_no_clamp_when_frame_fits(capsys, monkeypatch):
    """A frame shorter than the terminal is painted whole — no clamp, no hidden marker,
       every field visible. Guards the clamp against firing when there is room."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 50)
    rows = [(None, "Cat", None, None)] + [(f"Field{i}", f"f{i}", "v", True) for i in range(5)]
    mm.render("Doc", "sub", rows, None)
    out = capsys.readouterr().out
    assert "…" not in out                                 # nothing hidden
    assert "Field0" in out and "Field4" in out            # every field shown


def test_render_scrollable_view_is_not_clamped(capsys, monkeypatch):
    """A scrollable view lives in the normal buffer and is meant to overflow into the
       scrollback for the scroll wheel — the height clamp must NOT fire there, or fields the
       user scrolls to reach would be hidden."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    rows = [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]
    mm.render("Doc", "sub", rows, None, scrollable=True)
    out = capsys.readouterr().out
    assert "…" not in out                                 # not clamped…
    assert "▲" not in out and "▼" not in out              # …no internal-scroll markers either
    assert "Field0" in out and "Field29" in out           # …the full list is emitted


def test_render_scrollable_overflow_reserves_bottom_row_on_the_input_fallback(monkeypatch):
    """A scrollable browse view overflows into the scrollback, so its prompt sits on the LAST
       row; on the input() fallback the echoed Enter would scroll the frame up one line before
       the next repaint. A browse view cannot clamp (it would hide fields), so it pushes one
       blank line under the prompt and steps the caret back onto it — only on the fallback,
       and only when the frame overflows."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    monkeypatch.setattr(mm, "_raw_mode", False)           # the input() fallback: no raw reader…
    monkeypatch.setattr(mm, "_win_raw", False)

    class _Tty:                                           # …but a real console: isatty() True, so
        def __init__(self): self.buf = []                 #  the terminal echoes Enter itself
        def write(self, s): self.buf.append(s); return len(s)
        def flush(self): pass
        def isatty(self): return True
    out = _Tty()
    monkeypatch.setattr(mm.sys, "stdout", out)
    monkeypatch.setattr(mm.sys, "stdin", _Tty())
    rows = [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]

    mm.render("Doc", "sub", rows, None, scrollable=True)  # overflows 10 rows
    raw = "".join(out.buf); out.buf.clear()
    assert "…" not in raw and "Field29" in raw            # still the full list, never clamped
    # The reserve: one blank line pushed under the prompt, then the caret stepped back onto it.
    assert raw.endswith(f"\n\033[A\r{mm.PROMPT}")

    monkeypatch.setattr(mm, "_edit_scroll", 0)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 50)   # now the frame FITS
    mm.render("Doc", "sub", rows, None, scrollable=True)
    fits = "".join(out.buf); out.buf.clear()
    assert not fits.endswith(f"\n\033[A\r{mm.PROMPT}")    # prompt not on the last row: no reserve

    monkeypatch.setattr(mm, "_raw_mode", True)            # a raw reader: nothing echoes Enter
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    mm.render("Doc", "sub", rows, None, scrollable=True)
    rawmode = "".join(out.buf)
    assert not rawmode.endswith(f"\n\033[A\r{mm.PROMPT}") # no echo, no scroll to guard against


def test_edit_view_scroll_reveals_lower_fields(capsys, monkeypatch):
    """Scrolling (wheel or ↓/PgDn via _scroll_edit) shifts the window over the field list
       without ever letting the frame overflow: it stays within term_height() and repaints in
       place (home, no \\033[2J wipe), so it never spills into the scrollback."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    rows = [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]
    mm.render("Doc", "sub", rows, None)
    top = capsys.readouterr().out
    assert "Field0" in top                                # the window starts at the top…
    assert "▲" not in top                                 # …nothing above it yet
    assert mm._edit_max_scroll > 0                         # but there IS something below to scroll to

    mm._scroll_edit(mm._edit_max_scroll)                  # jump to the very bottom
    bottom = capsys.readouterr().out
    assert "Field29" in bottom                            # the last field is on screen now…
    assert "Field0" not in bottom                         # …and the first has scrolled off the top
    assert bottom.count("\n") + 1 <= 10                    # still clamped: never overflows the height
    assert "\033[H" in bottom and "\033[2J" not in bottom # repainted in place, no flash
    assert "▲" not in bottom and "▼" not in bottom        # no scroll indicators, just the fields


def test_scroll_edit_is_bounded(capsys, monkeypatch):
    """_scroll_edit never leaves [0, _edit_max_scroll]: scrolling up past the top or down past
       the bottom is a no-op, so the wheel and the arrow keys cannot run the window off the list."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    rows = [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]
    mm.render("Doc", "sub", rows, None)
    capsys.readouterr()
    mm._scroll_edit(-5)                                   # already at the top: cannot go negative
    assert mm._edit_scroll == 0
    mm._scroll_edit(10_000)                               # far past the bottom: clamped to the max
    assert mm._edit_scroll == mm._edit_max_scroll


def test_handle_scroll_seq_maps_wheel_and_keys(capsys, monkeypatch):
    """The keyboard reader routes scroll inputs through _handle_scroll_seq: the SGR mouse wheel
       (\\033[<64/65;…M), the arrows and PgUp/PgDn each move _edit_scroll the right way and are
       reported handled (True). A plain key sequence is not a scroll input (False)."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    rows = [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]
    mm.render("Doc", "sub", rows, None)                   # arms _edit_max_scroll / _edit_page / _LAST_FRAME
    capsys.readouterr()

    assert mm._handle_scroll_seq("\x1b[<65;1;1M") is True # wheel down
    assert mm._edit_scroll == mm._WHEEL_STEP
    assert mm._handle_scroll_seq("\x1b[<64;1;1M") is True # wheel up, back to the top
    assert mm._edit_scroll == 0
    assert mm._handle_scroll_seq("\x1b[B") is True        # arrow down: one line
    assert mm._edit_scroll == 1
    assert mm._handle_scroll_seq("\x1b[A") is True        # arrow up: back
    assert mm._edit_scroll == 0
    mm._handle_scroll_seq("\x1b[6~")                      # PgDn: one window
    assert mm._edit_scroll == mm._edit_page
    assert mm._handle_scroll_seq("\x1bOH") is False       # Home key: not a scroll input


def test_render_enables_mouse_only_when_clamped_and_raw(capsys, monkeypatch):
    """Mouse-event reporting is enabled ONLY on a real terminal AND while the edit frame
       overflows (there is something to scroll). It is turned off again the moment the window
       fits, so ordinary mouse text-selection returns; a scrollable browse view never turns it
       on (its wheel scrolls the native scrollback instead)."""
    _reset_frame(monkeypatch)
    monkeypatch.setattr(mm, "_raw_mode", True)            # pretend we own a real terminal
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    tall = [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]
    short = [(f"Field{i}", f"f{i}", "v", True) for i in range(3)]

    mm.render("Doc", "sub", tall, None)                  # overflows → enable
    assert mm.MOUSE_ON in capsys.readouterr().out
    assert mm._mouse_on is True
    mm.render("Doc", "sub", tall, None)                  # still overflowing → NOT re-emitted
    assert mm.MOUSE_ON not in capsys.readouterr().out
    mm.render("Doc", "sub", short, None)                 # now fits → disable, selection returns
    assert mm.MOUSE_OFF in capsys.readouterr().out
    assert mm._mouse_on is False


def test_render_no_mouse_reporting_off_terminal(capsys, monkeypatch):
    """Off a real terminal (pipe, redirection, tests: _raw_mode False) the mouse is never
       touched even when the frame is clamped — nothing would consume the reports, and the
       output must stay clean for pipes and the test suite."""
    _reset_frame(monkeypatch)                             # _raw_mode stays False (module default)
    monkeypatch.setattr(mm, "term_width", lambda default=80: 100)
    monkeypatch.setattr(mm, "term_height", lambda default=24: 10)
    rows = [(f"Field{i}", f"f{i}", "v", True) for i in range(30)]
    mm.render("Doc", "sub", rows, None)
    out = capsys.readouterr().out
    assert mm.MOUSE_ON not in out and "\033[?1000h" not in out


def test_winch_redraws_current_frame(capsys, monkeypatch):
    """After a render(), a SIGWINCH (via _on_winch) repaints the last frame's title."""
    _reset_frame(monkeypatch)
    mm.render("My Title", "sub", [("Author", "au", "x", True)], None)
    capsys.readouterr()                               # discard the first paint
    mm._on_winch(None, None)
    assert "My Title" in capsys.readouterr().out


def test_winch_reprints_typed_input(capsys, monkeypatch):
    """While typing, a SIGWINCH repaints the frame AND re-echoes the in-progress line, so
       the half-typed text is not lost from view on a resize."""
    _reset_frame(monkeypatch)
    mm.render("Doc", "sub", [("Author", "au", "x", True)], None)
    monkeypatch.setattr(mm, "_typed", "Title Hello")  # a line being typed
    capsys.readouterr()
    mm._on_winch(None, None)
    out = capsys.readouterr().out
    assert "Doc" in out
    assert "Title Hello" in out


def test_winch_suppressed_on_custom_screen(capsys, monkeypatch):
    """A home-made screen (help, field focus, wipe, end summary) drops _LAST_FRAME via
       clear_screen(); a SIGWINCH then paints NOTHING over it."""
    _reset_frame(monkeypatch)
    mm.render("Doc", "sub", [("Author", "au", "x", True)], None)
    mm.clear_screen()                                 # own screen: _LAST_FRAME = None
    capsys.readouterr()                               # discard everything so far
    mm._on_winch(None, None)
    assert capsys.readouterr().out == ""


def test_raw_reader_refuses_bracketed_paste_on_a_bare_line(monkeypatch):
    """A bracketed paste dropped on an EMPTY line names no field: the reader swallows the whole
       block (markers included), gives the loop no command, and arms ONE notice."""
    monkeypatch.setattr(mm, "DEFAULT_LANG", "en")
    b, e = mm.PASTE_BEGIN.encode(), mm.PASTE_END.encode()
    _prime_reader(monkeypatch, pending=b + b"a\nb\nc" + e + b"\n")
    assert mm.ask() == ""
    assert mm._take_paste_notice() == mm.tr("paste_blocked")


def test_raw_reader_editing(monkeypatch):
    """Typing + backspace + Enter yields the corrected line; Ctrl-C → None; Ctrl-D on an
       empty line → None."""
    _prime_raw(monkeypatch, b"abX\x7fc\n")            # 'X' then backspace, then 'c'
    assert mm._read_raw() == "abc"
    _prime_raw(monkeypatch, b"\x03")                  # Ctrl-C
    assert mm._read_raw() is None
    _prime_raw(monkeypatch, b"\x04")                  # Ctrl-D on an empty line
    assert mm._read_raw() is None


def test_raw_reader_does_not_echo_the_enter_newline(monkeypatch):
    """The raw reader echoes the characters it reads but NOT the newline that validates:
       render() repaints from \\033[H and repositions the cursor itself, so an echoed newline
       only moved the caret down a row first (caret falling onto the reserved bottom row, or
       the whole screen scrolling where the prompt sits on the last row)."""
    class _Rec:
        def __init__(self): self.buf = []
        def write(self, s): self.buf.append(s); return len(s)
        def flush(self): pass
    rec = _Rec()
    monkeypatch.setattr(mm.sys, "stdout", rec)
    _prime_raw(monkeypatch, b"abc\n")
    assert mm._read_raw() == "abc"                    # the line comes back, Enter stripped as ever
    echoed = "".join(rec.buf)
    assert "a" in echoed and "b" in echoed and "c" in echoed   # the keystrokes WERE echoed…
    assert "\n" not in echoed and "\r" not in echoed           # …but the validating newline was NOT


def test_caret_left_inserts_midline(monkeypatch):
    """←/→ move the caret INSIDE the line: a typo is fixed in place instead of erasing
       back to it. "Bohemin", one ← , then "a" gives "Bohemian" — the requested case."""
    _prime_raw(monkeypatch, b"Bohemin\x1b[Da\n")
    assert mm._read_raw() == "Bohemian"


def test_caret_backspace_and_delete_at_caret(monkeypatch):
    # Backspace erases BEFORE the caret: "Bohemiann", ← , backspace → "Bohemian".
    _prime_raw(monkeypatch, b"Bohemiann\x1b[D\x7f\n")
    assert mm._read_raw() == "Bohemian"
    # Delete erases UNDER the caret: "Xabc", Home, Delete → "abc".
    _prime_raw(monkeypatch, b"Xabc\x1b[H\x1b[3~\n")
    assert mm._read_raw() == "abc"


def test_caret_home_end_and_bounds(monkeypatch):
    # Home then typing prepends; End returns to the tail; ← at 0 and → at the end no-op.
    _prime_raw(monkeypatch, b"bc\x1b[Ha\x1b[Fd\n")            # Home, 'a', End, 'd'
    assert mm._read_raw() == "abcd"
    _prime_raw(monkeypatch, b"ab\x1b[D\x1b[D\x1b[D\x1b[DX\n")  # ← past the start: stays at 0
    assert mm._read_raw() == "Xab"
    _prime_raw(monkeypatch, b"ab\x1b[C\x1b[CX\n")              # → past the end: stays at the end
    assert mm._read_raw() == "abX"
    _prime_raw(monkeypatch, b"ab\x1bODX\n")                    # SS3 variant (application mode)
    assert mm._read_raw() == "aXb"


def test_nav_arrows_walk_the_batch_on_empty_line(monkeypatch):
    """With nav=True (the batched single session), ←/→ on an EMPTY line return the
       navigation commands "p"/"n" directly — no Enter needed to change file."""
    _prime_raw(monkeypatch, b"\x1b[C")
    assert mm._read_raw(nav=True) == "n"
    _prime_raw(monkeypatch, b"\x1b[D")
    assert mm._read_raw(nav=True) == "p"
    # Text on the line: the same arrows edit the caret, they never navigate.
    _prime_raw(monkeypatch, b"ab\x1b[Dc\n")
    assert mm._read_raw(nav=True) == "acb"
    # nav off (every other prompt): arrows on an empty line are caret no-ops.
    _prime_raw(monkeypatch, b"\x1b[C\x1b[D\n")
    assert mm._read_raw() == ""


def test_pasted_text_stays_editable_at_the_caret(monkeypatch):
    """An ACCEPTED paste (the line already names a field) lands in the line like typed text:
       the caret still addresses it (here: one ← then 'e' fixes the last word), and nothing is
       submitted until Enter — the paste does not validate itself."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    _prime_paste(monkeypatch, b"c ", (mm.PASTE_BEGIN + "Nice Titl" + mm.PASTE_END).encode(),
                 after=b"\x1b[Ce\n")                       # → (a no-op at the end), 'e', Enter
    assert mm._read_line_raw(paste_ok=lambda prefix: prefix == "c ") == "c Nice Title"


def test_raw_reader_utf8(monkeypatch):
    """An accented input whose 2-byte character is split across two os.read() is recomposed
       correctly by the incremental decoder."""
    _prime_raw(monkeypatch, b"caf\xc3", reads=[b"\xa9", b"\n"])   # 'é' = \xc3\xa9 split in two
    assert mm._read_raw() == "café"


def test_raw_reader_survives_invalid_utf8(monkeypatch):
    """spar2 blocker — a stray non-UTF-8 byte (0xe9 = 'é' in Latin-1, a Meta/8-bit key, or
       line noise on the pty) must NOT crash the reader with a traceback (SPEC §3), matching
       input()+surrogateescape which survived. The byte is surrogate-escaped, not lost, and
       one decode() then yielding two chars ("\\udce9" + "\\n") is drained correctly."""
    class _Sink:                                      # tolerant stdout (main() sets errors="replace")
        def write(self, s): return len(s)
        def flush(self): pass
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    _prime_raw(monkeypatch, b"caf\xe9\n")             # 0xe9 alone is invalid UTF-8
    line = mm._read_raw()                              # returns, never raises
    assert line == "caf\udce9"                        # good part kept; stray byte escaped, '\n' ends the line


def test_raw_reader_resyncs_on_stray_esc_before_mouse_report(monkeypatch):
    """Regression (macOS Terminal.app '[<..M' leak): a lone ESC immediately followed by a
       mouse report (ESC[<..M) must not cost the report its own leading ESC. _read_escape used
       to swallow BOTH ESCs, so the report's bare bytes ('[<0;20;3M') fell through to the
       printable branch and were echoed into the line as visible garbage — the stray brackets
       that appeared around the input line. The reader now pushes the second ESC back and
       resynchronises: the report is consumed (as a wheel/click), the typed text comes clean."""
    class _Sink:                                      # swallow the reader's echo
        def write(self, s): return len(s)
        def flush(self): pass
    for report in (b"\x1b[<65;20;3M", b"\x1b[<0;20;3M"):   # SGR wheel-down, then a plain click
        monkeypatch.setattr(mm.sys, "stdout", _Sink())
        _prime_raw(monkeypatch, b"\x1b" + report + b"Author Hello\n")   # stray ESC glued to the report
        assert mm._read_raw() == "Author Hello"        # report fully consumed, nothing leaked in


def test_raw_reader_falls_back_off_tty(monkeypatch):
    """Off a terminal (_raw_mode False: pipe, redirection, Windows), _read_raw delegates to
       input() — the exact previous behaviour, so the existing test_ask_* stay valid."""
    monkeypatch.setattr(mm, "_raw_mode", False)
    monkeypatch.setattr("builtins.input", lambda: "typed via input")
    assert mm._read_raw() == "typed via input"

    def _eof():
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert mm._read_raw() is None                     # EOF/Ctrl-C via input() → None, unchanged


def test_ask_refuses_a_paste_already_sitting_in_the_buffer(monkeypatch):
    """spar1 B2 — seam §4. On a terminal WITHOUT bracketed paste (macOS Terminal.app), a paste
       lands in _pending as a raw burst, with nothing to mark it as pasted. Its characters share
       one read, which typing cannot do: the guard must refuse it whole rather than let its lines
       run as a command burst (the original destructive bug)."""
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    _prime_reader(monkeypatch, pending=b"cmd one\nline two\nline three\n")

    class _Stdin:
        def isatty(self): return True
        def fileno(self): return 0
    monkeypatch.setattr(mm.sys, "stdin", _Stdin())

    assert mm.ask() == ""                             # refused, not executed line by line
    assert mm._take_paste_notice()                    # …with one notice for the whole block


def test_ask_ignores_stray_escape_after_enter(monkeypatch):
    """REGRESSION: a mouse report (reporting is on while the edit frame overflows) or an arrow
       key landing in the SAME read as Enter must NOT be taken for a paste. The old guard froze
       the prompt and then rejected the real command as "Pasted input ignored (2 lines)". An
       escape sequence is not text: it is consumed without counting as an arrival, so the typed
       command comes back and no paste notice is armed."""
    monkeypatch.setattr(mm, "_LAST_FRAME", None)          # _handle_scroll_seq → _scroll_edit no-op
    monkeypatch.setattr(mm, "_edit_max_scroll", 0)
    monkeypatch.setattr(mm, "_rendering", False)
    monkeypatch.setattr(mm.sys, "stdout", _Sink())
    # "all" typed key by key, then Enter arriving in ONE read with a stray SGR wheel-down report.
    _prime_reader(monkeypatch, reads=[b"a", b"l", b"l", b"\n\x1b[<65;5;5M"])

    class _Stdin:
        def isatty(self): return True
        def fileno(self): return 0
    monkeypatch.setattr(mm.sys, "stdin", _Stdin())

    assert mm.ask() == "all"                              # the command runs…
    assert mm._take_paste_notice() is None                # …and no false paste notice


def test_typed_cleared_after_enter(monkeypatch):
    """spar1 B3 — seam §4, symmetric of test_winch_reprints_typed_input. Once a line is
       submitted (\\n), _typed is reset to "" so a resize during command processing (a slow
       write on a big video) re-echoes nothing stale at the prompt."""
    _prime_raw(monkeypatch, b"Author Hello\n")
    assert mm._read_raw() == "Author Hello"
    assert mm._typed == ""                            # cleared on Enter — nothing stale to reprint


def test_suspend_resume_rearms_cbreak(monkeypatch):
    """REGRESSION (Ctrl-Z then `fg`): the shell can leave the terminal cooked on resume, so our
       own echo would double every keystroke. On SIGTSTP we restore a cooked terminal FIRST then
       stop for real (default action); on SIGCONT we re-install the TSTP handler and re-arm cbreak."""
    calls = []
    monkeypatch.setattr(mm, "_restore_raw_term", lambda: calls.append("restore"))
    monkeypatch.setattr(mm, "_reenter_raw", lambda: calls.append("reenter"))
    monkeypatch.setattr(mm, "_LAST_FRAME", None)          # no repaint needed for this assertion
    sig, killed = [], []
    monkeypatch.setattr(mm.signal, "signal", lambda s, h: sig.append((s, h)))
    monkeypatch.setattr(mm.os, "kill", lambda pid, s: killed.append((pid, s)))
    monkeypatch.setattr(mm.os, "getpid", lambda: 4321)

    mm._on_tstp(mm.signal.SIGTSTP, None)                  # Ctrl-Z
    assert calls == ["restore"]                           # cooked terminal handed back FIRST
    assert (mm.signal.SIGTSTP, mm.signal.SIG_DFL) in sig  # default action armed…
    assert killed == [(4321, mm.signal.SIGTSTP)]          # …then we stop for real

    sig.clear()
    mm._on_cont(mm.signal.SIGCONT, None)                  # fg
    assert (mm.signal.SIGTSTP, mm._on_tstp) in sig        # our TSTP handler re-installed
    assert calls == ["restore", "reenter"]                # cbreak re-armed on resume


def test_raw_enter_leave_helpers_are_noop_off_raw_mode(monkeypatch):
    """The suspend/resume terminal helpers touch termios ONLY in raw mode: off it (pipe,
       redirection, Windows) they are pure no-ops, and in it they re-arm cbreak / restore the
       saved cooked state on the right fd."""
    import tty
    import termios
    monkeypatch.setattr(mm, "_raw_fd", 0)
    calls = {}
    monkeypatch.setattr(tty, "setcbreak", lambda fd, *a, **k: calls.__setitem__("cbreak", fd))
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, st: calls.__setitem__("set", (fd, when, st)))

    class _Sink:
        def write(self, s): return len(s)
        def flush(self): pass
    monkeypatch.setattr(mm.sys, "stdout", _Sink())

    monkeypatch.setattr(mm, "_raw_mode", False)           # off raw mode: nothing happens
    mm._reenter_raw()
    mm._restore_raw_term()
    assert calls == {}

    monkeypatch.setattr(mm, "_raw_mode", True)            # in raw mode: real termios calls
    monkeypatch.setattr(mm, "_saved_term", "SAVED")
    mm._reenter_raw()
    assert calls.get("cbreak") == 0                       # cbreak re-armed on our fd
    mm._restore_raw_term()
    assert calls.get("set") == (0, termios.TCSADRAIN, "SAVED")   # cooked state restored


def test_terminal_restored_on_exception(tmp_path, monkeypatch):
    """spar1 B1 — seam §4 / SPEC §2 promise "toujours restaurés, même sur exception". If the
       session body raises while cbreak is armed, the finally must still restore the saved
       termios (tcsetattr TCSADRAIN), drop _raw_mode, and restore the screen (PASTE_OFF +
       leave alt buffer) — otherwise a non-technical user is left with a mute shell."""
    import io
    import termios
    import tty
    calls = {}
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: "SAVED_STATE")
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, st: calls.__setitem__("set", (fd, when, st)))
    monkeypatch.setattr(tty, "setcbreak", lambda fd, *a, **k: calls.__setitem__("cbreak", fd))
    monkeypatch.setattr(mm, "_missing_dependencies", lambda paths: [])
    # main() loads config.json next to metmux.py — a LOCAL, gitignored file. Left
    # active, it would mutate DEFAULT_LANG/DEFAULT_DATE_ORDER for the rest of the
    # run (order-dependent failures) and tie the verdict to a file outside the repo.
    monkeypatch.setattr(mm, "load_config", lambda cfg_path=None: None)
    monkeypatch.setattr(mm.signal, "signal", lambda *a, **k: None)
    monkeypatch.setattr(mm.signal, "getsignal", lambda *a, **k: None)

    class _FakeTTY:
        def __init__(self, fd, sink=None): self._fd, self._sink = fd, sink
        def isatty(self): return True
        def fileno(self): return self._fd
        def reconfigure(self, **k): pass
        def write(self, s):
            if self._sink is not None: self._sink.write(s)
            return len(s)
        def flush(self):
            if self._sink is not None: self._sink.flush()

    out = io.StringIO()
    monkeypatch.setattr(mm.sys, "stdin", _FakeTTY(0))
    monkeypatch.setattr(mm.sys, "stdout", _FakeTTY(1, out))

    f = tmp_path / "note.txt"
    f.write_text("hello")
    monkeypatch.setattr(mm.sys, "argv", ["metmux", str(f)])
    monkeypatch.setattr(mm, "collect_paths", lambda args: [f])

    def _boom(paths):
        raise RuntimeError("boom in session")
    monkeypatch.setattr(mm, "walk_single", _boom)

    with pytest.raises(RuntimeError):
        mm.main()

    assert calls.get("cbreak") == 0                                        # cbreak armed on our fd
    assert calls.get("set") == (0, termios.TCSADRAIN, "SAVED_STATE")       # …restored in the finally
    assert mm._raw_mode is False                                          # flag dropped → back to input()
    assert mm.PASTE_OFF in out.getvalue() and "\033[?1049l" in out.getvalue()  # screen restored


# ============================================================
#  Section — pre-publication audit (2026-07-12) non-regressions
# ============================================================

def test_absurd_utc_offset_is_refused_not_a_crash(monkeypatch):
    # REGRESSION: an offset of ±24:00 or more (a typo for +0200, or a pasted oddity)
    # crashed the session on macOS/Windows — datetime.timezone() refuses it, and the
    # ValueError escaped _set_os_create_date despite its "never raises" promise. The
    # typed date must be refused as unreadable instead, on every path.
    assert mm.to_exif("2024:01:01 12:00:00+24:00", "FileCreateDate") is None
    assert mm.to_exif("01/01/2024 12:00+2400", "FileCreateDate") is None
    assert mm.to_exif("2024:01:01 12:00:00+23:99", "FileModifyDate") is None
    assert mm.to_exif("2024:01:01 12:00:00+23:59", "FileCreateDate") \
        == "2024:01:01 12:00:00+23:59"                    # extreme but legal: still accepted
    assert mm._set_os_create_date(Path("x"), "2024:01:01 12:00:00+24:00") is False
    # The bulk command refuses the whole input too (counted as "Unreadable date.").
    monkeypatch.setattr(mm, "read", lambda p, raw=False: {"FileModifyDate": "2024:01:01 00:00:00"})
    monkeypatch.setattr(mm, "writable", lambda p: {"FileModifyDate"})
    assert mm.cmd_dates(Path("x"), "25/12/2024 14:00:00+24:00") == (None, 0, 0)


def test_json_engines_tolerate_utf8_bom(tmp_path):
    # REGRESSION: a UTF-8 BOM (ArcGIS-style geojson exports) made every JSON engine
    # blind — read fell back to external data while the content field stayed listed
    # as editable, so each edit ended in "Write failed.". Reads now tolerate the BOM
    # and writes preserve it (same doctrine as the m3u8 engine).
    g = tmp_path / "m.geojson"
    g.write_bytes(b'\xef\xbb\xbf{"type": "FeatureCollection", "name": "Layer", "features": []}')
    assert mm.geojson_read(g).get("Name") == "Layer"
    assert mm.geojson_write(g, "Name", "New") is True
    assert g.read_bytes().startswith(b"\xef\xbb\xbf")     # BOM preserved
    assert mm.geojson_read(g).get("Name") == "New"

    h = tmp_path / "c.har"
    h.write_bytes(b'\xef\xbb\xbf' + _json.dumps(
        {"log": {"version": "1.2", "creator": {"name": "DevTools"}, "entries": []}}).encode())
    assert mm.har_read(h).get("Creator") == "DevTools"
    assert mm.har_write(h, "Comment", "note") is True
    assert mm.har_read(h).get("Comment") == "note"

    n = tmp_path / "n.ipynb"
    n.write_bytes(b'\xef\xbb\xbf' + _json.dumps(
        {"cells": [], "metadata": {"title": "Old"}, "nbformat": 4, "nbformat_minor": 5}).encode())
    assert mm.ipynb_read(n).get("Title") == "Old"
    assert mm.ipynb_write(n, "Title", "New") is True
    assert mm.ipynb_read(n).get("Title") == "New"
    assert n.read_bytes().startswith(b"\xef\xbb\xbf")


def test_match_tag_routes_shared_labels_deterministically():
    # REGRESSION: on a mixed batch exposing exiftool "Title" AND mutagen "title",
    # _match_tag walked an unordered set — the tag receiving "title X" (or the FR
    # label "Titre") changed with the process hash seed. The walk is sorted now:
    # same route on every launch. (Merging the duplicate labels into one row that
    # writes each file's own tag stays a backlog item.)
    tags = {"title", "Title"}
    assert mm._match_tag("title", {}, tags, "en") == "Title"
    assert mm._match_tag("TITLE", {}, tags, "en") == "Title"
    assert mm._match_tag("Titre", {}, tags, "fr") == "Title"


# ============================================================
#  Section 20 — every offered exiftool field is really writable
# ============================================================
# REGRESSION: SUGGESTED was a hand-written whitelist that nothing confronted with what
# exiftool can actually write. It offered "Synopsis" on an MP4 (read-only in exiftool, so
# every write failed) and ten similar tags; "LocationName" was worse: exiftool dropped it
# silently and metmux announced a success. The guard below is empirical — it WRITES each
# offered field and RE-READS it.

# Values a tag actually accepts: a GPS or a rating refuses "metmuxprobe", and that
# refusal says nothing about the tag being writable. Anything else takes free text.
_PROBE_VALUES = {
    "GPSCoordinates": "48.8566, 2.3522", "GPSLatitude": "48.8566",
    "GPSLongitude": "2.3522", "GPSAltitude": "35", "GPSLatitudeRef": "N",
    "GPSLongitudeRef": "E", "GPSAltitudeRef": "Above Sea Level",
    "Rating": "3", "Urgency": "5", "Compilation": "Yes", "Trapped": "True",
    "TrackNumber": "1", "Track": "1", "DiscNumber": "1",
    "TVSeason": "1", "TVEpisode": "1", "BeatsPerMinute": "120", "Year": "2020",
}


def _probe_value(tag):
    if tag in _PROBE_VALUES:
        return _PROBE_VALUES[tag]
    return "2020:01:02 03:04:05" if ("Date" in tag or "Time" in tag) else "metmuxprobe"


def _make_mp4(tmp_path):
    out = tmp_path / "video.mp4"
    r = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "color=c=black:s=16x16:d=0.1",
         "-pix_fmt", "yuv420p", "-y", str(out)], capture_output=True)
    return out if r.returncode == 0 and out.exists() else None


def _make_m4a(tmp_path):
    out = tmp_path / "audio.m4a"
    r = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=8000",
         "-t", "0.1", "-c:a", "aac", "-y", str(out)], capture_output=True)
    return out if r.returncode == 0 and out.exists() else None


def _make_wav(tmp_path):
    out = tmp_path / "audio.wav"
    r = subprocess.run(
        ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=8000",
         "-t", "0.1", "-y", str(out)], capture_output=True)
    return out if r.returncode == 0 and out.exists() else None


@needs_exiftool
@needs_ffmpeg
@pytest.mark.parametrize("factory", [_make_mp4, _make_m4a])
def test_every_suggested_exiftool_field_writes_and_reads_back(factory, tmp_path):
    src = factory(tmp_path)
    if src is None:
        pytest.skip("ffmpeg could not build the media")
    category = mm.exiftool_category(mm.et_read(src))
    offered = mm.SUGGESTED[category]
    assert offered, f"no field offered for {category}"
    for tag in offered:
        target = tmp_path / f"probe_{tag}{src.suffix}"
        shutil.copy2(src, target)
        value = _probe_value(tag)
        assert mm.et_write(target, tag, value) is True, \
            f"{category}: exiftool cannot write {tag} — do not offer it"
        # Announcing the write is not enough: exiftool can report success while dropping
        # the tag. Only a re-read proves the value reached the file.
        assert (mm.et_read(target) or {}).get(tag) is not None, \
            f"{category}: {tag} announced written but absent on re-read"


@needs_exiftool
def test_every_suggested_exiftool_field_writes_and_reads_back_image(tmp_path):
    src = _write_sample_jpg(tmp_path / "photo.jpg")
    for tag in mm.SUGGESTED["exiftool_image"]:
        target = tmp_path / f"probe_{tag}.jpg"
        shutil.copy2(src, target)
        assert mm.et_write(target, tag, _probe_value(tag)) is True, \
            f"exiftool cannot write {tag} — do not offer it"
        assert (mm.et_read(target) or {}).get(tag) is not None, \
            f"{tag} announced written but absent on re-read"


@needs_exiftool
@needs_ffmpeg
def test_format_exiftool_cannot_write_offers_no_content_field(tmp_path):
    # A WAV is READ by exiftool but never written ("Can't currently write RIFF WAVE
    # files"): offering its content fields means N writes that all fail. The file dates
    # stay editable — they belong to the filesystem, and exiftool does write them.
    wav = _make_wav(tmp_path)
    if wav is None:
        pytest.skip("ffmpeg could not build the media")
    editable = mm.writable(wav)
    assert editable - set(mm.FILE_BASE_TAGS) == set()
    assert set(mm.FILE_BASE_TAGS) <= editable
    assert mm.et_write(wav, "FileModifyDate", "2020:01:02 03:04:05") is True


@needs_exiftool
def test_content_readonly_keyed_on_detected_type_not_on_extension(tmp_path):
    # The guard reads the type exiftool DETECTED, never the file's name: a JPEG called
    # "photo.foo" is writable, and exiftool writes it. Keying on the extension would
    # take away a capability metmux has.
    disguised = _write_sample_jpg(tmp_path / "photo.foo")
    assert mm._et_content_readonly(mm.et_read(disguised)) is False
    assert mm.et_write(disguised, "Title", "kept") is True
    assert mm.et_read(disguised).get("Title") == "kept"


# ============================================================
#  Section — Resident exiftool (the Windows speed fix)
# ============================================================
# Launching exiftool is what costs on Windows (a packed Perl, re-scanned by the antivirus at
# every launch: ~1 s on an old machine), and metmux launched it 3 times per edited field. One
# resident exiftool, fed through a pipe, is the fix. It is Windows-only in production; the
# bench forces it ON here so the very same code path is proven on Linux.


@pytest.fixture
def resident(monkeypatch):
    monkeypatch.setenv("METMUX_EXIFTOOL_DAEMON", "1")
    mm._et_stop()
    mm._ET_DEATHS = 0
    yield
    mm._et_stop()
    mm._ET_DEATHS = 0


def test_the_resident_is_windows_only(monkeypatch):
    # macOS and Linux keep launching exiftool: a launch is cheap there.
    monkeypatch.delenv("METMUX_EXIFTOOL_DAEMON", raising=False)
    monkeypatch.setattr(os, "name", "posix")
    assert mm._et_daemon_wanted() is False
    monkeypatch.setattr(os, "name", "nt")
    assert mm._et_daemon_wanted() is True
    monkeypatch.setenv("METMUX_EXIFTOOL_DAEMON", "0")     # and the user can always turn it off
    assert mm._et_daemon_wanted() is False


def test_the_argfile_guard_sends_back_what_exiftool_would_eat():
    # exiftool's argfile parser (FilterArgfileLine) silently alters some arguments. Those go
    # back to a plain launch — the daemon must never change what gets written.
    assert mm._et_argfile_safe(["-Artist=Bob Dylan", "photo.jpg"]) is True
    assert mm._et_argfile_safe(["-Artist=Cost $5 @home"]) is True    # '$' and '@' survive raw
    assert mm._et_argfile_safe(["-sep", ", ", "-Keywords=a, b"]) is True
    assert mm._et_argfile_safe(["-Artist= Bob"]) is False            # ONE space after '=' is eaten
    assert mm._et_argfile_safe(["-Artist=x", " photo.jpg"]) is False  # leading space is stripped
    assert mm._et_argfile_safe(["#photo.jpg"]) is False               # a '#' line is a comment
    assert mm._et_argfile_safe([""]) is False                         # an empty line is dropped
    assert mm._et_argfile_safe(["-Comment=two\nlines"]) is False      # a newline splits the argument


@needs_exiftool
@pytest.mark.parametrize("value", [
    "Bob Dylan",
    "Cost $5 @home",          # '#[CSTR]' corrupts these two into '\$' and '\@' — hence raw lines
    'He said "hi"',
    "C:\\Users\\Mike",
    "Beyoncé — Café Müller",
    "100% pure",
    "#1 hit",
    "a=b=c",
    "-not an option",
    "  two leading spaces",   # the argfile would eat one: this one must fall back to a launch
    "a; b & c | d",
    "trailing space  ",
])
def test_the_resident_writes_exactly_what_a_launch_writes(tmp_path, resident, value):
    # The contract is not "the value survives" (exiftool itself trims a trailing space, with or
    # without us) — it is "the resident writes EXACTLY what launching exiftool would have".
    through_daemon = _write_sample_jpg(tmp_path / "daemon.jpg")
    assert mm.et_write(through_daemon, "Artist", value) is True
    got_daemon = (mm.et_read(through_daemon) or {}).get("Artist")

    mm._et_stop()
    os.environ["METMUX_EXIFTOOL_DAEMON"] = "0"            # the reference path: a plain launch
    try:
        through_launch = _write_sample_jpg(tmp_path / "launch.jpg")
        assert mm.et_write(through_launch, "Artist", value) is True
        got_launch = (mm.et_read(through_launch) or {}).get("Artist")
    finally:
        os.environ["METMUX_EXIFTOOL_DAEMON"] = "1"

    assert got_daemon == got_launch, f"the resident altered {value!r}"


@needs_exiftool
def test_the_resident_launches_exiftool_once_for_many_commands(tmp_path, resident, monkeypatch):
    # 10 commands, ONE exiftool: fails the day a launch per command comes back
    # (the 3-second field edit).
    launched, spawned = [], []
    real_popen, real_run = subprocess.Popen, subprocess.run
    monkeypatch.setattr(mm.subprocess, "Popen",
                        lambda *a, **k: (launched.append(a), real_popen(*a, **k))[1])
    monkeypatch.setattr(mm.subprocess, "run",
                        lambda *a, **k: (spawned.append(a), real_run(*a, **k))[1])
    p = _write_sample_jpg(tmp_path / "x.jpg")
    for i in range(5):
        assert mm.et_write(p, "Artist", f"take {i}") is True
        assert (mm.et_read(p) or {}).get("Artist") == f"take {i}"
    assert len(launched) == 1, f"{len(launched)} exiftool launched for 10 commands"
    assert spawned == [], "a command escaped the resident and launched exiftool"


@needs_exiftool
def test_the_resident_keeps_the_list_separator(tmp_path, resident):
    # '-sep' travels with ', ' — an argument ENDING in a space, on its own argfile line. If the
    # parser trimmed it, "cat, dog" would land as ONE keyword and the list structure would be lost.
    p = _write_sample_jpg(tmp_path / "x.jpg")
    assert mm.et_write(p, "Keywords", "cat, dog") is True
    assert (mm.et_read(p) or {}).get("Keywords") == ["cat", "dog"]


@needs_exiftool
def test_the_resident_finds_an_accented_file_name(tmp_path, resident):
    # Arguments in an argfile are NOT recoded to the system code page (exiftool's WINDOWS UNICODE
    # FILE NAMES): without '-charset filename=UTF8', "Café.jpg" would simply not be found on a
    # French Windows box — every write on it would fail.
    p = _write_sample_jpg(tmp_path / "Café Müller — été.jpg")
    assert mm.et_write(p, "Artist", "Zaz") is True
    assert (mm.et_read(p) or {}).get("Artist") == "Zaz"


@needs_exiftool
def test_the_resident_reports_a_failure_as_a_failure(tmp_path, resident):
    # -stay_open hides the exit code; '-echo4 ${status}' gets it back. Without it a failed
    # write would pass for a success.
    _, ok = mm.et_run("-overwrite_original", "-Artist=x", str(tmp_path / "does-not-exist.jpg"))
    assert ok is False


@needs_exiftool
def test_a_dead_resident_falls_back_to_a_launch_and_the_session_goes_on(tmp_path, resident):
    p = _write_sample_jpg(tmp_path / "x.jpg")
    assert mm.et_write(p, "Artist", "before") is True
    mm._ET_STAY[0].kill()                                # exiftool dies under us, mid-session
    mm._ET_STAY[0].wait()
    assert mm.et_write(p, "Artist", "after") is True     # the write still lands
    assert (mm.et_read(p) or {}).get("Artist") == "after"


@needs_exiftool
def test_burying_a_dead_resident_closes_its_stdin(tmp_path, resident):
    # A resident killed mid-command leaves stdin's BufferedWriter holding the unflushed command.
    # Left open, its finalizer flushes to the dead pipe and raises an ignored-during-finalize
    # BrokenPipeError (a PytestUnraisableExceptionWarning). _et_stop must close it on every path.
    mm.et_write(_write_sample_jpg(tmp_path / "x.jpg"), "Artist", "before")
    proc = mm._ET_STAY[0]
    proc.kill()
    proc.wait()
    mm.et_run("-Artist=after", str(tmp_path / "x.jpg"))  # broken flush -> _et_died -> _et_stop
    assert proc.stdin.closed is True


@needs_exiftool
def test_a_resident_that_keeps_dying_is_given_up_on(tmp_path, resident, monkeypatch):
    # A resident that dies at every command costs a launch AND a crash each time: after
    # _ET_MAX_DEATHS we stop resurrecting it for the session.
    p = _write_sample_jpg(tmp_path / "x.jpg")
    for _ in range(mm._ET_MAX_DEATHS):
        assert mm.et_read(p) is not None
        mm._ET_STAY[0].kill()
        mm._ET_STAY[0].wait()
        mm.et_read(p)                                    # notices the death, buries it
    assert mm._ET_STAY is False                          # given up on: plain launches from now on
    assert mm.et_read(p) is not None                     # and metmux still works
    assert mm._et_daemon() is None


def test_a_flooded_stderr_cannot_hang_the_resident():
    # A pipe holds ~64 KB. Waiting on stdout while leaving stderr unread would block a chatty
    # exiftool mid-write on stderr: it would never reach the marker we wait for on stdout, and
    # both sides would hang forever. Each pipe is therefore drained by its own thread.
    # The real exiftool always prints {readyN} at the START of a line (measured: "]\n{ready1}\n"
    # after -j, "...\n{ready2}\n" after a write), so the payload here ends with a newline before it.
    child = ("import sys\n"
             "sys.stderr.write('x' * 300000)\n"          # far past what a pipe can hold
             "sys.stderr.flush()\n"
             "sys.stdout.write('THE DATA\\n{ready1}\\n')\n"
             "sys.stdout.flush()\n")
    proc = subprocess.Popen([sys.executable, "-c", child],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out_pipe, err_pipe = mm._EtPipe(proc.stdout), mm._EtPipe(proc.stderr)
    got = []
    t = threading.Thread(target=lambda: got.append(out_pipe.take("{ready1}")), daemon=True)
    t.start()
    t.join(timeout=30)
    assert got, "deadlock: stdout was never delivered while stderr flooded"
    assert got[0] is not None and got[0][0] == "THE DATA\n"
    proc.wait(timeout=10)


def test_a_value_equal_to_the_marker_does_not_desync_the_resident():
    # A file whose metadata value (stdout) or filename echoed by a Warning (stderr) equals the
    # current {readyN} marker must not be mistaken for the real marker: exiftool prints the true
    # marker alone on its line, so take() only matches it at the start of a line. Without the
    # anchor, find() hit the in-value occurrence first, truncated the answer to None (a valid
    # file shown unreadable) AND left the true marker to poison the next command.
    out_pipe = mm._EtPipe.__new__(mm._EtPipe)
    out_pipe.buf = '[{\n  "Comment": "{ready3}"\n}]\n{ready3}\n'
    out_pipe.eof = True
    out_pipe.cond = threading.Condition()
    head, tail = out_pipe.take("{ready3}")
    parsed = json.loads(head)                        # the whole JSON survives, marker value intact
    assert parsed[0]["Comment"] == "{ready3}"
    # stderr side: a filename that equals the marker, re-printed by a Warning, then the true line
    err_pipe = mm._EtPipe.__new__(mm._EtPipe)
    err_pipe.buf = "Warning: bad char - {ready5}.jpg\n{ready5}0\n"
    err_pipe.eof = True
    err_pipe.cond = threading.Condition()
    _, status = err_pipe.take("{ready5}")
    assert status == "0"                             # exit status read from the real marker line


@needs_exiftool
def test_the_resident_is_shut_down_on_the_way_out(tmp_path, resident):
    # Left behind, it would linger as an orphan exiftool holding a pipe. _et_stop is registered
    # with atexit; here we check it really reaps the process.
    p = _write_sample_jpg(tmp_path / "x.jpg")
    assert mm.et_read(p) is not None
    proc = mm._ET_STAY[0]
    mm._et_stop()
    assert proc.poll() is not None, "the resident survived the shutdown"
    assert mm._ET_STAY is None


@needs_exiftool
def test_the_resident_is_tied_to_our_life_on_windows(tmp_path, resident):
    # exiftool does NOT die when its pipe closes (on EOF its argfile loop sleeps 10 ms and
    # retries, for ever): any death of metmux that skips atexit would strand one orphan per
    # session. On Windows a job object with KILL_ON_JOB_CLOSE hands the kill to the OS.
    # The one thing that can be silently wrong is the JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    # layout (a bad size makes SetInformationJobObject reject it). Its size is fixed by the
    # ABI and checkable off Windows: 144 bytes on 64-bit, 112 on 32-bit. Never guess a
    # memory layout.
    import ctypes
    size = ctypes.sizeof(mm._job_limit_struct())
    expected = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
    assert size == expected, f"job struct is {size} bytes, the ABI says {expected}"

    # And off Windows it must be a silent no-op that leaves the resident perfectly usable.
    p = _write_sample_jpg(tmp_path / "x.jpg")
    assert mm.et_read(p) is not None
    mm._et_tie_to_our_life(mm._ET_STAY[0])
    assert mm._ET_JOB is None                        # nothing tied, nothing broken
    assert (mm.et_read(p) or {}).get("FileType") == "JPEG"


# --- macOS Quick Action: the .sh and its copy inside document.wflow stay in sync ---
def test_the_quickaction_script_matches_the_workflow_copy():
    # The script lives twice: metmux_quickaction.sh and the COMMAND_STRING that Automator
    # stores in document.wflow. Every edit must land in both. Sole tolerated divergence:
    # the trailing newline (the .sh has one, Automator stores none) — never "repair" it,
    # Automator would overwrite the fix at the next save.
    import plistlib
    macos = SCRIPT.parent / "integrations" / "macos"
    if not macos.is_dir():
        pytest.skip("integrations/ not shipped (sdist runs the bench without it)")
    sh = (macos / "metmux_quickaction.sh").read_text(encoding="utf-8")
    with (macos / "Edit metadata.workflow" / "Contents" / "document.wflow").open("rb") as f:
        wflow = plistlib.load(f)
    (action,) = wflow["actions"]                     # one shell action, nothing else
    cmd = action["action"]["ActionParameters"]["COMMAND_STRING"]
    assert sh == cmd + "\n", "metmux_quickaction.sh and document.wflow diverged"
