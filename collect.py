#!/usr/bin/python3
"""Runs on the Slurm login node. Emits one JSON snapshot of the user's running
jobs, the physical GPUs allocated to each, and what is actually on those GPUs.

Deployed to <login-node>:~/gpumon/collect.py (or piped in over stdin for
extra clusters) -- keep it stdlib-only and Python 3.6 safe (RLLab's system
python on node01 is 3.6).

Flags:
  --whole-cluster  probe EVERY GPU node and tag each GPU with the Slurm job/user
                   holding it. For clusters with PrivateData=none, where squeue
                   shows everyone's jobs and ssh to any node is allowed.
  --stdin-ssh      send the node probe over ssh stdin instead of as a command
                   (some nodes print nothing for `ssh node 'cmd'`, e.g. node200).
  --context JSON   don't snapshot; read the job script / log tails / cwd listings
                   named in JSON (used to give Claude context for run summaries).
"""
import argparse
import concurrent.futures
import os
import socket
import getpass
import json
import re
import subprocess
import sys
import time

USER = getpass.getuser()
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
       "-o", "ConnectTimeout=5", "-o", "LogLevel=ERROR"]

NODE_PROBE = r"""
# nvidia-smi can take ~10s per call on nodes without persistence mode (node200),
# so the two queries run concurrently and the pid list is reused, not re-queried.
t=$(mktemp -d)
nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits > "$t/apps" 2>/dev/null &
nvidia-smi --query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,pci.bus_id --format=csv,noheader,nounits
wait
echo @@MINOR@@
for d in /proc/driver/nvidia/gpus/*/information; do
  bus=$(basename $(dirname "$d"))
  minor=$(awk -F: '/Device Minor/ {gsub(/[^0-9]/,"",$2); print $2}' "$d")
  echo "$bus $minor"
done
echo @@APPS@@
cat "$t/apps"
echo @@PS@@
pids=$(cut -d, -f2 "$t/apps" | tr -d ' ' | grep -E '^[0-9]+$' | sort -u | paste -sd, -)
rm -rf "$t"
[ -n "$pids" ] && ps -o pid=,user:32=,etimes=,pcpu=,rss=,args= -p "$pids"
echo @@CWD@@
# readable only for our own processes -- others' come back empty
for p in $(echo "$pids" | tr ',' ' '); do
  echo "$p|$(readlink /proc/$p/cwd 2>/dev/null)|$(readlink /proc/$p/fd/1 2>/dev/null)"
done
echo @@SYS@@
# load average, cpu count, memory, and D-state count (a pile of D procs = hung NFS)
echo "load $(cut -d' ' -f1-3 /proc/loadavg)"
echo "ncpu $(grep -c ^processor /proc/cpuinfo)"
awk '/^MemTotal:|^MemAvailable:/ {print $1, $2}' /proc/meminfo
echo "dstate $(ps -eo stat= | grep -c '^D')"
echo "uptime $(cut -d' ' -f1 /proc/uptime)"
exit 0
"""


def norm_bus(b):
    """'00000000:C1:00.0' and '0000:c1:00.0' -> 'c1:00.0'"""
    parts = b.strip().lower().split(":")
    return ":".join(parts[-2:]) if len(parts) >= 2 else b.strip().lower()


def run(cmd, timeout=25, stdin=None):
    # bytes in/out: text-mode stdin on Windows turns "\n" into "\r\n", which
    # breaks scripts piped to a remote shell, and decodes with the ANSI codepage.
    p = subprocess.run(cmd, input=stdin.encode("utf-8") if stdin is not None else None,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    return p.stdout.decode("utf-8", "replace")


def expand_idx(spec):
    """'4' | '0-3' | '0,2-3' -> [4] | [0,1,2,3] | [0,2,3]"""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(int(a), int(b) + 1))
        else:
            out.append(int(part))
    return out


