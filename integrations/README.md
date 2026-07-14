# metmux - File manager integrations

The simplest way to use `metmux`, and what it was originally built for, is to right-click a file, without having to go through the terminal.

Compatible with Windows, macOS and Linux.

> **Coming from the [main README](../README.md)?** You already have everything. Jump straight to your OS to continue the installation.
>
> Prerequisites otherwise:
> - Python 3.8+ and the engines (exiftool, ffmpeg, mutagen) installed — see [Installation](../README.md#installation) in the main README.
> - The repository fetched ([`metmux.py`](../metmux.py) **and** this [`integrations/`](../integrations/) folder) — see [Get the program](../README.md#get-the-program).

---

## Windows ⊞ — context menu

Goal: add an "Edit metadata" line when right-clicking a file.

1. Move the [`metmux.py`](../metmux.py) file into a stable folder (example: `C:\Tools\metmux\metmux.py`).
2. Open [`windows/metmux.bat`](windows/metmux.bat) in a text editor.
3. On line 5 (the `set "METMUX=` line), replace the example path (`C:\Tools\metmux\metmux.py`) with the real path where you placed [`metmux.py`](../metmux.py) and save.
4. Move [`metmux.bat`](windows/metmux.bat) into the same folder as [`metmux.py`](../metmux.py).
5. Open [`windows/metmux.reg`](windows/metmux.reg) in a text editor.
6. On the last line, replace the example path (`C:\\Tools\\metmux\\metmux.bat`) with the real path. Note: each backslash must be doubled: `\` → `\\`.
7. Save, then double-click [`metmux.reg`](windows/metmux.reg). Windows asks to confirm the addition to the registry: confirm.

→ It's ready. Right-clicking a file now shows "Edit metadata". Confirming opens `metmux` in Cmd.
> On Windows 11, under "Show more options".
>
> When several files are selected, Windows actually starts one metmux per file; metmux then merges them by itself into a single session.

To uninstall, simply double-click [`windows/metmux_uninstall.reg`](windows/metmux_uninstall.reg).

---

## macOS 🍎 — Quick Action (Finder)

Goal: add an "Edit metadata" line when right-clicking a file in the Finder via what macOS calls a "Quick Action".

### Method 1: automatic
1. Right-click [`macos/Edit metadata`](macos/Edit%20metadata.workflow), choose "Open with" then "Automator Installer".
2. Click "Install".
3. The action expects [`metmux.py`](../metmux.py) at `~/metmux/metmux.py` (the repository at the root of your home folder). Placed anywhere else? Go to `~/Library/Services` (a hidden folder: in the Finder, Go menu → "Go to Folder…", paste `~/Library/Services`), then right-click `Edit metadata`, "Open with" then "Automator". Fix the `METMUX=` line with your real path.

### Method 2: manual

1. Open Automator.
2. Choose **New document** at the bottom left, and choose **Quick Action**.
3. At the top of the window, set "Workflow receives current" to "files or folders" in "Finder".
4. In the left column, look for the action "Run Shell Script" and double-click it (or drag it into the area on the right).
5. Set in the box:
   	- Shell: /bin/bash
   	- Pass input: as arguments
6. Clear the default content in the script area, and paste the whole content of [`macos/metmux_quickaction.sh`](macos/metmux_quickaction.sh).
7. On line 5 (the `METMUX=` line), replace the example path (`$HOME/metmux/metmux.py`) with the real path where you placed [`metmux.py`](../metmux.py) and save.
8. Save (Cmd ⌘ + S) choosing a name, "Edit metadata" for example.

→ It's ready. Right-clicking a file in the Finder now shows "Quick Actions" then `Edit metadata` or whatever name you chose. Confirming opens `metmux` in Terminal.

> On the first launch, macOS asks to allow controlling "Terminal": accept. A refusal makes the action silently do nothing afterwards (fixable in System Settings → Privacy & Security → Automation).

---

## Linux 🐧 — context menu

### Nautilus (GNOME), Nemo, Caja

Goal: add an "Edit metadata" line to the **Scripts** menu of the right-click.

1. Open [`linux/Edit metadata`](linux/Edit%20metadata) in a text editor.
2. On line 5 (the `METMUX=` line), replace the example path (`$HOME/metmux/metmux.py`) with the real path where you placed [`metmux.py`](../metmux.py) and save.
3. Copy that script into the scripts folder of your file manager, then make it executable. The commands below run **from the repository folder** (the one holding `metmux.py` and `integrations/`): elsewhere, replace `integrations/linux/Edit metadata` with the full path of that file.

   For Nautilus (GNOME):
   ```sh
   mkdir -p ~/.local/share/nautilus/scripts
   cp "integrations/linux/Edit metadata" ~/.local/share/nautilus/scripts/
   chmod +x ~/.local/share/nautilus/scripts/"Edit metadata"
   ```
   For Nemo (Cinnamon, Linux Mint):
   ```sh
   mkdir -p ~/.local/share/nemo/scripts
   cp "integrations/linux/Edit metadata" ~/.local/share/nemo/scripts/
   chmod +x ~/.local/share/nemo/scripts/"Edit metadata"
   ```
   For Caja (MATE):
   ```sh
   mkdir -p ~/.config/caja/scripts
   cp "integrations/linux/Edit metadata" ~/.config/caja/scripts/
   chmod +x ~/.config/caja/scripts/"Edit metadata"
   ```

→ It's ready. Right-clicking a file now shows Scripts, then "Edit metadata". Confirming opens `metmux` in the console.

### Alternative method: "Open with" (all desktops)

If you prefer, or if your file manager has no Scripts menu (Dolphin on KDE/Plasma, Thunar on Xfce), the [`linux/metmux.desktop`](linux/metmux.desktop) file adds "Edit metadata" to the **Open with** menu.

1. Open [`linux/metmux.desktop`](linux/metmux.desktop) in a text editor.
2. On line 9 (the `Exec=` line), replace the example path (`/home/USER/metmux/metmux.py`) with the real path where you placed [`metmux.py`](../metmux.py), and save.
3. Run this command, from the repository folder:
   ```sh
   cp integrations/linux/metmux.desktop ~/.local/share/applications/
   ```
→ It's ready. Right-clicking a file now shows "Open with" then "Edit metadata". Depending on the desktop, the first time it may only appear under "Other application…": pick it once and it moves up the list.
