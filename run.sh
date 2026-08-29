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

( sleep 3; open "http://localhost:$PORT" ) &
exec python3 server.py "$@"
