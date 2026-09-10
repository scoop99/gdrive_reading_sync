# -*- coding: utf-8 -*-
"""BookOasis metadata plugin: gdrive_reading_sync (1단계: 감지 + 화면).

- 복사/스캔/삭제 코드 없음 (job.status='dry_run' 고정)
- rclone copy/sync/move/delete/purge 일체 호출 금지
- Drive 토큰은 utils.rclone_gdrive_copy.get_access_token 재사용 (캐시 포함)
- 백그라운드는 daemon thread 1개 + 라우트는 gamebooks의 _do_register_routes 패턴 그대로
"""
from __future__ import annotations

import glob as _glob
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from pathlib import Path

from flask import jsonify

from plugins.metadata.base import BaseMetadataProvider
from utils.rclone_gdrive_copy import get_access_token

from .drive_client import DriveAPIError, DriveClient
from .store import open_store
from .sync_worker import (
    config_fingerprint,
    poll_once,
    preflight,
    recover_on_start,
    rewind_and_poll,
    process_jobs,
)


logger = logging.getLogger(__name__)

SELF_ID = "gdrive_reading_sync"
ROUTE_BASE = f"/api/webhook/{SELF_ID}"


def _plugin_version() -> str:
    """설치된 버전 문자열. VERSION 을 못 읽으면 빈 문자열 (표시만 빠지고 동작은 그대로)."""
    try:
        raw = (Path(__file__).absolute().parent / "VERSION").read_text(encoding="utf-8")
        return str(json.loads(raw).get("plugin version") or "").strip()
    except Exception:
        return ""


# import 시점 1회. 화면 제목과 /status 응답에 실제 설치 버전을 노출한다.
PLUGIN_VERSION = _plugin_version()
_VER_SUFFIX = f" v{PLUGIN_VERSION}" if PLUGIN_VERSION else ""

# 자동 스캔이 라이브러리를 찾을 세션 (local_folder_watch 샘플 플러그인과 동일)
_WATCHED_SESSIONS = ("general", "adult", "audiobook", "video")


def _pick_library(libraries: list, path: str):
    """`path` 를 품는 가장 깊은 라이브러리를 고른다. 없으면 None.

    libraries: `(session, library_id, physical_path)` 튜플 목록.
    중첩 등록(부모/자식 둘 다 라이브러리)일 때 자식이 이긴다 — 그래야
    새 파일이 실제로 보이는 라이브러리 하나만 스캔한다.
    """
    target = os.path.normcase(os.path.abspath(path))
    best = None
    best_len = -1
    for session, library_id, physical_path in libraries:
        root = str(physical_path or "").strip()
        if not root:
            continue
        root_n = os.path.normcase(os.path.abspath(root))
        if target == root_n or target.startswith(root_n + os.sep):
            if len(root_n) > best_len:
                best, best_len = (session, library_id, root), len(root_n)
    return best


def _display_folder(folder: str, local_root: str) -> str:
    """§5.4 — 로그용 상대경로. 개인 절대경로를 로그에 새지 않기 위함.

    `folder` 가 `LOCAL_ROOT` 아래면 상대경로를 반환한다. 그 밖이거나 `..` 로
    시작하면(상대경로로 표현 불가) 루트를 떼고 슬래시 정규화만 해서 노출한다 —
    절대경로의 볼륨·드라이브 문자를 로그에 남기지 않는다 (T-P7-L5).
    """
    try:
        if local_root:
            rel = os.path.relpath(folder or "", local_root)
            if not rel.startswith("..") and rel != ".":
                return rel.replace(os.sep, "/")
    except ValueError:
        pass
    return (folder or "").replace("\\", "/").lstrip("/")


def _normpath_eq(a: str, b: str) -> bool:
    """§5.2 — `_pick_library` 와 같은 정규화로 두 경로의 동등 비교."""
    return os.path.normcase(os.path.abspath(a or "")) == os.path.normcase(
        os.path.abspath(b or "")
    )


class _DailyTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """**로컬** 자정마다 회전. 활성 파일명 `gdrive_reading_sync.log`,
    회전본은 `gdrive_reading_sync_<YYYYMMDD>.log`.

    로그를 읽는 사람은 서버 운영자다. 그 지역 시각으로 찍고 그 지역 자정에 회전한다.
    그래야 `20260903.log` 안에 그 지역의 9월 3일 하루가 그대로 담긴다.

    - subclass 의 rollover 가 기존 스트림을 닫고 다음 날짜의 `baseFilename`
      을 다시 연다. `delay=True` 라서 init 시점이 아닌 첫 write 시점에 연다.
    - 회전 직후 glob `gdrive_reading_sync_????????.log` 중 최신 `backupCount` 개만
      남긴다 (날짜 없는 활성 파일, `.log.YYYY-MM-DD` 형태 만들지 않음).
    - emit 도중 예외가 나면 console handler 가 남는 한 기록은 살아 있고, 이
      handler 만 비활성화한다 (§3.3).
    """

    suffix_template = "%Y%m%d"

    def __init__(self, base_filename: str, **kw) -> None:
        # baseFilename 은 활성 파일 (날짜 접미사 없음). 회전 시 직접 닫고
        # 자정 기준 YYYYMMDD 를 붙인 새 파일로 다시 연다.
        super().__init__(filename=base_filename, **kw)
        # namer/ rotator 기본값은 그대로 둔다 — 파일명만 우리가 덮어쓴다.

    def rotation_filename(self, default_name: str) -> str:  # type: ignore[override]
        # default_name = baseFilename + ".%Y-%m-%d" 같은 형태. 우리는 그걸 무시하고
        # baseFilename 의 stem 에 YYYYMMDD 를 붙인다.
        base = Path(self.baseFilename)
        # 속성명은 rolloverAt (카멜케이스). rollover_at 으로 쓰면 회전 시 AttributeError 가
        # 나고, emit 의 except 가 그것을 삼켜 handler 를 조용히 끈다 = 로그 사망.
        # 2026-09-03 소킹에서 UTC 자정에 실제로 터졌다.
        stamp = time.strftime(self.suffix_template, time.localtime(self.rolloverAt))
        return str(base.with_name(f"{base.stem}_{stamp}.log"))

    def getFilesToDelete(self):  # type: ignore[override]
        # 우리 패턴: gdrive_reading_sync_????????.log
        dir_name, _base = _split_base(self.baseFilename)
        pattern = str(Path(dir_name) / f"{Path(self.baseFilename).stem}_????????.log")
        files = sorted(_glob.glob(pattern))
        if self.backupCount <= 0:
            return []
        return files[: -int(self.backupCount)]

    def emit(self, record):  # type: ignore[override]
        try:
            super().emit(record)
        except Exception:
            self._disable_quietly()

    def handleError(self, record):  # type: ignore[override]
        # §3.3 — file handler 의 emit 실패가 stderr 에 traceback 을 dump 하지 않도록
        # mute 한다. console handler 가 같은 record 를 들고 있어 거기에는 남는다.
        self._disable_quietly()

    def _disable_quietly(self):
        try:
            self.disabled = True
        except Exception:
            pass
        try:
            self.close()
        except Exception:
            pass


