#!/bin/sh
# Start the dashboard and open it. Ctrl-C stops it.
#   ./run.sh                 # host/interval/port come from the config file
#   ./run.sh --interval 30   # any server.py flag passes through
cd "$(dirname "$0")" || exit 1

# Port to open in the browser: --port flag, else config, else 8777.
# (server.py may still pick the next free port if this one is taken.)
CFG="${GPUMON_CONFIG:-$HOME/.config/gpumonitor/config.json}"
PORT=$(python3 -c "import json;print(json.load(open('$CFG')).get('port',8777))" 2>/dev/null || echo 8777)
case " $* " in *" --port "*) PORT=$(echo "$*" | sed -n 's/.*--port \([0-9]*\).*/\1/p');; esac

# Open the browser on whatever OS we're on (macOS: open, Linux: xdg-open,
# Windows/Git-Bash: start). Best effort -- the dashboard URL is printed anyway.
open_url() {
  case "$(uname -s 2>/dev/null)" in
    Darwin)  open "$1" 2>/dev/null ;;
    CYGWIN*|MINGW*|MSYS*) start "" "$1" 2>/dev/null ;;
    *)       command -v xdg-open >/dev/null 2>&1 && xdg-open "$1" 2>/dev/null ;;
  esac
}
( sleep 3; open_url "http://localhost:$PORT" ) &
exec python3 server.py "$@"
