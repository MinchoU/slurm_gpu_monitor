#!/usr/bin/env python3
"""Local dashboard for Slurm GPU utilization.

Polls `ssh <host> python3 <remote>/collect.py` on an interval, keeps a rolling
history in sqlite, and serves a web UI at http://localhost:<port>/.
Stdlib only -- no pip install needed.

Per-user settings (ssh host etc.) live in ~/.config/gpumonitor/config.json;
run ./setup.sh (Windows: setup.cmd) or use the app's setup window to create it, or pass --host.
"""
import argparse
import concurrent.futures
import json
import os
import queue
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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

  1) 터미널에서 ./setup.sh 실행 (Windows: setup.cmd, 또는 앱의 "설정…" 메뉴)
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

# Top-level snapshot/error/... belong to the primary cluster ("host"); extra
# Slurm clusters (config "clusters") each get a slot in state["clusters"].
state = {"snapshot": None, "error": None, "polled_at": 0, "poll_ms": 0,
         "host": None, "name": None, "interval": 15, "direct": [], "clusters": [],
         "notify": False, "events": [], "event_seq": 0,
         "summaries": {}, "summary_error": None, "tasks": []}
state_lock = threading.Lock()
wake = queue.Queue(maxsize=1)
direct_wake = queue.Queue(maxsize=1)
extra_wakes = []  # one per extra cluster
CLUSTERS = {}     # name -> (cluster config, state slot); filled in main()

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
            entries.append({"node": node, "gpus": gpus, "sys": nd.get("sys")})
        with state_lock:
            state["direct"] = entries
        try:
            direct_wake.get(timeout=interval)
        except queue.Empty:
            pass


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    path = os.path.expanduser(CONFIG_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(path, 0o600)


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.execute("""CREATE TABLE IF NOT EXISTS samples (
        ts REAL, jobid TEXT, node TEXT, gpu_idx INTEGER,
        util INTEGER, mem_used INTEGER, nproc INTEGER)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_key ON samples(jobid, node, gpu_idx, ts)")
    conn.execute("""CREATE TABLE IF NOT EXISTS summaries (
        fp TEXT PRIMARY KEY, summary TEXT, progress TEXT, ts REAL)""")
    conn.commit()
    return conn


def collector_call(cl, args=""):
    """argv + stdin that run collect.py on a cluster's login host.

    The primary cluster runs the copy deployed to the login node. Extra clusters
    get the script piped through a heredoc on ssh stdin: nothing to deploy, and
    it also works on hosts whose alias forces `RemoteCommand bash -l` or that
    print nothing for `ssh host 'cmd'` (node200).
    """
    if cl.get("primary"):
        return (["ssh"] + SSH_OPTS + [cl["host"], f"/usr/bin/python3 {cl['remote']} {args}"],
                None)
    with open(os.path.join(HERE, "collect.py"), encoding="utf-8") as f:
        src = f.read()
    script = (f"{cl['python']} - {args} <<'__GPUMON_EOF__'\n{src}\n__GPUMON_EOF__\n")
    return (["ssh"] + SSH_OPTS + ["-o", "RemoteCommand=none", "-o", "RequestTTY=no",
                                  "-T", cl["host"]], script)


# ssh failed before the remote command could run -> safe to retry on another login host
SSH_UNREACHABLE = re.compile(r"Permission denied|Connection refused|timed out|Could not resolve|"
                             r"No route to host|kex_exchange_identification|Host key verification failed|"
                             r"Connection closed by .* port|Network is unreachable")


def run_collector(cl, args="", timeout=90):
    """Run on cl["host"]; if the login node can't be reached, try the
    cluster's fallback hosts in order and stick with the first that works.
    A timeout only falls through for read-only calls: a hung --alloc/--release
    may already have acted, and repeating it elsewhere would double it."""
    hosts = [cl["host"]] + [h for h in cl.get("hosts", []) if h != cl["host"]]
    readonly = not args.startswith(("--alloc ", "--release "))
    for i, h in enumerate(hosts):
        last = i == len(hosts) - 1
        try:
            p = _run_on(cl, h, args, timeout)
        except subprocess.TimeoutExpired:
            if last or not readonly:
                raise
            print(f"GPUMON {cl['name']}: {h} 응답 없음 → {hosts[i + 1]} 시도", flush=True)
            continue
        if p.returncode == 255 and not last and SSH_UNREACHABLE.search(p.stderr or ""):
            print(f"GPUMON {cl['name']}: {h} 접속 실패 ({(p.stderr or '').strip()[-120:]}) → {hosts[i + 1]} 시도",
                  flush=True)
            continue
        if h != cl["host"]:
            cl["host"] = h
            slot = CLUSTERS.get(cl["name"], (None, None))[1]
            if slot is not None:
                slot["host"] = h
            save_live_host(cl["name"], h)
        return _parse_collector(p)


# last login host that worked, per cluster -- so a restart keeps using ai2
# instead of first waiting on a broken ai again
LIVE_HOST_PATH = os.path.join(os.path.dirname(CONFIG_PATH), "live_host.json")


def load_live_host(name, hosts):
    try:
        with open(LIVE_HOST_PATH, encoding="utf-8") as f:
            h = json.load(f).get(name)
        return h if h in hosts else hosts[0]
    except Exception:  # noqa: BLE001
        return hosts[0]


def save_live_host(name, host):
    try:
        try:
            with open(LIVE_HOST_PATH, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:  # noqa: BLE001
            d = {}
        d[name] = host
        with open(LIVE_HOST_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:  # noqa: BLE001
        pass


def run_text(argv, input=None, timeout=None, **kw):
    """subprocess.run(..., text=True) without the Windows surprises: text-mode
    stdin rewrites "\n" as "\r\n" (breaks a script piped to a remote shell)
    and output is decoded with the ANSI codepage instead of UTF-8."""
    p = subprocess.run(argv, input=input.encode("utf-8") if input is not None else None,
                       capture_output=True, timeout=timeout, **kw)
    p.stdout = p.stdout.decode("utf-8", "replace")
    p.stderr = p.stderr.decode("utf-8", "replace")
    return p


def _run_on(cl, host, args, timeout):
    argv, stdin = collector_call(dict(cl, host=host), args)
    return run_text(argv, input=stdin, timeout=timeout)


def _parse_collector(p):
    # a login shell may print banners first; the JSON is the last {...} line
    line = next((ln for ln in reversed(p.stdout.splitlines()) if ln.startswith("{")), None)
    if line is None:
        raise RuntimeError((p.stderr or p.stdout or "ssh failed").strip()[-400:])
    return json.loads(line)


def gpu_key(g):
    return (g.get("node"), g.get("idx"))


def summary_fp(cl, job, g):
    """Identity of 'the run on this GPU': same job + same processes = same run."""
    pids = sorted(str(p.get("pid")) for p in g.get("procs", []))
    return f"{cl['name']}|{job['jobid']}|{g['node']}|{','.join(pids)}"


def poll_once(cl, conn):
    t0 = time.time()
    snap = run_collector(cl, "" if cl.get("primary") else
                         ("--whole-cluster --stdin-ssh" if cl["whole_cluster"] else "--stdin-ssh"))
    ts = snap["ts"]
    # history rows are keyed by jobid; prefix extra clusters' ids so they can't collide
    pre = "" if cl.get("primary") else cl["name"] + ":"

    rows = []
    for j in snap["jobs"]:
        for g in j.get("gpus", []):
            if g.get("error") or g.get("idx") is None:
                continue
            rows.append((ts, pre + j["jobid"], g["node"], g["idx"], g["util"] or 0,
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
            key = (pre + j["jobid"], g["node"], g["idx"])
            if g.get("procs"):
                g["sum_fp"] = summary_fp(cl, j, g)
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


def allocations(snap):
    """{jobid: (name, frozenset of "node:idx")} for the user's RUNNING jobs."""
    out = {}
    for j in snap["jobs"]:
        if j["state"] != "RUNNING":
            continue
        gpus = frozenset(f"{a['node']}:{i}" for a in j.get("alloc", [])
                         for i in a["gpu_idx"])
        if gpus:
            out[j["jobid"]] = (j["name"], gpus)
    return out


def describe(gpus):
    by_node = {}
    for g in sorted(gpus):
        node, idx = g.split(":")
        by_node.setdefault(node, []).append(idx)
    return ", ".join(f"{n}[{','.join(i)}]" for n, i in by_node.items())


def alloc_events(old, new, label=""):
    events = []
    for jid in sorted(new.keys() - old.keys()):
        name, gpus = new[jid]
        events.append({"title": f"GPU {len(gpus)}개 할당됨",
                       "body": f"잡 {jid} ({name}) · {describe(gpus)}"})
    for jid in sorted(old.keys() - new.keys()):
        name, gpus = old[jid]
        events.append({"title": f"GPU {len(gpus)}개 반납됨",
                       "body": f"잡 {jid} ({name}) 종료 · {describe(gpus)}"})
    for jid in sorted(old.keys() & new.keys()):
        if old[jid][1] != new[jid][1]:
            name, gpus = new[jid]
            events.append({"title": "GPU 할당 변경",
                           "body": f"잡 {jid} ({name}) · {describe(gpus)}"})
    if label:
        for ev in events:
            ev["title"] = f"[{label}] {ev['title']}"
    return events


def emit_events(events):
    # The .app wrapper reads these lines and turns them into macOS notifications
    # (only if the user enabled them); in a terminal they are just a log.
    # The page keeps the last few too, so a plain browser tab can notify itself.
    for ev in events:
        print("GPUMON_EVENT=" + json.dumps(ev, ensure_ascii=False), flush=True)
        with state_lock:
            state["event_seq"] += 1
            state["events"] = (state["events"] + [dict(ev, seq=state["event_seq"])])[-20:]


def poller(cl, slot, interval, wake_q):
    """Poll one Slurm cluster forever; results land in `slot` (a state dict)."""
    conn = db()
    announced = None  # allocation state last reported (None until first poll)
    seen = None       # previous poll's allocation state
    label = "" if cl.get("primary") and not state["clusters"] else cl["name"]
    while True:
        try:
            snap = poll_once(cl, conn)
            with state_lock:
                slot.update(snapshot=snap, error=None, polled_at=time.time(),
                            poll_ms=snap["poll_ms"])
            cur = allocations(snap)
            if announced is None:
                announced = cur
            # squeue can come back empty on a transient hiccup, so only report a
            # change once two consecutive polls agree on it.
            elif cur == seen and cur != announced:
                emit_events(alloc_events(announced, cur, label))
                announced = cur
            seen = cur
        except Exception as e:  # noqa: BLE001
            with state_lock:
                slot["error"] = f"{type(e).__name__}: {e}"
                slot["polled_at"] = time.time()
        try:
            wake_q.get(timeout=interval)
        except queue.Empty:
            pass


# ---------------------------------------------------------------------------
# "What is this GPU running?" -- one-line summaries written by Claude Code.
#
# For every GPU of mine with processes on it, gather what the run is (cmdline,
# job script, tail of its log, cwd listing) and ask `claude -p` for a short
# Korean summary. Cached per run fingerprint (job + pids) in history.db and
# refreshed every summary_refresh_min so the progress line stays current.

SUMMARY_PROMPT = """너는 GPU 모니터 대시보드에 들어갈 한 줄 요약을 쓴다.
아래는 Slurm 클러스터의 GPU 하나에서 돌아가는 내 학습/실행(run)에 대한 정보다.
이 run이 무엇인지 사람이 한눈에 알아보도록 요약하라.

반드시 JSON 한 줄로만 답하라 (코드블록·설명 금지):
{{"summary": "무엇을 하는 run인지 — 태스크/환경, 알고리즘/모델, 눈에 띄는 설정 (한국어, 60자 이내)",
  "progress": "로그로 확인되는 현재 진행 상황 — step/epoch/iter, reward/loss/success 등 수치와 추세 (한국어, 60자 이내, 알 수 없으면 빈 문자열)"}}
고유명사·하이퍼파라미터·지표 이름은 원문 그대로 써도 된다. 추측은 '추정'이라고 표시하라.

{context}
"""


def build_context(cl, job, g):
    procs = g.get("procs", [])
    logs = []
    for path in [job.get("stdout")] + [p.get("stdout") for p in procs]:
        if path and path.startswith("/") and not path.startswith(("/dev/", "/proc/")) \
                and path not in logs:
            logs.append(path)
    dirs = []
    for d in [p.get("cwd") for p in procs] + [job.get("workdir")]:
        if d and d not in dirs:
            dirs.append(d)
    spec = {"script": job.get("command"), "logs": logs, "dirs": dirs[:2]}
    try:
        ctx = run_collector(cl, "--context " + shlex.quote(json.dumps(spec)), timeout=60)
    except Exception as e:  # noqa: BLE001  -- summarize from what we already have
        ctx = {"script": None, "logs": {}, "dirs": {}, "error": str(e)}

    parts = [f"[잡] id={job['jobid']} name={job['name']} partition={job['partition']} "
             f"경과={job['elapsed']} 노드={g['node']} GPU={g.get('idx')} ({g.get('name')})",
             f"[GPU 현재] util={g.get('util')}% mem={g.get('mem_used')}/{g.get('mem_total')}MiB"]
    for p in procs[:6]:
        parts.append(f"[프로세스] user={p.get('user')} 실행시간={p.get('etimes')}s "
                     f"gpu_mem={p.get('gpu_mem')}MiB cwd={p.get('cwd', '?')}\n  $ {p.get('cmd', '')[:1500]}")
    if job.get("command"):
        parts.append(f"[잡 커맨드] {job['command']}")
    if ctx.get("script"):
        parts.append("[잡 스크립트 앞부분]\n" + ctx["script"][:2500])
    for d, names in (ctx.get("dirs") or {}).items():
        parts.append(f"[작업 디렉터리 {d} — 최근 수정순]\n" + " ".join(names))
    for path, text in (ctx.get("logs") or {}).items():
        if text:
            parts.append(f"[로그 끝부분: {path}]\n{text[-3500:]}")
    return "\n\n".join(parts)


def claude_bin(cfg):
    for c in (cfg.get("claude"), shutil.which("claude"),
              os.path.expanduser("~/.local/bin/claude"),
              os.path.expanduser("~/.local/bin/claude.exe"), "/opt/homebrew/bin/claude",
              "/usr/local/bin/claude"):
        if c and os.path.isfile(os.path.expanduser(c)):
            return os.path.expanduser(c)
    return None


def ask_claude(binary, model, prompt):
    # Strip the vars a parent Claude Code session sets, so the child runs as a
    # plain headless CLI; run from /tmp so no project CLAUDE.md is picked up.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CLAUDECODE", "CLAUDE_CODE_"))}
    p = run_text([binary, "-p", "--model", model, "--tools", "",
                  "--no-session-persistence", "--strict-mcp-config"],
                 input=prompt, timeout=180, env=env, cwd=tempfile.gettempdir())
    out = p.stdout.strip()
    if p.returncode != 0 or not out:
        raise RuntimeError((p.stderr or out or f"claude exit {p.returncode}").strip()[-300:])
    start, end = out.find("{"), out.rfind("}")
    try:
        d = json.loads(out[start:end + 1])
        return str(d.get("summary", "")).strip(), str(d.get("progress", "")).strip()
    except ValueError:
        return out.splitlines()[0][:120], ""


def summarizer(slots, cfg):
    conn = db()
    model = cfg.get("summary_model") or "haiku"
    refresh = 60 * float(cfg.get("summary_refresh_min") or 30)
    conn.execute("DELETE FROM summaries WHERE ts < ?", (time.time() - 3 * 86400,))
    conn.commit()
    cache = {fp: {"summary": s_, "progress": pr, "ts": ts}
             for fp, s_, pr, ts in conn.execute("SELECT * FROM summaries")}
    failed = {}  # fp -> (time of last failure, reason); retried after 10 min
    binary = claude_bin(cfg)
    if not binary:
        with state_lock:
            state["summary_error"] = "claude CLI를 찾을 수 없음 (설정 'claude'에 경로 지정)"
        return

    while True:
        todo = []  # (cl, job, gpu) for runs needing a (fresh) summary
        live = set()
        with state_lock:
            snaps = [(cl, slot.get("snapshot")) for cl, slot in slots]
        now = time.time()
        for cl, snap in snaps:
            for j in (snap or {}).get("jobs", []):
                for g in j.get("gpus", []):
                    fp = g.get("sum_fp")
                    if not fp or fp in live:
                        continue
                    live.add(fp)
                    c = cache.get(fp)
                    stale = c is None or now - c["ts"] > refresh
                    if stale and now - failed.get(fp, (0, ""))[0] > 600:
                        todo.append((c is not None, cl, j, g))
        todo.sort(key=lambda t: t[0])  # never-summarized runs first
        with state_lock:
            state["summaries"] = {fp: dict(cache[fp], pending=False) for fp in live if fp in cache}
            for fp in live - cache.keys():  # failed ones stay visible, with why
                if fp in failed:
                    state["summaries"][fp] = {"pending": False, "error": failed[fp][1]}
            for _, _, _, g in todo:
                state["summaries"].setdefault(g["sum_fp"], {})["pending"] = True

        if not todo:
            time.sleep(10)
            continue
        _, cl, job, g = todo[0]
        fp = g["sum_fp"]
        try:
            summ, prog = ask_claude(binary, model,
                                    SUMMARY_PROMPT.format(context=build_context(cl, job, g)))
            cache[fp] = {"summary": summ, "progress": prog, "ts": time.time()}
            conn.execute("INSERT OR REPLACE INTO summaries VALUES (?,?,?,?)",
                         (fp, summ, prog, cache[fp]["ts"]))
            conn.commit()
            with state_lock:
                state["summary_error"] = None
                state["summaries"][fp] = dict(cache[fp], pending=False)
        except Exception as e:  # noqa: BLE001
            failed[fp] = (time.time(), f"{type(e).__name__}: {e}"[:200])
            print(f"GPUMON_SUMMARY_FAIL {fp}: {failed[fp][1]}", flush=True)
            with state_lock:
                state["summary_error"] = f"요약 실패: {type(e).__name__}: {e}"[:300]


def release_spec(name, jobid):
    cl, slot = CLUSTERS[name]
    with state_lock:
        snap = slot.get("snapshot") or {}
    job = next((j for j in snap.get("jobs", []) if j["jobid"] == str(jobid)), None)
    if job is None:
        raise ValueError(f"{name}에 잡 {jobid}이 없습니다")
    gpus = [{"node": g["node"], "uuid": g["uuid"], "idx": g["idx"]}
            for g in job.get("gpus", []) if g.get("uuid")]
    return cl, {"jobid": job["jobid"], "stdin_ssh": not cl.get("primary"), "gpus": gpus,
                "name": job["name"]}


# ---------------------------------------------------------------------------
# Background tasks: release (stop/scancel) and GPU requests run in their own
# threads so the page never blocks; the page shows them as chips with a log.

task_seq = [0]


def new_task(kind, cluster, jobid, title):
    with state_lock:
        task_seq[0] += 1
        t = {"id": task_seq[0], "kind": kind, "cluster": cluster, "jobid": jobid,
             "title": title, "status": "running", "phase": "시작", "log": [],
             "started": time.time(), "ended": None}
        state["tasks"] = (state["tasks"] + [t])[-30:]
    return t


def task_update(t, line=None, **kw):
    with state_lock:
        if line:
            t["log"].append(time.strftime("%H:%M:%S ") + line)
        t.update(kw)
        if kw.get("status") in ("done", "failed"):
            t["ended"] = time.time()


def run_task(t, fn):
    def wrapper():
        try:
            fn(t)
        except Exception as e:  # noqa: BLE001
            task_update(t, f"오류: {type(e).__name__}: {e}", status="failed", phase="실패")
    threading.Thread(target=wrapper, daemon=True).start()


def release_task(t, cl, spec):
    what = "정지 + 반납(scancel)" if spec["scancel"] else "프로세스 정지 (할당 유지)"
    task_update(t, f"{what} 시작 — 컨테이너 정지에 최대 30초", phase="종료 중")
    out = run_collector(cl, "--release " + shlex.quote(json.dumps(spec)), timeout=300)
    for ln in out.get("log") or []:
        task_update(t, ln)
    print(f"GPUMON_RELEASE {cl['name']} {spec['jobid']} scancel={spec['scancel']}: "
          f"{out.get('log')}", flush=True)
    wake_all()
    if out.get("error"):
        task_update(t, out["error"], status="failed", phase="실패")
    elif out.get("changed"):
        task_update(t, "확인 후 GPU 위 프로세스가 바뀌어 실행하지 않음 — 다시 눌러 확인하세요",
                    status="failed", phase="중단됨")
    else:
        task_update(t, status="done" if out.get("ok") else "failed",
                    phase=("반납 완료" if spec["scancel"] else "정지 완료") if out.get("ok")
                    else "일부 남음")


def find_job(slot, jobid):
    with state_lock:
        snap = slot.get("snapshot") or {}
        return next((j for j in snap.get("jobs", []) if j["jobid"] == jobid), None), \
            snap.get("ts", 0)


def alloc_task(t, cl, slot, spec):
    task_update(t, "요청: " + json.dumps({k: v for k, v in spec.items() if v}, ensure_ascii=False),
                phase="제출 중")
    out = run_collector(cl, "--alloc " + shlex.quote(json.dumps(spec)), timeout=60)
    for ln in out.get("log") or []:
        task_update(t, ln)
    if not out.get("ok"):
        return task_update(t, out.get("error") or "salloc 실패", status="failed", phase="실패")
    jobid = out["jobid"]
    task_update(t, f"명령: {out['cmd']}", jobid=jobid, phase="대기 중 (PENDING)")
    wake_all()
    # Follow the job until it runs (or disappears). Pending can take hours.
    seen, last_state, start = False, None, time.time()
    while time.time() - start < 14 * 86400:
        time.sleep(5)
        job, _ = find_job(slot, jobid)
        if job is None:
            if seen or time.time() - start > 120:
                return task_update(t, "잡이 큐에서 사라짐 (취소/실패)", status="failed", phase="사라짐")
            continue
        seen = True
        if job["state"] != last_state:
            last_state = job["state"]
            if job["state"] == "RUNNING":
                where = ", ".join(f"{a['node']} GPU {','.join(map(str, a['gpu_idx']))}"
                                  for a in job.get("alloc", [])) or job["nodelist"]
                return task_update(t, f"할당됨: {where} — 접속: srun --jobid={jobid} --overlap --pty bash -i",
                                   status="done", phase=f"할당됨 {where}")
            task_update(t, f"상태 {job['state']} ({job.get('reason', '')})",
                        phase=f"대기 중 ({job.get('reason') or job['state']})")
    task_update(t, "추적 시간 초과", status="failed", phase="추적 중단")


def wake_all():
    for q in [wake, direct_wake] + extra_wakes:
        try:
            q.put_nowait(1)
        except queue.Full:
            pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        # Kill switches: demand a custom header so that only our own page (same
        # origin) can call them -- a cross-site form/fetch can't set it without a
        # CORS preflight, which this server never answers.
        if self.headers.get("X-GPUMon") != "1":
            return self._send(403, "forbidden", "text/plain")
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            if self.path in ("/api/alloc", "/api/alloc/check"):
                cl, slot = CLUSTERS[body["cluster"]]
                spec = {k: str(body.get(k, "")).strip() for k in
                        ("partition", "gpu_type", "gpus", "cpus", "mem", "time", "node", "name", "qos",
                         "exclude")}
                collect.alloc_argv(spec)  # validate before queueing
                if self.path == "/api/alloc/check" and body.get("probe_qos"):
                    spec["probe_qos"] = [[str(q), str(p)] for q, p in body["probe_qos"]]
                if self.path == "/api/alloc/check":  # sbatch --test-only: QOS + expected start
                    out = run_collector(cl, "--alloc-check " + shlex.quote(json.dumps(spec)), timeout=90)
                    return self._send(200, json.dumps(out), "application/json")
                t = new_task("alloc", body["cluster"], None,
                             f"GPU 신청 {body.get('label') or spec['gpu_type'] or 'GPU'}×{spec['gpus']}")
                run_task(t, lambda t: alloc_task(t, cl, slot, spec))
                return self._send(200, json.dumps({"ok": True, "task": t["id"]}), "application/json")
            cl, spec = release_spec(body["cluster"], body["jobid"])
            if self.path == "/api/release/plan":
                out = run_collector(cl, "--release-plan " + shlex.quote(json.dumps(spec)),
                                    timeout=150)
            elif self.path == "/api/release/exec":
                spec.update(token=body["token"], scancel=bool(body.get("scancel")))
                t = new_task("release", body["cluster"], spec["jobid"],
                             f"{spec['jobid']} {'반납' if spec['scancel'] else '정지'}")
                run_task(t, lambda t: release_task(t, cl, spec))
                out = {"ok": True, "task": t["id"]}
            else:
                return self._send(404, "not found", "text/plain")
            out["job_name"] = spec["name"]
            self._send(200, json.dumps(out, ensure_ascii=False), "application/json")
        except Exception as e:  # noqa: BLE001
            self._send(200, json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                       ensure_ascii=False), "application/json")

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
            with state_lock:  # tasks mutate from worker threads: serialize under the lock
                body = json.dumps(state)
            self._send(200, body, "application/json")
        elif self.path.startswith("/api/refresh"):
            wake_all()
            self._send(200, '{"ok":true}', "application/json")
        elif self.path.startswith("/api/notify"):
            on = "on=1" in self.path
            cfg = load_config()
            cfg["notify"] = on
            save_config(cfg)
            with state_lock:
                state["notify"] = on
            if on:  # the .app asks macOS for permission and sends a test banner
                print("GPUMON_NOTIFY_ON", flush=True)
            self._send(200, json.dumps({"notify": on}), "application/json")
        elif self.path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, "not found", "text/plain")


