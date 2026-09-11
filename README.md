# bookoasis-gdrive-reading-sync

Google Drive의 변경을 감지해 **로컬로 복사**하고, 목록 화면에서 상태를 보여 주는
BookOasis 메타데이터 플러그인입니다.

Drive를 마운트해서 쓰다 보면 재생·열람이 느리고 마운트가 끊기면 라이브러리가 통째로
사라집니다. 이 플러그인은 Drive에서 바뀐 것만 골라 로컬 디스크로 내려받아, BookOasis가
**로컬 파일을 직접 보게** 합니다.

**Drive에는 쓰지 않습니다.** 읽기 전용 명령만 사용하며, 쓰기 동사는 코드에서 차단합니다.

## 동기화 정책

> Drive에 있는 것은 로컬과 동일하게. **단 Drive에서 지워져도 로컬은 지우지 않습니다.**

| Drive에서 | 로컬 |
|---|---|
| 파일 추가 | 추가 |
| 파일 내용 변경 | 재복사 |
| 파일 이름변경 / 이동 | 이름변경 / 이동 (0바이트) |
| 폴더 이름변경 / 이동 | 이름변경 / 이동 (0바이트) |
| 삭제 / 휴지통 | **보존** (기록만 남김) |

삭제를 반영하지 않는 것은 의도된 설계입니다. Drive 쪽 실수나 정리 작업이 로컬
보관본을 지우는 일을 막습니다.

## 주요 기능

- **변경 감지** — Drive Changes API. 변경 토큰을 영속화해 재시작해도 이어서 감지
- **안전한 복사** — `rclone copyto` → 로컬 md5 + 크기 검증 → `os.replace`.
  **검증을 통과한 뒤에만** 완료로 기록하므로 깨진 파일이 남지 않음
- **중복 스킵** — 파일 존재 · 원격 md5 존재 · 크기 일치 · md5 일치, **네 조건을 모두**
  만족할 때만 건너뜀. 크기만 보고 넘기지 않음
- **무변경 EDIT 억제 (v0.3.6)** — Drive 가 **내용(size·md5)을 바꾸지 않고** 수정시각만
  올렸을 때는 `edit` 잡을 만들지 않습니다. 수정시각만 바뀐 변경은 어차피 `중복 스킵`
  로 끝나는 데 그 판정을 하려고 로컬 파일 전체를 다시 읽었던 부담을 없앤 것입니다
  (운영 실측 1,619 GB 재독해 방지). 내부적으로 같은 파일을 다시 폴링해도 잡이 쌓이지
  않습니다.
- **자동 스캔 (책 폴더 단위)** — 복사가 끝난 파일이 속한 **책 폴더 하나만** BookOasis
  증분 스캔(`scan_library_path(..., force=False)`)에 넣어 새로 들어온 권만 등록합니다.
  파일마다 라이브러리 전체를 다시 훑지 않습니다. `SCAN_DEBOUNCE_SECONDS`(기본 60초)
  동안 그 폴더에 새 파일이 안 들어오면 스캔하며, 스캔 상태는 목록 화면 경로 칸에
  한 줄로 표시됩니다.
- **진행 상황 표시 (v0.3.8)** — 진행률 막대와 함께 `종결(복사+중복+실패) / 전체` 기준
  퍼센트, **남은 건수**, 그리고 복사·중복·처리중·재시도·실패를 각각 나눠 보여줍니다.
  이전에는 `완료` 가 실제 복사 건수만 세서, 중복으로 끝난 수십만 건이 진행에 반영되지
  않아 84% 진행을 1% 처럼 보이게 했습니다
- **설치 버전 표시 (v0.3.5)** — 플러그인 이름·카테고리 탭·페이지 제목에 실제로 깔려
  있는 버전이 붙습니다. 하드코딩이 아니라 `VERSION` 파일을 읽으므로, 표시되는 숫자가
  곧 설치본입니다
- **병렬 전송** — 기본 5. 실제 바이트를 옮기는 작업만 병렬로 돌리고 이름변경 판정은
  직렬이라 경합이 없음
- **목록 화면** — 서버측 페이징 · 상태/유형/결과 필터 · 검색 · 정렬, 일괄 재처리,
  이력 자동 정리
