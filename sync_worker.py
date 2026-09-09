# -*- coding: utf-8 -*-
"""Drive Changes API → SQLite 동기화 워커 (감지 + 분류 + job 등록).

복사/스캔/삭제 코드 일절 없음. job.status='dry_run' 으로 항상 멈춘다 (1단계).

경로 해석과 변경 분류는 FF bookoasis_mate 의 검증된 구현을 이식했다:
  - _resolve_rel_path  ← gdrive_changes.py resolve_item (부모 체인 + item 캐시)
  - build_change_event ← gdrive_changes.py build_change_event (create/rename/edit/delete)

FF 와 다른 점: FF 는 item 에 전체 경로를 저장해 폴더 이동 시 move_prefix 로 하위
경로를 일괄 갱신해야 한다. 여기서는 (parent_id, name) 만 저장하고 경로를 매번
부모 체인으로 계산하므로 폴더 이름이 바뀌면 하위 경로가 저절로 따라온다.
move_prefix 가 필요 없다.
"""
from __future__ import annotations

import hashlib
import json as _json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath, PureWindowsPath

from .drive_client import DriveAPIError, DriveClient
from .store import Store


FOLDER_MIME = "application/vnd.google-apps.folder"
# ponytail: 환경 진단용 단일 파일명 패턴. 정규식 아님 — 그냥 substring.
PROBE_NAME_SUBSTR = "bookoasis_job_probe_"
# ponytail: READING 트리 실측 최대 깊이는 6. 30이면 순환/오해석을 확실히 끊는다.
MAX_DEPTH = 30
# 루트 밖으로 확정된 폴더 표식. Changes 는 계정 전체를 돌려주므로 한 사이클에
# READING 밖 변경이 압도적으로 많다(실측 709건 중 675건). 이 표식이 없으면
# 형제 파일마다 같은 조상 체인을 My Drive 루트까지 되짚는다.
_OUTSIDE = object()
# poll_once 는 sync_state.page_token 을 읽고→소비하고→쓴다. 백그라운드 루프와
# POST /backfill 이 같은 store 에 동시에 들어가면 같은 change 를 두 번 처리해
# 두 번째가 create 를 edit 으로 뒤집는다 (실측: edit 11건 유령 행).
_POLL_LOCK = threading.Lock()

# §3 (S1) — 로컬 루트 기본값은 **없다**. 사용자가 LOCAL_ROOT 를 직접 설정해야 한다.
# 특정 환경의 드라이브 문자를 기본값으로 두면 남의 설치에서 엉뚱한 경로를 가리킨다.
# 미설정이면 _local_root_path() 가 None 을 돌려주고, 복사는 failed/bad_path 로
# 안전하게 멈춘다 (ENABLE_SYNC 기본 꺼짐 + DRY_RUN 기본 켜짐이 1차 게이트).
DEFAULT_LOCAL_ROOT = ""

# §6 / §7 / §10 — 금지 동사/플래그. 래퍼가 호출 전에 중앙 차단한다.
FORBIDDEN_RCLONE_VERBS = {"sync", "move", "delete", "purge", "rmdirs", "deletefile"}
FORBIDDEN_RCLONE_FLAGS = {"--ignore-existing"}

# 종결 상태 모음 (canonical 6어휘 + dry_run = 1라운드 잔존).
TERMINAL_STATUSES = {"completed", "failed", "skipped"}


def _is_probe_file(name: str) -> bool:
    return bool(name) and PROBE_NAME_SUBSTR in name


def _norm_ext_list(raw: str) -> set[str]:
    return {e.strip().lower() for e in (raw or "").split(",") if e.strip()}


def _norm_excluded(raw: str) -> set[str]:
    return {e.strip() for e in (raw or "").split(",") if e.strip()}


# rclone 인코딩 대응 (2026-09-09 운영 실측으로 확인)
#
# rclone 은 백엔드마다 "쓸 수 없는 문자"를 전각 문자로 바꿔 표현하고,
# 원래 이름에 그 전각 문자가 이미 들어 있으면 `‛`(U+201B) 를 앞에 붙여 구분한다.
#
#   Drive 실제 이름   [마블] 스파이더맨／데드풀      (／ = U+FF0F)
#   rclone 표현       [마블] 스파이더맨‛／데드풀
#
# 우리가 `‛` 없이 `／` 를 넘기면 rclone 이 그걸 반각 `/` 로 되돌려 경로 구분자로
# 해석한다 → directory not found (copyto exit 3). 실측 51건이 이것이었다.
_RCLONE_ESCAPE = "‛"

# Drive 백엔드 기본 인코딩은 Slash,InvalidUtf8 — 전각 슬래시만 escape 하면 된다.
_DRIVE_ENCODED = "／"          # ／

# 로컬(Windows) 백엔드 기본 인코딩:
#   Slash,LtGt,DoubleQuote,Colon,Question,Asterisk,Pipe,BackSlash,Ctl,
#   RightSpace,RightPeriod,InvalidUtf8
# Windows 가 파일명에 못 쓰는 문자를 전각으로 바꾼다. 우리가 로컬 경로를 만들 때
# 같은 규칙을 적용하지 않으면 os.rename/open 이 WinError 123 으로 실패한다 (실측 8건).
_WIN_ILLEGAL = {
    "<": "＜", ">": "＞", ":": "：", '"': "＂",
    "|": "｜", "?": "？", "*": "＊",
}


def _rclone_remote_escape(rel: str) -> str:
    """rclone 에 넘길 원격 상대경로에서, 이름 안의 전각 슬래시를 escape 한다.

    구분자로 쓰는 `/` 는 그대로 두고, 이름의 일부인 `／` 만 `‛／` 로 바꾼다.
    """
    return (rel or "").replace(_DRIVE_ENCODED, _RCLONE_ESCAPE + _DRIVE_ENCODED)


def _win_safe_segment(name: str) -> str:
    """Windows 가 거부하는 문자를 rclone 과 같은 전각 문자로 바꾼다.

    끝의 공백/마침표도 Windows 가 조용히 잘라내므로 전각으로 바꿔 보존한다.
    """
    if not name:
        return name
    out = "".join(_WIN_ILLEGAL.get(ch, ch) for ch in name)
    if out.endswith(" "):
        out = out[:-1] + "␣"      # 끝 공백
    if out.endswith("."):
        out = out[:-1] + "．"      # 끝 마침표 (．)
    return out


def _local_path(local_root: str, rel: str) -> str:
    """§3 (S1) — root 문법에 맞는 Pure path 결합.

    절대 경로/`.`/`..` 세그먼트는 거부하지 않고 비워서 반환한다 — 감지 단계에서
    빈 경로는 실행 단계의 `bad_path` 가드가 차단한다. 마지막에 슬래시를 특정
    방향으로 강제 치환하지 않는다 (이게 POSIX 의 함정이었다).
    """
    rel_norm = (rel or "").replace("\\", "/").lstrip("/")
    if not rel_norm:
        return ""
    root = (local_root or "").strip()
    if not root:
        return rel_norm
    # Windows drive/UNC
    if root.startswith("\\\\") or (len(root) >= 2 and root[1] == ":"):
        # 세그먼트마다 Windows 금지 문자를 전각으로 (rclone 로컬 인코딩과 같은 규칙).
        # 안 하면 ':' 나 끝 마침표가 든 이름에서 WinError 123 으로 실패한다.
        safe = "/".join(_win_safe_segment(seg) for seg in rel_norm.split("/"))
        path = PureWindowsPath(root) / PureWindowsPath(safe.replace("/", "\\"))
        return str(path)
    # POSIX 절대경로
    if root.startswith("/"):
        return str(PurePosixPath(root) / PurePosixPath(rel_norm))
    # 환경 종속 — 현재 플랫폼 Path 로 결합하고 결과는 OS 형태 그대로
    return os.path.join(root, rel_norm.replace("/", os.sep))


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- 경로 해석

