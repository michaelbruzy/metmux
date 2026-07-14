#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Called by the Finder context menu (Automator Quick Action).
# Replace the path below with the real path of your metmux.py file :
METMUX="$HOME/metmux/metmux.py"

if [ ! -f "$METMUX" ]; then
  osascript - "$METMUX" <<'GUARD'
on run argv
    display dialog "metmux.py was not found here:

" & (item 1 of argv) & "

Open the Quick Action in Automator and fix the METMUX= line with the real path of metmux.py." buttons {"OK"} default button "OK" with icon caution with title "metmux"
end run
GUARD
  exit 1
fi

# LC_ALL=C makes printf %q emit ASCII only: the command survives osascript unmangled.
files=""
for f in "$@"; do
  files="$files $(LC_ALL=C printf '%q' "$f")"
done

# Automator's PATH is minimal: a bare python3 lookup picks Apple's stub, not the Homebrew/python.org one holding mutagen.
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3 python; do
  PY="$(command -v "$c")" && break
done
if [ -z "$PY" ]; then
  osascript <<'GUARD'
display dialog "Python 3 was not found. Install it (python.org or Homebrew), then retry." buttons {"OK"} default button "OK" with icon caution with title "metmux"
GUARD
  exit 1
fi

# Terminal's "do script" types the command into the shell: the leading printf clears screen and scrollback first.
CMD="printf '\\033[2J\\033[3J\\033[H'; $(LC_ALL=C printf '%q' "$PY") $(LC_ALL=C printf '%q' "$METMUX") --mode=ask$files"

# CMD is passed as an osascript argument, never spliced into the AppleScript text: apostrophes and accents cannot break its syntax (-2741).
osascript - "$CMD" <<'APPLESCRIPT'
on run argv
    tell application "Terminal"
        activate
        do script (item 1 of argv)
    end tell
end run
APPLESCRIPT