def deploy_collector(host, fallbacks=()):
    """Keep the login node's copy of the collector in step with this checkout."""
    for h in [host, *fallbacks]:
        if _deploy_to(h):
            return


def _deploy_to(host):
    local = os.path.join(HERE, "collect.py")
    try:
        subprocess.run(["ssh"] + SSH_OPTS + [host, "mkdir -p ~/gpumon"],
                       capture_output=True, timeout=30, check=True)
        subprocess.run(["scp", "-q", "-o", "PermitLocalCommand=no", local,
                        f"{host}:~/gpumon/collect.py"],
                       capture_output=True, timeout=30, check=True)
        return True
    except Exception as e:  # noqa: BLE001  -- not fatal: an older copy may still work
        print(f"  collector 배포 실패 ({host}, 기존 사본으로 계속): {e}", flush=True)
        return False


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


def norm_clusters(raw):
    """Config "clusters": extra Slurm clusters reached through their own login
    host. Accepts ["node1"] or [{"name": "RLLab", "host": "node1", ...}]."""
    out = []
    for c in raw or []:
        if isinstance(c, str):
            c = {"host": c}
        if not isinstance(c, dict) or not c.get("host"):
            continue
        out.append({"name": str(c.get("name") or c["host"]), "host": str(c["host"]),
                    "hosts": [str(c["host"])] + [str(h) for h in (c.get("fallback_hosts") or [])],
                    "python": str(c.get("python") or "python3"),
                    "whole_cluster": bool(c.get("whole_cluster", True))})
    return out