def _resolve_rel_path(
    client: DriveClient,
    store: Store,
    file_obj: dict,
    root_id: str,
    cache: dict,
    summary: dict | None = None,
) -> str | None:
    """file_obj 의 parents 체인을 root_id 까지 올라가 READING 기준 상대경로를 만든다.

    루트 밖이거나 해석 실패면 None. 거쳐 간 폴더는 item 테이블에 적어 두어
    다음 사이클에는 API 호출 없이 풀린다 (FF resolve_item 과 같은 전략).
    """
    name = file_obj.get("name") or ""
    if not name:
        return None
    segs = [name]
    parents = file_obj.get("parents") or []
    cur = parents[0] if parents else ""
    walked: list[str] = []
    outside = False
    for _ in range(MAX_DEPTH):
        if not cur:
            outside = True  # My Drive 루트까지 갔다 = READING 밖
            break
        if cur == root_id:
            return "/".join(segs)
        rec = cache.get(cur)
        if rec is _OUTSIDE:
            outside = True
            break
        if rec is None:
            rec = store.get_item(cur)
        if rec is None:
            try:
                folder = client.get_file(cur)
            except DriveAPIError:
                # 조상을 못 읽으면 READING 아래인지 판정 불가. 이 사이클 동안은
                # 밖으로 보고(형제마다 재시도하지 않도록) 건수를 따로 센다.
                # ponytail: 그 사이클에서 그 파일은 놓친다. backfill?back=N 으로
                # 다시 훑으면 복구된다. 2라운드(복사)로 가면 여기서 토큰을 붙잡고
                # 재시도하는 쪽이 맞다.
                if summary is not None:
                    summary["resolve_errors"] = summary.get("resolve_errors", 0) + 1
                cache[cur] = _OUTSIDE
                for fid in walked:
                    cache[fid] = _OUTSIDE
                return None
            fparents = folder.get("parents") or []
            rec = {
                "name": folder.get("name") or "",
                "parent_id": fparents[0] if fparents else "",
            }
            store.upsert_item(
                {
                    "file_id": cur,
                    "parent_id": rec["parent_id"],
                    "name": rec["name"],
                    "remote_path": "",
                    "is_directory": True,
                }
            )
        cache[cur] = rec
        walked.append(cur)
        if not rec["name"]:
            return None
        segs.insert(0, rec["name"])
        cur = rec["parent_id"]
    # 루트 밖이 확정된 경우에만 체인 전체를 표시한다. MAX_DEPTH 소진은
    # 원인이 불분명하므로 표시하지 않는다 (잘못 표시하면 진짜 파일을 놓친다).
    if outside:
        for fid in walked:
            cache[fid] = _OUTSIDE
    return None


# ------------------------------------------------------------ 변경 분류 (FF)

def build_change_event(previous: dict | None, current: dict | None) -> dict:
    """이전 상태와 현재 상태를 비교해 action 을 정한다.

    FF gdrive_changes.py build_change_event 이식. 판정 순서가 규약이다 —
    delete → create → rename → edit. 뒤집으면 rename 이 create 로 새어 나가
    2단계에서 같은 파일을 한 벌 더 복사하게 된다.
    """
    previous = previous or {}
    current = current or {}
    old_path = str(previous.get("remote_path") or "")
    new_path = str(current.get("remote_path") or "")
    is_dir = current.get("is_directory", previous.get("is_directory"))
    item_type = "directory" if is_dir else "file"
    if current.get("trashed") or (old_path and not new_path):
        return {
            "action": "delete",
            "item_type": item_type,
            "path": old_path,
            "removed_path": old_path,
        }
    if not old_path:
        return {"action": "create", "item_type": item_type, "path": new_path, "removed_path": ""}
    if old_path != new_path:
        return {
            "action": "rename",
            "item_type": item_type,
            "path": new_path,
            "removed_path": old_path,
        }
    return {"action": "edit", "item_type": item_type, "path": new_path, "removed_path": ""}


def _event_key(action: str, file_id: str, path: str, modified_time: str, size) -> str:
    return f"{action}:{file_id}:{path}:{modified_time or ''}:{int(size or 0)}"


# ---------------------------------------------------------------- 토큰 되감기

def rewind_token(client: DriveClient, store: Store, back: int) -> dict:
    """현재 startPageToken 에서 back 만큼 뺀 토큰을 저장한다.

    Drive change 토큰은 계정 단위로 1씩 증가하는 정수라 빼면 과거로 간다.
    실측(2026-09-01): 현재 7387303 기준 -1000 이 2026-08-29, -30000 이 08-25.
    변경이 생기기를 기다리지 않고 검증할 수 있는 유일한 수단이다.
    """
    token = client.start_page_token()
    try:
        rewound = max(1, int(token) - max(0, int(back)))
    except (TypeError, ValueError):
        raise DriveAPIError(f"startPageToken 이 정수가 아니라 되감을 수 없음: {token!r}")
    store.set_state(page_token=str(rewound), status="ready", error="", last_poll_at=_iso_now())
    return {"start_page_token": token, "rewound_to": str(rewound), "back": int(back)}


def rewind_and_poll(client: DriveClient, store: Store, cfg: dict, back: int, log=print) -> dict:
    """되감기와 소비를 한 락 안에서. 사이에 백그라운드 루프가 끼면 되감기가 무의미해진다."""
    with _POLL_LOCK:
        rewind = rewind_token(client, store, back)
        return {"rewind": rewind, "summary": _poll_once_locked(client, store, cfg, log)}


# ---------------------------------------------------------------- 폴링 1회

def poll_once(client: DriveClient, store: Store, cfg: dict, log=print) -> dict:
    """Drive Changes 를 page_token 이후로 소비해 job 을 만든다.

    한 번에 하나만 돈다 — 백그라운드 루프와 POST /backfill 이 겹치면 안 된다.
    """
    with _POLL_LOCK:
        return _poll_once_locked(client, store, cfg, log)


