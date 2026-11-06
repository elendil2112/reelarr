#!/bin/sh
# Run Reelarr on Linux or macOS without Docker.
#
#   ./run.sh            first run: makes a private Python environment in .venv
#                       and installs what Reelarr needs, then starts it
#   git pull && ./run.sh   update: re-installs only if requirements changed
#
# Settings, databases and logs live in your user data folder
# (~/.config/reelarr or ~/Library/Application Support/Reelarr), never here.
set -eu
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "Reelarr needs Python 3.10 or newer: install it, or set PYTHON=/path/to/python3" >&2
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    echo "Reelarr needs Python 3.10 or newer; $PY is $("$PY" -V 2>&1)" >&2
    exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    echo "warning: ffmpeg/ffprobe not found — Reelarr can't read or convert audio without them" >&2
    echo "         (Debian/Ubuntu: sudo apt install ffmpeg · macOS: brew install ffmpeg)" >&2
fi

if [ ! -x .venv/bin/python ]; then
    echo "Setting up Reelarr's Python environment in .venv (one time)…"
    "$PY" -m venv .venv
fi
stamp=.venv/.requirements
want=$(cksum < requirements.txt)
if [ ! -f "$stamp" ] || [ "$(cat "$stamp")" != "$want" ]; then
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -r requirements.txt
    echo "$want" > "$stamp"
fi

exec .venv/bin/python -m app "$@"