def cluster_occupancy():
    """Every GPU node's total/allocated GPU count, from ONE scontrol call.

    This is all Slurm will reveal about nodes we have no job on: the cluster sets
    PrivateData=jobs (squeue hides other users' jobs) and ssh to a node is denied
    without an allocation there, so real utilization and *who* holds each GPU are
    simply not obtainable remotely. Occupancy (free vs. busy) still is.
    """
    try:
        raw = run(["scontrol", "show", "node", "--oneliner"], timeout=20)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in raw.splitlines():
        f = dict(re.findall(r"(\w+)=(\S*)", line))
        gres = f.get("Gres", "")
        m = re.search(r"gpu:(?:([^:]+):)?(\d+)", gres)
        if not m:
            continue  # CPU-only node
        total = int(m.group(2))
        am = re.search(r"gres/gpu=(\d+)", f.get("AllocTRES", ""))
        alloc = int(am.group(1)) if am else 0
        def num(k):
            try:
                return int(f.get(k, "0"))
            except ValueError:
                return 0
        out.append({
            "node": f.get("NodeName", "?"), "model": m.group(1),
            "gpu_total": total, "gpu_alloc": alloc, "gpu_free": max(0, total - alloc),
            "state": f.get("State", ""),
            # for the allocation form: where can a request fit right now
            "partitions": [x for x in f.get("Partitions", "").split(",") if x],
            "cpu_total": num("CPUTot"), "cpu_alloc": num("CPUAlloc"),
            "mem_total": num("RealMemory"), "mem_alloc": num("AllocMem"),
        })
    out.sort(key=lambda n: n["node"])
    return out


def partitions():
    try:
        raw = run(["scontrol", "show", "partition", "--oneliner"], timeout=20)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in raw.splitlines():
        f = dict(re.findall(r"(\w+)=(\S*)", line))
        if "PartitionName" in f:
            out.append({"name": f["PartitionName"], "max_time": f.get("MaxTime", ""),
                        "default": f.get("Default") == "YES", "state": f.get("State", ""),
                        "allow_qos": [q for q in f.get("AllowQos", "ALL").split(",")
                                      if q and q != "ALL"]})
    return out


def qos_limits():
    """Per-QOS priority, per-user GPU cap and MY current GPU use, plus my
    association's max walltime. sacctmgr is unreachable from Yonsei's login
    node, but slurmctld's assoc_mgr cache has the same numbers ("limit(used)")."""
    out, max_wall = {}, None
    try:
        raw = run(["scontrol", "show", "assoc_mgr", "flags=qos"], timeout=20)
    except Exception:  # noqa: BLE001
        raw = ""
    for b in re.split(r"\n(?=QOS=)", raw):
        m = re.match(r"QOS=([\w.-]+)", b)
        if not m:
            continue
        pr = re.search(r"Priority=(\d+)", b)
        cap = re.search(r"\bgres/gpu=(\d+)\(", b)  # same limit is printed on every user line
        mine = re.search(r"\n\s+%s\(\d+\)\n(.*?)(?=\n\s+[^\s=]+\(\d+\)\n|\Z)" % re.escape(USER), b, re.S)
        used = re.search(r"\bgres/gpu=(?:\d+|N)\((\d+)\)", mine.group(1)) if mine else None
        out[m.group(1)] = {"priority": int(pr.group(1)) if pr else None,
                           "gpu_cap": int(cap.group(1)) if cap else None,
                           "gpu_used": int(used.group(1)) if used else 0}
    try:
        raw = run(["scontrol", "show", "assoc_mgr", "users=" + USER, "flags=assoc"], timeout=20)
        m = re.search(r"UserName=%s\(\d+\).*?MaxWallPJ=(\d+)" % re.escape(USER), raw, re.S)
        max_wall = int(m.group(1)) if m else None  # minutes
    except Exception:  # noqa: BLE001
        pass
    return {"qos": out, "max_wall_min": max_wall}


# ---------------------------------------------------------------------------
# GPU request: `salloc --no-shell` holds an allocation with no shell attached;
# attach later with `srun --jobid=N --overlap --pty bash -i`. salloc blocks
# until the job starts, so it is detached (own session) and logs to a file.

ALLOC_FIELDS = {
    # several partitions = "any of these" (Slurm starts the job in whichever fits
    # first); that is how a VRAM tier spanning A6000/6000ADA partitions is asked for
    "partition": r"^[\w.-]{1,64}(,[\w.-]{1,64}){0,19}$", "exclude": r"^([\w.-]+(,[\w.-]+){0,199})?$", "gpu_type": r"^[\w.-]{0,32}$", "gpus": r"^[0-8]$",
    "cpus": r"^(\d{1,3})?$", "mem": r"^(\d{1,7}[KMGT]?)?$",
    "time": r"^(\d{1,2}-)?\d{1,2}(:\d{2}){0,2}$|^UNLIMITED$", "node": r"^[\w.-]{0,64}$",
    "name": r"^[\w.-]{1,40}$", "qos": r"^[\w.-]{0,40}$",
}


