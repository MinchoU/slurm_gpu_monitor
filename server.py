#!/usr/bin/env python3
"""Local dashboard for Slurm GPU utilization.

Polls `ssh <host> python3 <remote>/collect.py` on an interval, keeps a rolling
history in sqlite, and serves a web UI at http://localhost:<port>/.
Stdlib only -- no pip install needed.

Per-user settings (ssh host etc.) live in ~/.config/gpumonitor/config.json;
run ./setup.sh or use the app's setup window to create it, or pass --host.
"""
import argparse
import concurrent.futures
import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import collect  # same directory -- reuses the remote probe script

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "history.db")
INDEX = os.path.join(HERE, "static", "index.html")
CONFIG_PATH = os.environ.get("GPUMON_CONFIG") \
    or os.path.expanduser("~/.config/gpumonitor/config.json")

SETUP_HINT = f"""\
아직 Slurm 로그인 노드가 설정되어 있지 않습니다.

  1) 터미널에서 ./setup.sh 실행  (또는 앱의 "설정…" 메뉴)
  2) 또는 직접 {CONFIG_PATH} 에 작성:
       {{"host": "<ssh에 쓰는 alias, 예: ai>"}}
  3) 또는 실행 시 --host <alias> 지정

단, 그 alias로 비밀번호 없이 ssh (BatchMode)가 통해야 합니다:
  ssh -o BatchMode=yes <alias> echo ok
"""

# ssh multiplexing keeps repeated polls at ~0.3s instead of ~2s.
# PermitLocalCommand=no suppresses the cluster's xhost/X11 warning spam.
# Windows OpenSSH has no multiplexing (and ':' is illegal in ControlPath), so
# skip it there -- plain ssh still works, just a bit slower per poll.
BASE_SSH_OPTS = ["-o", "PermitLocalCommand=no", "-o", "ForwardX11=no",
                 "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
if sys.platform != "win32":
    SSH_OPTS = BASE_SSH_OPTS + [
        "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/gpumon-ssh-%r@%h:%p",
        "-o", "ControlPersist=600",
    ]
else:
    SSH_OPTS = BASE_SSH_OPTS

state = {"snapshot": None, "error": None, "polled_at": 0, "poll_ms": 0,
         "host": None, "interval": 15, "direct": []}
state_lock = threading.Lock()
wake = queue.Queue(maxsize=1)
direct_wake = queue.Queue(maxsize=1)

# Standalone GPU machines (e.g. RLLab boxes) that are NOT part of the Slurm
# cluster: probed by ssh'ing straight to them. Their ssh aliases often carry
# `RemoteCommand bash -l` / `RequestTTY force`, which break non-interactive
# command execution -- override both.
DIRECT_SSH = collect.SSH + ["-o", "RemoteCommand=none", "-o", "RequestTTY=no"]


def probe_direct(node):
    try:
        _, nd = collect.probe_node(node, DIRECT_SSH)
    except subprocess.TimeoutExpired:
        return node, {"error": "ssh timeout"}
    except Exception as e:  # noqa: BLE001
        return node, {"error": str(e)}
    if not nd.get("gpus"):
        return node, {"error": "nvidia-smi 출력 없음 (접속/드라이버 문제)"}
    return node, nd


def direct_poller(nodes, interval):
    """Poll the direct (non-Slurm) nodes in parallel, same rhythm as the
    Slurm poller. Result lands in state['direct'] for the dashboard."""
    while True:
        data = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(nodes))) as ex:
            for node, nd in ex.map(probe_direct, nodes):
                data[node] = nd
        entries = []
        for node in nodes:
            nd = data.get(node, {"error": "no data"})
            if "error" in nd:
                entries.append({"node": node, "error": nd["error"]})
                continue
            gpus = sorted((dict(g) for g in nd["gpus"].values()),
                          key=lambda g: (g.get("idx") is None, g.get("idx")))
            for g in gpus:
                g.setdefault("idx", g.get("smi_idx"))
            entries.append({"node": node, "gpus": gpus})
        with state_lock:
            state["direct"] = entries
        try:
            direct_wake.get(timeout=interval)
        except queue.Empty:
            pass


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    path = os.path.expanduser(CONFIG_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(path, 0o600)


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS samples (
        ts REAL, jobid TEXT, node TEXT, gpu_idx INTEGER,
        util INTEGER, mem_used INTEGER, nproc INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_key ON samples(jobid, node, gpu_idx, ts)")
    conn.commit()
    return conn


def gpu_key(g):
    return (g.get("node"), g.get("idx"))


def poll_once(host, remote, conn):
    t0 = time.time()
    p = subprocess.run(["ssh"] + SSH_OPTS + [host, f"/usr/bin/python3 {remote}"],
                       capture_output=True, text=True, timeout=90)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or "ssh failed").strip()[:400])
    snap = json.loads(p.stdout)
    ts = snap["ts"]

    rows = []
    for j in snap["jobs"]:
        for g in j.get("gpus", []):
            if g.get("error") or g.get("idx") is None:
                continue
            rows.append((ts, j["jobid"], g["node"], g["idx"], g["util"] or 0,
                         g["mem_used"] or 0, len(g.get("procs", []))))
    if rows:
        conn.executemany("INSERT INTO samples VALUES (?,?,?,?,?,?,?)", rows)
    conn.execute("DELETE FROM samples WHERE ts < ?", (ts - 24 * 3600,))
    conn.commit()

    # Enrich each GPU with its recent util history and how long it has been idle.
    for j in snap["jobs"]:
        for g in j.get("gpus", []):
            if g.get("error") or g.get("idx") is None:
                continue
            key = (j["jobid"], g["node"], g["idx"])
            hist = conn.execute(
                "SELECT ts, util FROM samples WHERE jobid=? AND node=? AND gpu_idx=? "
                "AND ts > ? ORDER BY ts", key + (ts - 3600,)).fetchall()
            g["spark"] = [h[1] for h in hist][-120:]
            busy = conn.execute(
                "SELECT MAX(ts) FROM samples WHERE jobid=? AND node=? AND gpu_idx=? "
                "AND (nproc > 0 OR util >= 5)", key).fetchone()[0]
            if g.get("procs"):
                g["idle_for"] = 0
            elif busy:
                g["idle_for"] = ts - busy
            else:
                g["idle_for"] = None  # never seen busy in retained history

    snap["poll_ms"] = int((time.time() - t0) * 1000)
    return snap


