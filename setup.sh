#!/bin/sh
# One-time setup -- see setup.py (Windows: setup.cmd).
cd "$(dirname "$0")" || exit 1
exec python3 setup.py "$@"
