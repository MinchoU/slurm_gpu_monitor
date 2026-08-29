#!/usr/bin/python3
"""Runs on the Slurm login node. Emits one JSON snapshot of the user's running
jobs, the physical GPUs allocated to each, and what is actually on those GPUs.

Deployed to <login-node>:~/gpumon/collect.py -- keep it stdlib-only and
Python 3.9 safe.
"""
import concurrent.futures
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
nvidia-smi --query-gpu=index,uuid,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,pci.bus_id --format=csv,noheader,nounits
echo @@MINOR@@
for d in /proc/driver/nvidia/gpus/*/information; do
  bus=$(basename $(dirname "$d"))
  minor=$(awk -F: '/Device Minor/ {gsub(/[^0-9]/,"",$2); print $2}' "$d")
  echo "$bus $minor"
done
echo @@APPS@@
nvidia-smi --query-compute-apps=gpu_uuid,pid,used_memory --format=csv,noheader,nounits
echo @@PS@@
pids=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader | tr '\n' ',' | sed 's/,$//')
[ -n "$pids" ] && ps -o pid=,user:32=,etimes=,pcpu=,rss=,args= -p "$pids"
exit 0
"""


def norm_bus(b):
    """'00000000:C1:00.0' and '0000:c1:00.0' -> 'c1:00.0'"""
    parts = b.strip().lower().split(":")
    return ":".join(parts[-2:]) if len(parts) >= 2 else b.strip().lower()


def run(cmd, timeout=25):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.stdout


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
        out.append({
            "node": f.get("NodeName", "?"), "model": m.group(1),
            "gpu_total": total, "gpu_alloc": alloc, "gpu_free": max(0, total - alloc),
            "state": f.get("State", ""),
        })
    out.sort(key=lambda n: n["node"])
    return out


def squeue_jobs():
    fmt = "%i|%P|%j|%T|%M|%N|%b|%C|%R"
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


def probe_node(node):
    try:
        out = run(SSH + [node, NODE_PROBE], timeout=25)
    except subprocess.TimeoutExpired:
        return node, {"error": "ssh timeout"}
    except Exception as e:  # noqa: BLE001
        return node, {"error": str(e)}

    gpu_txt, _, rest = out.partition("@@MINOR@@")
    minor_txt, _, rest = rest.partition("@@APPS@@")
    app_txt, _, ps_txt = rest.partition("@@PS@@")

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
            "smi_idx": int(f[0]), "uuid": f[1], "name": f[2],
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

    for line in app_txt.strip().splitlines():
        f = [x.strip() for x in line.split(",")]
        if len(f) < 3:
            continue
        g = by_uuid.get(f[0])
        if g is None:
            continue
        pid = int(f[1])
        p = dict(procs.get(pid, {"pid": pid, "user": "?", "etimes": 0, "pcpu": 0.0,
                                 "rss_mb": 0, "cmd": "(process info unavailable)"}))
        try:
            p["gpu_mem"] = int(f[2])
        except ValueError:
            p["gpu_mem"] = 0
        g["procs"].append(p)

    return node, {"gpus": gpus}


def main():
    jobs = squeue_jobs()
    fill_allocations(jobs)

    nodes = sorted({a["node"] for j in jobs for a in j["alloc"]})
    node_data = {}
    if nodes:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, len(nodes))) as ex:
            for node, data in ex.map(probe_node, nodes):
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
            node_gpus.append(g)
        others = sorted({p["user"] for g in node_gpus for p in g.get("procs", [])
                         if p["user"] not in (USER, "?")})
        shared.append({"node": node, "gpus": node_gpus,
                       "my_idx": sorted(mine), "others": others})
    shared.sort(key=lambda s: s["node"])

    json.dump({"ts": time.time(), "user": USER, "jobs": jobs,
               "shared_nodes": shared, "cluster": cluster_occupancy(),
               "node_errors": {n: d["error"] for n, d in node_data.items() if "error" in d}},
              sys.stdout)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