def alloc_argv(spec):
    for k, rx in ALLOC_FIELDS.items():
        if not re.match(rx, str(spec.get(k, ""))):
            raise ValueError("잘못된 값: %s=%r" % (k, spec.get(k)))
    argv = ["salloc", "--no-shell", "-J", spec["name"], "-p", spec["partition"],
            "-N", "1", "-n", "1", "-t", spec["time"]]
    if spec.get("cpus"):  # empty = partition default (DefCpuPerGPU)
        argv += ["-c", str(spec["cpus"])]
    if int(spec["gpus"]):
        argv.append("--gres=gpu:%s%s" % (spec["gpu_type"] + ":" if spec["gpu_type"] else "",
                                         spec["gpus"]))
    if spec.get("mem"):
        argv += ["--mem", spec["mem"]]
    if spec.get("node"):
        argv += ["-w", spec["node"]]
    if spec.get("exclude"):
        argv += ["-x", spec["exclude"]]
    if spec.get("qos"):
        argv += ["-q", spec["qos"]]
    return argv


def test_only(spec):
    """`sbatch --test-only` with the same request: validates it and says when and
    where it would start, without submitting anything."""
    argv = ["sbatch", "--test-only"] + alloc_argv(spec)[2:] + ["--wrap", "true"]
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       universal_newlines=True, timeout=20)
    return p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""


def alloc_check(spec):
    """Find a QOS the partition accepts (some partitions reject the default one,
    e.g. Yonsei's gigabyte_pro6000 needs pro6000_qos) and the expected start."""
    spec = dict(spec)
    probe = spec.pop("probe_qos", None)
    if probe:  # which QOS my account may use: [[qos, a partition that allows it], ...]
        ok = {}
        for q, part in probe[:12]:
            if re.match(r"^[\w.-]{1,40}$", q) and re.match(r"^[\w.-]{1,64}$", part):
                ok[q] = "Invalid qos" not in test_only(dict(spec, qos=q, partition=part,
                                                             node="", exclude="", gpu_type=""))
        return {"qos_ok": ok}
    if spec.get("qos"):
        cands = [spec["qos"]]
    else:
        mine = run(["squeue", "-h", "-u", USER, "-p", spec["partition"], "-o", "%q"], timeout=15).split()
        known = re.findall(r"^QOS=([\w.-]+)", run(["scontrol", "show", "assoc_mgr", "flags=qos"],
                                                   timeout=15), re.M)
        cands = [""] + sorted(set(mine), key=mine.count, reverse=True) + \
            sorted(set(known) - set(mine), key=lambda q: (spec["partition"].split("_")[-1] not in q, q))
    msg = ""
    for q in cands:
        spec["qos"] = q
        msg = test_only(spec)
        if "Invalid qos" not in msg and "QOS" not in msg:
            return {"ok": "to start at" in msg, "qos": q, "msg": msg}
    return {"ok": False, "qos": "", "msg": msg}


def alloc(spec):
    if not spec.get("qos"):
        spec = dict(spec, qos=alloc_check(spec)["qos"])
    argv = alloc_argv(spec)
    d = os.path.expanduser("~/gpumon/alloc")
    os.makedirs(d, exist_ok=True)
    log = os.path.join(d, "%s_%d.log" % (spec["name"], int(time.time() * 1000)))
    with open(log, "w") as f:
        subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    text = ""
    for _ in range(40):  # salloc prints "Pending/Granted job allocation N" within a second or two
        time.sleep(0.25)
        with open(log) as f:
            text = f.read()
        if re.search(r"job allocation \d+|error", text):
            break
    m = re.search(r"job allocation (\d+)", text)
    return {"ok": bool(m), "jobid": m.group(1) if m else None, "cmd": " ".join(argv),
            "log": text.strip().splitlines()[-6:], "logfile": log}


def pending_queue():
    """Everyone's pending jobs, in scheduling-priority order. Only meaningful
    with PrivateData=none (RLLab); elsewhere squeue shows just our own."""
    fmt = "%i|%u|%j|%P|%b|%C|%V|%R|%q|%l|%Q|%S"
    try:
        raw = run(["squeue", "-h", "-t", "PD", "--sort=-p,i", "-o", fmt], timeout=15)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in raw.strip().splitlines():
        f = line.split("|")
        if len(f) < 12:
            continue
        out.append({"jobid": f[0].strip(), "user": f[1].strip(), "name": f[2].strip(),
                    "partition": f[3].strip(), "tres": f[4].strip(), "cpus": f[5].strip(),
                    "submit": f[6].strip(), "reason": f[7].strip(), "qos": f[8].strip(),
                    "time_limit": f[9].strip(), "priority": f[10].strip(),
                    "start": f[11].strip()})  # Slurm's expected start (N/A if unknown)
    return out


