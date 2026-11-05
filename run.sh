#!/bin/sh
# Start the dashboard and open it in the browser. Ctrl-C stops it.
#   ./run.sh                 # host/interval/port come from the config file
#   ./run.sh --interval 30   # any server.py flag passes through
# Windows: run.cmd
cd "$(dirname "$0")" || exit 1
exec python3 server.py --open "$@"
