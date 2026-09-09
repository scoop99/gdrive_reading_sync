# -*- coding: utf-8 -*-
"""SQLite-backed local state for gdrive_reading_sync.

워커 스레드 전용 writer 연결 1개 + 웹 핸들러용 reader 연결. 책 §8에 따라 단일
라이터 가정이라 추가 락은 두지 않는다. schema/migrations는 CREATE TABLE IF NOT
EXISTS만; 향후 ALTER 시점에만 점진 확장.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path


PLUGIN_ID = "gdrive_reading_sync"

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS sync_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        page_token TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'ready',
        last_poll_at TEXT,
        error TEXT NOT NULL DEFAULT '',
        daily_bytes INTEGER NOT NULL DEFAULT 0,
        daily_date TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS item (
        file_id TEXT PRIMARY KEY,
        parent_id TEXT,
        name TEXT,
        remote_path TEXT,
        is_directory INTEGER NOT NULL DEFAULT 0,
        size INTEGER,
        md5 TEXT,
        modified_time TEXT,
        updated_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS job (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT UNIQUE,
        target TEXT NOT NULL DEFAULT 'general',
        action TEXT NOT NULL,
        file_id TEXT,
        remote_path TEXT,
        removed_path TEXT NOT NULL DEFAULT '',
        item_type TEXT NOT NULL DEFAULT 'file',
        local_path TEXT,
        size INTEGER DEFAULT 0,
        md5 TEXT,
        modified_time TEXT,
        status TEXT NOT NULL DEFAULT 'dry_run',
        attempts INTEGER NOT NULL DEFAULT 0,
        bytes_done INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '',
        result TEXT NOT NULL DEFAULT '',
        created_at TEXT,
        updated_at TEXT,
        started_at TEXT,
        finished_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_job_status ON job(status, id)",
    # P9 — 복사 완료 후 책 폴더 단위 자동 스캔. 1행 = 1책 폴더 = 1스캔 단위.
    # queue 를 거치지 않으므로 여기서 debounce(queued_at 기준)와 claim 상태를 관리한다.
    """CREATE TABLE IF NOT EXISTS scan (
        folder      TEXT PRIMARY KEY,   -- 로컬 절대경로. 스캔 단위 = 책 폴더
        session     TEXT NOT NULL DEFAULT 'general',
        library_id  INTEGER,
        status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|skipped
        reason      TEXT NOT NULL DEFAULT '',         -- skipped 사유: library_root|no_library
        queued_at   TEXT,                             -- 마지막 landing 시각 = 디바운스 기준점
        started_at  TEXT,
        finished_at TEXT,
        files       INTEGER NOT NULL DEFAULT 0,       -- 이번 대기분에 내려앉은 파일 수
        new_books   INTEGER,                          -- §5.3 / §12-A — 본체 훅으로만 기록. 못 받으면 NULL
        error       TEXT NOT NULL DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_scan_status ON scan(status, queued_at)",
]

# 기존 state.db 에 나중에 생긴 컬럼을 붙인다. CREATE TABLE IF NOT EXISTS 로는
# 이미 있는 테이블에 컬럼이 추가되지 않는다. 중복 실행은 OperationalError 로
# 조용히 넘어간다.
_MIGRATIONS = [
    "ALTER TABLE job ADD COLUMN removed_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE job ADD COLUMN item_type TEXT NOT NULL DEFAULT 'file'",
    "ALTER TABLE job ADD COLUMN result TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE job ADD COLUMN started_at TEXT",
    # S2 — 폴더 rename 단계 복구용 내부 상태. 사용자 결과가 아니다. 최종 응답 직전 '' 로 되돌린다.
    "ALTER TABLE job ADD COLUMN op_state TEXT NOT NULL DEFAULT ''",
    # S3 — 연속 복구 횟수. 이 값이 단조 증가하여 MAX_CONSECUTIVE_RECOVERIES 를 넘으면
    # recover_processing 가 그 job 을 failed 로 보낸다 (attempts 보전과 별개 상한).
    "ALTER TABLE job ADD COLUMN recovery_count INTEGER NOT NULL DEFAULT 0",
    # 리뷰 r2 F2 — dry_run→queued 승격 UPDATE 를 여기서 제거. 조회 부작용 방지.
    # 1회성 격리는 promote_dry_run_to_queued() 가 sync_state.dry_run_promoted 로 가드.
    "ALTER TABLE sync_state ADD COLUMN dry_run_promoted INTEGER NOT NULL DEFAULT 0",
]

_INIT_LOCK = threading.Lock()
_WRITER_LOCK = threading.Lock()