def resolve_settings(args):
    """CLI flag > config file > default."""
    cfg = load_config()
    return {
        "host": args.host or cfg.get("host"),
        "fallback_hosts": [str(h) for h in (cfg.get("fallback_hosts") or [])],
        "name": cfg.get("name") or args.host or cfg.get("host"),
        "remote": args.remote or cfg.get("remote") or "~/gpumon/collect.py",
        "interval": args.interval or int(cfg.get("interval") or 15),
        "port": args.port or int(cfg.get("port") or 8777),
        "nodes": norm_nodes(cfg.get("nodes")),
        "clusters": norm_clusters(cfg.get("clusters")),
        "summaries": cfg.get("summaries", True),
    }


def primary_cluster(s):
    hosts = [s["host"]] + s["fallback_hosts"]
    return {"name": s["name"], "host": load_live_host(s["name"], hosts), "hosts": hosts,
            "remote": s["remote"], "primary": True}


def test_connection(s, deploy):
    """One-shot poll; prints a summary. Exit 0 on success."""
    if deploy:
        deploy_collector(s["host"], s["fallback_hosts"])
    conn = db()
    rc = 0
    for cl in [primary_cluster(s)] + s["clusters"]:
        try:
            snap = poll_once(cl, conn)
        except Exception as e:  # noqa: BLE001
            print(f"{cl['name']} ({cl['host']}): 실패 — {e}")
            rc = 1
            continue
        running = [j for j in snap["jobs"] if j["state"] == "RUNNING"]
        n_gpu = sum(len([g for g in j.get("gpus", []) if not g.get("error")])
                    for j in running)
        extra = ""
        if snap.get("whole_cluster"):
            tot = sum(len(n["gpus"]) for n in snap["shared_nodes"])
            extra = f" · 클러스터 전체 GPU {tot}장 프로브"
        if snap.get("node_errors"):
            extra += f" · 노드 오류 {snap['node_errors']}"
        print(f"성공: {cl['name']} ({cl['host']}) 연결 OK · 잡 {len(snap['jobs'])}개"
              f"(실행 {len(running)}) · 내 GPU {n_gpu}개 · {snap['poll_ms']}ms{extra}")
    nodes = s["nodes"]
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
    ap.add_argument("--open", action="store_true",
                    help="open the dashboard in the default browser once it's up")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows consoles/pipes may not be UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    s = resolve_settings(args)
    if not s["host"]:
        print(SETUP_HINT)
        raise SystemExit(2)

    if args.test:
        raise SystemExit(test_connection(s, not args.no_deploy))

    if not args.no_deploy:
        deploy_collector(s["host"], s["fallback_hosts"])

    state["host"] = s["host"]
    state["name"] = s["name"]
    state["interval"] = s["interval"]
    state["notify"] = bool(load_config().get("notify"))
    srv, port = bind(s["port"])
    slots = [(primary_cluster(s), state)]
    state["host"] = slots[0][0]["host"]
    for cl in s["clusters"]:
        cl["host"] = load_live_host(cl["name"], cl["hosts"])
        slot = {"name": cl["name"], "host": cl["host"], "snapshot": None, "error": None,
                "polled_at": 0, "poll_ms": 0}
        state["clusters"].append(slot)
        slots.append((cl, slot))
    CLUSTERS.update({cl["name"]: (cl, slot) for cl, slot in slots})
    for cl, slot in slots:
        q = wake if slot is state else queue.Queue(maxsize=1)
        if slot is not state:
            extra_wakes.append(q)
        threading.Thread(target=poller, args=(cl, slot, s["interval"], q),
                         daemon=True).start()
    if s["summaries"]:
        threading.Thread(target=summarizer, args=(slots, load_config()), daemon=True).start()
    if s["nodes"]:
        threading.Thread(target=direct_poller, args=(s["nodes"], s["interval"]),
                         daemon=True).start()
    # The .app wrapper reads this line to learn which port to open.
    print(f"GPUMON_PORT={port}", flush=True)
    print(f"  SLURM GPU Monitor  ->  http://localhost:{port}")
    if args.open:
        import webbrowser
        threading.Timer(1.0, webbrowser.open, (f"http://localhost:{port}",)).start()
    extra = f"  + 직접 노드 {', '.join(s['nodes'])}" if s["nodes"] else ""
    if s["clusters"]:
        extra += "  + 클러스터 " + ", ".join(f"{c['name']}({c['host']})" for c in s["clusters"])
    print(f"  polling {s['host']} every {s['interval']}s{extra}   (ctrl-c to stop)",
          flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
