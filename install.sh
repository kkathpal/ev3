#!/bin/sh
# EV3 dashboard setup for macOS / Linux: run  sh install.sh  in this folder on each laptop.
# Installs the packages the apps need and sets this laptop's brick. Safe to run again.
cd "$(dirname "$0")" || exit 1
echo
echo " === EV3 dashboard setup ==="
echo

# ---- 1. Python 3.8 or newer, with tkinter ------------------------------------------
if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))'; then
    echo "Python 3 isn't installed."
    echo "Install it from https://www.python.org/downloads/ (on a Mac, the macOS installer), then run this again."
    exit 1
fi
python3 -c "import sys; print('Python', sys.version.split()[0], 'found.')"
if ! python3 -c 'import tkinter' 2>/dev/null; then
    echo "This Python has no tkinter, which the apps' windows need."
    echo "  Mac: install Python from https://www.python.org/downloads/macos/ (or: brew install python-tk)"
    echo "  Linux: sudo apt install python3-tk"
    exit 1
fi

# ---- 2. Packages -----------------------------------------------------------------
echo
echo "Installing the packages (paramiko)..."
# Some Pythons (Homebrew, newer Linux) refuse plain pip installs; fall back to a user install.
python3 -m pip install --disable-pip-version-check -q -r requirements.txt 2>/dev/null ||
    python3 -m pip install --disable-pip-version-check -q --user -r requirements.txt 2>/dev/null ||
    python3 -m pip install --disable-pip-version-check -q --user --break-system-packages -r requirements.txt
if ! python3 -c 'import paramiko' 2>/dev/null; then
    echo "Installing the packages failed. Check the internet connection and run this again."
    exit 1
fi
echo "Packages OK."

# ---- 3. This laptop's brick --------------------------------------------------------
echo
python3 ev3_config.py 2>&1 | grep '^This laptop drives'
printf "Brick name or IP for this laptop (e.g. ev3kishan), or Enter to keep it: "
read -r BRICK
if [ -n "$BRICK" ]; then
    python3 ev3_config.py "$BRICK"
fi

echo
echo " All set. Connect the brick, then:  python3 ev3_drive.pyw"
echo " Phone controller: python3 ev3_phone.py    Status widget: python3 ev3_widget.pyw"