def fairshare():
    """Every user's fair-share factor (sshare, fair-tree), best first. Higher
    factor = higher priority boost; rank 1 gets scheduled first, all else equal."""
    try:
        raw = run(["sshare", "-a", "-P", "-n", "-o",
                   "Account,User,RawShares,RawUsage,EffectvUsage,FairShare"], timeout=15)
    except Exception:  # noqa: BLE001
        return None
    users = []
    for line in raw.splitlines():
        f = [x.strip() for x in line.split("|")]
        if len(f) < 6 or not f[1] or f[1] == "root" or not f[5]:
            continue
        try:
            users.append({"account": f[0], "user": f[1], "raw_usage": int(float(f[3] or 0)),
                          "usage": float(f[4] or 0), "fairshare": float(f[5])})
        except ValueError:
            continue
    users.sort(key=lambda u: (-u["fairshare"], u["raw_usage"], u["user"]))
    for i, u in enumerate(users):  # equal factors share a rank (1, 1, 3, ...)
        u["rank"] = users[i - 1]["rank"] if i and u["fairshare"] == users[i - 1]["fairshare"] else i + 1
    cfg = {}
    try:
        for line in run(["scontrol", "show", "config"], timeout=10).splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("PriorityDecayHalfLife", "PriorityWeightFairShare", "PriorityWeightAge"):
                cfg[k.strip()] = v.strip()
    except Exception:  # noqa: BLE001
        pass
    return {"users": users, "half_life": cfg.get("PriorityDecayHalfLife"),
            "weight": cfg.get("PriorityWeightFairShare"), "weight_age": cfg.get("PriorityWeightAge")}


def squeue_jobs():
    fmt = "%i|%P|%j|%T|%M|%N|%b|%C|%R|%q|%L|%l|%V|%Q|%S"
    raw = run(["squeue", "-h", "-u", USER, "-o", fmt])
    jobs = []
    for line in raw.strip().splitlines():
        f = line.split("|")
        if len(f) < 9:
            continue
        jobs.append({
            "jobid": f[0].strip(), "partition": f[1].strip(), "name": f[2].strip(),
            "state": f[3].strip(), "elapsed": f[4].strip(), "nodelist": f[5].strip(),
            "tres": f[6].strip(), "cpus": f[7].strip(), "reason": f[8].strip(),
            "qos": f[9].strip() if len(f) > 9 else "",
            "time_left": f[10].strip() if len(f) > 10 else "",
            "time_limit": f[11].strip() if len(f) > 11 else "",
            "submit": f[12].strip() if len(f) > 12 else "",    # submit time (ISO, local)
            "priority": f[13].strip() if len(f) > 13 else "",
            "start": f[14].strip() if len(f) > 14 else "",     # Slurm's expected start / N/A
            "alloc": [],  # [{node, gpu_idx:[...], cpu_ids}]
        })
    return jobs


def fill_allocations(jobs):
    """scontrol show job -d gives the physical GPU index per node.

    It rejects a comma-separated id list, so query one job at a time.
    """
    running = [j for j in jobs if j["state"] == "RUNNING"]
    if not running:
        return

    def one(job):
        raw = run(["scontrol", "show", "job", "-d", job["jobid"]], timeout=15)
        for k in ("Command", "WorkDir", "StdOut"):
            m = re.search(r"\b%s=(\S*)" % k, raw)
            if m and m.group(1) not in ("", "(null)"):
                job[k.lower()] = m.group(1)
        alloc = []
        for line in raw.splitlines():
            m = re.search(r"Nodes=(\S+)\s+CPU_IDs=(\S+)", line)
            if not m:
                continue
            gm = re.search(r"GRES=\S*?\(IDX:([\d,\-]+)\)", line)
            alloc.append({
                "node": m.group(1),
                "cpu_ids": m.group(2),
                "gpu_idx": expand_idx(gm.group(1)) if gm else [],
            })
        return job, alloc

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for job, alloc in ex.map(one, running):
            job["alloc"] = alloc


def node_sh(node, script, ssh=SSH, stdin_ssh=False, timeout=25):
    """Run a bash script on a compute node and return its stdout."""
    if node == socket.gethostname().split(".")[0]:
        return run(["bash", "-s"], stdin=script, timeout=timeout)
    if stdin_ssh:
        # The marker skips anything a login shell prints before the script.
        return run(ssh + ["-T", node], stdin="echo @@BEGIN@@\n" + script,
                   timeout=timeout + 15).partition("@@BEGIN@@")[2]
    return run(ssh + [node, script], timeout=timeout)