def poller(host, remote, interval):
    conn = db()
    while True:
        try:
            snap = poll_once(host, remote, conn)
            with state_lock:
                state.update(snapshot=snap, error=None, polled_at=time.time(),
                             poll_ms=snap["poll_ms"])
        except Exception as e:  # noqa: BLE001
            with state_lock:
                state["error"] = f"{type(e).__name__}: {e}"
                state["polled_at"] = time.time()
        try:
            wake.get(timeout=interval)
        except queue.Empty:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            with state_lock:
                payload = dict(state)
            self._send(200, json.dumps(payload), "application/json")
        elif self.path.startswith("/api/refresh"):
            for q in (wake, direct_wake):
                try:
                    q.put_nowait(1)
                except queue.Full:
                    pass
            self._send(200, '{"ok":true}', "application/json")
        elif self.path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")


def deploy_collector(host):
    """Keep the login node's copy of the collector in step with this checkout."""
    local = os.path.join(HERE, "collect.py")
    try:
        subprocess.run(["ssh"] + SSH_OPTS + [host, "mkdir -p ~/gpumon"],
                       capture_output=True, timeout=30, check=True)
        subprocess.run(["scp", "-q", "-o", "PermitLocalCommand=no", local,
                        f"{host}:~/gpumon/collect.py"],
                       capture_output=True, timeout=30, check=True)
    except Exception as e:  # noqa: BLE001  -- not fatal: an older copy may still work
        print(f"  collector 배포 실패 (기존 사본으로 계속): {e}", flush=True)


def bind(port):
    """Take the requested port, or the next free one if something else holds it."""
    for p in range(port, port + 12):
        try:
            return ThreadingHTTPServer(("127.0.0.1", p), Handler), p
        except OSError:
            continue
    raise SystemExit(f"{port}-{port + 11} 사이에 빈 포트가 없습니다")


def norm_nodes(raw):
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [x for x in raw.split(",")]
    return [str(x).strip() for x in raw if str(x).strip()]


def resolve_settings(args):
    """CLI flag > config file > default."""
    cfg = load_config()
    return {
        "host": args.host or cfg.get("host"),
        "remote": args.remote or cfg.get("remote") or "~/gpumon/collect.py",
        "interval": args.interval or int(cfg.get("interval") or 15),
        "port": args.port or int(cfg.get("port") or 8777),
        "nodes": norm_nodes(cfg.get("nodes")),
    }


def test_connection(host, remote, deploy, nodes):
    """One-shot poll; prints a summary. Exit 0 on success."""
    if deploy:
        deploy_collector(host)
    conn = db()
    try:
        snap = poll_once(host, remote, conn)
    except Exception as e:  # noqa: BLE001
        print(f"실패: {e}")
        return 1
    running = [j for j in snap["jobs"] if j["state"] == "RUNNING"]
    n_gpu = sum(len([g for g in j.get("gpus", []) if not g.get("error")])
                for j in running)
    print(f"성공: {host} 연결 OK · 잡 {len(snap['jobs'])}개(실행 {len(running)}) · "
          f"GPU {n_gpu}개 · {snap['poll_ms']}ms")
    rc = 0
    for node in nodes:
        _, nd = probe_direct(node)
        if "error" in nd:
            print(f"직접 노드 {node}: 실패 — {nd['error']}")
            rc = 1
        else:
            n = len(nd["gpus"])
            busy = sum(1 for g in nd["gpus"].values() if g.get("procs"))
            print(f"직접 노드 {node}: OK · GPU {n}개(가동 {busy})")
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", help="ssh host alias of the login node")
    ap.add_argument("--remote", help="collector path on the login node")
    ap.add_argument("--interval", type=int, help="poll seconds")
    ap.add_argument("--port", type=int)
    ap.add_argument("--no-deploy", action="store_true")
    ap.add_argument("--test", action="store_true",
                    help="run one poll, print the result, and exit")
    args = ap.parse_args()

    s = resolve_settings(args)
    if not s["host"]:
        print(SETUP_HINT)
        raise SystemExit(2)

    if args.test:
        raise SystemExit(test_connection(s["host"], s["remote"], not args.no_deploy,
                                         s["nodes"]))

    if not args.no_deploy:
        deploy_collector(s["host"])

    state["host"] = s["host"]
    state["interval"] = s["interval"]
    srv, port = bind(s["port"])
    threading.Thread(target=poller, args=(s["host"], s["remote"], s["interval"]),
                     daemon=True).start()
    if s["nodes"]:
        threading.Thread(target=direct_poller, args=(s["nodes"], s["interval"]),
                         daemon=True).start()
    # The .app wrapper reads this line to learn which port to open.
    print(f"GPUMON_PORT={port}", flush=True)
    print(f"  SLURM GPU Monitor  ->  http://localhost:{port}")
    extra = f"  + 직접 노드 {', '.join(s['nodes'])}" if s["nodes"] else ""
    print(f"  polling {s['host']} every {s['interval']}s{extra}   (ctrl-c to stop)",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