- **날짜별 로그** — 로컬 시각 + UTC 오프셋으로 기록하고 로컬 자정마다 회전.
  보존 일수 설정 가능
- **preflight** — 실제로 실행된 rclone의 절대경로와 버전을 보고. 최소 버전 미만이면 차단
- **재시작 복구** — 중단된 작업을 되살리고 임시 파일을 정리. 중단은 실패로 세지 않음

## 요구 사항

- BookOasis
- **rclone v1.75.0 이상** — Drive 리모트가 설정돼 있어야 합니다
- Python 3.10+ — **외부 의존성 없음** (표준 라이브러리만 사용)
- BookOasis `.env` 에 `ALLOW_PLUGIN_SUBPROCESS=true` (rclone 실행에 필요)

## 설치

### 1. `subprocess` 허용 설정 (필수)

이 플러그인은 **rclone 실행이 존재 이유**라 `subprocess` 를 씁니다. BookOasis 는 기본적으로
플러그인의 프로세스 실행을 차단하므로, 본체 `.env` 에 아래를 넣어야 로드됩니다.

```
# 외부 프로세스 실행
ALLOW_PLUGIN_SUBPROCESS=true
```

없으면 로드 단계에서 `SecurityError` 로 차단됩니다. 켜 두면 로드는 허용되고, 어떤
플러그인이 어떤 호출을 썼는지는 `logs/plugin_subprocess_allowed.log` 에만 기록됩니다
(대시보드에는 노출되지 않습니다).

> 이 스위치는 **모든** 플러그인에 적용됩니다. 신뢰하는 플러그인만 두세요.

### 2. 파일 배치

폴더 전체를 BookOasis 의 `plugins/metadata/gdrive_reading_sync/` 에 복사합니다.
폴더 이름은 반드시 `gdrive_reading_sync` 여야 합니다 (모듈명·플러그인 id 와 일치해야 함).

```
plugins/metadata/gdrive_reading_sync/
├── gdrive_reading_sync.py   sync_worker.py   store.py   drive_client.py
├── index.html   script.js   style.css
├── settings.html   settings.js   settings.css
└── __init__.py   VERSION
```

### 3. 활성화

1. BookOasis 를 재시작합니다.
2. 환경설정 → 플러그인에서 활성화하고 아래 설정을 채웁니다.

`.py` 를 수정한 뒤에는 **BookOasis 를 재시작해야** 반영됩니다. 서버가 모듈을 메모리에
들고 있어서, 재시작 없이 하는 확인은 옛 코드에 대고 하는 것입니다.

### 갱신

파일을 다시 복사하고 재시작합니다. 자동 업데이트(`update_manifest`)는 꺼 두었습니다 —
저장소가 비공개라 `raw.githubusercontent.com` 이 인증 없이 파일을 주지 않습니다.
저장소를 공개로 전환하면 되살릴 수 있습니다.

## rclone 준비

### 1. 설치 확인

```bash
rclone version
```

**v1.75.0 이상**이어야 합니다. 미만이면 preflight가 차단합니다.

### 2. Drive 리모트 만들기

```bash
rclone config          # n → 이름 입력 → drive 선택 → 브라우저 인증
rclone listremotes     # 리모트 이름 확인
rclone lsd myremote:   # 접근되는지 확인
```

이미 Drive 리모트가 있으면 그것을 쓰면 됩니다.

### 3. 리모트 방식 두 가지

리모트 종류에 따라 명령 조립이 다릅니다. `REMOTE_KIND` 로 고릅니다.

| `REMOTE_KIND` | 조립되는 원본 경로 | 추가 인자 |
|---|---|---|
| `gds` | `<리모트>:<REMOTE_ROOT_PATH>/<파일경로>` | 없음 |
| `folder_id` | `<리모트>:<파일경로>` | `--drive-root-folder-id <REMOTE_ROOT_FOLDER_ID>` |
| `auto` | 리모트 설정을 보고 위 둘 중 하나로 판단 | — |

`REMOTE_ROOT_PATH` 에는 **리모트 기준 상대 경로**를 넣습니다 (`MyDrive/Books`).
`/mnt/...` 같은 마운트 경로가 아닙니다.