def probe_node(node, ssh=SSH, stdin_ssh=False):
    try:
        out = node_sh(node, NODE_PROBE, ssh, stdin_ssh)
    except subprocess.TimeoutExpired:
        return node, {"error": "ssh timeout"}
    except Exception as e:  # noqa: BLE001
        return node, {"error": str(e)}

    gpu_txt, _, rest = out.partition("@@MINOR@@")
    minor_txt, _, rest = rest.partition("@@APPS@@")
    app_txt, _, rest = rest.partition("@@PS@@")
    ps_txt, _, rest = rest.partition("@@CWD@@")
    cwd_txt, _, sys_txt = rest.partition("@@SYS@@")

    # Slurm's GRES IDX is the /dev/nvidia<N> minor number, which is NOT the
    # nvidia-smi index -- on most nodes the two orderings differ. Join them
    # through the PCI bus id, which both sides agree on.
    minor_by_bus = {}
    for line in minor_txt.strip().splitlines():
        f = line.split()
        if len(f) == 2 and f[1].isdigit():
            minor_by_bus[norm_bus(f[0])] = int(f[1])

    gpus, by_uuid = {}, {}
    for line in gpu_txt.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 9:
            continue
        def num(v, cast=float):
            try:
                return cast(v)
            except ValueError:
                return None
        bus = norm_bus(f[8])
        g = {
            "smi_idx": num(f[0], int), "uuid": f[1], "name": f[2],
            "util": num(f[3], int), "mem_used": num(f[4], int), "mem_total": num(f[5], int),
            "temp": num(f[6], int), "power": num(f[7]), "bus": bus,
            "idx": minor_by_bus.get(bus),  # slurm GRES index
            "procs": [],
        }
        if g["idx"] is not None:
            gpus[g["idx"]] = g
        by_uuid[g["uuid"]] = g

    if not gpus and by_uuid:  # /proc unreadable: fall back to smi index, flag it
        for g in by_uuid.values():
            g["idx"] = g["smi_idx"]
            g["idx_unverified"] = True
            gpus[g["idx"]] = g

    procs = {}
    for line in ps_txt.strip().splitlines():
        m = re.match(r"\s*(\d+)\s+(\S+)\s+(\d+)\s+(\S+)\s+(\d+)\s+(.*)", line)
        if m:
            procs[int(m.group(1))] = {
                "pid": int(m.group(1)), "user": m.group(2), "etimes": int(m.group(3)),
                "pcpu": float(m.group(4)), "rss_mb": int(m.group(5)) // 1024,
                "cmd": m.group(6).strip(),
            }
    for line in cwd_txt.strip().splitlines():
        f = line.split("|")
        if len(f) == 3 and f[0].isdigit() and int(f[0]) in procs:
            if f[1]:
                procs[int(f[0])]["cwd"] = f[1]
            if f[2]:
                procs[int(f[0])]["stdout"] = f[2]

    for line in app_txt.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 3:
            continue
        g = by_uuid.get(f[0])
        if g is None:
            continue
        try:
            pid = int(f[1])
        except ValueError:  # nvidia-smi reports "[N/A]" for pids it can't resolve
            pid = None
        p = dict(procs.get(pid, {"pid": pid, "user": "?", "etimes": 0, "pcpu": 0.0,
                                 "rss_mb": 0, "cmd": "(process info unavailable)"}))
        try:
            p["gpu_mem"] = int(f[2])
        except ValueError:
            p["gpu_mem"] = 0
        g["procs"].append(p)

    return node, {"gpus": gpus, "sys": parse_sys(sys_txt)}


def parse_sys(txt):
    kv = {}
    for line in txt.strip().splitlines():
        k, _, v = line.partition(" ")
        kv[k.rstrip(":")] = v.split()
    try:
        return {
            "load": [float(x) for x in kv["load"]],
            "ncpu": int(kv["ncpu"][0]),
            "mem_total_gb": int(kv["MemTotal"][0]) / 1048576,
            "mem_avail_gb": int(kv["MemAvailable"][0]) / 1048576,
            "dstate": int(kv["dstate"][0]),
            "uptime_s": float(kv["uptime"][0]),
        }
    except (KeyError, IndexError, ValueError):
        return None


def expand_hostlist(spec):
    """'node200' -> ['node200']; 'node[01,100]' -> ['node01', 'node100']"""
    if "[" not in spec:
        return spec.split(",")
    return run(["scontrol", "show", "hostnames", spec], timeout=10).split() or [spec]


