# SLURM GPU Monitor

개인 Slurm 클러스터의 GPU 점유/활성 현황을 로컬 대시보드로 보는 도구.
로그인 노드에 배포되는 수집 스크립트(`collect.py`)를 일정한 간격으로 ssh로 돌려,
내 잡이 쓰는 GPU의 util/메모리/프로세스, 같은 노드를 공유하는 사람들,
클러스터 전체 GPU 점유 현황을 웹 대시보드에 표시한다.

A local web dashboard for your personal Slurm cluster: which of your allocated
GPUs are actually busy (vs. just held), who shares your nodes, and cluster-wide
GPU occupancy.

- 순수 stdlib Python — pip 설치 불필요
- macOS 앱(도크) 또는 터미널로 실행
- 설정은 사용자별: `~/.config/gpumonitor/config.json` (리포지토리에 저장 안 됨)

## 전제

- 로그인 노드로 **비밀번호 없이** ssh가 되는 상태 (키 인증, `BatchMode` 가능)
  ```
  ssh -o BatchMode=yes <host-alias> echo ok   # "ok"가 나오면 됨
  ```
- 로그인 노드에 `python3`, `squeue`, `scontrol` 사용 가능 (Slurm 클러스터라면 보통 됨)
- 내 잡이 있는 노드로도 ssh 가능 (대부분의 Slurm은 허용)

## 설치

```
git clone https://github.com/MinchoU/slurm_gpu_monitor ~/gpu-monitor
cd ~/gpu-monitor
```

(앱을 쓸 거면 `~/gpu-monitor` 경로에 clone하는 게 가장 쉬움.
다른 경로로 clone했으면 아래 설정의 `repo` 키로 지정.)

## 설정 (최초 1회)

세 가지 중 아무거나:

1. **macOS 앱**: `GPU Monitor.app` 실행 → 초기 설정 화면에서 SSH host 입력
   → "연결 테스트" → "저장하고 시작"
2. **터미널**: `./setup.sh`
3. **직접 작성**: `~/.config/gpumonitor/config.json`
   ```json
   {
     "host": "ai",
     "interval": 15,
     "port": 8777
   }
   ```

| 키 | 뜻 | 기본값 |
|---|---|---|
| `host` | 로그인 노드의 ssh alias (필수) | — |
| `remote` | 로그인 노드의 collect.py 경로 | `~/gpumon/collect.py` |
| `interval` | 폴링 간격(초) | `15` |
| `port` | 대시보드 포트 (차질이 있으면 다음 포트 자동 시도) | `8777` |
| `repo` | (앱 전용) 이 리포지토리가 있는 경로 | `~/gpu-monitor` |

`host`만 있어야 동작하고, 나머지는 기본값 사용.
CLI 인자(`--host` 등)는 설정 파일보다 우선한다.

## 실행

- **터미널**: `./run.sh` (설정값 사용, 브라우저 자동 열림)
- **옵션**: `python3 server.py --host ai --interval 30 --port 8765`
- **연결 테스트**: `python3 server.py --test` (한 번 폴링 후 종료)
- **macOS 앱**: 아래 빌드 후 `GPU Monitor.app` 실행 (설정 메뉴 ⌘, 로 변경)

## macOS 앱 빌드 (선택)

Xcode Command Line Tools와 Chrome(아이콘 렌더링용)이 필요합니다.

```
./app/build_app.sh
```

`~/Applications/GPU Monitor.app` 을 만듭니다. 소스를 고치면 다시 실행.

## 동작 방식

1. `server.py`(로컬)가 `ssh <host> python3 <remote>` 로 수집 스크립트를 실행
   - 시작할 때 `collect.py` 를 자동으로 scp 재배포
   - ssh ControlMaster로 폴링당 연결 ~0.3s
2. `collect.py`(로그인 노드)가
   - `squeue`/`scontrol` 로 내 잡 + 물리 GPU 인덱스 수집
   - 잡이 있는 각 노드로 ssh → `nvidia-smi` + `ps` 로 실사용률/프로세스/공유 사용자 수집
   - `scontrol show node` 로 클러스터 전체 GPU 할당 현황 수집
3. `server.py`가 24시간 롤링 히스토리를 sqlite(`history.db`)에 저장하고
   `http://localhost:<port>/` 으로 대시보드 서빙

## 알려진 한계

- `PrivateData=jobs` 계열 클러스터에선 **남의 잡** 상세 정보는 Slurm이 노출하지
  않음. 단, 내 잡이 있는 노드로는 ssh가 되므로 그 노드 전체(다른 사용자 포함)의
  실제 util/프로세스는 볼 수 있음.
- 잡이 없는 노드는 ssh 불가 → 클러스터 뷰엔 할당 수(여유/사용)만 보임.
- GRES IDX는 `/dev/nvidia<N>` 마이너 번호라 nvidia-smi 인덱스와 다를 수 있음.
  이 도구는 PCI bus id로 정합시켜 표시함.