def _db_path(plugin_root: Path) -> Path:
    # metadata 플러그인의 data 형제: plugins/data/gdrive_reading_sync/state.db
    return plugin_root.parent.parent / "data" / PLUGIN_ID / "state.db"


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def open_store(plugin_file: str | os.PathLike[str]) -> "Store":
    # absolute() 이지 resolve() 가 아니다. 테스트 설치는 정션
    # (BookOasis 의 plugins/metadata/gdrive_reading_sync → 이 저장소)이라
    # resolve() 하면 링크를 따라가 state.db 가 C:\projects\data\ 로 새어 나간다.
    # 정션이 없는 운영과 위치가 달라져 dev 에서 본 상태가 운영과 다르게 된다.
    plugin_root = Path(plugin_file).absolute().parent
    return Store(_db_path(plugin_root))


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        _ensure_parent(db_path)
        with _INIT_LOCK:
            self._init(db_path)
        # writer: 단일 스레드(워커)에서만 사용. 락으로 다른 스레드 진입 차단.
        self._writer = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10.0)
        self._writer.execute("PRAGMA journal_mode=WAL")
        self._writer.execute("PRAGMA synchronous=NORMAL")

    @staticmethod
    def _init(db_path: Path) -> None:
        conn = sqlite3.connect(str(db_path))
        try:
            for stmt in _SCHEMA:
                conn.execute(stmt)
            for stmt in _MIGRATIONS:
                try:
                    conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # 이미 있는 컬럼
            conn.execute(
                "INSERT OR IGNORE INTO sync_state (id, status) VALUES (1, 'ready')"
            )
            conn.commit()
        finally:
            conn.close()

    # ----- writer-only helpers -----
    def with_writer(self):
        return _WriterCtx(self._writer)

    def get_state(self) -> dict:
        cur = self._writer.execute("SELECT * FROM sync_state WHERE id = 1")
        row = cur.fetchone()
        if not row:
            return {"page_token": "", "status": "ready", "last_poll_at": None, "error": "", "daily_bytes": 0, "daily_date": None}
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def set_state(self, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with _WRITER_LOCK:
            self._writer.execute(f"UPDATE sync_state SET {cols} WHERE id = 1", list(fields.values()))
            self._writer.commit()

    def upsert_job(self, job: dict) -> int:
        now = job.get("created_at") or _now()
        event_key = job.get("event_key")
        with _WRITER_LOCK:
            cur = self._writer.execute("SELECT id FROM job WHERE event_key = ?", (event_key,))
            existing = cur.fetchone()
            if existing:
                # §9.1: 종결 상태(completed/failed/skipped)와 진행 상태(processing/retry)는
                # 같은 event_key 의 재검출로 되돌리지 않는다. dry_run → queued 만 허용.
                cur2 = self._writer.execute(
                    "SELECT status FROM job WHERE id = ?", (existing[0],)
                )
                row = cur2.fetchone()
                prev_status = (row[0] if row else "queued") or "queued"
                next_status = job.get("status", "queued")
                if prev_status == "dry_run":
                    next_status = "queued"
                elif prev_status in ("completed", "failed", "skipped", "processing", "retry"):
                    next_status = prev_status
                self._writer.execute(
                    """UPDATE job SET
                        action=?, item_type=?, file_id=?, remote_path=?, removed_path=?,
                        local_path=?, size=?, md5=?, modified_time=?, status=?, updated_at=?
                       WHERE id=?""",
                    (
                        job.get("action", ""),
                        job.get("item_type", "file"),
                        job.get("file_id", ""),
                        job.get("remote_path", ""),
                        job.get("removed_path", "") or "",
                        job.get("local_path", ""),
                        int(job.get("size") or 0),
                        job.get("md5", "") or "",
                        job.get("modified_time", "") or "",
                        next_status,
                        now,
                        existing[0],
                    ),
                )
                self._writer.commit()
                return int(existing[0])

            cur = self._writer.execute(
                """INSERT INTO job
                    (event_key, target, action, item_type, file_id, remote_path,
                     removed_path, local_path, size, md5, modified_time, status,
                     attempts, bytes_done, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '', ?, ?)""",
                (
                    event_key,
                    job.get("target", "general"),
                    job.get("action", ""),
                    job.get("item_type", "file"),
                    job.get("file_id", ""),
                    job.get("remote_path", ""),
                    job.get("removed_path", "") or "",
                    job.get("local_path", ""),
                    int(job.get("size") or 0),
                    job.get("md5", "") or "",
                    job.get("modified_time", "") or "",
                    job.get("status", "dry_run"),
                    now,
                    now,
                ),
            )
            self._writer.commit()
            return int(cur.lastrowid)

    def upsert_item(self, item: dict) -> None:
        now = _now()
        with _WRITER_LOCK:
            self._writer.execute(
                """INSERT INTO item
                    (file_id, parent_id, name, remote_path, is_directory,
                     size, md5, modified_time, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(file_id) DO UPDATE SET
                     parent_id=excluded.parent_id,
                     name=excluded.name,
                     remote_path=excluded.remote_path,
                     is_directory=excluded.is_directory,
                     size=excluded.size,
                     md5=excluded.md5,
                     modified_time=excluded.modified_time,
                     updated_at=excluded.updated_at""",
                (
                    item["file_id"],
                    item.get("parent_id") or "",
                    item.get("name") or "",
                    item.get("remote_path") or "",
                    1 if item.get("is_directory") else 0,
                    int(item.get("size") or 0),
                    item.get("md5") or "",
                    item.get("modified_time") or "",
                    now,
                ),
            )
            self._writer.commit()

    def get_item(self, file_id: str) -> dict | None:
        """경로 해석·변경 분류용 이전 상태. 없으면 None."""
        cur = self._writer.execute(
            "SELECT parent_id, name, remote_path, is_directory FROM item WHERE file_id = ?",
            (file_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "parent_id": row[0] or "",
            "name": row[1] or "",
            "remote_path": row[2] or "",
            "is_directory": bool(row[3]),
        }

    def delete_item(self, file_id: str) -> None:
        with _WRITER_LOCK:
            self._writer.execute("DELETE FROM item WHERE file_id = ?", (file_id,))
            self._writer.commit()

    def claim_jobs(self, limit: int, max_attempts: int) -> list[dict]:
        """queued/retry 중 attempts < max_attempts 만 processing 으로 바꾼 뒤 반환.

        같은 트랜잭션 안에서 상태 전이+attempts+1+started_at 기록. 한 번에 최대
        limit 건만 가져오고 폴더 rename 을 파일 job 보다 먼저 claim 한다 (§4.2).
        """
        with _WRITER_LOCK:
            # S3 — exhausted 전이. attempts >= max_attempts 인 queued/retry 행을
            # 즉시 failed/max_attempts_exceeded 로 옮긴 뒤 나머지만 claim 한다.
            # 이 전이가 없으면 retry/attempts=3 이 영구 retry 로 남는다.
            self._writer.execute(
                """UPDATE job
                      SET status='failed',
                          result='max_attempts_exceeded',
                          error=CASE WHEN error='' OR error IS NULL
                                     THEN 'max attempts exceeded' ELSE error END,
                          finished_at=?,
                          updated_at=?
                    WHERE status IN ('queued','retry')
                      AND attempts >= ?""",
                (_now(), _now(), int(max_attempts)),
            )
            # S2 — 폴더 rename 우선. 같은 묶음 안에서 폴더 rename 이 파일보다 앞이며,
            # 각 그룹 안에서는 id 오름차순 (안정성 + crash 복구 가정과 일치).
            cur = self._writer.execute(
                """SELECT id FROM job
                    WHERE status IN ('queued','retry') AND attempts < ?
                    ORDER BY
                      CASE WHEN item_type='directory' AND action='rename'
                           THEN 0 ELSE 1 END ASC,
                      id ASC
                    LIMIT ?""",
                (int(max_attempts), int(limit)),
            )
            ids = [int(r[0]) for r in cur.fetchall()]
            if not ids:
                self._writer.commit()
                return []
            now = _now()
            placeholders = ",".join("?" for _ in ids)
            self._writer.execute(
                f"""UPDATE job
                       SET status='processing',
                           attempts=attempts+1,
                           started_at=COALESCE(started_at, ?),
                           updated_at=?,
                           bytes_done=0,
                           result='',
                           error=''
                     WHERE id IN ({placeholders})""",
                [now, now, *ids],
            )
            self._writer.commit()
            cur = self._writer.execute(
                f"""SELECT id, event_key, target, action, item_type, file_id,
                          remote_path, removed_path, local_path, size, md5,
                          modified_time, status, attempts
                     FROM job WHERE id IN ({placeholders})
                     ORDER BY CASE WHEN item_type='directory' AND action='rename'
                                   THEN 0 ELSE 1 END ASC, id ASC""",
                ids,
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def finish_job(
        self,
        job_id: int,
        status: str,
        *,
        result: str = "",
        bytes_done: int = 0,
        error: str = "",
        reset_recovery: bool = True,
    ) -> None:
        """종결/재시도 전이. 짧게 lock 만 잡는다.

        §5.1 — 실제 rclone 결과가 기록되는 시점에 recovery_count 를 0 으로 둔다.
        단 recover_on_start 의 finish_job (재시작 복구 마무리) 은 reset_recovery=False 로
        호출해 recovery_count 를 보전한다 — 그래야 6번째 연속 복구에서
        recovery_limit_exceeded 로 종료된다.
        """
        with _WRITER_LOCK:
            now = _now()
            self._writer.execute(
                """UPDATE job
                      SET status=?, result=?, bytes_done=?, error=?,
                          recovery_count=CASE WHEN ? THEN 0 ELSE recovery_count END,
                          finished_at=?, updated_at=?
                    WHERE id=?""",
                (
                    status,
                    result or "",
                    int(bytes_done or 0),
                    error or "",
                    1 if reset_recovery else 0,
                    now,
                    now,
                    int(job_id),
                ),
            )
            self._writer.commit()

    def promote_dry_run_to_queued(self) -> int:
        """리뷰 r2 F2 — 1라운드 dry_run 행을 queued 로 승격. 정확히 한 번만 실행.

        sync_state.dry_run_promoted 가 0일 때만 UPDATE 후 1로 set. 그 다음 호출은
        no-op. 승격 자체는 §9.1 의 contract — 없애지 않는다.
        """
        with _WRITER_LOCK:
            row = self._writer.execute(
                "SELECT dry_run_promoted FROM sync_state WHERE id = 1"
            ).fetchone()
            flag = int(row[0]) if row and row[0] is not None else 0
            if flag:
                return 0
            cur = self._writer.execute(
                "UPDATE job SET status='queued', updated_at=COALESCE(updated_at, created_at) "
                "WHERE status='dry_run'"
            )
            promoted = int(cur.rowcount or 0)
            self._writer.execute(
                "UPDATE sync_state SET dry_run_promoted=1, last_poll_at=? WHERE id = 1",
                (_now(),),
            )
            self._writer.commit()
            return promoted

    def recover_processing(
        self, max_attempts: int, max_consecutive_recoveries: int = 5
    ) -> list[dict]:
        """인터럽트된 processing 행을 retry/failed 로 되돌리고 id 를 반환.

        §5.1 — attempts 보전 정책:
        - `recovery_count + 1 <= max_consecutive_recoveries` → status=retry, attempts
          를 1 감소 (시작은 시도 실패가 아님), `recovery_count` 단조 증가.
        - 6번째 (`>5`) 연속 복구 → failed, result=recovery_limit_exceeded, attempts
          는 감소하지 않음. rclone 진짜 실패 3회는 `claim_jobs` 의 exhausted 전이가
          별도 failed 로 만든다 (영구 retry 가 되지 않게).
        """
        recovered: list[dict] = []
        with _WRITER_LOCK:
            cur = self._writer.execute(
                "SELECT id, attempts, recovery_count FROM job WHERE status='processing'"
            )
            rows = cur.fetchall()
            cap = int(max_consecutive_recoveries)
            for job_id, attempts, recovery_count in rows:
                next_recovery = int(recovery_count or 0) + 1
                if next_recovery <= cap:
                    new_attempts = max(int(attempts or 0) - 1, 0)
                    self._writer.execute(
                        """UPDATE job
                              SET status='retry',
                                  attempts=?,
                                  recovery_count=?,
                                  result='recovered',
                                  error=CASE WHEN error='' OR error IS NULL
                                             THEN 'recovered after interrupted processing' ELSE error END,
                                  updated_at=?
                            WHERE id=?""",
                        (new_attempts, next_recovery, _now(), int(job_id)),
                    )
                    recovered.append({"id": int(job_id), "status": "retry", "result": "recovered"})
                else:
                    # 상한 초과 — 명시적 failed, attempts 보전.
                    self._writer.execute(
                        """UPDATE job
                              SET status='failed',
                                  result='recovery_limit_exceeded',
                                  error=CASE WHEN error='' OR error IS NULL
                                             THEN 'consecutive recovery limit exceeded' ELSE error END,
                                  finished_at=?,
                                  updated_at=?
                            WHERE id=?""",
                        (_now(), _now(), int(job_id)),
                    )
                    recovered.append({"id": int(job_id), "status": "failed", "result": "recovery_limit_exceeded"})
            self._writer.commit()
        return recovered

    # ---- §4.3 / §4.4 — 폴더 rename 안전 상태기계 ----------------------

    def mark_job_op_state(self, job_id: int, op_state: str) -> None:
        """§4.3 — crash-safe rename 직전 기록. 짧은 단일 컬럼 UPDATE 만.

        잠금 안 잡는다 (호출 측이 _WRITER_LOCK 의 같은 트랜잭션 안에서 호출 가능).
        """
        self._writer.execute(
            "UPDATE job SET op_state=? WHERE id=?",
            (op_state, int(job_id)),
        )

    def mark_job_op_state_locked(self, job_id: int, op_state: str) -> None:
        """3라운드-B T2 — `_WRITER_LOCK` 안에서 op_state 기록 + commit.

        호출 측이 lock 을 직접 잡지 않아도 되도록 묶었다. sync_worker 가
        `store._writer.commit()` 을 직접 호출하지 않도록 (§P6 정적 검증).
        """
        with _WRITER_LOCK:
            self._writer.execute(
                "UPDATE job SET op_state=? WHERE id=?",
                (op_state, int(job_id)),
            )
            self._writer.commit()

    def get_job_op_state(self, job_id: int) -> str:
        """3라운드-B T2 — 폴더 rename 의 op_state 를 잠긴 Store 메서드로 노출.

        - sync_worker.py 가 `_writer.execute` 를 직접 호출하지 않도록 (§4.2 / §P6).
        - 짧은 SELECT 만 — _WRITER_LOCK 안에서 다른 write 와 직렬화한다.
        """
        with _WRITER_LOCK:
            cur = self._writer.execute(
                "SELECT op_state FROM job WHERE id=?",
                (int(job_id),),
            )
            row = cur.fetchone()
        if not row:
            return ""
        return (row[0] or "") if row[0] is not None else ""

    def finish_directory_rename(
        self,
        job_id: int,
        old_prefix: str,
        new_prefix: str,
        local_path_for,
    ) -> dict:
        """§4.4 — 폴더 rename 완료 처리.

        한 `_WRITER_LOCK` 트랜잭션 안에서:
        1) item.remote_path / updated_at 갱신 — `LIKE` 대신 `substr` 으로 경계 처리.
        2) pending job (`status IN ('queued','retry')`) 의 remote_path / removed_path /
           local_path 도 같이 갱신. `local_path` 는 문자열 치환이 아니라 callback 으로
           다시 계산 (Store 는 플랫폼 경로 규칙을 모름).
        3) 처리 job 자식은 `skipped/renamed_directory/bytes_done=0` 으로 종결.
        `processing` / 종결 job (completed/failed/skipped) 은 조회·UPDATE 대상이
        아니다 (불변).

        반환: `{"items_updated": int, "jobs_updated": int}`.
        """
        with _WRITER_LOCK:
            now = _now()
            # 1) item 갱신 — path 가 old 와 같거나, old/ 접두사를 가진 행만
            self._writer.execute(
                """UPDATE item
                      SET remote_path = CASE
                            WHEN remote_path = :old THEN :new
                            ELSE :new || substr(remote_path, length(:old) + 1)
                          END,
                          updated_at = :now
                    WHERE remote_path = :old
                       OR substr(remote_path, 1, length(:old) + 1) = :old || '/'""",
                {"old": old_prefix, "new": new_prefix, "now": now},
            )
            items_updated = int(self._writer.execute(
                "SELECT changes() AS n"
            ).fetchone()[0])

            # 2) pending job 의 path 게산
            cur = self._writer.execute(
                """SELECT id, remote_path, removed_path
                     FROM job
                    WHERE status IN ('queued','retry')
                      AND (remote_path = :old
                           OR substr(remote_path, 1, length(:old) + 1) = :old || '/'
                           OR removed_path = :old
                           OR substr(removed_path, 1, length(:old) + 1) = :old || '/')""",
                {"old": old_prefix},
            )
            pending_rows = cur.fetchall()
            new_prefix_prefix = new_prefix + "/"

            for rid, remote_path, removed_path in pending_rows:
                new_remote = remote_path
                if remote_path == old_prefix:
                    new_remote = new_prefix
                elif remote_path[: len(old_prefix) + 1] == old_prefix + "/":
                    new_remote = new_prefix_prefix + remote_path[len(old_prefix) + 1:]

                new_removed = removed_path or ""
                if removed_path:
                    if removed_path == old_prefix:
                        new_removed = new_prefix
                    elif removed_path[: len(old_prefix) + 1] == old_prefix + "/":
                        new_removed = new_prefix_prefix + removed_path[len(old_prefix) + 1:]

                # local_path 는 callback 으로 다시 계산 (Store 는 경로 규칙 모름)
                new_local = local_path_for(new_remote)
                self._writer.execute(
                    """UPDATE job
                          SET remote_path=?, removed_path=?, local_path=?, updated_at=?
                        WHERE id=? AND status IN ('queued','retry')""",
                    (new_remote, new_removed, new_local, now, int(rid)),
                )

            jobs_updated = len(pending_rows)

            # 3) 처리 job 자체를 종결
            self._writer.execute(
                """UPDATE job
                      SET status='skipped',
                          result='renamed_directory',
                          bytes_done=0,
                          error='',
                          op_state='',
                          finished_at=?,
                          updated_at=?
                    WHERE id=?""",
                (now, now, int(job_id)),
            )
            self._writer.commit()
        return {"items_updated": items_updated, "jobs_updated": jobs_updated}

    # ---- §5 (S5) — 일괄 재시도 -------------------------------

    def retry_jobs(
        self,
        job_ids: list[int] | None = None,
        *,
        failed_all: bool = False,
    ) -> dict:
        """§5.1 — atomic terminal-only 재시도.

        두 모드 중 정확히 하나만 허용:
        - job_ids 모드: 요청 id 들을 한 _WRITER_LOCK 트랜잭션에서 검증 후 모두 queued 로.
          비종결 상태거나 미존재 id 가 하나라도 있으면 전체 거부 (행 변경 0).
        - failed_all 모드: 현재 status='failed' 인 행만 queued 로.
          다른 종결 상태는 그대로.

        성공 시 모든 행에 공통 UPDATE:
          status='queued', attempts=0, recovery_count=0, op_state='',
          bytes_done=0, error='', result='', started_at=NULL, finished_at=NULL,
          updated_at=now.

        반환: {requested, retried, job_ids, rejected_ids, missing_ids}.
        """
        with _WRITER_LOCK:
            now = _now()
            if failed_all:
                if job_ids is not None:
                    return {
                        "requested": 0, "retried": 0, "job_ids": [],
                        "rejected_ids": [], "missing_ids": [],
                    }
                cur = self._writer.execute(
                    "SELECT id FROM job WHERE status='failed'"
                )
                ids = [int(r[0]) for r in cur.fetchall()]
                requested = len(ids)
                missing: list[int] = []
                rejected: list[int] = []
            else:
                if not job_ids:
                    return {
                        "requested": 0, "retried": 0, "job_ids": [],
                        "rejected_ids": [], "missing_ids": [],
                    }
                ids_request = sorted({int(x) for x in job_ids if x is not None})
                # 한 트랜잭션 안에서 검증: existence + 상태 확인
                placeholders = ",".join("?" for _ in ids_request)
                cur = self._writer.execute(
                    f"SELECT id, status FROM job WHERE id IN ({placeholders})",
                    ids_request,
                )
                found = {int(r[0]): r[1] for r in cur.fetchall()}
                missing = [i for i in ids_request if i not in found]
                # queued/retry/processing/dry_run 은 재시도 불가
                TERMINAL = {"completed", "failed", "skipped"}
                rejected = [
                    i for i in ids_request
                    if i in found and found[i] not in TERMINAL
                ]
                ids = [i for i in ids_request if i in found and found[i] in TERMINAL]
                requested = len(ids_request)
            if not ids:
                self._writer.commit()
                return {
                    "requested": requested, "retried": 0, "job_ids": [],
                    "rejected_ids": rejected, "missing_ids": missing,
                }
            placeholders = ",".join("?" for _ in ids)
            self._writer.execute(
                f"""UPDATE job
                       SET status='queued',
                           attempts=0,
                           recovery_count=0,
                           op_state='',
                           bytes_done=0,
                           error='',
                           result='',
                           started_at=NULL,
                           finished_at=NULL,
                           updated_at=?
                     WHERE id IN ({placeholders})""",
                [now, *ids],
            )
            self._writer.commit()
        return {
            "requested": requested, "retried": len(ids), "job_ids": ids,
            "rejected_ids": rejected, "missing_ids": missing,
        }

    # ---- §6 (S6) — 보존기간·자동 정리 ----------------------------

    def cleanup_terminal(
        self,
        retention_days: int = 30,
        *,
        delete_all: bool = False,
    ) -> int:
        """§6 / §8.2 — TERMINAL_STATUSES 만 정리. item/sync_state 절대 안 지움.

        delete_all=True 면 retention 무시, status IN (completed,failed,skipped) 전체 삭제.
        delete_all=False 면 updated_at < cutoff 인 것만 삭제 (cutoff = now UTC - retention_days).

        반환: 삭제된 job 수.
        """
        # retention_days 정규화
        try:
            rd = max(1, min(int(retention_days), 3650))
        except (TypeError, ValueError):
            rd = 30
        # cutoff: ISO 형식 — UTC 기준 n일 전
        import datetime as _dt
        utc_now = _dt.datetime.utcnow()
        cutoff_dt = utc_now - _dt.timedelta(days=rd)
        cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")

        with _WRITER_LOCK:
            if delete_all:
                cur = self._writer.execute(
                    "DELETE FROM job WHERE status IN ('completed','failed','skipped')"
                )
            else:
                cur = self._writer.execute(
                    """DELETE FROM job
                        WHERE status IN ('completed','failed','skipped')
                          AND updated_at IS NOT NULL
                          AND updated_at < ?""",
                    (cutoff_iso,),
                )
            deleted = int(cur.rowcount or 0)
            self._writer.commit()
        return deleted

    # ---- P9 — 복사 완료 후 책 폴더 단위 자동 스캔 ---------------------

    def scan_touch(
        self, folder: str, session: str, library_id: int | None
    ) -> None:
        """§4 / §5.2 — upsert. 기존 행이 `running` 이면 건드리지 않고 조용히 무시
        (스캔 중 도착분은 다음 landing 이 다시 `scan_touch` 로 잡는다).
        그 외에는 `status='pending'`, `queued_at=now`, `files=files+1`,
        `reason=''`, `error=''` 로 재대기시킨다.
        """
        now = _now()
        with _WRITER_LOCK:
            cur = self._writer.execute(
                "SELECT status FROM scan WHERE folder = ?", (folder,)
            )
            row = cur.fetchone()
            if row is not None and row[0] == "running":
                # scan 중 도착 — 이번 대기분은 건드리지 않는 게 안전하다.
                self._writer.commit()
                return
            if row is None:
                self._writer.execute(
                    """INSERT INTO scan
                        (folder, session, library_id, status, queued_at, files)
                       VALUES (?, ?, ?, 'pending', ?, 1)""",
                    (folder, session, library_id, now),
                )
            else:
                self._writer.execute(
                    """UPDATE scan
                          SET status='pending', queued_at=?, files=files+1,
                              reason='', error='', started_at=NULL, finished_at=NULL,
                              new_books=NULL
                        WHERE folder=?""",
                    (now, folder),
                )
            self._writer.commit()

    def scan_skip(self, folder: str, session: str, reason: str) -> None:
        """§5.2 — `status='skipped'`, `reason=<사유>`, `finished_at=now` upsert.
        skipped 는 되집지 않는다 (`scan_claim_due` 는 pending 만 본다).
        """
        now = _now()
        with _WRITER_LOCK:
            cur = self._writer.execute(
                "SELECT status FROM scan WHERE folder = ?", (folder,)
            )
            row = cur.fetchone()
            if row is None:
                self._writer.execute(
                    """INSERT INTO scan
                        (folder, session, status, reason, finished_at)
                       VALUES (?, ?, 'skipped', ?, ?)""",
                    (folder, session, reason, now),
                )
            else:
                # running 중 skip 은 일어나지 않는다 (§5.2 — folder matching 이
                # pending 생성 전에 끝난다). 그래도 무조건 덮어쓴다 (스캔 단위 결정).
                self._writer.execute(
                    """UPDATE scan
                          SET status='skipped', reason=?, finished_at=?,
                              session=?, started_at=NULL, files=0, new_books=NULL,
                              error=''
                        WHERE folder=?""",
                    (reason, now, session, folder),
                )
            self._writer.commit()

    def scan_claim_due(self, before_iso: str) -> dict | None:
        """§4.1 — 프로세스 간 안전하게 pending 된 폴더 1건을 `running` 으로
        되집는다. 조건부 UPDATE + rowcount 로 정확히 1명만 통과시킨다.

        `_WRITER_LOCK` 은 스레드 락이라 gunicorn 워커 N 개면 못 막는다. sqlite
        쓰기는 파일 락으로 직렬화되므로 `WHERE status='pending'` 재확인 + rowcount
        검사가 프로세스 간에도 정확히 1명만 통과시킨다.

        대상 `folder` 는 같은 트랜잭션 안에서 서브쿼리로 읽어 UPDATE 의 WHERE 에
        못박아 두 프로세스가 같은 폴더를 노려도 하나만 성공하게 한다.
        """
        now = _now()
        with _WRITER_LOCK:
            # 1) 후보 folder 를 같은 트랜잭션 안에서 읽는다.
            cur = self._writer.execute(
                """SELECT folder FROM scan
                    WHERE status='pending' AND queued_at <= ?
                    ORDER BY queued_at ASC, folder ASC
                    LIMIT 1""",
                (before_iso,),
            )
            target = cur.fetchone()
            if target is None:
                self._writer.commit()
                return None
            folder = target[0]
            # 2) 조건부 UPDATE — folder+status 재확인 후 running 전이.
            cur = self._writer.execute(
                """UPDATE scan
                      SET status='running', started_at=?
                    WHERE folder=? AND status='pending'""",
                (now, folder),
            )
            if cur.rowcount != 1:
                self._writer.commit()
                return None
            # 3) 되집은 행 SELECT.
            cur = self._writer.execute(
                "SELECT * FROM scan WHERE folder = ?", (folder,)
            )
            cols = [d[0] for d in cur.description]
            row = cur.fetchone()
            self._writer.commit()
            if row is None:
                return None
            return dict(zip(cols, row))

    def scan_finish(
        self,
        folder: str,
        status: str,
        new_books: int | None = None,
        error: str = "",
    ) -> None:
        """§5.3 — `status`(done/failed), `finished_at=now`, `new_books`, `error`
        기록. `files=0` 으로 리셋. skip 보존정리(§12-D)가 놓치지 않게 대상 행을
        정확히 기록한다.
        """
        now = _now()
        with _WRITER_LOCK:
            self._writer.execute(
                """UPDATE scan
                      SET status=?, finished_at=?, files=0,
                          new_books=COALESCE(?, new_books), error=?
                    WHERE folder=?""",
                (status, now, new_books, error, folder),
            )
            self._writer.commit()

    def scan_map(self, folders: list[str]) -> dict:
        """§5.5 — `list[str]` -> `{folder: row_dict}`. 화면용 조회 전용.
        빈 리스트면 `{}`. `IN (?,?,...)` 는 500 개 단위로 끊어 읽는다.
        """
        if not folders:
            return {}
        out: dict[str, dict] = {}
        with self._reader() as conn:
            step = 500
            for i in range(0, len(folders), step):
                chunk = folders[i:i + step]
                placeholders = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"SELECT * FROM scan WHERE folder IN ({placeholders})",
                    chunk,
                )
                for row in cur.fetchall():
                    out[str(row["folder"])] = dict(row)
        return out

    def scan_set_new_books(self, library_id: int, n: int) -> None:
        """§12-A — 본체 `on_scan_new_books_detected` 훅이 나중에 도착했을 때 쓴다.

        그 `library_id` 의 **`running` 또는 가장 최근 `done`** 행이 **정확히 1개**
        일 때만 `new_books=n` 을 기록한다. 애매하면 아무 것도 안 한다 (본체 cron
        전체 스캔과 겹쳐 오염되는 것을 줄인다).
        """
        with _WRITER_LOCK:
            # running 우선 — 스캔 도중 또는 막 끝난 우리 스캔이 대상.
            cur = self._writer.execute(
                "SELECT folder FROM scan "
                "WHERE library_id=? AND status='running'",
                (library_id,),
            )
            running = [r[0] for r in cur.fetchall()]
            if len(running) == 1:
                self._writer.execute(
                    "UPDATE scan SET new_books=? WHERE folder=?",
                    (int(n), running[0]),
                )
                self._writer.commit()
                return
            if running:
                # running 이 2개 이상인데 도착하면 귀속 애매 — 아무 것도 안 한다.
                self._writer.commit()
                return
            # running 없으면 가장 최근 done 1건.
            cur = self._writer.execute(
                """SELECT folder FROM scan
                    WHERE library_id=? AND status='done'
                    ORDER BY finished_at DESC, folder ASC
                    LIMIT 2""",
                (library_id,),
            )
            done = [r[0] for r in cur.fetchall()]
            if len(done) == 1:
                self._writer.execute(
                    "UPDATE scan SET new_books=? WHERE folder=?",
                    (int(n), done[0]),
                )
            self._writer.commit()

    def scan_recover_running(self) -> int:
        """§12-E — 프로세스 재시작 시 `status='running'` 고아 행을 `pending` 으로
        되돌리고 건수를 반환한다. 부분 스캔은 mtime 스킵 덕에 다시 돌려도 싸다.
        """
        with _WRITER_LOCK:
            cur = self._writer.execute(
                """UPDATE scan
                      SET status='pending', started_at=NULL, finished_at=NULL
                    WHERE status='running'"""
            )
            n = int(cur.rowcount or 0)
            self._writer.commit()
            return n

    def scan_cleanup(self, retention_days: int) -> int:
        """§12-D — `status IN ('done','failed','skipped')` 이고 `finished_at` 이
        보존기간 지난 행만 삭제. **`pending` / `running` 은 절대 지우지 않는다.**

        완료 시각은 UTC ISO (`_now()`). retention_days 는 `cleanup_terminal` 과
        같은 정규화를 호출 측에서 해서 넘긴다.
        """
        import datetime as _dt
        rd = max(1, min(int(retention_days), 3650))
        utc_now = _dt.datetime.utcnow()
        cutoff_iso = (utc_now - _dt.timedelta(days=rd)).strftime("%Y-%m-%dT%H:%M:%S")
        with _WRITER_LOCK:
            cur = self._writer.execute(
                """DELETE FROM scan
                    WHERE status IN ('done','failed','skipped')
                      AND finished_at IS NOT NULL
                      AND finished_at < ?""",
                (cutoff_iso,),
            )
            n = int(cur.rowcount or 0)
            self._writer.commit()
            return n

    # ----- reader helpers (별도 연결) -----
    def _reader(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def read_status(self) -> dict:
        state = self.get_state()
        counts = {}
        try:
            with self._reader() as conn:
                cur = conn.execute("SELECT status, COUNT(*) AS n FROM job GROUP BY status")
                counts = {row["status"]: int(row["n"]) for row in cur.fetchall()}
        except Exception:
            counts = {}
        return {
            "success": True,
            "page_token": state.get("page_token") or "",
            "status": state.get("status") or "ready",
            "last_poll_at": state.get("last_poll_at"),
            "error": state.get("error") or "",
            "job_counts": counts,
        }

    def read_jobs(self, limit: int = 100) -> dict:
        """§6.1 / §6.3 — 옛 `?limit=` 호환 wrapper.

        의미: limit 은 "첫 limit 행". 즉 page=1, page_size=limit 으로 단일 호출.
        새 list_page 의 total 은 진짜 COUNT 다 — 옛 total=len(rows) 와 다름.
        """
        try:
            page_size = max(1, min(int(limit), 500))
        except (TypeError, ValueError):
            page_size = 50
        out = self.list_page(page=1, page_size=page_size)
        # 옛 호출자는 jobs 키와 total=len(rows) 기대. 새 list_page 의 total 과
        # pages 는 명세대로 진짜 COUNT 다.
        return {
            "success": True,
            "jobs": out["items"],
            "total": out["total"],
            "pages": out["pages"],
            "page": out["page"],
            "page_size": out["page_size"],
            "status_counts": out["status_counts"],
            "result_counts": out["result_counts"],
            "action_counts": out["action_counts"],
            "summary": out["summary"],
            "worker_status": out["worker_status"],
        }

    def list_page(
        self,
        page: int = 1,
        page_size: int = 50,
        status: str = "",
        action: str = "",
        result: str = "",
        search: str = "",
        order: str = "desc",
    ) -> dict:
        """§6.1 — 서버측 페이징 + 필터 + total 정정.

        정규화: `page=max(1, int(page))`, `page_size = max(10, min(int(page_size), 500))`.
        `order = asc` 면 id ASC, 그 외 id DESC.
        search 는 앞뒤 공백 제거 후 remote_path / local_path / removed_path / error 의
        부분 검색 (LIKE escape: %/_/\\ 모두 ESCAPE '\\' 와 함께 이스케이프).
        total = 필터 적용 후 COUNT(*). pages = (total+page_size-1)//page_size.
        counts 및 summary 는 페이지/검색 흔들림 없는 전체 기준 (전체 job 의 status_counts,
        result_counts, summary).
        worker_status 는 같은 reader 연결에서 sync_state SELECT (writer 락 무관).
        """
        try:
            page = max(1, int(page))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = max(10, min(int(page_size), 500))
        except (TypeError, ValueError):
            page_size = 50
        order_asc = str(order or "").lower() == "asc"
        status_n = (status or "").strip()
        action_n = (action or "").strip()
        result_n = (result or "").strip()
        search_n = (search or "").strip()

        where_parts: list[str] = ["1=1"]
        params: list = []
        if status_n:
            where_parts.append("status = ?")
            params.append(status_n)
        if action_n:
            where_parts.append("action = ?")
            params.append(action_n)
        if result_n:
            where_parts.append("result = ?")
            params.append(result_n)
        if search_n:
            esc = search_n.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pat = f"%{esc}%"
            where_parts.append(
                "(remote_path LIKE ? ESCAPE '\\' OR local_path LIKE ? ESCAPE '\\' "
                "OR removed_path LIKE ? ESCAPE '\\' OR error LIKE ? ESCAPE '\\')"
            )
            params.extend([pat, pat, pat, pat])
        where_sql = " AND ".join(where_parts)
        order_sql = "ORDER BY id ASC" if order_asc else "ORDER BY id DESC"

        try:
            with self._reader() as conn:
                cur_total = conn.execute(
                    f"SELECT COUNT(*) AS n FROM job WHERE {where_sql}", params
                )
                total = int(cur_total.fetchone()[0])
                offset = (page - 1) * page_size
                cur = conn.execute(
                    f"""SELECT id, event_key, action, item_type, file_id, remote_path,
                              removed_path, local_path, size, md5, modified_time,
                              status, attempts, bytes_done, error, result,
                              op_state, recovery_count,
                              created_at, updated_at, started_at, finished_at
                          FROM job WHERE {where_sql}
                          {order_sql}
                          LIMIT ? OFFSET ?""",
                    [*params, page_size, offset],
                )
                rows = [dict(r) for r in cur.fetchall()]
        except Exception:
            rows = []
            total = 0

        pages = (total + page_size - 1) // page_size if total else 0

        # status_counts / result_counts / summary / worker_status — 페이지·검색과 무관
        status_counts: dict = {}
        result_counts: dict = {}
        action_counts: dict = {}
        summary: dict = {}
        worker_status: dict = {"status": "ready", "last_poll_at": "", "error": ""}
        try:
            with self._reader() as conn:
                cur_sc = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM job GROUP BY status"
                )
                status_counts = {r["status"]: int(r["n"]) for r in cur_sc.fetchall()}
                cur_rc = conn.execute(
                    "SELECT result, COUNT(*) AS n FROM job "
                    "WHERE result <> '' GROUP BY result"
                )
                result_counts = {r["result"]: int(r["n"]) for r in cur_rc.fetchall()}
                cur_ac = conn.execute(
                    "SELECT action, COUNT(*) AS n FROM job GROUP BY action"
                )
                action_counts = {r["action"]: int(r["n"]) for r in cur_ac.fetchall()}
                # summary — 전체 기준 all / completed / bytes_done
                cur_sm = conn.execute(
                    "SELECT COUNT(*) AS n, "
                    "COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END), 0) AS completed, "
                    "COALESCE(SUM(bytes_done), 0) AS bytes_done FROM job"
                )
                row_sm = cur_sm.fetchone()
                summary = {
                    "all": int(row_sm[0] or 0),
                    "completed": int(row_sm[1] or 0),
                    "bytes_done": int(row_sm[2] or 0),
                }
                # worker_status — sync_state SELECT
                cur_ws = conn.execute(
                    "SELECT status, last_poll_at, error FROM sync_state WHERE id = 1"
                )
                ws_row = cur_ws.fetchone()
                if ws_row is not None:
                    worker_status = {
                        "status": ws_row[0] or "ready",
                        "last_poll_at": ws_row[1] or "",
                        "error": ws_row[2] or "",
                    }
        except Exception:
            pass

        return {
            "success": True,
            "items": rows,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "status_counts": status_counts,
            "action_counts": action_counts,
            "result_counts": result_counts,
            "summary": summary,
            "worker_status": worker_status,
        }
        rows = []
        try:
            with self._reader() as conn:
                cur = conn.execute(
                    """SELECT id, event_key, action, item_type, file_id, remote_path,
                              removed_path, local_path, size, md5, modified_time,
                              status, attempts, bytes_done, error, result,
                              op_state, recovery_count,
                              created_at, updated_at, started_at, finished_at
                       FROM job ORDER BY id DESC LIMIT ?""",
                    (max(1, min(int(limit), 500)),),
                )
                rows = [dict(r) for r in cur.fetchall()]
        except Exception:
            rows = []
        counts: dict = {}
        try:
            with self._reader() as conn:
                cur = conn.execute("SELECT action, COUNT(*) AS n FROM job GROUP BY action")
                counts = {row["action"]: int(row["n"]) for row in cur.fetchall()}
        except Exception:
            counts = {}
        return {"success": True, "jobs": rows, "total": len(rows), "action_counts": counts}


class _WriterCtx:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def __enter__(self):
        _WRITER_LOCK.acquire()
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            _WRITER_LOCK.release()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())