#!/bin/sh
# One-time setup: asks for your Slurm login node and writes the config file
# that server.py / the app read. Re-run any time to change settings.
cd "$(dirname "$0")" || exit 1

CFG="$HOME/.config/gpumonitor/config.json"
[ -n "${GPUMON_CONFIG:-}" ] && CFG="$GPUMON_CONFIG"

cur() {
  python3 - "$CFG" "$1" "$2" <<'EOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        print(json.load(f).get(sys.argv[2], sys.argv[3]))
except Exception:
    print(sys.argv[3])
EOF
}

HOST=$(cur "$CFG" host "")
INTERVAL=$(cur "$CFG" interval "15")
PORT=$(cur "$CFG" port "8777")

echo "SLURM GPU Monitor 설정"
echo
echo "로그인 노드로 'ssh <alias>' 하면 바로 들어가는 상태여야 합니다"
echo "(비밀번호/2FA 없이 키 인증). 예: ~/.ssh/config 에 host alias가 이미 있으면"
echo "그 이름을 그대로 쓰세요."
echo

printf "SSH host alias [%s]: " "$HOST"
read H; [ -n "$H" ] && HOST="$H"
printf "폴링 간격(초) [%s]: " "$INTERVAL"
read I; [ -n "$I" ] && INTERVAL="$I"
printf "대시보드 포트 [%s]: " "$PORT"
read P; [ -n "$P" ] && PORT="$P"

[ -n "$HOST" ] || { echo "host를 입력하세요"; exit 1; }

if python3 server.py --host "$HOST" --interval "$INTERVAL" --test; then
  :
else
  echo
  printf "연결 테스트에 실패했습니다. 그래도 저장할까요? [y/N] "
  read a
  case "$a" in y|Y) ;; *) exit 1;; esac
fi

python3 - "$CFG" "$HOST" "$INTERVAL" "$PORT" <<'EOF'
import json, os, sys
path, host, interval, port = sys.argv[1:5]
os.makedirs(os.path.dirname(path), exist_ok=True)
cfg = {"host": host, "interval": int(interval), "port": int(port)}
try:
    with open(path) as f:
        old = json.load(f)
    if isinstance(old, dict):
        old.update(cfg)
        cfg = old
except Exception:
    pass
with open(path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
    f.write("\n")
os.chmod(path, 0o600)
EOF

echo
echo "저장 완료: $CFG"
echo "시작: ./run.sh   (또는 python3 server.py --port $PORT)"