def _poll_once_locked(client: DriveClient, store: Store, cfg: dict, log=print) -> dict:
    root_id = (cfg.get("REMOTE_ROOT_FOLDER_ID") or "").strip()
    if not root_id:
        return {"ok": False, "phase": "config", "error": "REMOTE_ROOT_FOLDER_ID 가 비어 있음"}

    excluded = _norm_excluded(cfg.get("EXCLUDED_TOP", ""))
    extensions = _norm_ext_list(cfg.get("EXTENSIONS", ""))
    local_root = cfg.get("LOCAL_ROOT", "") or ""

    state = store.get_state()
    page_token = (state.get("page_token") or "").strip()
    if not page_token:
        try:
            token = client.start_page_token()
        except DriveAPIError as exc:
            store.set_state(status="error", error=f"startPageToken: {exc}")
            log(f"[gdrive_reading_sync] startPageToken failed: {exc}")
            return {"ok": False, "phase": "start_token", "error": str(exc)}
        store.set_state(page_token=token, status="ready", error="", last_poll_at=_iso_now())
        log(f"[gdrive_reading_sync] startPageToken stored: {token} (첫 사이클, job 없음)")
        return {"ok": True, "phase": "start_token", "new_token": token, "jobs_created": 0}

    summary = {
        "ok": True,
        "phase": "poll",
        "changes_seen": 0,
        "jobs_created": 0,
        "folders_seen": 0,
        "out_of_root_skipped": 0,
        "excluded_skipped": 0,
        "extension_skipped": 0,
        "probe_skipped": 0,
        "resolve_errors": 0,
        "by_action": {},
    }
    cache: dict[str, dict] = {}
    next_token = page_token
    pages = 0
    # 한 사이클 페이지 상한. Drive changes.list 는 사용자당 요청 속도 제한이 있어
    # 수백 페이지를 연속 호출하면 403 Forbidden(rate limit)이 난다.
    # 2026-09-06 실측: 구드 쪽 대량 변경으로 pages=327/530/576 까지 치솟자
    # 하루 61회 403. 상한을 두면 밀린 이력을 여러 사이클에 나눠 소화한다.
    try:
        max_pages = int(cfg.get("MAX_PAGES_PER_POLL") or 50)
    except (TypeError, ValueError):
        max_pages = 50
    max_pages = max(1, min(max_pages, 500))
    truncated = False

    try:
        while True:
            data = client.list_changes(next_token)
            pages += 1
            for change in data.get("changes", []) or []:
                fid = change.get("fileId") or ""
                if not fid:
                    continue
                summary["changes_seen"] += 1
                file_obj = change.get("file") or {}
                removed = bool(change.get("removed")) or bool(file_obj.get("trashed"))
                previous = store.get_item(fid)

                current = None
                if not removed:
                    if not file_obj.get("parents"):
                        try:
                            file_obj = client.get_file(fid)
                        except DriveAPIError:
                            file_obj = {}
                    file_obj.setdefault("id", fid)
                    rel = _resolve_rel_path(client, store, file_obj, root_id, cache, summary)
                    if rel is None:
                        # 루트 밖. 전에 알던 항목이면 밖으로 나간 것이라 delete 로 본다.
                        if not previous:
                            summary["out_of_root_skipped"] += 1
                            continue
                    else:
                        parents = file_obj.get("parents") or []
                        current = {
                            "remote_path": rel,
                            "is_directory": file_obj.get("mimeType") == FOLDER_MIME,
                            "size": file_obj.get("size"),
                            "md5": file_obj.get("md5Checksum") or "",
                            "modified_time": file_obj.get("modifiedTime") or "",
                            "name": file_obj.get("name") or "",
                            "parent_id": parents[0] if parents else "",
                        }

                event = build_change_event(previous, current)
                path = event["path"]

                # item 갱신은 필터보다 먼저. 제외 대상이라도 경로 캐시는 살려 둔다.
                if current:
                    store.upsert_item(
                        {
                            "file_id": fid,
                            "parent_id": current["parent_id"],
                            "name": current["name"],
                            "remote_path": current["remote_path"],
                            "is_directory": current["is_directory"],
                            "size": current["size"],
                            "md5": current["md5"],
                            "modified_time": current["modified_time"],
                        }
                    )
                elif previous:
                    store.delete_item(fid)

                # §4.1 — 디렉터리 처리
                if event["item_type"] == "directory":
                    summary["folders_seen"] += 1
                    # rename 이벤트만 0바이트 job 으로 잡아 폴더 단위 os.rename 한다.
                    # create/edit/delete 는 job 안 만든다 (원격 폴더 삭제로 로컬
                    # 트리를 건드리지 않는 사용자 정책 그대로 유지).
                    if event["action"] != "rename":
                        continue
                    removed = event.get("removed_path") or ""
                    new_path = event.get("path") or ""
                    # 제외 최상위 경계는 일반 파일 분기와 같은 규칙 — rename 도
                    # 제외 최상위 안에서는 만들지 않는다.
                    if not new_path or new_path.split("/")[0] in excluded:
                        summary["excluded_skipped"] += 1
                        continue
                    # size=0, md5='', bytes_done=0 — rename 후 복사 0건
                    store.upsert_job(
                        {
                            "event_key": _event_key(
                                "rename", fid, new_path,
                                (current or {}).get("modified_time") or "",
                                0,
                            ),
                            "action": "rename",
                            "item_type": "directory",
                            "file_id": fid,
                            "remote_path": new_path,
                            "removed_path": removed,
                            "local_path": _local_path(local_root, new_path),
                            "size": 0,
                            "md5": "",
                            "modified_time": (current or {}).get("modified_time") or "",
                            "status": "queued",
                        }
                    )
                    summary["jobs_created"] += 1
                    summary["by_action"]["rename"] = (
                        summary["by_action"].get("rename", 0) + 1
                    )
                    continue
                if not path:
                    summary["out_of_root_skipped"] += 1
                    continue

                leaf = path.split("/")[-1]
                if _is_probe_file(leaf):
                    summary["probe_skipped"] += 1
                    continue
                if path.split("/")[0] in excluded:
                    summary["excluded_skipped"] += 1
                    continue
                if extensions and PurePosixPath(leaf).suffix.lower() not in extensions:
                    summary["extension_skipped"] += 1
                    continue

                size = (current or {}).get("size")
                modified_time = (current or {}).get("modified_time") or ""
                store.upsert_job(
                    {
                        "event_key": _event_key(event["action"], fid, path, modified_time, size),
                        "action": event["action"],
                        "item_type": event["item_type"],
                        "file_id": fid,
                        "remote_path": path,
                        "removed_path": event["removed_path"],
                        "local_path": _local_path(local_root, path),
                        "size": int(size or 0),
                        "md5": (current or {}).get("md5") or "",
                        "modified_time": modified_time,
                        "status": "queued",
                    }
                )
                summary["jobs_created"] += 1
                summary["by_action"][event["action"]] = (
                    summary["by_action"].get(event["action"], 0) + 1
                )

            next_page = data.get("nextPageToken")
            if next_page:
                next_token = next_page
                # 상한에 닿으면 여기까지의 토큰을 커밋하고 다음 사이클로 넘긴다.
                if pages >= max_pages:
                    truncated = True
                    break
                continue
            new_token = data.get("newStartPageToken")
            if new_token:
                next_token = new_token
            break

        store.set_state(page_token=next_token, status="ready", error="", last_poll_at=_iso_now())
    except DriveAPIError as exc:
        # 진행분을 반드시 보존한다. 여기서 토큰을 버리면 다음 사이클이 같은 자리에서
        # 다시 시작해 또 같은 지점에서 실패한다 — 영구 무한루프가 된다.
        # 2026-09-06~07 실측: 403 이 하루 61회, page_token 이 7434511 에 고정된 채
        # queued 가 102,719 건까지 쌓였다. nextPageToken 은 이어받기용으로 유효하므로
        # 부분 저장이 안전하다.
        if next_token and next_token != page_token:
            store.set_state(page_token=next_token, status="error", error=str(exc),
                            last_poll_at=_iso_now())
            log(f"[gdrive_reading_sync] poll error: {exc} "
                f"(진행분 {pages}페이지 보존, token={next_token})")
        else:
            store.set_state(status="error", error=str(exc), last_poll_at=_iso_now())
            log(f"[gdrive_reading_sync] poll error: {exc} (진행분 없음)")
        summary["ok"] = False
        summary["error"] = str(exc)

    summary["pages"] = pages
    summary["truncated"] = truncated
    summary["page_token"] = next_token
    # folders_seen / probe_skipped 를 반드시 함께 찍는다.
    # 이 둘이 빠져 있으면 changes_seen 이 어디로 갔는지 로그만으로 설명되지 않는다.
    # 특히 folders_seen 은 폴더 rename 전파(S2)의 유일한 관측 창구다 — 실제 폴더
    # 이벤트가 지나가도 로그에 흔적이 없으면 놓치면 영영 모른다 (2026-09-03 소킹에서
    # 잔여 17건의 정체를 로그로 못 밝히고 DB 를 뒤져야 했다).
    log(
        f"[gdrive_reading_sync] poll pages={pages} changes={summary['changes_seen']} "
        f"jobs={summary['jobs_created']} {summary['by_action']} "
        f"folders={summary['folders_seen']} "
        f"out_of_root={summary['out_of_root_skipped']} excluded={summary['excluded_skipped']} "
        f"ext_skip={summary['extension_skipped']} probe_skip={summary['probe_skipped']} "
        f"resolve_err={summary['resolve_errors']}"
    )
    return summary


# ---------------------------------------------------------------- 복사 (2라운드)


def _local_root_path(cfg: dict) -> Path | None:
    """§3 (S1) — 설정 → Path. 비우면 DEFAULT_LOCAL_ROOT, 그것도 없으면 None.

    기본값이 없으므로 미설정이면 None 이다. 호출자는 None 을 받으면
    복사를 시작하지 않고 failed/bad_path 로 끝낸다.
    """
    raw = (cfg.get("LOCAL_ROOT") or "").strip()
    if raw:
        return Path(raw)
    if DEFAULT_LOCAL_ROOT:
        return Path(DEFAULT_LOCAL_ROOT)
    return None


def _tmp_root_path(cfg: dict, local_root: Path | None) -> Path | None:
    """§3 (S1) — 설정 TMP_ROOT → Path. 비면 LOCAL_ROOT.parent/_reading_sync_tmp,
    LOCAL_ROOT 도 없으면 None.

    같은 볼륨에 두는 게 목적이다 — os.replace 의 원자성을 유지해야 하므로
    TMP_ROOT 만 따로 두고 다른 볼륨으로 가는 것은 사용자의 책임이다.
    """
    raw = (cfg.get("TMP_ROOT") or "").strip()
    if raw:
        return Path(raw)
    if local_root is None:
        return None
    return Path(local_root).parent / "_reading_sync_tmp"


def _safe_under(base: Path, target: Path) -> bool:
    try:
        return bool(target.resolve().is_relative_to(base.resolve()))
    except AttributeError:
        # py3.8 호환 가드 (실 행은 py3.10+)
        try:
            target.resolve().relative_to(base.resolve())
            return True
        except ValueError:
            return False
    except ValueError:
        # resolve 후에도 base 의 자식이 아닐 때 — 안전 거절
        return False