def _split_base(base_filename: str) -> tuple[str, str]:
    return (str(Path(base_filename).parent), Path(base_filename).name)

_SERVICE_STARTED = False
_SERVICE_LOCK = threading.Lock()
_REGISTERED_APPS: set[int] = set()
_ROUTES_LOCK = threading.Lock()


class GdriveReadingSyncMetadataProvider(BaseMetadataProvider):
    id = "gdrive_reading_sync"
    name = "구드 독서 동기화" + _VER_SUFFIX
    is_searchable = False
    category_tab = {
        "title": "구드 동기화" + _VER_SUFFIX,
        "icon": "fa-solid fa-cloud-arrow-down",
        "order": 85,
        "sessions": ["general"],
    }
    # 자동 업데이트 (guide_plugins.md §3 "플러그인 내부 업데이트 계약").
    #
    # 2026-09-09 활성화. 저장소를 공개로 전환해 raw.githubusercontent.com 이
    # 200 을 돌려주는 것을 확인했다. 코어의 업데이트 경로는 인증 헤더 없이 raw URL 에
    # GET 하므로, **저장소가 다시 비공개가 되면 즉시 404 로 실패한다.**
    #
    # files 에 런타임 파일을 "전부" 나열해야 한다. 문서 예시는 모듈/__init__/VERSION
    # 세 개뿐이지만, 그대로 두면 gdrive_reading_sync.py 만 새 버전이 되고
    # sync_worker.py·store.py 는 옛 버전으로 남아 확실히 깨진다.
    #
    # (subprocess 차단은 본체 ALLOW_PLUGIN_SUBPROCESS=true 로 해소됨 — 2026-09-09.
    #  이제 남은 조건은 저장소 공개 전환 하나뿐이다.)
    update_manifest = {
        "enabled": True,
        "provider": "github-raw",
        "raw_base_url": (
            "https://raw.githubusercontent.com/"
            "scoop99/gdrive_reading_sync/main"
        ),
        "files": [
            "gdrive_reading_sync.py",
            "sync_worker.py",
            "store.py",
            "drive_client.py",
            "__init__.py",
            "index.html",
            "script.js",
            "style.css",
            "settings.html",
            "settings.js",
            "settings.css",
            "VERSION",
        ],
        "version_file": "VERSION",
        "version_key": "plugin version",
        "show_sample_update_button": True,
    }
    # copy-on-write 용 베이스. _refresh_remote_options() 가 새 list 를 만들어 대입한다.
    # 게시판 설치 검증(plugin_board.py:1648)은 `config_schema` 가
    #   (a) 어노테이션 없는 일반 대입문이고  (b) 값이 리터럴 리스트
    # 일 때만 필수 필드로 인정한다. `config_schema: list = _BASE_CONFIG_SCHEMA`
    # 형태는 둘 다 어겨서 '필수 필드: 클래스에 없음' 으로 설치가 막혔다.
    # 그래서 리터럴을 config_schema 에 직접 두고, copy-on-write 기준인
    # _BASE_CONFIG_SCHEMA 는 그 뒤에서 같은 객체를 가리키게 한다.
    config_schema = [
        {"key": "ENABLE_SYNC", "label": "동기화 활성화", "type": "checkbox", "default": False},
        {"key": "DRY_RUN", "label": "dry_run — 감지만, 복사 안 함 (안전 게이트)", "type": "checkbox", "default": True},
        {"key": "RCLONE_BIN", "label": "rclone 실행 파일", "type": "text", "default": "",
         "description": "비우면 PATH에서 찾아 사용합니다. 설정 화면의 실행 확인으로 경로와 버전을 검사할 수 있습니다."},
        # §10 (S8) — RCLONE_CONFIG: 비우면 기존 BookOasis env / 도커 번들 / ~/.config
        # 폴백을 그대로 쓴다. 명시 경로가 정본이면 그 파일을 사용, 부재 시 preflight 실패.
        {"key": "RCLONE_CONFIG", "label": "rclone 설정 파일 경로", "type": "text", "default": "",
         "description": "비우면 BookOasis 기본 설정을 사용합니다. 확인하면 Drive 리모트 목록도 갱신됩니다."},
        {"key": "TRANSFER_REMOTE", "label": "전송 rclone 리모트", "type": "select",
         "options": [], "default": ""},
        {"key": "DETECT_REMOTE", "label": "감지용 rclone 리모트", "type": "select",
         "options": [], "default": ""},
        {"key": "REMOTE_KIND", "label": "원격 연결 방식", "type": "select",
         "options": [{"value": "auto", "label": "자동 판별"},
                     {"value": "gds", "label": "GDS 경로 방식"},
                     {"value": "folder_id", "label": "Google Drive 폴더 ID 방식"}],
         "default": "auto",
         "description": "자동 판별은 리모트 레코드에 GDS 접속 정보가 있으면 GDS, 없으면 폴더 ID 방식을 사용합니다."},
        {"key": "REMOTE_ROOT_PATH", "label": "원격 루트 경로", "type": "text", "default": "",
         "description": "GDS 경로 방식에서 리모트 이름 뒤에 붙는 기준 경로입니다."},
        {"key": "REMOTE_ROOT_FOLDER_ID", "label": "원격 루트 폴더 ID", "type": "text", "default": "",
         "description": "폴더 ID 방식에서 동기화 범위를 제한할 Google Drive 폴더 ID입니다."},
        {"key": "LOCAL_ROOT", "label": "로컬 루트", "type": "text", "default": ""},
        # §3 (S1) — TMP_ROOT 비우면 same-volume 폴백 (LOCAL_ROOT.parent / _reading_sync_tmp)
        {"key": "TMP_ROOT", "label": "임시 폴더", "type": "text", "default": "",
         "description": "전송 중 .part 파일을 저장합니다. 비우면 로컬 루트 상위의 _reading_sync_tmp를 사용합니다."},
        {"key": "EXCLUDED_TOP", "label": "제외 최상위 (콤마)", "type": "text", "default": ""},
        {"key": "EXTENSIONS", "label": "허용 확장자 (콤마)", "type": "text", "default": ".zip,.cbz,.epub,.pdf,.txt,.yaml,.xml,.json"},
        {"key": "POLL_SECONDS", "label": "폴링 주기(초)", "type": "number", "default": 60},
        {"key": "MAX_ATTEMPTS", "label": "시도 횟수 상한", "type": "number", "default": 3},
        {"key": "RCLONE_TIMEOUT", "label": "rclone 명령 타임아웃(초)", "type": "number", "default": 1800},
        {"key": "JOBS_PER_CYCLE", "label": "사이클당 claim 상한", "type": "number", "default": 20},
        # Drive changes.list 는 사용자당 요청 속도 제한이 있다. 한 사이클에 수백 페이지를
        # 연속 호출하면 403(rate limit)이 난다. 상한을 두면 밀린 이력을 여러 사이클에
        # 나눠 소화한다. 1~500 범위로 정규화.
        {"key": "MAX_PAGES_PER_POLL", "label": "사이클당 변경 페이지 상한", "type": "number", "default": 50},
        # §8 (S6) — 보존기간 + 자동 정리
        {"key": "RETENTION_DAYS", "label": "종결 이력 보존 기간 (일)", "type": "number", "default": 30},
        {"key": "AUTO_CLEANUP", "label": "종결 이력 자동 정리", "type": "checkbox", "default": True,
         "description": "한 시간마다 확인해 보존 기간이 지난 완료·건너뜀·실패 이력만 삭제합니다. 실제 파일과 진행 중 작업은 삭제하지 않습니다."},
        # 3라운드-B T1 — 날짜별 로그. 기본값은 의도적으로 빈 문자열.
        # 비우면 plugin data 디렉터리(state.db 가 있는 곳) 아래 logs/ 로 폴백한다.
        # 특정 환경의 드라이브 문자나 개인 폴더명을 기본값으로 두지 않는다 (공개 저장소).
        {"key": "LOG_DIR", "label": "로그 디렉터리 (비우면 플러그인 데이터 아래 logs/)", "type": "text", "default": ""},
        {"key": "LOG_RETENTION_DAYS", "label": "로그 파일 보존 기간 (일)", "type": "number", "default": 14},
        # 3라운드-B T2 — 병렬 전송 (복사 job 만). 기본 5, 1..32 clamp. =1 이면
        # 기존 단일 스레드와 100% 같은 코드 경로를 탄다 (§4.1 / 합격선 P1).
        {"key": "PARALLEL_TRANSFERS", "label": "병렬 전송 동시 실행 수 (1이면 직렬)", "type": "number", "default": 5},
        # 복사가 끝난 폴더를 품는 라이브러리만 증분 스캔(force=False)에 넣는다.
        {"key": "AUTO_SCAN", "label": "복사 완료 후 자동 스캔", "type": "checkbox", "default": True,
         "description": "파일이 로컬에 내려앉으면 그 폴더가 속한 BookOasis 라이브러리를 스캔해 새 책을 자동 등록합니다. 증분 스캔이라 이미 등록된 책은 다시 읽지 않습니다."},
        # P9 — books 폴더 단위 자동 스캔의 디바운스. 새 파일이 안 들어온 채 이 시간이
        # 지나야 그 폴더를 스캔한다. 티크 주기는 상수 5초 (§5.1).
        {"key": "SCAN_DEBOUNCE_SECONDS", "label": "스캔 디바운스(초)", "type": "number",
         "default": 60,
         "description": "이 시간 동안 그 폴더에 새 파일이 안 들어오면 스캔합니다."},
    ]
    # copy-on-write 기준. _refresh_remote_options() 가 이걸 원본 삼아
    # 새 list 를 만들어 self.config_schema 에 대입한다 (원본 불변).
    _BASE_CONFIG_SCHEMA = config_schema
    _last_cleanup_monotonic: float = 0.0  # §8.2 — 자동 정리 1시간 gate
    # 3라운드-B T1 — runtime logger 캐시. fingerprint 가 바뀔 때만 handler 를 갈아끼운다.
    _runtime_logger = None  # type: ignore[assignment]
    _runtime_logger_fp: tuple = ()
    _runtime_logger_lock = threading.Lock()

    # ---- search/apply stub (BaseMetadataProvider 계약을 위한 최소 구현) ----
    def search(self, db_type, query):
        return []

    def apply(self, db_type, book_id, item_data):
        return False, "이 플러그인은 메타데이터 적용을 지원하지 않습니다."

    # ---- config helpers ----
    def _cfg(self, db_type: str) -> dict:
        cfg = self.get_plugin_config(db_type, default={}) or {}
        # 스키마 default 병합
        merged: dict = {}
        for entry in self.config_schema:
            key = entry.get("key")
            if key:
                merged.setdefault(key, entry.get("default"))
        for k, v in cfg.items():
            if k in merged:
                merged[k] = v
        return merged

    # §4.1 — 동적 options 갱신. 클래스 속성 config_schema 를 통째로 교체한다.
    def _refresh_remote_options(self, cfg: dict) -> dict:
        from .sync_worker import _rclone_config_dump, _resolve_rclone_bin, _rclone_config_args
        bin_path = _resolve_rclone_bin(cfg)
        try:
            # §10 — RCLONE_CONFIG 정본 경로 또는 폴백. 부재 경로 시 아래 except 에서 잡힘.
            dump = _rclone_config_dump(bin_path, cfg)
            names = sorted(
                n for n, rec in (dump or {}).items()
                if isinstance(rec, dict) and rec.get("type") == "drive"
            )
            error = ""
        except FileNotFoundError as exc:
            # §10 — 명시 RCLONE_CONFIG 가 부재. 설정 화면엔 빈 remotes + 오류만 노출.
            names = []
            error = str(exc)
        except Exception as exc:
            # 폴백은 현재 설정값만. 특정 리모트 이름을 코드에 박지 않는다.
            names = sorted({
                (cfg.get("TRANSFER_REMOTE") or "").strip(),
                (cfg.get("DETECT_REMOTE") or "").strip(),
            } - {""})
            error = str(exc)
        opts = [{"value": n, "label": n} for n in names]
        new_schema = []
        for entry in self._BASE_CONFIG_SCHEMA:
            if entry.get("key") in ("TRANSFER_REMOTE", "DETECT_REMOTE"):
                clone = dict(entry)
                clone["options"] = opts
                new_schema.append(clone)
            else:
                new_schema.append(entry)
        # copy-on-write — 부분 수정된 schema 를 다른 thread 가 보지 않게 한다.
        self.config_schema = new_schema
        return {"remotes": names, "error": error}

    def _cleanup_if_due(
        self,
        store,
        cfg: dict,
        now_monotonic: float | None = None,
        *,
        log=print,
    ) -> int:
        """§8.2 — 자동 정리 1시간 due. AUTO_CLEANUP=true 일 때만 동작.

        monotonic 1시간 gate. _WRITER_LOCK 안에서 짧게 DELETE.
        """
        import time as _t
        if not self._is_truthy(cfg.get("AUTO_CLEANUP", True)):
            return 0
        now_m = now_monotonic if now_monotonic is not None else _t.monotonic()
        if now_m - float(self._last_cleanup_monotonic or 0.0) < 3600.0:
            return 0
        rd = self._retention_days(cfg)
        try:
            deleted = store.cleanup_terminal(retention_days=rd)
        except Exception as exc:
            log(f"[{SELF_ID}] cleanup failed: {exc}")
            store.set_state(status="error", error=f"cleanup: {exc}", last_poll_at=_iso_now())
            return 0
        # §12-D — `scan` 행 보존 정리. 같은 RETENTION_DAYS / 같은 1시간 gate 재사용.
        # pending / running 은 절대 지우지 않는다 (scan_cleanup 이 보장).
        try:
            deleted_scans = store.scan_cleanup(retention_days=rd)
            if deleted_scans:
                log(f"[{SELF_ID}] cleanup: {deleted_scans} scan row(s) deleted (retention={rd})")
        except Exception as exc:
            log(f"[{SELF_ID}] scan_cleanup failed: {exc}")
        self._last_cleanup_monotonic = now_m
        if deleted:
            log(f"[{SELF_ID}] cleanup: {deleted} terminal job(s) deleted (retention={rd})")
        return deleted

    @staticmethod
    def _is_truthy(val) -> bool:
        if isinstance(val, str):
            return val.strip().lower() in ("true", "1", "yes", "on")
        if val is None:
            return False
        return bool(val)

    @staticmethod
    def _retention_days(cfg: dict) -> int:
        try:
            v = int(cfg.get("RETENTION_DAYS", 30))
        except Exception:
            v = 30
        return max(1, min(v, 3650))

    # ---- 3라운드-B T1 — 날짜별 로그 -----------------------------------
    @staticmethod
    def _log_retention_days(cfg: dict) -> int:
        """§3.2 — LOG_RETENTION_DAYS 정규화. 비수치 14, 1..3650 clamp."""
        try:
            v = int(cfg.get("LOG_RETENTION_DAYS", 14))
        except Exception:
            v = 14
        return max(1, min(v, 3650))

    @staticmethod
    def _resolve_log_dir(cfg: dict, data_dir: Path) -> Path:
        """§3.1 — LOG_DIR 정규화.

        - 공백이면 `data_dir/logs` (플러그인 데이터 디렉터리, state.db 옆).
        - 명시값은 `Path(value).expanduser().absolute()` — `resolve()` 금지 규약 준수.
        - 절대 경로 비교만 한다 (resolve() 안 함).
        """
        raw = (cfg.get("LOG_DIR") or "")
        if isinstance(raw, str):
            raw = raw.strip()
        if not raw:
            return Path(data_dir) / "logs"
        return Path(raw).expanduser().absolute()

    def _configure_runtime_logger(self, cfg: dict, data_dir: Path):
        """§3 — runtime logger 한 번 구성 (fingerprint 가 같으면 그대로).

        - 항상 console StreamHandler 를 먼저 등록한다.
        - 파일 handler 는 mkdir/FileHandler 생성 실패 시 console 로 경고만 남기고
          console 만으로 진행한다.
        - fingerprint (정규화 LOG_DIR, LOG_RETENTION_DAYS) 가 바뀌면 handler 를
          닫고 새로 만든다.
        """
        retention = self._log_retention_days(cfg)
        log_dir = self._resolve_log_dir(cfg, data_dir)
        fp = (str(log_dir), int(retention))
        with self._runtime_logger_lock:
            existing = self._runtime_logger
            if existing is not None and self._runtime_logger_fp == fp:
                return existing
            # 새 구성 — 기존 handler 모두 닫고 logger 자체도 새로.
            if existing is not None:
                for h in list(existing.handlers):
                    try:
                        h.close()
                    except Exception:
                        pass
            logger = logging.getLogger("gdrive_reading_sync.runtime")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            # console — 항상 1개만.
            for h in list(logger.handlers):
                try:
                    h.close()
                except Exception:
                    pass
            # 로그는 서버를 운영하는 사람이 읽는다. **로컬 시각 + UTC 오프셋**으로 찍는다.
            #   2026-09-03T13:48:06+0900
            # 읽는 사람이 암산할 필요가 없고, 오프셋이 붙어 모호하지도 않다.
            # 회전도 로컬 자정 기준이라, 20260903.log 는 그 지역의 9월 3일 하루가 담긴다.
            #
            # converter 는 **Formatter** 의 속성이다. Handler 에 붙이면 아무 효과가 없다
            # (2026-09-03 소킹에서 실측된 결함 — 로컬 시각에 'Z' 만 붙어 UTC 인 척했다).
            def _log_formatter():
                f = logging.Formatter(
                    fmt="%(asctime)s %(levelname)s %(threadName)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S%z",
                )
                f.converter = time.localtime
                return f

            sh = logging.StreamHandler(sys.stdout)
            sh.setFormatter(_log_formatter())
            logger.addHandler(sh)
            # 파일 — 실패해도 worker 죽이면 안 됨.
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
                base = log_dir / "gdrive_reading_sync.log"
                fh = _DailyTimedRotatingFileHandler(
                    base_filename=str(base),
                    when="midnight",
                    interval=1,
                    backupCount=retention,
                    encoding="utf-8",
                    delay=True,
                    utc=False,   # 로컬 자정 회전 — 파일명 날짜와 내용이 같은 하루
                )
                fh.setFormatter(_log_formatter())
                logger.addHandler(fh)
            except Exception as exc:
                # console 폴백 — 절대 worker 를 죽이면 안 됨 (T1 요구 §3.3)
                logger.warning(
                    "log file handler disabled: dir=%s err=%s: %s",
                    str(log_dir), type(exc).__name__, exc,
                )
            self._runtime_logger = logger
            self._runtime_logger_fp = fp
            return logger

    def _run_preflight(self, cfg: dict) -> dict:
        # §4.4 — preflight 자체가 options 갱신을 겸한다.
        out = self._refresh_remote_options(cfg)
        options_info = out
        pre = preflight(cfg)
        if pre.get("success"):
            pre["remotes"] = options_info.get("remotes")
            self._last_preflight_fp = config_fingerprint(cfg, pre)
        else:
            pre["remotes"] = options_info.get("remotes")
            self._last_preflight_fp = None
        return pre

    _last_preflight_fp = None  # 클래스 레벨 캐시 (프로세스 수명)

    def _is_enabled(self, db_type: str) -> bool:
        val = self._cfg(db_type).get("ENABLE_SYNC", False)
        # ponytail: bool("false") == True 인 함정 회피. 문자열은 명시 화이트리스트,
        # 그 외는 None 가드 후 bool() 사용.
        if isinstance(val, str):
            return val.strip().lower() in ("true", "1", "yes", "on")
        if val is None:
            return False
        return bool(val)

    def _poll_seconds(self, db_type: str) -> float:
        try:
            v = float(self._cfg(db_type).get("POLL_SECONDS", 60))
        except Exception:
            v = 60.0
        return max(5.0, min(3600.0, v))

    # ---- background service ----
    def start_background_service(self, db_type: str):
        global _SERVICE_STARTED
        with _SERVICE_LOCK:
            if _SERVICE_STARTED:
                return None
            _SERVICE_STARTED = True

        try:
            self._ensure_routes()
        except Exception as exc:
            logger.error(f"[{SELF_ID}] ensure_routes failed: {exc}")

        thread = threading.Thread(target=self._run_loop, name="gdrive-reading-sync", daemon=True)
        thread.start()
        # P9 — 폴더 단위 자동 스캔 전용 스레드. `scan_library_path` 는 동기이며
        # 수 초~수십 초 걸리므로(§3) 폴링 스레드에서 부르면 Drive 감지·복사가
        # 통째로 멈춘다. debounce 된 pending 를 한 번에 1건만 되집는다 (§5.3).
        scan_thread = threading.Thread(target=self._scan_loop, name="gdrs-scan", daemon=True)
        scan_thread.start()
        return None

    def _run_loop(self):
        store = open_store(__file__)
        cfg0 = self._cfg("general")
        # 3라운드-B T1 — §3.1. plugin data 디렉터리 = state.db 가 있는 곳. LOG_DIR
        # 공백이면 그 아래 logs/ 로 폴백한다. logger 는 fingerprint (정규화 LOG_DIR,
        # LOG_RETENTION_DAYS) 가 바뀌면 cycle 사이에서만 재구성한다.
        runtime_logger = self._configure_runtime_logger(cfg0, store.db_path.parent)
        log = runtime_logger.info
        log_exc = runtime_logger.exception
        # startup — §9.3 / §4.1 / 리뷰 r2 F2. 실패도 sync_state 에 남긴다.
        # dry_run → queued 승격은 Store.__init__ 마다 도는 부작용이 있어
        # 명시적 1회성 호출로 격리했다.
        try:
            promoted = store.promote_dry_run_to_queued()
            log(f"[{SELF_ID}] promote_dry_run_to_queued: {promoted} promoted")
        except Exception as exc:
            log(f"[{SELF_ID}] promote_dry_run_to_queued failed: {exc}")
        try:
            rec = recover_on_start(store, cfg0, log=log)
            log(f"[{SELF_ID}] recover_on_start: {rec.get('recovered')} recovered, "
                f"tmp_cleanup={len(rec.get('tmp_cleanup') or [])}")
        except Exception as exc:
            log_exc(f"[{SELF_ID}] recover_on_start failed")
            store.set_state(status="error", error=f"recover: {exc}", last_poll_at=_iso_now())
        # §12-E — 스캔 도중 죽어 `running` 으로 남은 고아 행을 `pending` 으로 되돌린다.
        # 부분 스캔이라 mtime 스킵으로 다시 돌려도 싸다.
        try:
            recovered_scans = store.scan_recover_running()
            if recovered_scans:
                log(f"[{SELF_ID}] scan recover: {recovered_scans} running -> pending")
        except Exception as exc:
            log_exc(f"[{SELF_ID}] scan recover failed")
        try:
            self._refresh_remote_options(cfg0)
        except Exception as exc:
            log(f"[{SELF_ID}] refresh_remote_options failed: {exc}")

        sleep_s = self._poll_seconds("general")
        while True:
            try:
                # §8.2 — 1시간 gate 자동 정리 (AUTO_CLEANUP on 일 때만).
                self._cleanup_if_due(store, self._cfg("general"), log=log)
                if self._is_enabled("general"):
                    cfg = self._cfg("general")
                    # §3.1 — fingerprint 변경 시 logger 재구성.
                    runtime_logger = self._configure_runtime_logger(
                        cfg, store.db_path.parent
                    )
                    log = runtime_logger.info
                    log_exc = runtime_logger.exception
                    dry_run = cfg.get("DRY_RUN", True)
                    # === 감지 (D6 분리) — 토큰/Drive 실패는 job claim 을 막지 않는다
                    try:
                        token = get_access_token(cfg.get("DETECT_REMOTE", ""))
                        client = DriveClient(token)
                        poll_once(client, store, cfg, log=log)
                    except Exception as exc:
                        store.set_state(status="error", error=f"detect: {exc}",
                                        last_poll_at=_iso_now())
                        log(f"[{SELF_ID}] detect failed: {exc}")
                    # === 복사 (D6 분리) — preflight 성공 + dry_run off 일 때만
                    if not dry_run:
                        try:
                            fp = config_fingerprint(cfg, {
                                "rclone_resolved": "",
                                "transfer_remote": cfg.get("TRANSFER_REMOTE", ""),
                                "remote_kind": "",
                                "remotes": [],
                                "probe": {},
                            })
                            cur_fp = self._last_preflight_fp
                            need_preflight = (
                                cur_fp is None
                                or fp[:3] != (cur_fp[0], cur_fp[1], cur_fp[2])
                                or fp[3] != cur_fp[3]
                                or fp[4] != cur_fp[4]
                            )
                            if need_preflight:
                                pre = self._run_preflight(cfg)
                                if not pre.get("success"):
                                    log(f"[{SELF_ID}] preflight failed: {pre.get('error')}")
                            if self._last_preflight_fp is not None:
                                out = process_jobs(store, cfg, log=log)
                                if out.get("claimed"):
                                    log(f"[{SELF_ID}] process_jobs: {out}")
                                self._auto_scan(
                                    cfg, out.get("landed_dirs") or [], store=store, log=log
                                )
                        except Exception as exc:
                            log_exc(f"[{SELF_ID}] copy phase failed")
                            store.set_state(status="error", error=f"copy: {exc}",
                                            last_poll_at=_iso_now())
                else:
                    # 비활성: 호출 없이 sleep만
                    time.sleep(sleep_s)
                    continue
            except Exception as exc:
                log_exc(f"[{SELF_ID}] loop error")
                try:
                    store.set_state(status="error", error=str(exc), last_poll_at=_iso_now())
                except Exception:
                    pass
            time.sleep(sleep_s)

    # ---- 자동 스캔 (복사 완료 -> 폴더 단위 증분 스캔) ----
    def _auto_scan(self, cfg: dict, landed_dirs: list, store=None, log=print) -> None:
        """복사가 끝난 폴더 하나를 BookOasis 라이브러리 스캔에 넣는다 (v0.3.4).

        v0.3.3 의 전용 큐 등록 방식을 걷어내고, 이 플러그인의 `scan` 테이블에
        폴더 단위(책 단위) pending 을 남긴다. 실제 스캔은 별도 `gdrs-scan` 스레드가
        debounce 후 `scan_library_path(folder, force=False)` 로 수행한다.

        `store` 는 폴링 스레드에서 재사용 중인 인스턴스를 넘긴다. 없으면(테스트 등)
        `open_store(__file__)` 로 새로 만든다.
        """
        if not self._is_truthy(cfg.get("AUTO_SCAN", True)):
            return

        if not landed_dirs:
            return

        if store is None:
            store = open_store(__file__)
        local_root = (cfg.get("LOCAL_ROOT") or "").strip()

        libraries = self._all_libraries(log=log)
        for d in landed_dirs:
            hit = _pick_library(libraries, d)
            if hit is None:
                store.scan_skip(d, "general", "no_library")
                log(f"[{SELF_ID}] scan skip folder={_display_folder(d, local_root)} "
                    f"reason=no_library")
                continue
            session, library_id, physical_path = hit
            # 라이브러리 루트 직하 파일은 스캔 단위(책 폴더)가 아니므로 걸러낸다.
            if _normpath_eq(d, physical_path):
                store.scan_skip(d, session, "library_root")
                log(f"[{SELF_ID}] scan skip folder={_display_folder(d, local_root)} "
                    f"reason=library_root")
                continue
            store.scan_touch(d, session, library_id)
            log(f"[{SELF_ID}] scan queued  folder={_display_folder(d, local_root)} "
                f"files=1")

    @staticmethod
    def _all_libraries(log=print) -> list:
        """`(session, library_id, physical_path)` 목록. 조회 실패한 세션은 건너뛴다."""
        from repositories.category_repository import CategoryRepository

        out = []
        for session in _WATCHED_SESSIONS:
            try:
                rows = CategoryRepository.get_all_libraries(session)
            except Exception as exc:
                log(f"[{SELF_ID}] auto_scan: '{session}' 라이브러리 조회 실패 — {exc}")
                continue
            for lib in rows or []:
                out.append((session, lib.get("id"), str(lib.get("physical_path") or "")))
        return out

    # ---- P9 — 스캔 전용 스레드 ---------------------------------------

    @staticmethod
    def _scan_debounce_seconds(cfg: dict) -> int:
        """§5.1 — SCAN_DEBOUNCE_SECONDS 정규화. 비수치 60, 10..3600 clamp."""
        try:
            v = int(cfg.get("SCAN_DEBOUNCE_SECONDS", 60))
        except (TypeError, ValueError):
            v = 60
        return max(10, min(v, 3600))

    def _scan_loop(self):
        """§5.3 — `gdrs-scan` 스레드. 5초마다 debounce 된 pending 폴더 1건을
        되집어 `_run_one_scan` 으로 스캔한다.

        Store 커넥션은 루프 시작 시 1회만 연다 (§12-F). 스레드는 절대 죽으면 안
        되므로 tick 예외는 전부 logger 로 삼킨다.
        """
        store = open_store(__file__)
        log = logger.info
        log_exc = logger.exception
        while True:
            try:
                cfg = self._cfg("general")
                if self._is_truthy(cfg.get("AUTO_SCAN", True)):
                    debounce = self._scan_debounce_seconds(cfg)
                    due = _iso_now_offset(-debounce)
                    row = store.scan_claim_due(due)
                    if row is not None:
                        self._run_one_scan(store, row, cfg, log=log, log_exc=log_exc)
                        continue  # 밀린 게 있으면 쉬지 않고 바로 다음 건
            except Exception:
                log_exc(f"[{SELF_ID}] scan loop error")
            time.sleep(5.0)

    def _run_one_scan(
        self, store, row: dict, cfg: dict, *, log=print, log_exc=logger.exception
    ) -> None:
        """§5.3 — pending 폴더 1건을 `scan_library_path(folder, force=False)` 로
        스캔하고 `scan_finish` 로 종결한다.

        본체 스캐너가 성공/실패를 예외로만 알린다(§12-A). 예외 없이 끝나면
        `done` 이고, 예외는 `failed` 로 삼킨다 — 스레드 로직 밖으로 절대 새지
        않는다 (T-AS6).
        """
        import time as _t
        local_root = (cfg.get("LOCAL_ROOT") or "").strip()
        try:
            import database
            from tools.scanner.core import scan_library_path
        except Exception as exc:
            store.scan_finish(row["folder"], "failed",
                              error=f"import: {type(exc).__name__}: {exc}")
            log(f"[{SELF_ID}] scan failed  folder="
                f"{_display_folder(row['folder'], local_root)} error=import:{exc}")
            return
        db_path = database.get_db_path(row["session"])
        log(f"[{SELF_ID}] scan start   folder="
            f"{_display_folder(row['folder'], local_root)} "
            f"library={row['session']}#{row['library_id']}")
        t0 = _t.monotonic()
        try:
            # §5.2 — 반드시 폴더 경로. 파일 경로면 os.walk 가 아무것도 안 걸린다(§2).
            # force=False — §⑮ mtime 스킵으로 기존 권을 재파싱하지 않는다.
            scan_library_path(db_path, row["library_id"], row["folder"], force=False)
        except Exception as exc:
            store.scan_finish(row["folder"], "failed",
                              error=f"{type(exc).__name__}: {exc}")
            log(f"[{SELF_ID}] scan failed  folder="
                f"{_display_folder(row['folder'], local_root)} error={exc}")
            return
        store.scan_finish(row["folder"], "done")
        elapsed = _t.monotonic() - t0
        log(f"[{SELF_ID}] scan done    folder="
            f"{_display_folder(row['folder'], local_root)} elapsed={elapsed:.1f}s")
        self._purge_recent_cache(row["session"], log=log)

    def _purge_recent_cache(self, session: str, log=print) -> None:
        """§5.3 — 대시보드 cache 를 직접 소거. 실패는 로그만 (스캔 성공은 유지)."""
        try:
            from utils.redis_helper import redis_delete_pattern
            redis_delete_pattern(f"cache:recent_added*:{session}:*")
        except Exception as exc:
            log(f"[{SELF_ID}] scan cache purge 실패: {exc}")

    # §12-A — 본체가 scan 완료 시 부르는 훅. 반드시 예외를 밖으로 내지 않는다 (§12-C).
    def on_scan_new_books_detected(self, db_type, payload) -> None:
        """본체 `engine.py:828` 가 활성화된 metadata 플러그인에 발행한다.

        payload = {db_type, library_id, library_name, new_books_count, sample_titles}.
        이 플러그인이 `scan_library_path` 를 직접 부르므로 신규 도서 수는 여기서만
        받는다 (§5.3). `library_id` 만으로 귀속하므로 본체 cron 전체 스캔과 겹치면
        어긋날 수 있다 — `scan_set_new_books` 가 후보가 정확히 1개일 때만 기록한다.
        """
        # ponytail: library_id 로만 귀속한다. payload 에 경로가 생기면 folder 로
        #           정확히 맞출 것.
        try:
            n = int(payload.get("new_books_count") or 0)
            lib = payload.get("library_id")
            if n > 0 and lib is not None:
                open_store(__file__).scan_set_new_books(int(lib), n)
        except Exception:
            logger.exception(f"[{SELF_ID}] on_scan_new_books_detected")
        return None

    # ---- routes (gamebooks 패턴 차용) ----
    def _ensure_routes(self):
        # ponytail: 부팅 훅이 없는 환경에선 current_app 컨텍스트가 없을 수 있다.
        # 그때는 core.app 을 직접 끌어와 라우트를 붙인다. 1) current_app 우선, 2) core.app 폴백.
        app = None
        try:
            from flask import current_app
            app = current_app._get_current_object()
        except Exception:
            app = None
        if app is None:
            try:
                from core import app as core_app  # type: ignore
                app = core_app
            except Exception as exc:
                logger.error(f"[{SELF_ID}] ensure_routes: core.app not importable: {exc}")
                return
        try:
            self._do_register_routes(app)
        except Exception as exc:
            logger.error(f"[{SELF_ID}] ensure_routes error: {exc}")

    def _do_register_routes(self, app):
        with _ROUTES_LOCK:
            app_id = id(app)
            if app_id in _REGISTERED_APPS:
                return
            try:
                from werkzeug.routing import Rule
                routes = {
                    "gdrs_status": (f"{ROUTE_BASE}/status", ["GET"]),
                    "gdrs_jobs": (f"{ROUTE_BASE}/jobs", ["GET"]),
                    "gdrs_backfill": (f"{ROUTE_BASE}/backfill", ["POST"]),
                    "gdrs_preflight": (f"{ROUTE_BASE}/preflight", ["GET"]),
                    "gdrs_rclone_check": (f"{ROUTE_BASE}/rclone-check", ["POST"]),
                    # §7 (S5) — 일괄 재시도
                    "gdrs_retry": (f"{ROUTE_BASE}/retry", ["POST"]),
                    # §8 (S6) — 보존기간/정리
                    "gdrs_cleanup": (f"{ROUTE_BASE}/cleanup", ["POST"]),
                }
                registered = set(rule.endpoint for rule in app.url_map.iter_rules())
                for endpoint, (path, methods) in routes.items():
                    handler = f"_route_{endpoint.replace('gdrs_', '')}"
                    view_func = getattr(self, handler, None)
                    if view_func:
                        app.view_functions[endpoint] = view_func
                        if endpoint not in registered:
                            app.url_map.add(Rule(path, endpoint=endpoint, methods=methods))

                if not getattr(app, "_gdrs_wsgi_patched", False):
                    orig_wsgi = app.wsgi_app

                    def _gdrs_wsgi(environ, start_response):
                        path = environ.get("PATH_INFO", "")
                        if path.startswith(ROUTE_BASE):
                            try:
                                self._do_register_routes(app)
                            except Exception:
                                pass
                        return orig_wsgi(environ, start_response)

                    app.wsgi_app = _gdrs_wsgi
                    app._gdrs_wsgi_patched = True

                _REGISTERED_APPS.add(app_id)
            except Exception as exc:
                logger.error(f"[{SELF_ID}] route register error: {exc}")

    # ---- HTTP handlers ----
    def _route_status(self):
        store = open_store(__file__)
        out = store.read_status()
        # 화면 제목에 실제 설치 버전을 붙이기 위한 값. 못 읽었으면 빈 문자열.
        if isinstance(out, dict):
            out["plugin_version"] = PLUGIN_VERSION
        return jsonify(out)

    def _route_jobs(self):
        from flask import request
        store = open_store(__file__)
        # §6.3 — page_size/limit 동시: page_size 우선. limit 만: 옛 의미 (첫 limit 행).
        # 둘 다 없음: page=1, page_size=50.
        page_size_raw = request.args.get("page_size")
        limit_raw = request.args.get("limit", default=None, type=int)
        if page_size_raw is not None:
            try:
                page_size = int(page_size_raw)
            except (TypeError, ValueError):
                page_size = 50
            resp = store.list_page(
                page=request.args.get("page", default=1, type=int),
                page_size=page_size,
                status=request.args.get("status", default=""),
                action=request.args.get("action", default=""),
                result=request.args.get("result", default=""),
                search=request.args.get("search", default=""),
                order=request.args.get("order", default="desc"),
            )
        elif limit_raw is not None:
            resp = store.read_jobs(limit=limit_raw)
        else:
            resp = store.list_page(
                page=request.args.get("page", default=1, type=int),
                page_size=50,
                status=request.args.get("status", default=""),
                action=request.args.get("action", default=""),
                result=request.args.get("result", default=""),
                search=request.args.get("search", default=""),
                order=request.args.get("order", default="desc"),
            )
        # §5.5 — job 행의 부모 폴더(= 책 폴더)에 대한 스캔 상태를 `scans` 로 얹는다.
        # 키는 os.path.dirname(local_path) 원문 그대로 — 프론트가 같은 계산으로 찾는다.
        # 스캔 정보 때문에 이 목록 라우트가 500 이 나면 안 되므로 실패 시 {} 로 삼킨다.
        try:
            job_rows = resp.get("items") or resp.get("jobs") or []
            folders = {
                os.path.dirname(j.get("local_path") or "") for j in job_rows if j
            }
            resp["scans"] = store.scan_map(sorted(f for f in folders if f))
        except Exception:
            resp["scans"] = {}
        return jsonify(resp)

    # ponytail: Drive 쓰기 0 — changes GET 만. 강제 1회 재생.
    # Drive change 토큰은 계정 단위로 1씩 증가하는 정수라 빼면 과거로 되감긴다.
    # ?back=N  → 현재 startPageToken - N 부터 재생 (기본 1000 ≈ 2~3일).
    # 이게 "파일이 바뀌기를 기다리지 않고" 감지 파이프를 검증하는 유일한 수단이다.
    def _route_backfill(self):
        from flask import request
        cfg = self._cfg("general")
        if not self._is_enabled("general"):
            return jsonify({"success": False, "error": "ENABLE_SYNC 꺼져 있음"}), 400
        try:
            token = get_access_token(cfg.get("DETECT_REMOTE", ""))
        except Exception as exc:
            return jsonify({"success": False, "error": f"token: {exc}"}), 500
        client = DriveClient(token)
        store = open_store(__file__)
        try:
            back = int(request.args.get("back", 1000))
        except (TypeError, ValueError):
            back = 1000
        back = max(0, min(back, 200000))
        try:
            out = rewind_and_poll(client, store, cfg, back, log=print)
        except DriveAPIError as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        except Exception as exc:
            return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        return jsonify({"success": True, **out})

    def _route_retry(self):
        """§7 (S5) — atomic terminal-only 재시도.

        body: {"job_ids":[12,13]} 또는 {"failed_all":true}.
        """
        from flask import request
        store = open_store(__file__)
        try:
            payload = request.get_json(force=True, silent=False) or {}
        except Exception:
            return jsonify({"success": False, "error": "json_body_required"}), 400
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "json_object_required"}), 400
        if "job_ids" in payload:
            ids = payload.get("job_ids") or []
            if not isinstance(ids, list):
                return jsonify({"success": False, "error": "job_ids_list_required"}), 400
            out = store.retry_jobs([int(x) for x in ids])
            if out["retried"] != len(ids):
                return jsonify({
                    "success": False, "error": "jobs_not_retryable", **out,
                }), 409
            return jsonify({"success": True, **out})
        if payload.get("failed_all") is True:
            out = store.retry_jobs(failed_all=True)
            return jsonify({"success": True, **out})
        return jsonify({"success": False, "error": "job_ids_or_failed_all_required"}), 400

    def _route_cleanup(self):
        """§8 (S6) — 수동 cleanup. body: {"delete_all": false} 또는 true."""
        from flask import request
        store = open_store(__file__)
        try:
            payload = request.get_json(force=True, silent=True) or {}
        except Exception:
            payload = {}
        cfg = self._cfg("general")
        delete_all = bool(payload.get("delete_all"))
        rd = self._retention_days(cfg)
        try:
            deleted = store.cleanup_terminal(retention_days=rd, delete_all=delete_all)
        except Exception as exc:
            return jsonify({"success": False, "error": str(exc)}), 500
        return jsonify({
            "success": True, "deleted": deleted,
            "delete_all": delete_all, "retention_days": rd,
        })

    def _route_preflight(self):
        # §4.4 — Drive / 로컬에 쓰지 않는다. 실제 실행된 rclone 경로/버전과 probe 만.
        cfg = self._cfg("general")
        try:
            out = self._run_preflight(cfg)
        except Exception as exc:
            return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500
        # 인증값/시크릿 노출 방지 — 응답은 whitelist 키만.
        safe = {
            "success": bool(out.get("success")),
            "error": out.get("error", "") or "",
            "rclone_bin": out.get("rclone_bin", "") or "",
            "rclone_resolved": out.get("rclone_resolved", "") or "",
            "rclone_version": out.get("rclone_version", "") or "",
            "config_file": out.get("config_file", "") or "",
            "remotes": out.get("remotes", []) or [],
            "transfer_remote": out.get("transfer_remote", "") or "",
            "remote_kind": out.get("remote_kind", "") or "",
            "probe": out.get("probe", {}) or {},
        }
        return jsonify(safe)

    def _route_rclone_check(self):
        """설정 화면용 읽기 전용 검사.

        아직 저장하지 않은 RCLONE_BIN/RCLONE_CONFIG 값을 받아 실행 파일의 버전과
        설정 파일의 Drive 리모트 목록을 확인한다. rclone 쓰기 명령은 호출하지 않는다.
        """
        from flask import request
        from .sync_worker import inspect_rclone_setup

        try:
            payload = request.get_json(force=True, silent=False) or {}
        except Exception:
            return jsonify({"success": False, "error": "JSON 요청이 필요합니다."}), 400
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "JSON 객체가 필요합니다."}), 400

        mode = str(payload.get("mode") or "binary").strip().lower()
        if mode not in ("binary", "config"):
            return jsonify({"success": False, "error": "지원하지 않는 확인 유형입니다."}), 400

        cfg = self._cfg("general")
        for key, limit in (("RCLONE_BIN", 1024), ("RCLONE_CONFIG", 2048)):
            if key.lower() in payload:
                value = str(payload.get(key.lower()) or "").strip()
                if len(value) > limit:
                    return jsonify({"success": False, "error": f"{key} 값이 너무 깁니다."}), 400
                cfg[key] = value

        out = inspect_rclone_setup(cfg, include_config=(mode == "config"))
        # config dump 원문에는 인증 정보가 있으므로 절대로 응답하지 않는다.
        safe = {
            "success": bool(out.get("success")),
            "error": out.get("error", "") or "",
            "rclone_resolved": out.get("rclone_resolved", "") or "",
            "rclone_version": out.get("rclone_version", "") or "",
            "config_file": out.get("config_file", "") or "",
            "remotes": out.get("remotes", []) or [],
        }
        return jsonify(safe), (200 if safe["success"] else 400)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _iso_now_offset(seconds: float) -> str:
    """`now + seconds` 를 `store._now()` 와 같은 UTC ISO 형식 (초 단위, Z 없음).

    `scan_claim_due` 는 `queued_at`(store._now() 형식) 과 문자열 비교하므로
    시간대 접미사를 붙이면 경계에서 어긋난다. 같은 형식으로 맞춘다.
    """
    import datetime as _dt
    return (_dt.datetime.utcnow() + _dt.timedelta(seconds=seconds)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