def all_gpu_holders():
    """{node: {gres_idx: {jobid, user, name, elapsed}}} for every RUNNING job.

    Only meaningful with PrivateData=none; otherwise this is just our own jobs.
    One `scontrol show job -d --oneliner` call covers the whole cluster.
    """
    try:
        raw = run(["scontrol", "show", "job", "-d", "--oneliner"], timeout=20)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    for line in raw.splitlines():
        if "JobState=RUNNING" not in line:
            continue
        f = dict(re.findall(r"(\w+)=(\S*)", line))
        holder = {"jobid": f.get("JobId", "?"), "name": f.get("JobName", ""),
                  "user": re.sub(r"\(.*", "", f.get("UserId", "?")),
                  "elapsed": f.get("RunTime", "")}
        for m in re.finditer(r"Nodes=(\S+)\s+CPU_IDs=\S+.*?GRES=\S*?\(IDX:([\d,\-]+)\)", line):
            for node in expand_hostlist(m.group(1)):
                for idx in expand_idx(m.group(2)):
                    out.setdefault(node, {})[idx] = holder
    return out


def tail_text(path, nbytes=6000, nlines=40):
    """Last lines of a log, with tqdm-style carriage-return redraws collapsed."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - nbytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    lines = [ln.rsplit("\r", 1)[-1] for ln in data.splitlines()]
    lines = [re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", ln) for ln in lines if ln.strip()]
    return "\n".join(lines[-nlines:])


def context(spec):
    """Read the files named in spec (on the shared filesystem) for a run summary."""
    out = {"script": None, "logs": {}, "dirs": {}}
    script = spec.get("script")
    if script and os.path.isfile(script):
        try:
            with open(script, errors="replace") as f:
                out["script"] = f.read(4000)
        except OSError:
            pass
    for path in spec.get("logs", [])[:4]:
        if path and os.path.isfile(path):
            out["logs"][path] = tail_text(path)
    for d in spec.get("dirs", [])[:3]:
        try:
            ents = sorted(os.scandir(d), key=lambda e: e.stat().st_mtime, reverse=True)
            out["dirs"][d] = [e.name + ("/" if e.is_dir() else "") for e in ents[:15]]
        except OSError:
            pass
    return out


# ---------------------------------------------------------------------------
# Stop / release: tear down what runs on a job's GPUs, then optionally scancel.
#
# scancel alone is not enough: docker containers are children of dockerd, not of
# the job, so they keep the GPU busy after the job is gone. Every GPU process is
# classified by its cgroup -- a docker-<id>.scope means "stop that container".

# Lists every compute process on the node with its owner and docker container.
NODE_GPU_PROCS = r"""
nvidia-smi --query-compute-apps=gpu_uuid,pid --format=csv,noheader 2>/dev/null |
while IFS=', ' read -r u p; do
  case "$p" in ''|*[!0-9]*) continue;; esac
  user=$(ps -o user:32= -p "$p" 2>/dev/null | tr -d ' ')
  cid=$(grep -oE 'docker[-/][0-9a-f]{64}' /proc/$p/cgroup 2>/dev/null | head -1 | sed 's/^docker[-\/]//' | cut -c1-12)
  cmd=$(ps -o args= -p "$p" 2>/dev/null | cut -c1-300)
  echo "PROC|$p|$user|$u|$cid|$cmd"
done
for c in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader 2>/dev/null |
           xargs -r -I{} grep -ohE 'docker[-/][0-9a-f]{64}' /proc/{}/cgroup 2>/dev/null |
           sed 's/^docker[-\/]//' | cut -c1-12 | sort -u); do
  echo "CONT|$c|$(docker inspect --format '{{.Name}}|{{.Config.Image}}' "$c" 2>/dev/null | sed 's#^/##')"