def md5_file(path: Path) -> str:
    """로컬 파일 md5. 64KB 청크."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def same_content(path: Path, remote_size: int, remote_md5: str) -> bool:
    """§5 — D1. 네 항 동시 충족일 때만 True.

    원격 md5 가 빈 문자열이면 False (skip 금지). 해시 읽기 실패는 호출 측에서
    retry 로 보낸다.
    """
    if not remote_md5:
        return False
    if not path.is_file():
        return False
    if path.stat().st_size != int(remote_size or 0):
        return False
    try:
        local_md5 = md5_file(path)
    except OSError:
        return False
    return local_md5.lower() == remote_md5.lower()


def find_cold_start_candidate(
    target: Path,
    remote_size: int,
    remote_md5: str,
    log=print,
) -> Path | None:
    """§6.2 — 같은 폴더 직계에서 크기+md5 가 같은 다른 이름 파일.

    후보 0개 또는 md5 없음 → None. 후보 2개 이상이면 'safety_conflict' 표식으로
    호출 측이 retry/failed 로 보낸다. 이 함수는 단일 후보일 때만 Path 를 돌려준다.
    """
    if not remote_md5:
        return None
    if not target.parent.is_dir():
        return None
    try:
        size = int(remote_size or 0)
    except (TypeError, ValueError):
        return None
    matches: list[Path] = []
    for entry in target.parent.iterdir():
        if not entry.is_file():
            continue
        if entry.name == target.name:
            continue
        try:
            if entry.stat().st_size != size:
                continue
        except OSError as exc:
            log(f"[gdrive_reading_sync] stat failed during cold-start scan: {entry}: {exc}")
            continue
        try:
            if md5_file(entry).lower() != remote_md5.lower():
                continue
        except OSError as exc:
            log(f"[gdrive_reading_sync] md5 failed during cold-start scan: {entry}: {exc}")
            continue
        matches.append(entry)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # 호출 측이 retry/failed 로 보낼 표식 — 별도 예외 없이 같은 None 반환
        # 하되, 매칭 다중을 호출 측에서 알아채게 한다. 별도 함수가 매칭 수를
        # 다시 세는 비용을 피하려고 (size+md5) 같은 후보를 list 로 노출한다.
        return Path("__MULTIPLE__")  # 표식 sentinel
    return None


# ---- rclone 래퍼 ------------------------------------------------------

# §4.3 — _run_rclone 은 BookOasis 의 _rclone_config_args() 를 재사용한다.
def _bookoasis_config_args() -> list[str]:
    try:
        from utils.rclone_gdrive_copy import _rclone_config_args as _orig
    except Exception:
        return []
    try:
        args = _orig()
        return list(args) if args else []
    except Exception:
        return []


def _rclone_config_args(cfg: dict) -> list[str]:
    """§10 (S8) — rclone 호출마다 동일 인자를 만든다.

    RCLONE_CONFIG 가 빈 값이면 기존 `_bookoasis_config_args()` (BookOasis env /
    도커 번들 / ~/.config) 폴백을 그대로 사용 — 라이브 동작과 완전 호환.
    값이 있으면 `Path(value).expanduser()` 가 존재하는 파일이면 `["--config", str(path)]`,
    부재면 `FileNotFoundError` (조용히 폴백하지 않는다).
    """
    raw = (cfg.get("RCLONE_CONFIG") or "").strip() if isinstance(cfg, dict) else ""
    if not raw:
        return _bookoasis_config_args()
    path = Path(raw).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"RCLONE_CONFIG 경로 부재: {raw!r}")
    return ["--config", str(path)]


def _rclone_config_dump(bin_path: str, cfg: dict | None = None) -> dict:
    """config dump 결과를 dict 로. 실패 시 {}.

    §10 — _rclone_config_args(cfg) 를 통해 호출 (RCLONE_CONFIG 정본 경로 / 폴백).
    """
    bin = (bin_path or "rclone").strip() or "rclone"
    base_args = _rclone_config_args(cfg or {})
    args = [bin, *base_args, "config", "dump"]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"rclone 바이너리 없음: {bin!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"rclone config dump 시간 초과: {bin}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"rclone config dump 실패 (code={proc.returncode}): "
            f"{(proc.stderr or '').strip()[:200]}"
        )
    try:
        import json as _json
        return _json.loads(proc.stdout or "{}")
    except Exception as exc:
        raise RuntimeError(f"config dump 파싱 실패: {exc}") from exc


def _resolve_rclone_bin(cfg: dict) -> str:
    raw = (cfg.get("RCLONE_BIN") or "").strip()
    return raw or "rclone"


class RcloneCommandError(ValueError):
    """금지 동사/플래그 또는 argv 자체가 잘못됐을 때."""


def _validate_rclone_argv(args: list[str]) -> None:
    if not args:
        raise RcloneCommandError("빈 rclone argv")
    verb = args[0]
    if verb in FORBIDDEN_RCLONE_VERBS:
        raise RcloneCommandError(f"금지 rclone 동사: {verb}")
    for flag in args[1:]:
        if flag in FORBIDDEN_RCLONE_FLAGS:
            raise RcloneCommandError(f"금지 rclone 플래그: {flag}")


def _run_rclone(args: list[str], cfg: dict, timeout: float) -> tuple[int, bytes, bytes]:
    """§4.3 / §10 — cfg['RCLONE_BIN'] 또는 PATH 의 'rclone' 호출.

    `cfg['RCLONE_CONFIG']` 가 있으면 `--config <path>` 사용 (정본 경로).
    비우면 BookOasis 의 환경 폴백 — 라이브와 완전 호환.
    RCLONE_CONFIG 가 명시돼 있는데 파일이 없으면 FileNotFoundError 를 그대로
    올려서 호출 측이 `failed/bad_config` 로 종결하도록 둔다.
    """
    _validate_rclone_argv(args)
    bin_path = _resolve_rclone_bin(cfg)
    config_args = _rclone_config_args(cfg)
    argv = [bin_path, *config_args, *args]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"rclone 바이너리 부재: {bin_path!r} (argv={argv[:1]})"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        # timeout 도 nonzero code 로 보고 (rclone 자체 timeout 메시지는 stdout 에 남는다)
        raise RuntimeError(f"rclone timeout after {timeout}s: {args[:3]}") from exc
    return proc.returncode, proc.stdout or b"", proc.stderr or b""


def resolve_remote_kind(cfg: dict, remote_record: dict | None) -> str:
    """§4.1 — auto/명시 분기. record 가 gds_endpoint/gds_apikey 를 가지면 gds."""
    kind = (cfg.get("REMOTE_KIND") or "auto").strip().lower() or "auto"
    if kind in ("gds", "folder_id"):
        return kind
    if isinstance(remote_record, dict):
        for key in ("gds_endpoint", "gds_apikey", "gds_token"):
            if remote_record.get(key):
                return "gds"
    return "folder_id"


def build_remote_source(
    cfg: dict,
    remote_path: str,
    remote_record: dict | None,
) -> tuple[str, list[str]]:
    """§7.1 — GDS/folder_id 분기. copyto 와 lsjson 폴백 양쪽에 같은 extra 가 들어가야 한다.

    remote_path 는 READING 기준 상대경로. POSIX 구분자.
    """
    remote = (cfg.get("TRANSFER_REMOTE") or "").strip()
    if not remote:
        raise ValueError("TRANSFER_REMOTE 설정값 비어 있음")
    rel = (remote_path or "").replace("\\", "/").lstrip("/")
    # 이름 안의 전각 슬래시는 rclone 이 반각으로 되돌려 구분자로 읽는다 → escape 필수.
    rel = _rclone_remote_escape(rel)
    kind = resolve_remote_kind(cfg, remote_record)
    if kind == "gds":
        root = (cfg.get("REMOTE_ROOT_PATH") or "").strip().strip("/")
        if root:
            src = f"{remote}:{root}/{rel}" if rel else f"{remote}:{root}"
        else:
            src = f"{remote}:{rel}" if rel else f"{remote}:"
        return src, []
    # folder_id
    src = f"{remote}:{rel}" if rel else f"{remote}:"
    folder_id = (cfg.get("REMOTE_ROOT_FOLDER_ID") or "").strip()
    extra: list[str] = []
    if folder_id:
        extra = ["--drive-root-folder-id", folder_id]
    return src, extra


# ---- §7 검증 (r3: rclone check 제거) -----------------------------------

def _fetch_remote_md5(
    run,
    cfg: dict,
    timeout: float,
    source: str,
    extra: list[str],
) -> str:
    """Drive 의 lsjson --hash 폴백. 응답에서 첫 번째 비-디렉터리 항목의 MD5.

    rclone lsjson 의 한 항목은 `MD5` 키 또는 `Hashes` dict 에 `md5` 키를 가진다.
    """
    code, so, _se = run(["lsjson", "--hash", source, *extra], cfg, timeout)
    if code != 0 or not so:
        return ""
    try:
        arr = _json.loads(so.decode("utf-8", "replace") or "[]")
    except Exception:
        return ""
    if not isinstance(arr, list):
        return ""
    for item in arr:
        if not isinstance(item, dict):
            continue
        if item.get("IsDir"):
            continue
        md5 = item.get("MD5") or ""
        if not md5:
            hashes = item.get("Hashes") or {}
            if isinstance(hashes, dict):
                md5 = hashes.get("md5") or hashes.get("MD5") or ""
        if md5:
            return str(md5).strip().lower()
    return ""


def _verify_copy(
    run,
    cfg: dict,
    timeout: float,
    temp_path: Path,
    *,
    expected_size: int,
    expected_md5: str,
    source: str,
    extra: list[str],
    log=print,
) -> dict:
    """copyto 직후 검증. 리뷰 r3 F3.

    1) temp 파일이 존재하고 expected_size 와 일치하는지.
    2) expected_md5 가 있으면 md5 비교.
    3) expected_md5 가 비어 있으면 lsjson --hash 폴백으로 원격 md5 조회 후 비교.
    4) 둘 다 md5 가 없으면 크기 일치만 통과 + result="copied_size_only".
    """
    try:
        if not temp_path.is_file():
            return {"status": "fail", "result": "",
                    "error": f"verify: temp 파일 없음 {temp_path}"}
        actual_size = temp_path.stat().st_size
        if int(expected_size or 0) and actual_size != int(expected_size):
            return {"status": "fail", "result": "",
                    "error": f"verify: size mismatch local={actual_size} "
                             f"remote={expected_size}"}
    except OSError as exc:
        return {"status": "fail", "result": "",
                "error": f"verify: temp stat 실패: {exc}"}

    target_md5 = expected_md5.strip().lower()
    if not target_md5:
        # 폴백 — lsjson --hash 1회
        try:
            target_md5 = _fetch_remote_md5(run, cfg, timeout, source, extra)
        except Exception as exc:
            log(f"[gdrive_reading_sync] lsjson 폴백 실패: {exc}")
            target_md5 = ""
    if target_md5:
        try:
            local_md5 = md5_file(temp_path).lower()
        except OSError as exc:
            return {"status": "fail", "result": "",
                    "error": f"verify: md5 읽기 실패: {exc}"}
        if local_md5 != target_md5:
            return {"status": "fail", "result": "",
                    "error": f"verify: md5 mismatch local={local_md5} "
                             f"remote={target_md5}"}
        return {"status": "ok", "result": "copied", "error": ""}
    # md5 폴백도 실패 — 크기만 일치. result 로 구분 가능하게 남긴다.
    return {"status": "ok", "result": "copied_size_only", "error": ""}


# ---- 단일 job 처리 ----------------------------------------------------

def _process_directory_rename(
    store: Store,
    job_id: int,
    job: dict,
    local_root: Path,
    *,
    log=print,
) -> dict:
    """§4.3 / §4.4 — 폴더 rename 한 건 처리.

    동작:
    1) old / target 모두 _safe_under 통과해야 한다. 아니면 failed/bad_path.
    2) removed_path 비거나 옛 폴더 부재 + op_state 부재 → skipped/rename_source_missing.
       target 을 만들지 않는다.
    3) target 이미 존재 + op_state 부재 → skipped/rename_conflict. 덮어쓰지 않는다.
    4) os.rename 직전에 `op_state='directory_rename_started'` 기록 (crash-safe).
    5) actual: target.parent.mkdir 후 os.rename(old, target).
    6) 종료: store.finish_directory_rename(...) — 한 트랜잭션으로 item/queued/retry
       job 의 prefix 일괄 갱신 + 자기 자신을 skipped/renamed_directory 로 종결.
       local_path_for 콜백은 root 문법/플랫폼 규칙을 모르는 store 를 위한 순수
       결합 헬퍼 (`_local_path(local_root, remote_path)`).

    반환: dict {job_id, status, result, bytes_done, items_updated, jobs_updated, error}.
    rclone 호출 0건. bytes_done=0.
    """
    removed_path = (job.get("removed_path") or "").replace("\\", "/").lstrip("/")
    new_remote = (job.get("remote_path") or "").replace("\\", "/").lstrip("/")

    # 1) 안전 검사 — old / target 둘 다 LOCAL_ROOT 아래
    target = Path(job.get("local_path") or "")
    if not removed_path:
        store.finish_job(job_id, "skipped", result="rename_source_missing",
                         error="missing removed_path")
        return {"job_id": job_id, "status": "skipped", "result": "rename_source_missing",
                "bytes_done": 0, "items_updated": 0, "jobs_updated": 0,
                "error": "missing removed_path"}
    old = local_root / removed_path
    if not _safe_under(local_root, old) or not _safe_under(local_root, target):
        store.finish_job(job_id, "failed", result="bad_path",
                         error="directory path outside LOCAL_ROOT")
        return {"job_id": job_id, "status": "failed", "result": "bad_path",
                "bytes_done": 0, "items_updated": 0, "jobs_updated": 0,
                "error": "directory path outside LOCAL_ROOT"}

    # 현재 op_state 조회 (crash 재개 가드) — 첫 시도(op_state 비어있음) 또는 재개
    # §4.2 / §P6 — Store.get_job_op_state 로 잠긴 메서드 경유. 직접 writer SQL 0건.
    op_state = store.get_job_op_state(int(job_id))
    started = bool(op_state == "directory_rename_started")

    # 2) source 부재 + 첫 시도 → skipped, 생성 0
    if not old.exists() and not started:
        store.finish_job(job_id, "skipped", result="rename_source_missing",
                         error="old folder does not exist")
        return {"job_id": job_id, "status": "skipped", "result": "rename_source_missing",
                "bytes_done": 0, "items_updated": 0, "jobs_updated": 0,
                "error": "old folder does not exist"}

    # 3) target 이미 존재 + 첫 시도 → conflict
    if target.exists() and not started:
        store.finish_job(job_id, "skipped", result="rename_conflict",
                         error="target already exists")
        return {"job_id": job_id, "status": "skipped", "result": "rename_conflict",
                "bytes_done": 0, "items_updated": 0, "jobs_updated": 0,
                "error": "target already exists"}

    # 4) os.rename 직전 op_state 기록 (재시작 가용성)
    if not started:
        # §4.2 / §P6 — mark_job_op_state_locked 가 _WRITER_LOCK 안에서 commit 까지
        # 책임진다. sync_worker 가 store._writer 를 직접 만지지 않는다.
        store.mark_job_op_state_locked(job_id, "directory_rename_started")

    # crash 재개 케이스: started 가드에서 old 부재 + target 존재 = 이미 옮겨짐. noop.
    if started and not old.exists() and target.is_dir():
        log(f"[gdrive_reading_sync] directory rename resumed for job {job_id}: already done")
    elif started and old.exists():
        # 비정상 케이스: started 인데 old가 다시 살아있음 — 다시 rename 시도 안 한다.
        store.finish_job(job_id, "skipped", result="rename_conflict",
                         error="directory_rename_started but source exists again")
        return {"job_id": job_id, "status": "skipped", "result": "rename_conflict",
                "bytes_done": 0, "items_updated": 0, "jobs_updated": 0,
                "error": "stale started state"}
    else:
        # 정상 케이스 (첫 시도): 실제 os.rename
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old, target)
        except OSError as exc:
            store.finish_job(job_id, "retry", result="",
                             error=f"os.rename directory failed: {exc}")
            return {"job_id": job_id, "status": "retry", "result": "",
                    "bytes_done": 0, "items_updated": 0, "jobs_updated": 0,
                    "error": f"os.rename directory failed: {exc}"}

    # 5) DB 마무리 — item prefix 갱신 + queued/retry 갱신 + 자기 자신 종결
    #    local_path_for callback 은 _local_path 와 동일 (단, _local_root_path 규칙 사용)
    def _local_path_for(remote_path: str) -> str:
        return _local_path(str(local_root), remote_path)

    out = store.finish_directory_rename(
        job_id, removed_path, new_remote, _local_path_for,
    )
    return {
        "job_id": job_id, "status": "skipped", "result": "renamed_directory",
        "bytes_done": 0,
        "items_updated": out["items_updated"], "jobs_updated": out["jobs_updated"],
        "error": "",
    }


class _GLOBAL_NOOP:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _prepare_copy_job(
    store: Store,
    job: dict,
    cfg: dict,
    *,
    tmp_root: Path | None = None,
    log=print,
) -> dict:
    """§4.1 — process_job 의 pre-copy 판정 (D1/rename/delete) 만 떼어낸 helper.

    반환:
      `{"terminal": True, "out": <process_job 호환 dict>}` — 이미 종결 결정됨.
      `{"terminal": False, "job": job, "local_root": Path, "tmp_root": Path,
        "target": Path}` — 실제 copy 가 필요. 호출 측이 `_execute_prepared_copy` 로.

    디렉터리 rename 분기는 _process_directory_rename 으로 별도 분기되므로
    process_job 의 첫 단계에서 처리된다. 여기 들어오지 않는다.
    """
    job_id = int(job.get("id"))
    action = (job.get("action") or "").lower()
    target = Path(job.get("local_path") or "")
    local_root = _local_root_path(cfg)
    if local_root is None:
        store.finish_job(
            job_id, "failed", result="bad_path",
            error="LOCAL_ROOT not configured on this platform",
        )
        return {"terminal": True, "out": {
            "job_id": job_id, "status": "failed", "result": "bad_path",
            "bytes_done": 0, "error": "LOCAL_ROOT not configured"}}
    tmp = (
        Path(tmp_root) if tmp_root is not None else _tmp_root_path(cfg, local_root)
    )
    if not target or not _safe_under(local_root, target):
        store.finish_job(job_id, "failed", result="bad_path",
                         error="local_path outside LOCAL_ROOT")
        return {"terminal": True, "out": {
            "job_id": job_id, "status": "failed", "result": "bad_path",
            "bytes_done": 0, "error": "local_path outside LOCAL_ROOT"}}

    # §6.1 — 1겹 rename
    if action == "rename" and job.get("removed_path"):
        old_rel = (job.get("removed_path") or "").replace("\\", "/").lstrip("/")
        old = local_root / old_rel if old_rel else Path()
        if old.is_file() and not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(old, target)
            except OSError as exc:
                store.finish_job(
                    job_id, "retry", result="", error=f"os.rename failed: {exc}"
                )
                return {"terminal": True, "out": {
                    "job_id": job_id, "status": "retry", "result": "",
                    "bytes_done": 0, "error": str(exc)}}
            if same_content(target, int(job.get("size") or 0),
                            job.get("md5") or ""):
                store.finish_job(
                    job_id, "skipped", result="renamed_event", bytes_done=0
                )
                return {"terminal": True, "out": {
                    "job_id": job_id, "status": "skipped",
                    "result": "renamed_event", "bytes_done": 0, "error": ""}}
        elif target.exists() and not same_content(
            target, int(job.get("size") or 0), job.get("md5") or ""
        ):
            store.finish_job(
                job_id, "retry", result="rename_conflict",
                error="rename target exists with different content",
            )
            return {"terminal": True, "out": {
                "job_id": job_id, "status": "retry",
                "result": "rename_conflict", "bytes_done": 0,
                "error": "rename target exists with different content"}}

    # §5 — D1
    if same_content(target, int(job.get("size") or 0), job.get("md5") or ""):
        store.finish_job(job_id, "skipped", result="duplicate", bytes_done=0)
        return {"terminal": True, "out": {
            "job_id": job_id, "status": "skipped", "result": "duplicate",
            "bytes_done": 0, "error": ""}}

    # §6.2 — 콜드 스타트
    if action != "delete":
        candidate = find_cold_start_candidate(
            target, int(job.get("size") or 0), job.get("md5") or "", log=log
        )
        if candidate is not None:
            if str(candidate) == "__MULTIPLE__":
                store.finish_job(
                    job_id, "retry", result="rename_candidates_multiple",
                    error="multiple cold-start candidates, refusing random pick",
                )
                return {"terminal": True, "out": {
                    "job_id": job_id, "status": "retry",
                    "result": "rename_candidates_multiple",
                    "bytes_done": 0, "error": "multiple candidates"}}
            try:
                os.rename(candidate, target)
            except OSError as exc:
                store.finish_job(
                    job_id, "retry", result="",
                    error=f"os.rename (cold) failed: {exc}"
                )
                return {"terminal": True, "out": {
                    "job_id": job_id, "status": "retry", "result": "",
                    "bytes_done": 0, "error": str(exc)}}
            if same_content(target, int(job.get("size") or 0),
                            job.get("md5") or ""):
                store.finish_job(
                    job_id, "skipped", result="renamed_cold_start", bytes_done=0
                )
                return {"terminal": True, "out": {
                    "job_id": job_id, "status": "skipped",
                    "result": "renamed_cold_start", "bytes_done": 0, "error": ""}}

    if action == "delete":
        store.finish_job(
            job_id, "skipped", result="remote_deleted", bytes_done=0
        )
        return {"terminal": True, "out": {
            "job_id": job_id, "status": "skipped",
            "result": "remote_deleted", "bytes_done": 0, "error": ""}}

    return {"terminal": False, "job": job, "local_root": local_root,
            "tmp_root": tmp, "target": target}


def _execute_prepared_copy(
    store: Store,
    prepared: dict,
    cfg: dict,
    *,
    run_rclone=None,
    log=print,
) -> dict:
    """§4.1 — `_prepare_copy_job` 의 `terminal=False` 결과를 받아 copy 3단 수행.

    copyto → 로컬 md5+크기 검증 → os.replace → finish_job. rename/delete/D1/cold-start
    코드는 포함하지 않는다. store 는 Store 메서드 경유로만 만진다 (직접 writer SQL 0건).
    """
    run = run_rclone or _run_rclone
    job = prepared["job"]
    tmp = prepared["tmp_root"]
    target = prepared["target"]
    job_id = int(job.get("id"))
    try:
        tmp.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_path = tmp / f"{job_id}.part"

        remote_record = None
        if (cfg.get("REMOTE_KIND") or "auto").strip().lower() == "auto":
            try:
                dump = _rclone_config_dump(_resolve_rclone_bin(cfg), cfg)
                rec = dump.get((cfg.get("TRANSFER_REMOTE") or "").strip())
                if isinstance(rec, dict):
                    remote_record = rec
            except Exception as exc:
                log(f"[gdrive_reading_sync] remote record 조회 실패: {exc}")
                remote_record = None

        source, extra = build_remote_source(
            cfg, str(job.get("remote_path") or ""), remote_record
        )
        timeout = float(cfg.get("RCLONE_TIMEOUT") or 1800)

        code, _, err = run(["copyto", source, str(temp_path), *extra], cfg, timeout)
        if code != 0:
            err_tail = (err or b"")[:400].decode("utf-8", "replace")
            store.finish_job(
                job_id, "retry", result="",
                error=f"copyto exit {code}: {err_tail}",
            )
            return {"job_id": job_id, "status": "retry", "result": "",
                    "bytes_done": 0, "error": f"copyto exit {code}"}

        verify_result = _verify_copy(
            run, cfg, timeout, temp_path,
            expected_size=int(job.get("size") or 0),
            expected_md5=(job.get("md5") or "").strip(),
            source=source, extra=extra, log=log,
        )
        if verify_result["status"] != "ok":
            err_msg = verify_result["error"]
            try:
                if temp_path.is_file():
                    temp_path.unlink()
            except OSError:
                pass
            store.finish_job(
                job_id, "retry", result="", error=err_msg,
            )
            return {"job_id": job_id, "status": "retry", "result": "",
                    "bytes_done": 0, "error": err_msg}

        try:
            os.replace(temp_path, target)
        except OSError as exc:
            store.finish_job(
                job_id, "retry", result="", error=f"os.replace failed: {exc}"
            )
            return {"job_id": job_id, "status": "retry", "result": "",
                    "bytes_done": 0, "error": str(exc)}

        size = int(job.get("size") or 0)
        store.finish_job(
            job_id, "completed", result=verify_result["result"],
            bytes_done=size,
        )
        return {"job_id": job_id, "status": "completed",
                "result": verify_result["result"],
                "bytes_done": size, "error": ""}
    except Exception as exc:
        log(f"[gdrive_reading_sync] process_job {job_id} exception: {exc}")
        store.finish_job(
            job_id, "retry", result="", error=f"{type(exc).__name__}: {exc}"
        )
        return {"job_id": job_id, "status": "retry", "result": "",
                "bytes_done": 0, "error": str(exc)}


def _parallel_transfers(cfg: dict) -> int:
    """§2 — PARALLEL_TRANSFERS 정규화. 비수치 5, 1..32 clamp."""
    try:
        v = int(cfg.get("PARALLEL_TRANSFERS", 5))
    except Exception:
        v = 5
    return max(1, min(v, 32))


def process_job(
    store: Store,
    job: dict,
    cfg: dict,
    *,
    run_rclone=None,
    tmp_root: Path | None = None,
    log=print,
) -> dict:
    """한 job 의 종결까지. 호출 전 store 가 이미 processing 으로 옮겨둔 상태.

    §4.1 — 외부 시그니처는 그대로. 내부에서 `_prepare_copy_job` + `_execute_prepared_copy`
    직렬 호출 wrapper. action=rename + item_type=directory 는 _process_directory_rename
    으로 별도 분기 (병렬 executor 진입 금지 — §P5).
    """
    job_id = int(job.get("id"))
    action = (job.get("action") or "").lower()
    item_type = (job.get("item_type") or "file").lower()

    # 폴더 rename 분기 — executor 진입 금지. process_jobs 에서도 별도 직렬.
    if action == "rename" and item_type == "directory":
        local_root = _local_root_path(cfg)
        if local_root is None:
            store.finish_job(
                job_id, "failed", result="bad_path",
                error="LOCAL_ROOT not configured on this platform",
            )
            return {"job_id": job_id, "status": "failed", "result": "bad_path",
                    "bytes_done": 0, "error": "LOCAL_ROOT not configured"}
        return _process_directory_rename(
            store, job_id, job, local_root, log=log,
        )

    prepared = _prepare_copy_job(
        store, job, cfg, tmp_root=tmp_root, log=log,
    )
    if prepared.get("terminal"):
        return prepared["out"]
    return _execute_prepared_copy(
        store, prepared, cfg, run_rclone=run_rclone, log=log,
    )


def process_jobs(
    store: Store,
    cfg: dict,
    *,
    run_rclone=None,
    tmp_root: Path | None = None,
    log=print,
) -> dict:
    """한 사이클 — JOBS_PER_CYCLE 만큼 claim 하고 처리.

    §4.1 — `PARALLEL_TRANSFERS=1` 이면 기존 직렬 for 루프 그대로 (P1 회귀 보장).
    `>1` 이면: directory rename → file rename → 기타 non-copy(create/edit 의 D1/rename
    판정 포함) 순으로 main thread 에서 직렬 처리한 뒤, 실제 copy 가 필요한
    create/edit 만 `ThreadPoolExecutor` 로 동시 실행한다. executor 는 copy phase 만
    사용하며 rename 이나 그 밖의 0바이트 job 은 executor 에 들어가지 않는다.
    """
    limit = int(cfg.get("JOBS_PER_CYCLE") or 20)
    max_attempts = int(cfg.get("MAX_ATTEMPTS") or 3)
    jobs = store.claim_jobs(limit=limit, max_attempts=max_attempts)
    summary = {
        "claimed": len(jobs),
        "completed": 0,
        "skipped": 0,
        "retried": 0,
        "failed": 0,
        "bytes_done": 0,
        "errors": [],
        # 실제로 로컬 트리가 바뀐 폴더들. 호출 측(플러그인)이 이 폴더를 품는
        # BookOasis 라이브러리만 골라 스캔 큐에 넣는다.
        "landed_dirs": [],
    }

    parallel_n = _parallel_transfers(cfg)

    def _accumulate(out: dict, job: dict | None = None) -> None:
        st = out.get("status")
        if st == "completed":
            summary["completed"] += 1
        elif st == "skipped":
            summary["skipped"] += 1
        elif st == "failed":
            summary["failed"] += 1
        else:
            summary["retried"] += 1
        summary["bytes_done"] += int(out.get("bytes_done") or 0)
        if out.get("error"):
            summary["errors"].append(f"{out['job_id']}:{out['error']}")
        # 새 파일이 내려앉았거나(completed) 로컬에서 이름이 바뀐 경우(renamed_*)만
        # 디스크가 실제로 변했다. duplicate/remote_deleted 는 변화 없음.
        landed = st == "completed" or str(out.get("result") or "").startswith("renamed")
        if landed and job is not None:
            d = os.path.dirname(str(job.get("local_path") or ""))
            if d and d not in summary["landed_dirs"]:
                summary["landed_dirs"].append(d)

    if parallel_n <= 1 or len(jobs) <= 1:
        # P1 — 기존 직렬 경로. 동작/어휘/요약 순서 동일.
        for job in jobs:
            try:
                out = process_job(
                    store, job, cfg, run_rclone=run_rclone,
                    tmp_root=tmp_root, log=log,
                )
            except Exception as exc:
                log(f"[gdrive_reading_sync] process_job uncaught: {exc}")
                store.finish_job(
                    int(job.get("id")), "retry", result="",
                    error=f"{type(exc).__name__}: {exc}",
                )
                out = {"job_id": int(job.get("id")), "status": "retry",
                       "result": "", "bytes_done": 0, "error": str(exc)}
            _accumulate(out, job)
        return summary

    # §4.1 — 병렬 경로. 단계:
    #   1) directory rename 만 별도 직렬 (executor 진입 금지 — §P5)
    #   2) file rename / delete / 기타 non-copy create/edit 직렬 preparation
    #   3) 실제 copy 가 필요한 create/edit 만 executor 에 동시 제출
    copy_jobs: list[dict] = []
    for job in jobs:
        action = (job.get("action") or "").lower()
        item_type = (job.get("item_type") or "file").lower()
        if action == "rename" and item_type == "directory":
            try:
                out = process_job(
                    store, job, cfg, run_rclone=run_rclone,
                    tmp_root=tmp_root, log=log,
                )
            except Exception as exc:
                log(f"[gdrive_reading_sync] process_job uncaught: {exc}")
                store.finish_job(
                    int(job.get("id")), "retry", result="",
                    error=f"{type(exc).__name__}: {exc}",
                )
                out = {"job_id": int(job.get("id")), "status": "retry",
                       "result": "", "bytes_done": 0, "error": str(exc)}
            _accumulate(out, job)
            continue
        try:
            prepared = _prepare_copy_job(
                store, job, cfg, tmp_root=tmp_root, log=log,
            )
        except Exception as exc:
            log(f"[gdrive_reading_sync] prepare_copy_job uncaught: {exc}")
            store.finish_job(
                int(job.get("id")), "retry", result="",
                error=f"{type(exc).__name__}: {exc}",
            )
            prepared = {"terminal": True, "out": {
                "job_id": int(job.get("id")), "status": "retry",
                "result": "", "bytes_done": 0, "error": str(exc)}}
        if prepared.get("terminal"):
            _accumulate(prepared["out"], job)
            continue
        copy_jobs.append(prepared)

    if not copy_jobs:
        return summary

    # copy phase — main thread 가 future 를 수거. executor 가 store._writer 를
    # 직접 만지지 않는 것은 _execute_prepared_copy 가 Store 메서드만 호출하기 때문.
    max_workers = min(parallel_n, len(copy_jobs))
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="gdrs-transfer",
    ) as ex:
        future_to_prepared = {
            ex.submit(
                _execute_prepared_copy,
                store, prepared, cfg,
                run_rclone=run_rclone, log=log,
            ): prepared
            for prepared in copy_jobs
        }
        for fut in future_to_prepared:
            prepared = future_to_prepared[fut]
            try:
                out = fut.result()
            except Exception as exc:
                job_id = int(prepared["job"].get("id"))
                log(f"[gdrive_reading_sync] copy future uncaught: {exc}")
                store.finish_job(
                    job_id, "retry", result="",
                    error=f"{type(exc).__name__}: {exc}",
                )
                out = {"job_id": job_id, "status": "retry",
                       "result": "", "bytes_done": 0, "error": str(exc)}
            _accumulate(out, prepared["job"])
    return summary



# ---- §9.3 — 재시작 복구 ------------------------------------------------

def _recover_tmp_files(
    tmp_root: Path,
    job_ids: list[int],
    log=print,
) -> list[dict]:
    """tmp_root 아래 {정수}.part 만 삭제. 규칙 밖은 절대 손대지 않는다."""
    out: list[dict] = []
    try:
        if not tmp_root.exists():
            return out
    except OSError:
        return out
    expected = {int(i) for i in job_ids}
    for entry in tmp_root.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not name.endswith(".part"):
            continue
        stem = name[: -len(".part")]
        try:
            n = int(stem)
        except ValueError:
            # 정수가 아니면 보존 (사용자 파일 보호)
            continue
        if n not in expected:
            continue
        try:
            entry.unlink()
            out.append({"path": str(entry), "deleted": True})
            log(f"[gdrive_reading_sync] tmp cleanup: {entry}")
        except OSError as exc:
            out.append({"path": str(entry), "deleted": False, "error": str(exc)})
            log(f"[gdrive_reading_sync] tmp cleanup FAIL: {entry}: {exc}")
    return out


def recover_on_start(
    store: Store,
    cfg: dict,
    *,
    tmp_root: Path | None = None,
    log=print,
) -> dict:
    """§9.3 — startup 직후 1회. processing → retry/failed + tmp 파일 정리."""
    local_root = _local_root_path(cfg)
    recovered = store.recover_processing(
        max_attempts=int(cfg.get("MAX_ATTEMPTS") or 3),
    )
    tmp = (
        Path(tmp_root) if tmp_root is not None else _tmp_root_path(cfg, local_root)
    )
    job_ids = [r["id"] for r in recovered]
    if tmp is None:
        cleanup: list[dict] = []
    else:
        cleanup = _recover_tmp_files(tmp, job_ids, log=log)
    failed_cleanup = [c for c in cleanup if not c.get("deleted")]
    for rec in recovered:
        status = rec["status"]
        result = rec.get("result") or "recovered"
        # §5.1 — recover_processing 이 이미 status/result/attempts/recovery_count 까지
        # 결정해서 박았다. 후속 finish_job 은 그 값을 그대로 보존하는 의미만 가지므로
        # 여기서도 같은 값으로 한 번 더 박되 reset_recovery=False 로 recovery_count 를
        # 유지하고, error/result/status 도 recover_processing 의 값을 보존한다.
        error_msg = (
            "consecutive recovery limit exceeded"
            if result == "recovery_limit_exceeded"
            else "recovered after interrupted processing"
        )
        store.finish_job(
            rec["id"], status,
            result=result,
            error=error_msg,
            reset_recovery=False,  # §5.1 — recovery_count 는 연속 복구 상한을 위해 보전
        )
        if status == "failed" or failed_cleanup:
            # §9.3 — 삭제 실패면 해당 job 을 failed 로 두고 복사 시작 금지.
            # 이미 failed 인 행이면 그대로 둔다. recovery_limit_exceeded 의 failed 는
            # 그대로 두어야 한다 (덮어쓰면 안 됨).
            if status != "failed" and result != "recovery_limit_exceeded":
                store.finish_job(
                    rec["id"], "failed",
                    result="recovered_tmp_cleanup_failed",
                    error="recovered tmp file deletion failed",
                    reset_recovery=False,
                )
    return {
        "recovered": len(recovered),
        "tmp_cleanup": cleanup,
    }


# ---- §4.4 — preflight 헬퍼 -------------------------------------------

def _parse_version_tuple(s: str) -> tuple[int, ...]:
    """`rclone version` 첫 줄에서 major/minor[/patch] 만 추출.

    리뷰 r2 F1 — 기존 구현은 `"rclone v1.67.0-106".replace("v","").split(".")` 의
    첫 조각 `"rclone 1"` 에서 `'r'`에서 break 해 major 를 통째로 잃었다. 정규식으로
    첫 숫자 시퀀스를 단단히 잡고, 매치 실패 시 빈 tuple 을 돌려준다 (호출 측이
    차단으로 처리한다).
    """
    import re as _re
    if not s:
        return ()
    m = _re.search(r"v?(\d+)\.(\d+)(?:\.(\d+))?", s)
    if not m:
        return ()
    g1, g2, g3 = m.group(1), m.group(2), m.group(3)
    try:
        nums = [int(g1), int(g2)]
    except (TypeError, ValueError):
        return ()
    if g3 is not None:
        try:
            nums.append(int(g3))
        except (TypeError, ValueError):
            return ()
    return tuple(nums)


def _config_file_from_output(raw: bytes | str) -> str:
    """`rclone config file` 출력에서 실제 경로 한 줄만 꺼낸다."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def inspect_rclone_setup(cfg: dict, *, include_config: bool = False) -> dict:
    """설정 UI에서 사용할 읽기 전용 rclone 검사 결과를 반환한다.

    `version`, `config file`, `config dump`만 실행한다. config dump의 원문이나
    리모트 레코드는 반환하지 않아 토큰·API 키가 UI 응답으로 노출되지 않는다.
    """
    bin_setting = _resolve_rclone_bin(cfg)
    resolved = shutil.which(bin_setting)
    if not resolved:
        candidate = Path(bin_setting).expanduser()
        resolved = str(candidate.resolve()) if candidate.is_file() else ""
    out = {
        "success": False,
        "error": "",
        "rclone_resolved": resolved,
        "rclone_version": "",
        "config_file": "",
        "remotes": [],
    }
    if not resolved:
        out["error"] = f"rclone 실행 파일을 찾을 수 없습니다: {bin_setting!r}"
        return out

    check_cfg = dict(cfg)
    check_cfg["RCLONE_BIN"] = resolved
    # 실행 파일 확인은 사용자가 입력한 설정 파일의 유효성과 독립적이어야 한다.
    if not include_config:
        check_cfg["RCLONE_CONFIG"] = ""
    try:
        code, stdout, stderr = _run_rclone(["version"], check_cfg, 15)
    except Exception as exc:
        out["error"] = str(exc)
        return out
    if code != 0:
        out["error"] = (stderr or b"").decode("utf-8", "replace")[:300].strip()
        return out

    first = (stdout or b"").decode("utf-8", "replace").splitlines()
    out["rclone_version"] = first[0].strip() if first else ""
    parsed = _parse_version_tuple(out["rclone_version"])
    if not parsed:
        out["error"] = f"rclone 버전을 읽을 수 없습니다: {out['rclone_version']!r}"
        return out
    if parsed < (1, 75, 0):
        out["error"] = f"rclone v1.75.0 이상이 필요합니다: {out['rclone_version']}"
        return out
    if not include_config:
        out["success"] = True
        return out

    try:
        code, stdout, stderr = _run_rclone(["config", "file"], check_cfg, 10)
        if code != 0:
            out["error"] = (stderr or b"").decode("utf-8", "replace")[:300].strip()
            return out
        out["config_file"] = _config_file_from_output(stdout)
        dump = _rclone_config_dump(resolved, check_cfg)
        out["remotes"] = sorted(
            name for name, record in (dump or {}).items()
            if isinstance(record, dict) and record.get("type") == "drive"
        )
    except Exception as exc:
        out["error"] = str(exc)
        return out

    out["success"] = True
    return out