### 4. rclone.conf 위치

`RCLONE_CONFIG` 를 비우면 BookOasis의 기본 폴백을 씁니다. 경로를 지정하면 그 파일만
쓰고, 파일이 없으면 **조용히 폴백하지 않고 preflight가 실패**합니다.

**도커에서 주의** — rclone은 토큰을 갱신할 때 임시 파일을 만든 뒤 교체합니다.
`rclone.conf` 는 **쓰기 가능한 디렉터리**에 있어야 하고,
**파일 하나만 bind mount 하지 말고 부모 디렉터리를 통째로** 마운트하세요.

여러 프로세스가 같은 `rclone.conf` 를 동시에 갱신하면 저장 경쟁이 생기므로
**전용 conf 를 권장**합니다.

### 5. 토큰 만료

액세스 토큰이 만료되면 401이 나고 refresh 토큰으로 자동 갱신됩니다. 보통 손댈 일이
없습니다. `invalid_grant` 가 나오면 **refresh 토큰 자체가 만료·취소된 것**이라 리모트를
다시 연결해야 합니다.

```bash
rclone --config <conf경로> config reconnect myremote:
```

## 첫 실행

**첫 실행은 전체 목록을 훑지 않습니다.** 현재 시점의 변경 토큰만 저장하고 그 이후
변경부터 감지합니다. 그래서 대용량 드라이브라도 초기 지연이 없습니다.

> **이미 있는 파일을 채우는 용도가 아닙니다.** 첫 전량 복사는 `rclone copy` 로 따로
> 하고, 이 플러그인은 그 뒤의 증분 반영에 쓰세요.

권장 순서:

1. `DRY_RUN` 을 켠 채로 `ENABLE_SYNC` 활성화 → 감지만 되는지 목록에서 확인
2. 경로가 제대로 잡히면 `DRY_RUN` 을 끄고 복사 시작

`ENABLE_SYNC` 는 기본 꺼짐, `DRY_RUN` 은 기본 켜짐입니다. 설정을 안 채우면 아무 일도
일어나지 않습니다.

> 리모트 셀렉트(`TRANSFER_REMOTE` / `DETECT_REMOTE`)의 목록은 설정 화면 진입 시
> 자동으로 채워집니다. 조회가 실패해도 저장되어 있던 값은 그대로 선택된 상태로
> 유지되며, 저장만 눌러도 설정이 사라지지 않습니다.

## 설정

환경설정 → 플러그인 → 구드 독서 동기화.

> **경로 칸은 `찾아보기` 버튼으로 고를 수 있습니다.** 탐색기는 **폴더만** 선택합니다
> (BookOasis 탐색 API 가 디렉터리만 돌려줍니다). `rclone 실행 파일` / `rclone 설정 파일`
> 두 칸은 파일명이 정해져 있어 **폴더를 고르면 파일명이 자동으로 붙습니다**
> (`rclone.exe`(Windows) / `rclone`(그 외), `rclone.conf`). 다른 파일명을 쓰면 직접
> 입력하세요. 탐색기를 취소하면 기존 입력값은 그대로입니다.

