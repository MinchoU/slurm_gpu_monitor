#!/usr/bin/env python3
"""One-time setup (macOS / Linux / Windows): asks for your Slurm login node and
writes the config file that server.py / the app read. Re-run any time.

    python3 setup.py        (Windows: setup.cmd, or  py -3 setup.py)
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.environ.get("GPUMON_CONFIG") or os.path.expanduser("~/.config/gpumonitor/config.json")


def ask(prompt, cur):
    v = input(f"{prompt} [{cur}]: ").strip()
    return v or cur


def main():
    try:
        with open(CFG, encoding="utf-8") as f:
            old = json.load(f)
        old = old if isinstance(old, dict) else {}
    except (OSError, ValueError):
        old = {}

    print("SLURM GPU Monitor 설정\n")
    print("로그인 노드로 'ssh <alias>' 하면 바로 들어가는 상태여야 합니다")
    print("(비밀번호/2FA 없이 키 인증). ~/.ssh/config 에 host alias가 이미 있으면")
    print("그 이름을 그대로 쓰세요.\n")

    host = ask("SSH host alias", old.get("host", ""))
    interval = ask("폴링 간격(초)", str(old.get("interval", 15)))
    port = ask("대시보드 포트", str(old.get("port", 8777)))
    nodes = old.get("nodes", [])
    nodes = ask("직접 ssh되는 GPU 노드(쉼표 구분, 비우면 없음)",
                ",".join(nodes) if isinstance(nodes, list) else str(nodes))
    if not host:
        sys.exit("host를 입력하세요")

    rc = subprocess.call([sys.executable, os.path.join(HERE, "server.py"),
                          "--host", host, "--interval", interval, "--test"])
    if rc != 0 and input("\n연결 테스트에 실패했습니다. 그래도 저장할까요? [y/N] ").strip().lower() != "y":
        sys.exit(1)

    old.update({"host": host, "interval": int(interval), "port": int(port),
                "nodes": [x.strip() for x in nodes.split(",") if x.strip()]})
    os.makedirs(os.path.dirname(CFG), exist_ok=True)
    with open(CFG, "w", encoding="utf-8") as f:
        json.dump(old, f, indent=2, ensure_ascii=False)
        f.write("\n")
    try:
        os.chmod(CFG, 0o600)
    except OSError:
        pass
    print(f"\n저장 완료: {CFG}")
    print("시작: ./run.sh  (Windows: run.cmd)")


if __name__ == "__main__":
    main()