done
exit 0
"""


def node_gpu_procs(node, stdin_ssh):
    out = node_sh(node, NODE_GPU_PROCS, stdin_ssh=stdin_ssh, timeout=40)
    procs, conts = [], {}
    for line in out.splitlines():
        f = line.split("|")
        if f[0] == "PROC" and len(f) >= 6:
            procs.append({"pid": int(f[1]), "user": f[2] or "?", "uuid": f[3],
                          "container": f[4], "cmd": "|".join(f[5:])})
        elif f[0] == "CONT" and len(f) >= 2:
            conts[f[1]] = {"id": f[1], "name": f[2] if len(f) > 2 else "",
                           "image": f[3] if len(f) > 3 else ""}
    return procs, conts


def release_plan(spec):
    """What stopping job spec['jobid'] would do, without doing it.

    spec: {jobid, stdin_ssh, gpus: [{node, uuid, idx}]}. Only processes on the
    job's own GPUs are considered; a container is stopped only if ALL of its GPU
    processes are on this job's GPUs; other users' processes are never touched.
    """
    by_node = {}
    for g in spec["gpus"]:
        if not re.match(r"^GPU-[0-9a-fA-F-]+$", g.get("uuid") or ""):
            raise ValueError("bad gpu uuid")
        by_node.setdefault(g["node"], {})[g["uuid"]] = g.get("idx")
    plan = {"jobid": spec["jobid"], "nodes": []}
    for node, uuids in sorted(by_node.items()):
        procs, conts = node_gpu_procs(node, spec.get("stdin_ssh"))
        mine = [p for p in procs if p["uuid"] in uuids]
        stop, kill, skip = [], [], []
        for cid in sorted({p["container"] for p in mine if p["container"]}):
            c = dict(conts.get(cid, {"id": cid, "name": "", "image": ""}))
            outside = sorted({p["uuid"] for p in procs
                              if p["container"] == cid and p["uuid"] not in uuids})
            c["gpus"] = sorted({uuids[p["uuid"]] for p in mine if p["container"] == cid},
                               key=str)
            if outside:
                c["why"] = "이 잡 밖의 GPU도 쓰는 컨테이너라 건드리지 않음"
                skip.append(c)
            else:
                stop.append(c)
        for p in mine:
            if p["container"]:
                continue
            p = dict(p, gpu=uuids[p["uuid"]])
            if p["user"] == USER:
                kill.append(p)
            else:
                p["why"] = ("다른 사용자 프로세스라 건드리지 않음" if p["user"] != "root"
                            else "root 프로세스(컨테이너 아님)라 권한 없음")
                skip.append(p)
        plan["nodes"].append({"node": node, "stop": stop, "kill": kill, "skip": skip})
    plan["token"] = "|".join(
        "%s:%s:%s" % (n["node"], ",".join(c["id"] for c in n["stop"]),
                      ",".join(str(p["pid"]) for p in n["kill"]))
        for n in plan["nodes"])
    return plan


NODE_TEARDOWN = r"""
CONTS="%(conts)s"; PIDS="%(pids)s"
for c in $CONTS; do
  echo "docker stop $c: $(docker stop -t 30 "$c" 2>&1 | tail -1)"
done
desc() { for c in $(pgrep -P "$1"); do desc "$c"; echo "$c"; done; }
ALL=""
for p in $PIDS; do ALL="$ALL $(desc "$p" | tr '
' ' ') $p"; done
[ -n "${ALL// }" ] && kill -TERM $ALL 2>/dev/null && echo "SIGTERM:$ALL"
for i in $(seq 1 15); do
  left=""; for p in $ALL; do kill -0 "$p" 2>/dev/null && left="$left $p"; done
  [ -z "$left" ] && break; sleep 1