| 키 | 설명 |
|---|---|
| `ENABLE_SYNC` | 동기화 활성화 (기본 꺼짐) |
| `DRY_RUN` | 감지만 하고 복사하지 않음 (기본 켜짐) |
| `RCLONE_BIN` | rclone 실행 파일. 비우면 PATH |
| `RCLONE_CONFIG` | rclone 설정 파일 경로. 비우면 기본 폴백 |
| `TRANSFER_REMOTE` / `DETECT_REMOTE` | 전송용 / 감지용 리모트 |
| `REMOTE_KIND` | `auto` · `gds` · `folder_id` |
| `REMOTE_ROOT_PATH` | 원격 루트 (리모트 기준 상대 경로) |
| `REMOTE_ROOT_FOLDER_ID` | 원격 루트 폴더 ID (`folder_id` 방식) |
| `LOCAL_ROOT` | 로컬 루트. **반드시 설정** |
| `TMP_ROOT` | 임시 폴더. 비우면 `LOCAL_ROOT` 와 같은 볼륨 |
| `EXCLUDED_TOP` | 제외할 최상위 폴더 (쉼표 구분) |
| `EXTENSIONS` | 허용 확장자 (쉼표 구분) |
| `POLL_SECONDS` | 폴링 주기 (기본 60초) |
| `PARALLEL_TRANSFERS` | 병렬 전송 수 (기본 5, 1이면 직렬) |
| `AUTO_SCAN` | 복사가 끝난 **책 폴더**를 품는 라이브러리를 자동으로 스캔 (기본 켜짐) |
| `SCAN_DEBOUNCE_SECONDS` | 책 폴더 자동 스캔 디바운스(초). 이 시간 동안 그 폴더에 새 파일이 안 들어오면 스캔 (기본 60) |
| `JOBS_PER_CYCLE` | 사이클당 처리 상한 |
| `MAX_ATTEMPTS` / `RCLONE_TIMEOUT` | 재시도 상한 / 명령 타임아웃 |
| `RETENTION_DAYS` / `AUTO_CLEANUP` | 종결 이력 보존 일수 / 자동 정리 |
| `LOG_DIR` / `LOG_RETENTION_DAYS` | 로그 디렉터리 (비우면 플러그인 데이터 아래 `logs/`) / 보존 일수 |

`TMP_ROOT` 는 `LOCAL_ROOT` 와 **같은 볼륨**이어야 합니다. 다른 볼륨이면 `os.replace`
가 원자적으로 동작하지 않습니다.

## API

```
GET  /api/webhook/gdrive_reading_sync/status
GET  /api/webhook/gdrive_reading_sync/preflight
GET  /api/webhook/gdrive_reading_sync/jobs?page=1&page_size=50&status=&action=&result=&search=&order=desc
POST /api/webhook/gdrive_reading_sync/retry     {"job_ids":[..]} | {"failed_all":true}
POST /api/webhook/gdrive_reading_sync/cleanup   {"delete_all":false}
POST /api/webhook/gdrive_reading_sync/backfill?back=N
POST /api/webhook/gdrive_reading_sync/rclone-check
```

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| preflight 실패 | rclone 경로·버전(v1.75.0 이상), 리모트 이름 철자, `RCLONE_CONFIG` 파일 존재 |
| 목록이 계속 비어 있음 | Drive에서 실제 변경이 없으면 정상입니다. `/status` 의 `page_token` 이 계속 커지면 감지는 살아 있습니다 |
| 복사가 안 시작됨 | `DRY_RUN` 이 켜져 있는지, `LOCAL_ROOT` 가 설정됐는지, preflight가 성공인지 |
| 403 / 429 | Drive API 한도입니다. `backfill` 폭을 크게 준 직후에 잘 납니다. 잠시 기다리면 회복됩니다 |
| `invalid_grant` | refresh 토큰 만료. `rclone config reconnect` |
| 로그가 안 생김 | `LOG_DIR` 쓰기 권한. 실패해도 워커는 죽지 않고 콘솔로 폴백합니다 |

`backfill` 은 되감기 폭이 클수록 계정 전체 변경을 훑어 느리고 Drive가 한도 오류를
낼 수 있습니다. **작은 값부터** 시도하세요.

## 한계

- **Drive 삭제는 로컬에 반영되지 않습니다** (의도된 정책)
- **첫 실행 시점 이전의 파일은 감지되지 않습니다.** 전량 복사는 `rclone copy` 로 별도
- 폴더 이름변경 전파는 오프라인 테스트로 검증했으나, 개발 환경에서 실제 Drive 폴더
  이름변경 이벤트를 관측할 기회가 없어 **라이브 검증은 미완**입니다

## 테스트

```bash
python test_sync_worker.py     # 오프라인 회귀 93종. 네트워크·BookOasis 불필요
node test_settings_js.js       # 설정 화면 순수 함수 회귀 16케이스
node test_script_js.js         # 진행률 계산 순수 함수 회귀 7케이스
```

## 변경 이력

[CHANGELOG.md](CHANGELOG.md) — 버전별 변경 내용과 그렇게 한 이유.

## 라이선스

[AGPL-3.0](LICENSE) — BookOasis 본체와 동일합니다.