def preflight(cfg: dict, run_rclone=None, log=print) -> dict:
    """§4.4 — 실제 실행된 바이너리·버전·리모트·probe 를 돌려준다.

    Drive / 로컬에 쓰지 않는다. 실패는 success=false. 테스트는 run_rclone 으로
    stub 한 뒤 shutil.which 도 함께 mock 하는 것을 권장한다.
    """
    run = run_rclone or _run_rclone
    bin_setting = _resolve_rclone_bin(cfg)
    # §4.4 — 테스트 호환: run_rclone 이 stub 이면 shutil.which 없이 통과한다.
    if run_rclone is not None:
        resolved = bin_setting
    else:
        resolved = shutil.which(bin_setting) or (bin_setting if Path(bin_setting).exists() else "")
    out: dict = {
        "success": False,
        "error": "",
        "rclone_bin": bin_setting,
        "rclone_resolved": resolved or "",
        "rclone_version": "",
        "config_file": "",
        "remotes": [],
        "transfer_remote": (cfg.get("TRANSFER_REMOTE") or "").strip(),
        "remote_kind": "",
        "probe": {"path": "", "ok": False, "sample": {}},
    }
    if not resolved:
        out["error"] = f"rclone 바이너리 부재: {bin_setting!r}"
        return out
    # version
    try:
        code, so, se = run(["version"], cfg, 15)
    except (FileNotFoundError, RuntimeError) as exc:
        out["error"] = str(exc)
        return out
    if code != 0:
        out["error"] = (se or b"")[:200].decode("utf-8", "replace")
        return out
    first = (so or b"").decode("utf-8", "replace").splitlines()[0] if so else ""
    out["rclone_version"] = first.strip()
    # 리뷰 r2 F1 — 파싱 실패는 차단으로 명시. 빈 tuple 은 "버전 못 읽음"이다.
    parsed = _parse_version_tuple(out["rclone_version"])
    if not parsed:
        out["error"] = f"rclone 버전 파싱 실패: {out['rclone_version']!r}"
        return out
    if parsed < (1, 75, 0):
        out["error"] = f"rclone v1.75.0 미만: {out['rclone_version']}"
        return out
    # config file
    try:
        code, so, se = run(["config", "file"], cfg, 10)
    except Exception as exc:
        out["error"] = f"config file: {exc}"
        return out
    out["config_file"] = (so or b"").decode("utf-8", "replace").strip()
    # config dump — drive remotes 만
    remotes: list[str] = []
    remote_records: dict = {}
    try:
        dump = _rclone_config_dump(bin_setting, cfg)
        for name, rec in (dump or {}).items():
            if isinstance(rec, dict) and rec.get("type") == "drive":
                remotes.append(name)
                remote_records[name] = rec
        remotes = sorted(set(remotes))
    except Exception as exc:
        log(f"[gdrive_reading_sync] preflight config dump 실패: {exc}")
    if not remotes:
        # fallback — 현재 설정값만. 특정 리모트 이름을 코드에 박지 않는다.
        remotes = sorted({
            (cfg.get("TRANSFER_REMOTE") or "").strip(),
            (cfg.get("DETECT_REMOTE") or "").strip(),
            } - {""})
    out["remotes"] = remotes
    # remote_kind
    transfer = (cfg.get("TRANSFER_REMOTE") or "").strip()
    out["remote_kind"] = resolve_remote_kind(cfg, remote_records.get(transfer))
    # probe — 같은 source 분기로 잡지/SPARK 의 첫 항목 1개만
    probe_path = "잡지/SPARK"
    try:
        rec = remote_records.get(transfer) if transfer else None
        src, extra = build_remote_source(cfg, probe_path, rec)
        code, so, se = run(
            ["lsjson", "--hash", src, *extra], cfg, 30
        )
        if code == 0 and so:
            import json as _json
            arr = _json.loads(so.decode("utf-8", "replace") or "[]")
            sample = {}
            for item in arr or []:
                if item.get("IsDir"):
                    continue
                sample = {
                    "name": item.get("Name") or "",
                    "size": int(item.get("Size") or 0),
                    "md5": item.get("MD5") or "",
                }
                break
            out["probe"] = {"path": probe_path, "ok": True, "sample": sample}
        else:
            tail = (se or b"")[:200].decode("utf-8", "replace")
            out["probe"] = {"path": probe_path, "ok": False, "sample": {}, "error": tail}
    except Exception as exc:
        out["probe"] = {"path": probe_path, "ok": False, "sample": {}, "error": str(exc)}
    out["success"] = True
    return out


def config_fingerprint(cfg: dict, preflight_out: dict) -> tuple:
    """preflight 성공 캐시 키. 설정이 바뀌거나 프로세스 재시작이면 gate 무효.

    §10 — RCLONE_CONFIG 의 정규화 문자열도 포함. 빈 값이면 '' 로 들어가지만 빈 값
    → 폴백 동작이 같은 한 fingerprint 가 같아야 한다 (이 경우 '' 이 어느 쪽과도 매치).
    """
    return (
        preflight_out.get("rclone_resolved") or "",
        preflight_out.get("transfer_remote") or (cfg.get("TRANSFER_REMOTE") or "").strip(),
        preflight_out.get("remote_kind") or "",
        (cfg.get("REMOTE_ROOT_PATH") or "").strip(),
        (cfg.get("REMOTE_ROOT_FOLDER_ID") or "").strip(),
        (cfg.get("RCLONE_CONFIG") or "").strip(),
    )