done
[ -n "$left" ] && kill -KILL $left 2>/dev/null && echo "SIGKILL:$left"
exit 0
"""


def release(spec):
    """Re-plan, refuse if the plan changed since the user confirmed it, then stop
    containers, TERM->KILL own processes (and their children), and scancel."""
    plan = release_plan(spec)
    if plan["token"] != spec.get("token"):
        return {"ok": False, "changed": True, "plan": plan,
                "log": ["확인한 뒤로 GPU 위 프로세스가 바뀌어서 실행하지 않았습니다. 다시 확인하세요."]}
    log = []
    for n in plan["nodes"]:
        conts = " ".join(c["id"] for c in n["stop"])
        pids = " ".join(str(p["pid"]) for p in n["kill"])
        if not conts and not pids:
            continue
        assert re.match(r"^[0-9a-f ]*$", conts) and re.match(r"^[0-9 ]*$", pids)
        out = node_sh(n["node"], NODE_TEARDOWN % {"conts": conts, "pids": pids},
                      stdin_ssh=spec.get("stdin_ssh"), timeout=90)
        log += ["%s: %s" % (n["node"], ln) for ln in out.strip().splitlines()]
    if spec.get("scancel"):
        p = subprocess.run(["scancel", str(spec["jobid"])], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
        log.append("scancel %s: %s" % (spec["jobid"],
                                       (p.stdout.strip() or "ok") if p.returncode == 0
                                       else "실패 " + p.stdout.strip()))
    left = []
    for n in plan["nodes"]:
        uu = {g["uuid"] for g in spec["gpus"] if g["node"] == n["node"]}
        try:
            procs, _ = node_gpu_procs(n["node"], spec.get("stdin_ssh"))
        except Exception:  # noqa: BLE001
            continue
        skipped = {p.get("pid") for p in n["skip"]}
        left += ["%s pid %d (%s)" % (n["node"], p["pid"], p["user"]) for p in procs
                 if p["uuid"] in uu and p["pid"] not in skipped]
    log.append("남은 프로세스: " + (", ".join(left) if left else "없음"))
    return {"ok": not left, "plan": plan, "log": log}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--whole-cluster", action="store_true")
    ap.add_argument("--stdin-ssh", action="store_true")
    ap.add_argument("--context")
    ap.add_argument("--release-plan")
    ap.add_argument("--release")
    ap.add_argument("--alloc")
    ap.add_argument("--alloc-check")
    args = ap.parse_args()

    if args.alloc or args.alloc_check:
        try:
            out = alloc(json.loads(args.alloc)) if args.alloc else alloc_check(json.loads(args.alloc_check))
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return

    if args.release_plan or args.release:
        try:
            out = (release_plan(json.loads(args.release_plan)) if args.release_plan
                   else release(json.loads(args.release)))
        except Exception as e:  # noqa: BLE001
            out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        json.dump(out, sys.stdout)
        sys.stdout.write("\n")
        return

    if args.context:
        json.dump(context(json.loads(args.context)), sys.stdout)
        sys.stdout.write("\n")
        return

    jobs = squeue_jobs()
    fill_allocations(jobs)
    occupancy = cluster_occupancy()

    nodes = {a["node"] for j in jobs for a in j["alloc"]}
    holders = {}
    if args.whole_cluster:
        nodes |= {c["node"] for c in occupancy
                  if not re.search(r"DOWN|DRAIN|FAIL|NOT_RESPONDING", c["state"])}
        holders = all_gpu_holders()
    nodes = sorted(nodes)
    node_data = {}
    if nodes:
        def probe(n):
            return probe_node(n, stdin_ssh=args.stdin_ssh)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(nodes))) as ex:
            for node, data in ex.map(probe, nodes):
                node_data[node] = data

    # Which GRES indices on each node are mine (for the whole-node view below).
    my_idx = {}
    for j in jobs:
        for a in j["alloc"]:
            my_idx.setdefault(a["node"], set()).update(a["gpu_idx"])

    for j in jobs:
        j["gpus"] = []
        for a in j["alloc"]:
            nd = node_data.get(a["node"], {})
            if "error" in nd:
                j["gpus"].append({"node": a["node"], "idx": None, "error": nd["error"]})
                continue
            for idx in a["gpu_idx"]:
                g = nd.get("gpus", {}).get(idx)
                if g is None:
                    j["gpus"].append({"node": a["node"], "idx": idx, "error": "gpu not found"})
                else:
                    g = dict(g)
                    g["node"] = a["node"]
                    j["gpus"].append(g)

    # Whole-node view for every node we CAN reach (i.e. have a job on). Because
    # ssh gets us onto the node, nvidia-smi + ps see the entire node -- including
    # other users' GPUs and their usernames, which Slurm's PrivateData would hide.
    shared = []
    for node, nd in node_data.items():
        if "error" in nd:
            continue
        mine = my_idx.get(node, set())
        node_gpus = []
        for idx in sorted(nd.get("gpus", {})):
            g = dict(nd["gpus"][idx])
            g["node"] = node
            g["mine"] = idx in mine
            if idx in holders.get(node, {}):
                g["slurm"] = holders[node][idx]
            node_gpus.append(g)
        # A GPU with a Slurm holder is attributed to that holder: its processes
        # often run as root inside a container, which says nothing about who.
        others = sorted({p["user"] for g in node_gpus if not g.get("slurm")
                         for p in g.get("procs", []) if p["user"] not in (USER, "?")} |
                        {g["slurm"]["user"] for g in node_gpus
                         if g.get("slurm") and g["slurm"]["user"] != USER})
        shared.append({"node": node, "gpus": node_gpus, "sys": nd.get("sys"),
                       "my_idx": sorted(mine), "others": others})
    shared.sort(key=lambda s: s["node"])

    json.dump({"ts": time.time(), "user": USER, "jobs": jobs,
               "shared_nodes": shared, "cluster": occupancy,
               "whole_cluster": args.whole_cluster,
               "queue": pending_queue() if args.whole_cluster else [], "fairshare": fairshare() if args.whole_cluster else None, "partitions": partitions(), "limits": qos_limits(),
               "node_errors": {n: d["error"] for n, d in node_data.items() if "error" in d}},
              sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
