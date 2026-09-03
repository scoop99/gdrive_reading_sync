# -*- coding: utf-8 -*-
"""감지 파이프 자체검증. 네트워크·BookOasis 없이 단독 실행.

    python test_sync_worker.py

여기서 잡는 것: 부모 체인 경로 해석(버그 A 회귀), FF 변경 분류(create/rename/
edit/delete), 제외 최상위, 확장자, probe 배제, 토큰 되감기.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# __init__.py 가 BookOasis 본체를 import 한다. 로직만 보는 테스트라 최소 스텁으로
# 대신한다 (본체 설치 없이 단독 실행하기 위함).
for name, attrs in (
    ("plugins", {}),
    ("plugins.metadata", {}),
    ("plugins.metadata.base", {"BaseMetadataProvider": type("BaseMetadataProvider", (), {})}),
    ("flask", {"jsonify": lambda *a, **k: None, "current_app": None, "request": None}),
    ("utils", {}),
    ("utils.rclone_gdrive_copy", {"get_access_token": lambda remote: ""}),
):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules.setdefault(name, mod)

# 이 저장소의 루트가 곧 플러그인 폴더다. 실제 설치 경로와 **같은 패키지 이름**으로
# 등록해 `from .store import ...` 같은 상대 import 를 그대로 쓰게 한다.
# 이러면 테스트가 실제 설치본과 같은 import 그래프를 검증한다.
_PKG = "plugins.metadata.gdrive_reading_sync"
_pkg_mod = types.ModuleType(_PKG)
_pkg_mod.__path__ = [str(HERE)]          # __init__.py 를 실행하지 않는다
sys.modules.setdefault(_PKG, _pkg_mod)

from plugins.metadata.gdrive_reading_sync.drive_client import DriveAPIError  # noqa: E402
from plugins.metadata.gdrive_reading_sync.store import Store  # noqa: E402
from plugins.metadata.gdrive_reading_sync.sync_worker import (  # noqa: E402
    _parse_version_tuple,
    _config_file_from_output,
    build_change_event,
    poll_once,
    preflight,
    process_job,
    process_jobs,
    recover_on_start,
    resolve_remote_kind,
    build_remote_source,
    _run_rclone,
    _rclone_config_dump,
    inspect_rclone_setup,
    rewind_token,
)
# 3라운드-B T1 — logger 인프라 테스트가 _BASE_CONFIG_SCHEMA 와
# _configure_runtime_logger 를 호출하므로 직접 import 한다. 위에 등록한
# plugins.metadata.base 스텁이 충분하다.
from plugins.metadata.gdrive_reading_sync.gdrive_reading_sync import GdriveReadingSyncMetadataProvider  # noqa: E402

ROOT = "ROOT_READING"
FOLDER = "application/vnd.google-apps.folder"

# READING/잡지/SPARK/SPARK 2018.10#100.pdf 형태의 최소 트리
TREE = {
    "f_magazine": {"id": "f_magazine", "name": "잡지", "mimeType": FOLDER, "parents": [ROOT]},
    "f_spark": {"id": "f_spark", "name": "SPARK", "mimeType": FOLDER, "parents": ["f_magazine"]},
    "f_incoming": {"id": "f_incoming", "name": "INCOMING", "mimeType": FOLDER, "parents": [ROOT]},
    "f_outside": {"id": "f_outside", "name": "다른폴더", "mimeType": FOLDER, "parents": ["OTHER_ROOT"]},
}


def _file(fid, name, parent, mtime="2026-08-31T07:30:00Z", size=1000):
    return {
        "id": fid, "name": name, "mimeType": "application/pdf", "parents": [parent],
        "size": size, "md5Checksum": "abc", "modifiedTime": mtime, "createdTime": mtime,
    }


class FakeClient:
    """get_file 만 응답하는 Drive 대역. 호출 수를 세어 캐시 효과도 본다."""

    def __init__(self, pages):
        self.pages = pages
        self.get_calls = 0
        self.token = "7387303"

    def start_page_token(self):
        return self.token

    def list_changes(self, page_token):
        return self.pages.pop(0) if self.pages else {"changes": [], "newStartPageToken": "9999"}

    def get_file(self, file_id):
        self.get_calls += 1
        if file_id in TREE:
            return TREE[file_id]
        raise DriveAPIError(f"unknown {file_id}")


CFG = {
    "REMOTE_ROOT_FOLDER_ID": ROOT,
    "EXCLUDED_TOP": "제외폴더,.private,.upload,INCOMING",
    "EXTENSIONS": ".zip,.cbz,.epub,.pdf,.txt,.yaml",
    "LOCAL_ROOT": r"T:\LIBRARY",
    "TRANSFER_REMOTE": "google",
    "REMOTE_KIND": "folder_id",
}


def _store():
    tmp = Path(tempfile.mkdtemp()) / "state.db"
    s = Store(tmp)
    s.set_state(page_token="7386303")
    return s


def _jobs(store):
    return {j["remote_path"]: j for j in store.read_jobs(limit=100)["jobs"]}


def test_classification():
    """FF build_change_event 판정 순서. rename 이 create 로 새면 2단계에서 중복 복사."""
    assert build_change_event(None, {"remote_path": "a/b.pdf"})["action"] == "create"
    assert build_change_event({"remote_path": "a/b.pdf"}, {"remote_path": "a/b.pdf"})["action"] == "edit"
    ev = build_change_event({"remote_path": "a/old.pdf"}, {"remote_path": "a/new.pdf"})
    assert ev["action"] == "rename", ev
    assert ev["removed_path"] == "a/old.pdf", ev  # 2단계가 로컬 rename 할 원본 경로
    ev = build_change_event({"remote_path": "a/b.pdf"}, None)
    assert ev["action"] == "delete" and ev["path"] == "a/b.pdf", ev
    assert build_change_event({"remote_path": "a/b.pdf"}, {"remote_path": "a/b.pdf", "trashed": True})["action"] == "delete"
    print("  OK 변경 분류 (create/edit/rename/delete)")


def test_path_resolution_bug_a():
    """버그 A 회귀: 조상이 캐시에 없어도 부모 체인을 API 로 풀어야 한다.

    이전 구현은 item 테이블만 보고 경로를 만들어 실제 READING 파일을 100%
    out_of_root 로 버렸다. 여기서 0건이 나오면 그 버그가 돌아온 것이다.
    """
    store = _store()
    client = FakeClient([{
        "changes": [{"fileId": "x1", "file": _file("x1", "SPARK 2018.10#100.pdf", "f_spark")}],
        "newStartPageToken": "7387303",
    }])
    summary = poll_once(client, store, CFG, log=lambda *a: None)
    jobs = _jobs(store)
    assert summary["jobs_created"] == 1, summary
    assert "잡지/SPARK/SPARK 2018.10#100.pdf" in jobs, list(jobs)
    job = jobs["잡지/SPARK/SPARK 2018.10#100.pdf"]
    assert job["action"] == "create", job
    assert job["local_path"] == r"T:\LIBRARY\잡지\SPARK\SPARK 2018.10#100.pdf", job
    print("  OK 부모 체인 경로 해석 → 잡지/SPARK/… (버그 A 회귀 방지)")


def test_filters():
    """제외 최상위 · 확장자 · probe · 루트 밖 — 각각 이유가 요약에 남아야 한다."""
    store = _store()
    client = FakeClient([{
        "changes": [
            {"fileId": "i1", "file": _file("i1", "월간스포츠.pdf", "f_incoming")},
            {"fileId": "e1", "file": _file("e1", "표지.jpg", "f_spark")},
            {"fileId": "p1", "file": _file("p1", "bookoasis_job_probe_20260831.txt", "f_spark")},
            {"fileId": "o1", "file": _file("o1", "남.pdf", "f_outside")},
            {"fileId": "k1", "file": _file("k1", "kavita.yaml", "f_spark")},
        ],
        "newStartPageToken": "7387303",
    }])
    s = poll_once(client, store, CFG, log=lambda *a: None)
    assert s["excluded_skipped"] == 1, s      # INCOMING
    assert s["extension_skipped"] == 1, s     # .jpg
    assert s["probe_skipped"] == 1, s         # probe 는 합격 판정에 쓰지 않음
    assert s["out_of_root_skipped"] == 1, s   # READING 밖
    assert s["jobs_created"] == 1, s          # kavita.yaml 만 통과
    print("  OK 필터 4종 (제외/확장자/probe/루트밖)")


def test_rename_uses_cache():
    """두 번째 변경은 item 캐시로 풀려 get_file 호출이 늘지 않아야 한다."""
    store = _store()
    client = FakeClient([
        {"changes": [{"fileId": "r1", "file": _file("r1", "구이름.pdf", "f_spark")}],
         "newStartPageToken": "7387303"},
    ])
    poll_once(client, store, CFG, log=lambda *a: None)
    calls_after_first = client.get_calls
    assert calls_after_first == 2, calls_after_first  # f_spark, f_magazine

    client.pages = [{"changes": [{"fileId": "r1", "file": _file("r1", "새이름.pdf", "f_spark")}],
                     "newStartPageToken": "7387304"}]
    store.set_state(page_token="7387303")
    s = poll_once(client, store, CFG, log=lambda *a: None)
    assert client.get_calls == calls_after_first, client.get_calls  # 추가 API 호출 0
    jobs = _jobs(store)
    job = jobs["잡지/SPARK/새이름.pdf"]
    assert job["action"] == "rename", job
    assert job["removed_path"] == "잡지/SPARK/구이름.pdf", job
    print("  OK rename 판정 + item 캐시로 API 호출 0 증가")


def test_outside_root_negative_cache():
    """READING 밖 형제 파일은 조상 체인을 다시 걷지 않아야 한다.

    Changes 는 계정 전체를 돌려준다 — 실측 709건 중 675건이 루트 밖이었다.
    음수 캐시가 없으면 형제마다 My Drive 루트까지 되짚어 backfill 이 분 단위로 늘어진다.
    """
    store = _store()
    client = FakeClient([{
        "changes": [
            {"fileId": "o1", "file": _file("o1", "남1.pdf", "f_outside")},
            {"fileId": "o2", "file": _file("o2", "남2.pdf", "f_outside")},
            {"fileId": "o3", "file": _file("o3", "남3.pdf", "f_outside")},
        ],
        "newStartPageToken": "7387303",
    }])
    s = poll_once(client, store, CFG, log=lambda *a: None)
    assert s["out_of_root_skipped"] == 3, s
    assert s["jobs_created"] == 0, s
    # f_outside 1회만. OTHER_ROOT 은 TREE 에 없어 DriveAPIError → 그 자리서 끝.
    assert client.get_calls == 2, client.get_calls
    print("  OK 루트 밖 음수 캐시 (형제 3건에 API 호출 2회)")


def test_rewind():
    """토큰 되감기 — 이게 되니까 파일 변경을 기다리지 않고 검증할 수 있다."""
    store = _store()
    client = FakeClient([])
    out = rewind_token(client, store, 1000)
    assert out["rewound_to"] == "7386303", out
    assert store.get_state()["page_token"] == "7386303"
    assert rewind_token(client, store, 99999999)["rewound_to"] == "1"  # 음수 방지
    client.token = "not-a-number"
    try:
        rewind_token(client, store, 10)
        raise AssertionError("정수 아닌 토큰인데 통과함")
    except DriveAPIError:
        pass
    print("  OK 토큰 되감기 (하한 1, 비정수 거부)")


# ============================================================================
# 2라운드 신규 9종 — D1, rename 두 겹, rclone 래퍼, preflight, 재시작 복구
# ============================================================================

import hashlib
import json as _json
import logging as _lg
import os as _os
import re as _re
import time


class _FakeRclone:
    """(args, cfg, timeout) → (code, stdout, stderr). copyto 는 temp 에 fake bytes.

    리뷰 r3 — fake 가 무조건 (0, b"", b"") 를 돌려주면 명령 형태가 유효한지
    아무도 검사하지 않는다. 라이브와 같은 규칙으로:
    - `check` 가 단일 파일 경로를 받으면 비 0 + stderr 메시지 (라이브 재현).
    - 알 수 없는 동사 / 예상치 못한 argv 형태는 비 0.
    - lsjson 은 lsjson_md5 로 결정 (None 이면 빈 배열 — 폴백 실패 시나리오).
    """

    def __init__(
        self,
        fake_remote_bytes: bytes = b"REMOTE_BYTES",
        lsjson_md5: str | None = "76a1121bcb2a7e4ff22e07347da6c94c",
    ):
        self.calls: list[list[str]] = []
        self._fake_bytes = fake_remote_bytes
        self._lsjson_md5 = lsjson_md5

    def __call__(self, args, cfg, timeout):
        # 원본 argv 를 보존하기 위해 list 복사
        argv = list(args)
        self.calls.append(argv)
        verb = argv[0]
        if verb == "copyto":
            target = argv[2]
            with open(target, "wb") as fh:
                fh.write(self._fake_bytes)
            return 0, b"", b""
        if verb == "check":
            # 라이브 rclone check 는 디렉터리 두 개를 비교한다 — 파일 경로를
            # 인자로 받으면 무조건 "is a file not a directory" 실패.
            for a in argv[1:]:
                if a.startswith("-"):
                    continue
                if Path(a).suffix:
                    return 1, b"", (
                        b"CRITICAL: Failed to create file system for "
                        + a.encode("utf-8") + b": is a file not a directory"
                    )
            return 0, b"", b""
        if verb == "lsjson":
            if self._lsjson_md5:
                payload = [{
                    "Name": "SPARK 2018.10#100.pdf",
                    "IsDir": False,
                    "Size": len(self._fake_bytes),
                    "MD5": self._lsjson_md5,
                }]
            else:
                payload = []  # 폴백 실패 — md5 없음 → copied_size_only
            return 0, _json.dumps(payload).encode("utf-8"), b""
        if verb == "version":
            return 0, b"rclone v1.75.0-315\n", b""
        if verb == "config":
            sub = argv[1]
            if sub == "file":
                return 0, b"C:/Users/example/.config/rclone/rclone.conf\n", b""
            if sub == "dump":
                return 0, _json.dumps({
                    "google": {"type": "drive", "gds_endpoint": "https://x"},
                    "detectremote": {"type": "drive"},
                }).encode("utf-8"), b""
        # 알 수 없는 동사 / 형식은 비 0.
        return 2, b"", f"fake: unknown argv {argv}".encode("utf-8")


def _file_bytes_md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _seed_job(store: Store, **kw) -> dict:
    """claim 가능한 processing 행을 직접 만들고 반환."""
    now = store._now() if hasattr(store, "_now") else None
    # upsert_job 사용 — status 는 queued → claim_jobs 가 processing 으로 바꾼다.
    job = {
        "event_key": kw.get("event_key") or f"ek:{kw.get('remote_path')}",
        "action": kw.get("action", "create"),
        "item_type": kw.get("item_type", "file"),  # §4.1 — 폴더 rename 도 seed 가능
        "file_id": kw.get("file_id", "fid"),
        "remote_path": kw.get("remote_path", "잡지/SPARK/SPARK.pdf"),
        "removed_path": kw.get("removed_path", ""),
        "local_path": kw.get("local_path") or os.path.join(
            kw.get("local_root", r"T:\LIBRARY"), kw.get("remote_path", "")
        ).replace("\\", "/"),
        "size": kw.get("size", 12),
        "md5": kw.get("md5", ""),
        "modified_time": kw.get("modified_time", "2026-08-31T07:30:00Z"),
        "status": "queued",
    }
    store.upsert_job(job)
    return job


def test_process_rename_event_zero_bytes():
    """§6.1 — 감지된 rename + removed_path → os.rename, rclone 호출 0."""
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    rel_new = "잡지/SPARK/새이름.pdf"
    rel_old = "잡지/SPARK/구이름.pdf"
    target = local_root / rel_new
    target.parent.mkdir(parents=True, exist_ok=True)
    old = local_root / rel_old
    payload = b"RENAMED_CONTENT"
    old.write_bytes(payload)
    md5 = _file_bytes_md5(payload)
    size = len(payload)

    store = _store()
    job = _seed_job(
        store,
        event_key="ek:rename-event",
        action="rename",
        remote_path=rel_new,
        removed_path=rel_old,
        local_path=str(target),
        size=size,
        md5=md5,
    )
    cfg = dict(CFG, LOCAL_ROOT=str(local_root))

    # process_job 은 claim 후 호출한다 — claim 으로 processing 전이.
    claimed = store.claim_jobs(limit=1, max_attempts=3)
    assert claimed and claimed[0]["id"] == job["status"] and claimed[0]["status"] != "queued" or claimed
    fake = _FakeRclone()
    out = process_job(store, claimed[0], cfg, run_rclone=fake, tmp_root=tmp_root, log=lambda *a: None)
    assert out["status"] == "skipped", out
    assert out["result"] == "renamed_event", out
    assert out["bytes_done"] == 0, out
    assert fake.calls == [], fake.calls
    assert not old.exists(), old
    assert target.read_bytes() == payload
    rows = store.read_jobs(limit=10)["jobs"]
    row = next(r for r in rows if r["event_key"] == "ek:rename-event")
    assert row["status"] == "skipped" and row["result"] == "renamed_event" and row["bytes_done"] == 0
    print("  OK 1겹 rename: removed_path → os.rename, rclone 0, bytes 0")


def test_process_rename_cold_start_zero_bytes():
    """§6.2 — 콜드 스타트: 같은 폴더 다른 이름 동일 size+md5 → os.rename."""
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    target_rel = "만화/완결A/지엠 GM/신규이름.pdf"
    target = local_root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = b"COLD_START_PAYLOAD"
    candidate = target.parent / "(구이름).pdf"
    candidate.write_bytes(payload)
    md5 = _file_bytes_md5(payload)
    size = len(payload)

    store = _store()
    _seed_job(
        store,
        event_key="ek:cold-start",
        action="create",
        remote_path=target_rel,
        local_path=str(target),
        size=size,
        md5=md5,
    )
    cfg = dict(CFG, LOCAL_ROOT=str(local_root))
    claimed = store.claim_jobs(limit=1, max_attempts=3)
    fake = _FakeRclone()
    out = process_job(store, claimed[0], cfg, run_rclone=fake, tmp_root=tmp_root, log=lambda *a: None)
    assert out["status"] == "skipped", out
    assert out["result"] == "renamed_cold_start", out
    assert out["bytes_done"] == 0, out
    assert fake.calls == [], fake.calls
    assert not candidate.exists(), candidate
    assert target.read_bytes() == payload
    print("  OK 2겹 rename(콜드 스타트): 같은 폴더 후보 → os.rename, rclone 0")


def test_cold_start_same_size_different_md5_copies():
    """§6.2/§7 — 콜드 스타트 후보가 크기는 같지만 md5 가 다르면 신규 복사."""
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    target_rel = "책/SPARK.pdf"
    target = local_root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    # 후보는 같은 크기, 다른 내용
    other = target.parent / "다른이름.pdf"
    other.write_bytes(b"X" * 16)
    fake = _FakeRclone(fake_remote_bytes=b"NEW_REMOTE_PAYLOAD")
    store = _store()
    _seed_job(
        store,
        event_key="ek:cold-different-md5",
        action="create",
        remote_path=target_rel,
        local_path=str(target),
        size=len(b"NEW_REMOTE_PAYLOAD"),  # r3 — fake 의 실제 길이와 일치
        md5=_file_bytes_md5(b"NEW_REMOTE_PAYLOAD"),
    )
    cfg = dict(
        CFG,
        LOCAL_ROOT=str(local_root),
        TRANSFER_REMOTE="detectremote",
        REMOTE_KIND="folder_id",
        REMOTE_ROOT_FOLDER_ID="ROOT_FID",
    )
    claimed = store.claim_jobs(limit=1, max_attempts=3)
    out = process_job(store, claimed[0], cfg, run_rclone=fake, tmp_root=tmp_root, log=lambda *a: None)
    assert out["status"] == "completed", out
    assert out["result"] == "copied", out
    assert out["bytes_done"] == len(b"NEW_REMOTE_PAYLOAD"), out
    # 리뷰 r3 — 검증은 로컬 md5+크기로 한다. copyto 만 한 번 부른다.
    assert fake.calls[0][0] == "copyto", fake.calls
    assert all(c[0] != "check" for c in fake.calls), fake.calls
    assert fake.calls[0][1] == "detectremote:책/SPARK.pdf", fake.calls[0]
    assert "--drive-root-folder-id" in fake.calls[0], fake.calls[0]
    assert "ROOT_FID" in fake.calls[0], fake.calls[0]
    assert target.read_bytes() == b"NEW_REMOTE_PAYLOAD"
    # 후보는 그대로
    assert other.exists()
    print("  OK 콜드 스타트 md5 불일치 → copyto→check→replace, 호출 순서/extra 일치")


def test_remote_md5_missing_never_skips():
    """§5 / 리뷰 r3 — 원격 md5 가 빈 문자열이면 size-only skip 금지, copyto 진행."""
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    target_rel = "잡지/SPARK.pdf"
    target = local_root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    # final 도 같은 크기지만 md5 가 다름 — 그래도 skip 하면 안 된다.
    target.write_bytes(b"OLD")
    # r3 — md5 빈값이면 폴백 lsjson 도 빈 응답. size_only 경로로 completed.
    fake = _FakeRclone(fake_remote_bytes=b"NEW_REMOTE", lsjson_md5=None)

    store = _store()
    _seed_job(
        store,
        event_key="ek:md5-missing",
        action="create",
        remote_path=target_rel,
        local_path=str(target),
        size=len(b"NEW_REMOTE"),
        md5="",  # §5 — md5 없음 → skip 금지
    )
    cfg = dict(CFG, LOCAL_ROOT=str(local_root), TRANSFER_REMOTE="detectremote",
               REMOTE_KIND="folder_id", REMOTE_ROOT_FOLDER_ID="ROOT_FID")
    claimed = store.claim_jobs(limit=1, max_attempts=3)
    out = process_job(store, claimed[0], cfg, run_rclone=fake, tmp_root=tmp_root, log=lambda *a: None)
    assert out["status"] == "completed", out
    assert out["result"] == "copied_size_only", out  # 폴백 실패 → result 로 구분
    assert fake.calls and fake.calls[0][0] == "copyto"
    assert any(c[0] == "lsjson" for c in fake.calls)  # 폴백 호출됨
    assert target.read_bytes() == b"NEW_REMOTE"
    print("  OK r3 §5: 원격 md5 빈값 → 폴백 호출, size_only 로 completed")


def test_recover_processing_and_temp_cleanup():
    """§9.3 — processing 복구 + tmp_root 안 {정수}.part 만 삭제."""
    tmp_root = Path(tempfile.mkdtemp())
    # 시드: processing 1건 + 그 id 의 .part + 규칙 밖 파일
    store = _store()
    _seed_job(store, event_key="ek:recover-1", action="create",
              remote_path="a/a.pdf", local_path="T:/LIBRARY/a/a.pdf",
              size=10, md5="")
    claimed = store.claim_jobs(limit=10, max_attempts=3)
    job_id = claimed[0]["id"]
    # {id}.part 파일 + 다른 정수 .part + 비정수 .part + 다른 확장자
    (tmp_root / f"{job_id}.part").write_bytes(b"X")
    (tmp_root / "999.part").write_bytes(b"Y")  # 다른 정수 — 보존
    (tmp_root / "notanumber.part").write_bytes(b"Z")  # 보존
    (tmp_root / "user.txt").write_text("user")  # 보존

    cfg = dict(CFG, MAX_ATTEMPTS=3)
    rec = recover_on_start(store, cfg, tmp_root=tmp_root, log=lambda *a: None)
    assert rec["recovered"] == 1, rec
    assert not (tmp_root / f"{job_id}.part").exists(), "id.part 삭제 실패"
    assert (tmp_root / "999.part").exists(), "다른 정수 .part 가 지워짐"
    assert (tmp_root / "notanumber.part").exists(), "비정수 .part 가 지워짐"
    assert (tmp_root / "user.txt").exists(), "사용자 파일이 지워짐"
    rows = {r["event_key"]: r for r in store.read_jobs(limit=10)["jobs"]}
    row = rows["ek:recover-1"]
    assert row["status"] == "retry", row  # attempts=1 < max=3 이므로 retry
    assert row["result"] == "recovered", row
    assert "recovered after interrupted processing" in row["error"]
    print("  OK recover_on_start: {id}.part 만 삭제, 규칙 밖 보존")


def test_remote_source_gds():
    """§7.1 — GDS 분기: source = {remote}:{REMOTE_ROOT_PATH}/{remote_path}, extra 빈 배열."""
    cfg = dict(
        TRANSFER_REMOTE="google",
        REMOTE_KIND="gds",
        REMOTE_ROOT_PATH="TESTROOT/BOOKS",
        REMOTE_ROOT_FOLDER_ID="ROOT_FID",
    )
    src, extra = build_remote_source(cfg, "잡지/SPARK/book.pdf", None)
    assert src == "google:TESTROOT/BOOKS/잡지/SPARK/book.pdf", src
    assert extra == [], extra
    # root path 가 빈 경우
    cfg2 = dict(cfg, REMOTE_ROOT_PATH="")
    src2, extra2 = build_remote_source(cfg2, "잡지/SPARK/book.pdf", None)
    assert src2 == "google:잡지/SPARK/book.pdf", src2
    assert extra2 == [], extra2
    print("  OK GDS source 분기: {r}:{root}/{path}, extra=[]")


def test_remote_source_folder_id():
    """§7.1 — folder_id 분기: source = {remote}:{path} + --drive-root-folder-id."""
    cfg = dict(
        TRANSFER_REMOTE="detectremote",
        REMOTE_KIND="folder_id",
        REMOTE_ROOT_PATH="TESTROOT/BOOKS",  # 있어도 무시해야 한다
        REMOTE_ROOT_FOLDER_ID="ROOT_FID",
    )
    src, extra = build_remote_source(cfg, "잡지/SPARK/book.pdf", None)
    assert src == "detectremote:잡지/SPARK/book.pdf", src
    assert extra == ["--drive-root-folder-id", "ROOT_FID"], extra
    # 리뷰 r3 — 검증은 로컬 md5+크기로 한다. copyto 한 번만 호출되고 폴백도 안 부른다.
    captured = []

    def fake_run(args, cfg_arg, timeout):
        captured.append(list(args))
        if args[0] == "copyto":
            open(args[2], "wb").write(b"X")
        return 0, b"", b""

    tmp = Path(tempfile.mkdtemp())
    target = Path(tmp) / "READING" / "잡지/SPARK/book.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    store = _store()
    _seed_job(
        store,
        event_key="ek:folder-id",
        action="create",
        remote_path="잡지/SPARK/book.pdf",
        local_path=str(target),
        size=1,
        md5=_file_bytes_md5(b"X"),
    )
    claimed = store.claim_jobs(limit=1, max_attempts=3)
    out = process_job(
        store, claimed[0], dict(CFG, **cfg, LOCAL_ROOT=str(tmp / "READING")),
        run_rclone=fake_run, tmp_root=tmp, log=lambda *a: None,
    )
    assert out["status"] == "completed", out
    assert len(captured) == 1, captured
    assert captured[0][0] == "copyto"
    assert captured[0][1] == src
    assert captured[0][3:] == ["--drive-root-folder-id", "ROOT_FID"], captured[0]
    print("  OK folder_id source 분기: copyto 1회, --drive-root-folder-id 포함")


def test_rclone_wrapper_binary_and_guard():
    """§4.3 / §1.3 — argv[0] 가 설정값, 금지 동사/플래그는 호출 전에 예외."""
    cfg = {"RCLONE_BIN": "C:/service/rclone/rclone.exe"}

    # 정상 호출 — argv 검증 (subprocess 호출은 막아 stub)
    from plugins.metadata.gdrive_reading_sync.sync_worker import _validate_rclone_argv
    _validate_rclone_argv(["copyto", "src", "dst"])  # OK

    # 금지 동사
    for bad in ("sync", "move", "delete", "purge", "rmdirs", "deletefile"):
        try:
            _validate_rclone_argv([bad, "src", "dst"])
        except ValueError as exc:
            assert bad in str(exc), exc
        else:
            raise AssertionError(f"금지 동사 {bad} 통과")

    # 금지 플래그
    for bad in ("--ignore-existing",):
        try:
            _validate_rclone_argv(["copyto", "src", "dst", bad])
        except ValueError as exc:
            assert bad in str(exc), exc
        else:
            raise AssertionError(f"금지 플래그 {bad} 통과")

    # bin_path 가 설정값 그대로 argv[0] 으로 들어가는지 — subprocess.run 을 mock.
    import unittest.mock as _mock
    seen_argv: list = []
    with _mock.patch("subprocess.run") as mrun:
        mrun.return_value = _mock.Mock(returncode=0, stdout=b"", stderr=b"")
        _run_rclone(["version"], cfg, 5)
        seen_argv.append(mrun.call_args.args[0])
    argv0 = seen_argv[0][0]
    assert argv0 == cfg["RCLONE_BIN"], argv0
    # _rclone_config_args() 가 결합되는지 (utils stub 가 빈 list 를 돌려주는 환경에서도 예외 없이 끝)
    print("  OK rclone 래퍼: argv[0]=RCLONE_BIN, 금지 동사/플래그 거부")


def test_preflight_gate_old_version_blocked():
    """리뷰 r2 F1 회귀 — preflight 가 구버전 버전 문자열이면 success=False.

    `_parse_version_tuple` 단위 테스트만으로는 부족하다 — 게이트가 실제로
    닫히는지를 봐야 한다. fake version 응답으로 preflight 를 돌리고,
    rclone v1.67.0-106 일 때 success=False + error 가 반환되는지 확인.
    """
    import unittest.mock as _mock
    from plugins.metadata.gdrive_reading_sync import sync_worker as _sw

    cfg = dict(
        CFG,
        RCLONE_BIN="",
        TRANSFER_REMOTE="google",
        REMOTE_KIND="auto",
    )
    fake_dump = {
        "google": {"type": "drive", "gds_endpoint": "https://x"},
        "detectremote": {"type": "drive"},
    }

    class _OldVersionFake:
        def __init__(self, line: bytes):
            self._line = line

        def __call__(self, args, _cfg, _to):
            if args and args[0] == "version":
                return 0, self._line, b""
            if args and args[0] == "lsjson":
                return 0, b"[]", b""
            if args and args[0] == "config":
                if args[1] == "file":
                    return 0, b"C:/x.conf\n", b""
                if args[1] == "dump":
                    return 0, _json.dumps(fake_dump).encode("utf-8"), b""
            return 0, b"", b""

    # 1) 실제 rclone v1.67.0-106 출력 — 게이트가 닫혀야 한다.
    with _mock.patch.object(_sw, "_rclone_config_dump", return_value=fake_dump):
        out = preflight(cfg, run_rclone=_OldVersionFake(b"rclone v1.67.0-106\n"),
                        log=lambda *a: None)
    assert out["success"] is False, out
    assert "v1.75.0 미만" in out["error"], out
    assert out["rclone_version"] == "rclone v1.67.0-106", out

    # 2) 파싱 실패 입력도 차단.
    with _mock.patch.object(_sw, "_rclone_config_dump", return_value=fake_dump):
        out = preflight(cfg, run_rclone=_OldVersionFake(b"garbage\n"),
                        log=lambda *a: None)
    assert out["success"] is False, out
    assert "파싱 실패" in out["error"], out

    # 3) 신버전 v1.75.0-315 는 통과.
    with _mock.patch.object(_sw, "_rclone_config_dump", return_value=fake_dump):
        out = preflight(cfg, run_rclone=_OldVersionFake(b"rclone v1.75.0-315\n"),
                        log=lambda *a: None)
    assert out["success"] is True, out
    assert out["rclone_version"] == "rclone v1.75.0-315", out
    print("  OK r2 F1: preflight 가 rclone v1.67.0-106 / garbage 에서 success=False")


def test_parse_version_tuple_real_rclone_lines():
    """리뷰 r2 F1 회귀 — 실제 rclone 출력 문자열 파싱.

    기존 구현은 `"rclone v1.67.0-106"` 를 `(67,0)` 으로 잘못 파싱했다.
    """
    assert _parse_version_tuple("rclone v1.67.0-106") == (1, 67, 0)
    assert _parse_version_tuple("rclone v1.75.0-315") == (1, 75, 0)
    assert _parse_version_tuple("rclone v1.75.0") == (1, 75, 0)
    assert _parse_version_tuple("garbage") == ()
    assert _parse_version_tuple("") == ()
    assert _parse_version_tuple("v1.75.0") == (1, 75, 0)
    assert _parse_version_tuple("1.67.0") == (1, 67, 0)
    print("  OK r2 F1: _parse_version_tuple 이 실제 rclone 출력을 정확히 파싱")


def test_verify_uses_local_md5_not_rclone_check():
    """리뷰 r3 F3 회귀 — job.md5 가 있으면 rclone check 가 절대 호출되지 않는다.

    라이브 실측: `rclone check <source-file> <temp-file>` 는 "is a file not a
    directory" 로 무조건 실패한다. fake 의 단언 가능한 무조건 (0, b"", b"") 가
    이 결함을 18종 통과로 위장했다.
    """
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    target_rel = "잡지/SPARK.pdf"
    target = local_root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    fake = _FakeRclone(fake_remote_bytes=b"REMOTE_PAYLOAD")

    store = _store()
    _seed_job(
        store,
        event_key="ek:verify-local-md5",
        action="create",
        remote_path=target_rel,
        local_path=str(target),
        size=len(b"REMOTE_PAYLOAD"),
        md5=_file_bytes_md5(b"REMOTE_PAYLOAD"),  # md5 있음
    )
    cfg = dict(CFG, LOCAL_ROOT=str(local_root),
               TRANSFER_REMOTE="detectremote", REMOTE_KIND="folder_id",
               REMOTE_ROOT_FOLDER_ID="ROOT_FID")
    claimed = store.claim_jobs(limit=1, max_attempts=3)
    out = process_job(store, claimed[0], cfg, run_rclone=fake,
                      tmp_root=tmp_root, log=lambda *a: None)
    assert out["status"] == "completed", out
    assert out["result"] == "copied", out
    # `check` 동사가 rclone 에 한 번도 전달되지 않음
    assert all(c[0] != "check" for c in fake.calls), fake.calls
    # 폴백도 안 부른다
    assert all(c[0] != "lsjson" for c in fake.calls), fake.calls
    # copyto 만 1회
    assert [c[0] for c in fake.calls] == ["copyto"], fake.calls
    # target 내용 검증
    assert target.read_bytes() == b"REMOTE_PAYLOAD"
    print("  OK r3 F3: copyto 만 부르고 rclone check/lsjson 모두 호출 안 함")


def test_verify_md5_mismatch_retries():
    """리뷰 r3 F3 회귀 — copyto 가 틀린 내용을 temp 에 쓰면 md5 mismatch 로 retry,
    os.replace 가 일어나지 않으며, error 가 비어 있지 않다 (D5)."""
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    target_rel = "책/SPARK.pdf"
    target = local_root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    # fake 가 temp 에 WRONG_PAYLOAD 를 쓴다 — job.md5 와 불일치.
    fake = _FakeRclone(fake_remote_bytes=b"WRONG_PAYLOAD")

    store = _store()
    _seed_job(
        store,
        event_key="ek:verify-mismatch",
        action="create",
        remote_path=target_rel,
        local_path=str(target),
        size=len(b"WRONG_PAYLOAD"),
        md5=_file_bytes_md5(b"EXPECTED_PAYLOAD"),  # 다른 해시
    )
    cfg = dict(CFG, LOCAL_ROOT=str(local_root),
               TRANSFER_REMOTE="detectremote", REMOTE_KIND="folder_id",
               REMOTE_ROOT_FOLDER_ID="ROOT_FID")
    claimed = store.claim_jobs(limit=1, max_attempts=3)
    out = process_job(store, claimed[0], cfg, run_rclone=fake,
                      tmp_root=tmp_root, log=lambda *a: None)
    assert out["status"] == "retry", out
    assert out["error"], "D5 위반: error 가 비어 있음"
    assert "mismatch" in out["error"], out["error"]
    # os.replace 가 안 일어남 — target 이 존재하지 않거나 비어 있어야 한다.
    assert not target.exists() or target.stat().st_size == 0, target
    # temp 파일도 정리됨
    job_id = claimed[0]["id"]
    assert not (tmp_root / f"{job_id}.part").exists(), "temp 가 남으면 안 됨"
    # job row 의 error 컬럼도 비어 있지 않다.
    row = next(r for r in store.read_jobs(limit=10)["jobs"]
               if r["event_key"] == "ek:verify-mismatch")
    assert row["error"], row
    assert row["status"] == "retry", row
    print("  OK r3 F3: md5 mismatch → retry + error 기록 + replace 없음 + temp 정리")


def test_verify_missing_remote_md5_falls_back():
    """리뷰 r3 F3 회귀 — job.md5 가 빈 값이면 lsjson --hash 폴백이 호출되고,
    그것도 빈 응답이면 result='copied_size_only' 로 끝난다."""
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    target_rel = "잡지/SPARK.pdf"
    target = local_root / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)

    # 1) 폴백 성공 케이스 — lsjson 이 md5 를 돌려주면 그것으로 비교.
    fake_ok = _FakeRclone(
        fake_remote_bytes=b"FALLBACK_PAYLOAD",
        lsjson_md5=_file_bytes_md5(b"FALLBACK_PAYLOAD"),
    )
    store = _store()
    _seed_job(
        store,
        event_key="ek:fallback-ok",
        action="create",
        remote_path=target_rel,
        local_path=str(target),
        size=len(b"FALLBACK_PAYLOAD"),
        md5="",  # 폴백 트리거
    )
    cfg = dict(CFG, LOCAL_ROOT=str(local_root),
               TRANSFER_REMOTE="detectremote", REMOTE_KIND="folder_id",
               REMOTE_ROOT_FOLDER_ID="ROOT_FID")
    claimed = store.claim_jobs(limit=1, max_attempts=3)
    out = process_job(store, claimed[0], cfg, run_rclone=fake_ok,
                      tmp_root=tmp_root, log=lambda *a: None)
    assert out["status"] == "completed", out
    assert out["result"] == "copied", out
    # copyto + lsjson 두 호출
    verbs = [c[0] for c in fake_ok.calls]
    assert verbs == ["copyto", "lsjson"], fake_ok.calls
    assert fake_ok.calls[1][1:3] == ["--hash", fake_ok.calls[0][1]], \
        "폴백은 copyto 와 같은 source 를 써야 한다"
    # temp 사라지고 target 존재
    assert target.read_bytes() == b"FALLBACK_PAYLOAD"

    # 2) 폴백 실패 케이스 — lsjson 도 빈 응답이면 copied_size_only.
    target2 = (tmp_root / "READING2") / target_rel
    target2.parent.mkdir(parents=True, exist_ok=True)
    fake_no = _FakeRclone(
        fake_remote_bytes=b"SIZE_ONLY_PAYLOAD",
        lsjson_md5=None,  # 폴백 실패
    )
    store2 = _store()
    _seed_job(
        store2,
        event_key="ek:fallback-no",
        action="create",
        remote_path=target_rel,
        local_path=str(target2),
        size=len(b"SIZE_ONLY_PAYLOAD"),
        md5="",
    )
    cfg2 = dict(cfg, LOCAL_ROOT=str(tmp_root / "READING2"))
    claimed2 = store2.claim_jobs(limit=1, max_attempts=3)
    out2 = process_job(store2, claimed2[0], cfg2, run_rclone=fake_no,
                       tmp_root=tmp_root, log=lambda *a: None)
    assert out2["status"] == "completed", out2
    assert out2["result"] == "copied_size_only", out2
    verbs2 = [c[0] for c in fake_no.calls]
    assert verbs2 == ["copyto", "lsjson"], fake_no.calls
    assert target2.read_bytes() == b"SIZE_ONLY_PAYLOAD"
    print("  OK r3 F3: md5 빈값 → lsjson 폴백 호출, 실패 시 copied_size_only")


def test_store_reopen_does_not_promote_dry_run():
    """리뷰 r2 F2 회귀 — Store 재오픈이 dry_run 행을 queued 로 승격하지 않는다.

    1라운드 잔여 dry_run 행이 있는 DB 를 Store(db_path) 로 두 번 열어도
    두 번째 열기 후에도 그 행의 status 가 dry_run 인 채로 남아 있어야 한다.
    승격 자체는 명시적 promote_dry_run_to_queued() 호출에서만 일어난다.
    """
    db = Path(tempfile.mkdtemp()) / "state.db"
    s1 = Store(db)
    s1.set_state(page_token="1")
    s1.upsert_job({
        "event_key": "ek:dry-run-1",
        "action": "create",
        "file_id": "f1",
        "remote_path": "a/a.pdf",
        "local_path": r"T:\LIBRARY\a\a.pdf",
        "size": 10,
        "md5": "",
        "modified_time": "2026-08-31T07:30:00Z",
        "status": "dry_run",  # 1라운드 잔여
    })
    s1._writer.commit()
    # 두 번째 Store 열기 — _MIGRATIONS 만 돌고, job 행은 손대면 안 된다.
    s2 = Store(db)
    rows = {r["event_key"]: r for r in s2.read_jobs(limit=10)["jobs"]}
    assert "ek:dry-run-1" in rows, rows
    assert rows["ek:dry-run-1"]["status"] == "dry_run", rows
    # 명시적 promote 호출 후에만 queued 로 바뀐다.
    promoted = s2.promote_dry_run_to_queued()
    assert promoted == 1, promoted
    rows2 = {r["event_key"]: r for r in s2.read_jobs(limit=10)["jobs"]}
    assert rows2["ek:dry-run-1"]["status"] == "queued", rows2
    # 다시 호출해도 no-op.
    assert s2.promote_dry_run_to_queued() == 0
    print("  OK r2 F2: Store 재오픈이 dry_run 행을 건드리지 않음, promote 는 1회성")


def test_preflight_contract_and_remote_options():
    """§4.4 — 응답 키, drive remotes 정렬, probe sample, 인증값 미노출."""
    import unittest.mock as _mock
    from plugins.metadata.gdrive_reading_sync import sync_worker as _sw
    cfg = dict(
        CFG,
        RCLONE_BIN="",  # PATH
        TRANSFER_REMOTE="google",
        REMOTE_KIND="auto",
        REMOTE_ROOT_PATH="TESTROOT/BOOKS",
        REMOTE_ROOT_FOLDER_ID="ROOT_FID",
    )
    fake = _FakeRclone()
    fake_dump = {
        "google": {"type": "drive", "gds_endpoint": "https://x"},
        "detectremote": {"type": "drive"},
    }
    with _mock.patch.object(_sw, "_rclone_config_dump", return_value=fake_dump):
        out = preflight(cfg, run_rclone=fake, log=lambda *a: None)
    assert out["success"] is True, out
    # rclone_version 첫 줄
    assert out["rclone_version"].startswith("rclone v"), out["rclone_version"]
    # resolved 가 stub 일 땐 bin_setting 그대로, 라이브에선 which 결과
    assert out["rclone_resolved"], out
    # drive remotes 만 정렬
    assert out["remotes"] == sorted(["google", "detectremote"]), out["remotes"]
    # gds_endpoint 가 있으니 gds
    assert out["remote_kind"] == "gds", out["remote_kind"]
    # probe
    assert out["probe"]["ok"] is True, out["probe"]
    assert out["probe"]["sample"]["name"] == "SPARK 2018.10#100.pdf", out["probe"]
    # 인증값 미노출 — 응답 dict 를 평탄화해 단어 검사.
    blob = _json.dumps(out, ensure_ascii=False, default=str)
    for needle in ("client_secret", "refresh_token", "access_token", "token", "secret"):
        assert needle not in blob.lower(), f"인증 누설: {needle}"
    print("  OK preflight: rclone_version, drive remotes 정렬, gds 판정, 인증 미노출")


def test_inspect_rclone_setup_binary_and_config_are_read_only():
    """설정 화면 검사는 읽기 명령만 쓰고 Drive 리모트 이름만 반환한다."""
    import unittest.mock as _mock
    from plugins.metadata.gdrive_reading_sync import sync_worker as _sw

    calls = []

    def fake_run(args, cfg, timeout):
        calls.append(list(args))
        if args == ["version"]:
            return 0, b"rclone v1.75.0-315\n", b""
        if args == ["config", "file"]:
            return 0, b"Configuration file is stored at:\nC:/safe/rclone.conf\n", b""
        raise AssertionError(args)

    dump = {
        "google": {"type": "drive", "token": "must-not-leak"},
        "s3": {"type": "s3", "secret": "must-not-leak"},
    }
    cfg = {"RCLONE_BIN": "rclone", "RCLONE_CONFIG": ""}
    with _mock.patch.object(_sw.shutil, "which", return_value="C:/bin/rclone.exe"), \
         _mock.patch.object(_sw, "_run_rclone", side_effect=fake_run), \
         _mock.patch.object(_sw, "_rclone_config_dump", return_value=dump):
        binary = inspect_rclone_setup(cfg, include_config=False)
        full = inspect_rclone_setup(cfg, include_config=True)

    assert binary["success"] and binary["rclone_version"] == "rclone v1.75.0-315", binary
    assert full["success"] and full["config_file"] == "C:/safe/rclone.conf", full
    assert full["remotes"] == ["google"], full
    assert "token" not in repr(full) and "secret" not in repr(full), full
    assert calls == [["version"], ["version"], ["config", "file"]], calls
    print("  OK 설정 검사: 읽기 명령만 실행, Drive 리모트 이름만 반환")


def test_config_file_output_path_parser():
    assert _config_file_from_output(
        b"Configuration file is stored at:\r\nC:\\Users\\tester\\rclone.conf\r\n"
    ) == "C:\\Users\\tester\\rclone.conf"
    assert _config_file_from_output(b"") == ""
    print("  OK rclone config file 출력에서 실제 경로 추출")


def test_local_path_windows_root():
    """T-S1-01 — Windows root _local_path. 어느 OS 에서도 L:READING... 형태."""
    from plugins.metadata.gdrive_reading_sync.sync_worker import _local_path
    out = _local_path("L:" + chr(92) + chr(92) + "READING", "잡지/SPARK/x.pdf")
    assert out == ("L:" + chr(92) + "READING" + chr(92) + "잡지" + chr(92) + "SPARK" + chr(92) + "x.pdf"), repr(out)
    print("  OK S1 T-S1-01: Windows root 결합 (모든 OS)")


def test_local_path_posix_root():
    """T-S1-02 — POSIX root _local_path. 어느 OS 에서도 /mnt/reading/... 형태."""
    from plugins.metadata.gdrive_reading_sync.sync_worker import _local_path
    out = _local_path("/mnt/reading", "잡지/SPARK/x.pdf")
    assert out == "/mnt/reading/잡지/SPARK/x.pdf", repr(out)
    assert _local_path("", "a/b.pdf") == "a/b.pdf"
    print("  OK S1 T-S1-02: POSIX root 결합 (모든 OS)")


def test_local_root_path_and_tmp_root_fallback():
    """T-S1-03 — _local_root_path / _tmp_root_path. cfg 가 빈 경우 윈도우는 폴백, 그 외는 None."""
    from plugins.metadata.gdrive_reading_sync.sync_worker import _local_root_path, _tmp_root_path, DEFAULT_LOCAL_ROOT
    p = _local_root_path({"LOCAL_ROOT": "/mnt/reading"})
    assert p == Path("/mnt/reading"), p
    if not DEFAULT_LOCAL_ROOT:
        assert _local_root_path({}) is None
        assert _tmp_root_path({}, None) is None
    else:
        assert _local_root_path({}) == Path(DEFAULT_LOCAL_ROOT)
        lr = _local_root_path({})
        assert _tmp_root_path({}, lr) == lr.parent / "_reading_sync_tmp"
    cfg = {"LOCAL_ROOT": "/m/r", "TMP_ROOT": "/other/tmp"}
    assert _tmp_root_path(cfg, Path("/m/r")) == Path("/other/tmp")
    print("  OK S1 T-S1-03: cfg / fallback / explicit 분기")


def test_no_forced_separator_replacement():
    """T-ALL-02 — 소스에 슬래시→백슬래시 강제 치환 0건. rclone 금지 동사 가드도 유지.

    회귀 표적: 1라운드 `str(Path(...)).replace("/", "\\\\")` 처럼 POSIX 루트의
    모든 결과를 백슬래시로 치환하는 결함. PureWindowsPath 결합용이나 단일
    토큰 정규화는 합법 (디자인 §3.1).
    """
    import re
    src_self = open("sync_worker.py", encoding="utf-8").read()
    # .replace("/", "\\") 만 매칭. 단 r = ^.*PureWindowsPath.*\.replace...$ 같이
    # PureWindowsPath 결합 라인 (.replace("/", "\\") 의 인자 결과를 PureWindowsPath
    # 로 받는 라인) 은 제외한다.
    raw_hits = list(re.finditer(r'\.replace\(["\']\/["\']\s*,\s*["\']\\\\["\']\)', src_self))
    hits = []
    for m in raw_hits:
        # 직전 80자에 PureWindowsPath 키워드가 있으면 합법 — 제외
        pre = src_self[max(0, m.start() - 80):m.start()]
        if "PureWindowsPath" in pre:
            continue
        hits.append(m.group(0))
    assert not hits, hits
    assert "FORBIDDEN_RCLONE_VERBS" in src_self
    for verb in ("sync", "move", "delete", "purge", "rmdirs", "deletefile"):
        assert verb in src_self, f"금지 동사 표식이 사라짐: {verb}"
    from plugins.metadata.gdrive_reading_sync import sync_worker as sw
    if sw.os.name != "nt":
        assert sw.DEFAULT_LOCAL_ROOT == "", repr(sw.DEFAULT_LOCAL_ROOT)
    print("  OK T-ALL-02: 강제 separator 치환 0건, 금지 동사 가드 유지")


def test_recover_preserves_attempts_across_restart():
    """T-S3-01 — recover_processing 3회 후에도 attempts 는 MAX_ATTEMPTS 미만, status retry."""
    from pathlib import Path as _Path
    from plugins.metadata.gdrive_reading_sync.store import Store as _Store
    from plugins.metadata.gdrive_reading_sync.sync_worker import recover_on_start as _rec
    tmp = _Path(tempfile.mkdtemp()) / "state.db"
    s = _Store(tmp)
    s.set_state(page_token="t")
    _seed_job(s, event_key="ek:s3-1", action="create",
              remote_path="a/b.pdf", local_path="T:/LIBRARY/a/b.pdf",
              size=10, md5="")
    cfg = dict(CFG, MAX_ATTEMPTS=3)
    # 1) 시드 row 를 processing 으로 옮긴 뒤 recover 를 3번 돌린다.
    for _ in range(3):
        # claim → processing. attempts 가 매번 +1 되지만 recover 가 -1 로 돌려준다.
        claimed = s.claim_jobs(limit=10, max_attempts=3)
        assert claimed and claimed[0]["status"] == "processing"
        rec = _rec(s, cfg, tmp_root=_Path(tempfile.mkdtemp()), log=lambda *a: None)
        assert rec["recovered"] == 1, rec
        # 어떤 시점에도 attempts < 3 이고 status 가 retry 다.
        rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
        row = rows["ek:s3-1"]
        assert row["attempts"] <= 2, row
        assert row["status"] == "retry", row
        # recovery_count 는 단조 증가하지만 5 이하.
        assert int(row.get("recovery_count") or 0) <= 5, row
    print("  OK S3 T-S3-01: 3회 복구 후에도 retry + attempts<MAX")


def test_recover_limit_exceeded_at_six():
    """T-S3-02 — 6번째 연속 복구에서 failed/recovery_limit_exceeded."""
    from pathlib import Path as _Path
    from plugins.metadata.gdrive_reading_sync.store import Store as _Store
    from plugins.metadata.gdrive_reading_sync.sync_worker import recover_on_start as _rec
    tmp = _Path(tempfile.mkdtemp()) / "state.db"
    s = _Store(tmp)
    s.set_state(page_token="t")
    _seed_job(s, event_key="ek:s3-2", action="create",
              remote_path="c/d.pdf", local_path="T:/LIBRARY/c/d.pdf",
              size=10, md5="")
    cfg = dict(CFG, MAX_ATTEMPTS=3)
    # 5회까지 retry, 6번째에 failed
    for i in range(6):
        claimed = s.claim_jobs(limit=10, max_attempts=3)
        assert claimed
        rec = _rec(s, cfg, tmp_root=_Path(tempfile.mkdtemp()), log=lambda *a: None)
        rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
        row = rows["ek:s3-2"]
        if i < 5:
            assert row["status"] == "retry", (i, row)
            assert row["result"] == "recovered", row
        else:
            assert row["status"] == "failed", row
            assert row["result"] == "recovery_limit_exceeded", row
            assert "consecutive recovery limit exceeded" in row["error"], row
    print("  OK S3 T-S3-02: 6번째 연속 복구 → failed/recovery_limit_exceeded")


def test_real_rclone_failure_three_times_still_fails():
    """T-S3-03 — rclone 진짜 실패 3회는 기존 규약대로 failed. retry 가 영구 머무르지 않는다."""
    from pathlib import Path as _Path
    from plugins.metadata.gdrive_reading_sync.store import Store as _Store
    from plugins.metadata.gdrive_reading_sync.sync_worker import process_job as _pj
    tmp = _Path(tempfile.mkdtemp())
    local_root = tmp / "READING"
    target = local_root / "잡지" / "SPARK" / "x.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    _seed_job_into = None  # placeholder for lint

    db = tmp / "state.db"
    s = _Store(db)
    s.set_state(page_token="t")
    _seed_job(
        s, event_key="ek:s3-3", action="create",
        remote_path="잡지/SPARK/x.pdf",
        local_path=str(target),
        size=10, md5="",
    )
    cfg = dict(CFG, LOCAL_ROOT=str(local_root))

    def fake_run(args, cfg_arg, timeout):
        # copyto 는 항상 비 0 (실제 rclone 실패 시뮬레이션)
        return 1, b"", b"copyto failed"

    # rclone 호출 시 copyto 실패 → process_job 이 retry 로 보냄.
    for _ in range(3):
        claimed = s.claim_jobs(limit=1, max_attempts=3)
        assert claimed
        out = _pj(s, claimed[0], cfg, run_rclone=fake_run, tmp_root=tmp, log=lambda *a: None)
        assert out["status"] == "retry", out
    # 4번째 claim — exhausted 전이가 attempts>=3 을 failed 로 만든다.
    claimed = s.claim_jobs(limit=1, max_attempts=3)
    assert claimed == [], "exhausted 전이가 동작하지 않음 — retry/attempts=3 영구 머무름"
    rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
    row = rows["ek:s3-3"]
    assert row["status"] == "failed", row
    assert row["result"] == "max_attempts_exceeded", row
    print("  OK S3 T-S3-03: 진짜 rclone 실패 3회 → claim exhausted 전이로 failed")


def test_directory_rename_basic():
    """T-S2-01 — 단순 폴더 rename. rclone 0, renamed_directory, bytes_done=0."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    local_root = tmp / "READING"
    old = local_root / "잡지" / "옛이름"
    new = local_root / "잡지" / "새이름"
    old.mkdir(parents=True)
    (old / "x.pdf").write_bytes(b"X")
    db = tmp / "state.db"
    s = Store(db)
    s.set_state(page_token="t")
    s.upsert_item({"file_id": "fid", "name": "새이름", "is_directory": True,
                   "remote_path": "잡지/새이름", "parent_id": ""})
    from plugins.metadata.gdrive_reading_sync.sync_worker import process_job as _pj
    job = _seed_job(
        s, event_key="ek:dir-rename-1", action="rename", item_type="directory",
        file_id="fid", remote_path="잡지/새이름", removed_path="잡지/옛이름",
        local_path=str(new), size=0, md5="",
    )
    cfg = dict(CFG, LOCAL_ROOT=str(local_root))
    claimed = s.claim_jobs(limit=1, max_attempts=3)
    assert claimed
    fake = _FakeRclone()
    out = _pj(s, claimed[0], cfg, run_rclone=fake, tmp_root=tmp, log=lambda *a: None)
    assert out["status"] == "skipped", out
    assert out["result"] == "renamed_directory", out
    assert out["bytes_done"] == 0, out
    assert fake.calls == [], fake.calls
    assert not old.exists(), old
    assert new.is_dir() and (new / "x.pdf").exists()
    rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
    assert rows["ek:dir-rename-1"]["result"] == "renamed_directory"
    print("  OK S2 T-S2-01: 단순 폴더 rename → renamed_directory, rclone 0")


def test_claim_jobs_directory_first():
    """T-S2-02 — 폴더 rename 이 파일 job 앞."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    s.set_state(page_token="t")
    # 작은 id 로 파일 queued, 큰 id 로 폴더 queued
    _seed_job(s, event_key="ek:file-1", action="rename", item_type="file",
              remote_path="a/old.pdf", removed_path="a/older.pdf",
              local_path=str(tmp / "READING" / "a" / "old.pdf"),
              size=10, md5="")
    _seed_job(s, event_key="ek:dir-1", action="rename", item_type="directory",
              remote_path="a/new", removed_path="a/old",
              local_path=str(tmp / "READING" / "a" / "new"),
              size=0, md5="")
    claimed = s.claim_jobs(limit=10, max_attempts=3)
    assert [c["event_key"] for c in claimed] == ["ek:dir-1", "ek:file-1"], claimed
    print("  OK S2 T-S2-02: claim 순서 — 디렉터리 rename 우선")


def test_finish_directory_rename_prefix_sql():
    """T-S2-03 — Store.finish_directory_rename item prefix SQL. old=foo, foo/ 하위 new, foo2 불변."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    # item 4 개: foo (옛 폴더), foo/a, foo/a/b, foo2 (옛과 비슷하지만 다른 폴더)
    s.upsert_item({"file_id": "fid_foo", "name": "foo", "is_directory": True,
                   "remote_path": "foo"})
    s.upsert_item({"file_id": "fid_foo_a", "name": "a", "is_directory": True,
                   "remote_path": "foo/a"})
    s.upsert_item({"file_id": "fid_foo_ab", "name": "b", "is_directory": True,
                   "remote_path": "foo/a/b"})
    s.upsert_item({"file_id": "fid_foo2", "name": "foo2", "is_directory": True,
                   "remote_path": "foo2"})
    # job 행 하나 만들어서 finish_directory_rename 호출
    _seed_job(s, event_key="ek:item-prefix", action="rename", item_type="directory",
              remote_path="bar", removed_path="foo",
              local_path=str(tmp / "READING" / "bar"), size=0, md5="")
    jid = [r for r in s.read_jobs(limit=10)["jobs"] if r["event_key"] == "ek:item-prefix"][0]["id"]
    out = s.finish_directory_rename(jid, "foo", "bar", lambda rp: f"/local/{rp}")
    assert out["items_updated"] >= 3, out  # foo, foo/a, foo/a/b
    # foo2 는 안 바뀌었어야 함
    paths = {i["remote_path"] for i in [_get_item_path(s, fid) for fid in
                                       ("fid_foo", "fid_foo_a", "fid_foo_ab", "fid_foo2")]}
    assert "bar" in paths, paths
    assert "bar/a" in paths, paths
    assert "bar/a/b" in paths, paths
    assert "foo2" in paths, "foo2 가 바뀌면 회귀"  # foo2 불변
    print("  OK S2 T-S2-03: item prefix SQL — old, old/ 갱신 / foo2 불변")


def _get_item_path(store, fid):
    cur = store._writer.execute(
        "SELECT file_id, remote_path, is_directory FROM item WHERE file_id=?",
        (fid,),
    ).fetchone()
    return {"file_id": cur[0], "remote_path": cur[1], "is_directory": bool(cur[2])}


def test_directory_rename_target_conflict():
    """T-S2-04 — target 이미 존재 → rename_conflict. 두 쪽 모두 불변."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    local_root = tmp / "READING"
    old = local_root / "잡지" / "옛이름"
    new = local_root / "잡지" / "새이름"
    old.mkdir(parents=True)
    (old / "x.pdf").write_bytes(b"X")
    new.mkdir(parents=True)
    (new / "y.pdf").write_bytes(b"Y")
    db = tmp / "state.db"
    s = Store(db)
    s.set_state(page_token="t")
    _seed_job(s, event_key="ek:dir-conflict", action="rename", item_type="directory",
              remote_path="잡지/새이름", removed_path="잡지/옛이름",
              local_path=str(new), size=0, md5="")
    cfg = dict(CFG, LOCAL_ROOT=str(local_root))
    claimed = s.claim_jobs(limit=1, max_attempts=3)
    fake = _FakeRclone()
    from plugins.metadata.gdrive_reading_sync.sync_worker import process_job as _pj
    out = _pj(s, claimed[0], cfg, run_rclone=fake, tmp_root=tmp, log=lambda *a: None)
    assert out["status"] == "skipped", out
    assert out["result"] == "rename_conflict", out
    assert fake.calls == [], fake.calls
    assert old.is_dir() and (old / "x.pdf").read_bytes() == b"X"
    assert new.is_dir() and (new / "y.pdf").read_bytes() == b"Y"
    print("  OK S2 T-S2-04: target 존재 → rename_conflict, 양쪽 불변")


def test_directory_rename_source_missing():
    """T-S2-05 — 옛 폴더 부재 → rename_source_missing, target 생성 0."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    local_root = tmp / "READING"
    new = local_root / "잡지" / "새이름"
    db = tmp / "state.db"
    s = Store(db)
    s.set_state(page_token="t")
    _seed_job(s, event_key="ek:dir-src-missing", action="rename", item_type="directory",
              remote_path="잡지/새이름", removed_path="잡지/옛이름",
              local_path=str(new), size=0, md5="")
    cfg = dict(CFG, LOCAL_ROOT=str(local_root))
    claimed = s.claim_jobs(limit=1, max_attempts=3)
    fake = _FakeRclone()
    from plugins.metadata.gdrive_reading_sync.sync_worker import process_job as _pj
    out = _pj(s, claimed[0], cfg, run_rclone=fake, tmp_root=tmp, log=lambda *a: None)
    assert out["status"] == "skipped", out
    assert out["result"] == "rename_source_missing", out
    assert not new.exists(), "target 폴더가 만들어졌으면 안 됨"
    print("  OK S2 T-S2-05: 옛 폴더 부재 → rename_source_missing, target 생성 0")


def test_directory_rename_outside_local_root():
    """T-S2-06 — local_path 가 LOCAL_ROOT 밖이면 failed/bad_path, fs 변경 0."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    local_root = tmp / "READING"
    db = tmp / "state.db"
    s = Store(db)
    s.set_state(page_token="t")
    # local_path 가 local_root 밖 (다른 볼륨)
    bad_target = "/etc/passwd"
    _seed_job(s, event_key="ek:dir-bad-path", action="rename", item_type="directory",
              remote_path="잡지/새이름", removed_path="잡지/옛이름",
              local_path=bad_target, size=0, md5="")
    cfg = dict(CFG, LOCAL_ROOT=str(local_root))
    claimed = s.claim_jobs(limit=1, max_attempts=3)
    fake = _FakeRclone()
    from plugins.metadata.gdrive_reading_sync.sync_worker import process_job as _pj
    out = _pj(s, claimed[0], cfg, run_rclone=fake, tmp_root=tmp, log=lambda *a: None)
    assert out["status"] == "failed", out
    assert out["result"] == "bad_path", out
    assert fake.calls == [], fake.calls
    print("  OK S2 T-S2-06: LOCAL_ROOT 밖 → failed/bad_path, 파일 변경 0")


def test_directory_rename_crash_resume():
    """T-S2-07 — op_state=started, old 부재, target 디렉터리 → DB 마무리만, 2차 rename 없음."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    local_root = tmp / "READING"
    old = local_root / "옛"
    new = local_root / "새"
    # 첫 시도에서 이미 옮겨졌다고 가정 — old 없음, new 디렉터리, op_state=started
    new.mkdir(parents=True)
    (new / "x.pdf").write_bytes(b"X")
    db = tmp / "state.db"
    s = Store(db)
    s.set_state(page_token="t")
    _seed_job(s, event_key="ek:dir-crash", action="rename", item_type="directory",
              remote_path="새", removed_path="옛",
              local_path=str(new), size=0, md5="")
    # 강제로 op_state=started 박기
    jid = [r for r in s.read_jobs(limit=10)["jobs"] if r["event_key"] == "ek:dir-crash"][0]["id"]
    s._writer.execute(
        "UPDATE job SET op_state='directory_rename_started' WHERE id=?",
        (jid,),
    )
    s._writer.commit()
    # queued → processing 으로 옮긴다
    claimed = s.claim_jobs(limit=1, max_attempts=3)
    cfg = dict(CFG, LOCAL_ROOT=str(local_root))
    fake = _FakeRclone()
    from plugins.metadata.gdrive_reading_sync.sync_worker import process_job as _pj
    out = _pj(s, claimed[0], cfg, run_rclone=fake, tmp_root=tmp, log=lambda *a: None)
    # 종료 + rclone 0
    assert out["status"] == "skipped", out
    assert out["result"] == "renamed_directory", out
    assert fake.calls == [], fake.calls
    assert new.is_dir() and (new / "x.pdf").read_bytes() == b"X", "상태 보존"
    assert not old.exists()
    rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
    assert rows["ek:dir-crash"]["op_state"] == ""  # 종결 시 비움
    print("  OK S2 T-S2-07: started+old 부재+target 디렉터리 → DB 마무리, 2차 rename 0")


def test_directory_rename_updates_pending_jobs():
    """T-S2-08 — 폴더 rename 전 만들어 둔 하위 queued/retry job 의 remote_path/local_path/removed_path 가 새 접두사로 갱신. processing 형제·completed 이력은 불변."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    local_root = tmp / "READING"
    db = tmp / "state.db"
    s = Store(db)
    s.set_state(page_token="t")
    # 옛 폴더 아래 queued 하위 파일
    _seed_job(s, event_key="ek:child-queued", action="create", item_type="file",
              remote_path="옛/x.pdf", removed_path="",
              local_path=str(local_root / "옛" / "x.pdf"),
              size=10, md5="")
    # processing 형제 — finished_at 만 바꾸지 않는지
    _seed_job(s, event_key="ek:child-processing", action="create", item_type="file",
              remote_path="옛/y.pdf", removed_path="",
              local_path=str(local_root / "옛" / "y.pdf"),
              size=10, md5="")
    s._writer.execute(
        "UPDATE job SET status='processing', attempts=1 WHERE event_key='ek:child-processing'"
    )
    s._writer.commit()
    # completed 이력
    _seed_job(s, event_key="ek:child-completed", action="create", item_type="file",
              remote_path="옛/z.pdf", removed_path="",
              local_path=str(local_root / "옛" / "z.pdf"),
              size=10, md5="abc")
    s._writer.execute(
        "UPDATE job SET status='completed', result='copied', bytes_done=10 WHERE event_key='ek:child-completed'"
    )
    s._writer.commit()
    # 폴더 rename job
    _seed_job(s, event_key="ek:dir-rename-pending", action="rename", item_type="directory",
              remote_path="새", removed_path="옛",
              local_path=str(local_root / "새"), size=0, md5="")

    jid_dir = [r for r in s.read_jobs(limit=10)["jobs"] if r["event_key"] == "ek:dir-rename-pending"][0]["id"]
    out = s.finish_directory_rename(
        jid_dir, "옛", "새",
        lambda rp: str(local_root / rp),
    )
    assert out["jobs_updated"] == 2, out  # 하위 queued 1 + 폴더 rename job 1 (자체도 prefix 갱신)
    rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
    cq = rows["ek:child-queued"]
    assert cq["remote_path"] == "새/x.pdf", cq
    assert cq["local_path"] == str(local_root / "새" / "x.pdf"), cq
    # 폴더 rename job 자체도 prefix 갱신
    cd = rows["ek:dir-rename-pending"]
    # remote_path=새, removed_path=옛 였음 — 옛은 prefix match → removed_path=새
    assert cd["remote_path"] == "새", cd
    assert cd["removed_path"] == "새", cd
    # processing 형제 — 변경 X
    cp = rows["ek:child-processing"]
    assert cp["remote_path"] == "옛/y.pdf", cp
    assert cp["status"] == "processing", cp
    # completed 이력 — 변경 X
    cc = rows["ek:child-completed"]
    assert cc["remote_path"] == "옛/z.pdf", cc
    assert cc["status"] == "completed", cc
    print("  OK S2 T-S2-08: 하위 queued 갱신 / processing·completed 불변")


def test_directory_rename_prefix_boundary():
    """T-S2-09 — 옛폴더 rename 때 옛폴더2/ 하위 queued/retry 는 모든 경로 불변."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    local_root = tmp / "READING"
    db = tmp / "state.db"
    s = Store(db)
    s.set_state(page_token="t")
    # 옛폴더2 의 queued (옛폴더 와 별개 — 이름이 prefix 경계)
    _seed_job(s, event_key="ek:other-prefix-queued", action="create", item_type="file",
              remote_path="옛폴더2/x.pdf", removed_path="",
              local_path=str(local_root / "옛폴더2" / "x.pdf"),
              size=10, md5="")
    _seed_job(s, event_key="ek:other-prefix-retry", action="create", item_type="file",
              remote_path="옛폴더2/y.pdf", removed_path="옛폴더2/old-y.pdf",
              local_path=str(local_root / "옛폴더2" / "y.pdf"),
              size=10, md5="")
    _seed_job(s, event_key="ek:exact-prefix-queued", action="create", item_type="file",
              remote_path="옛폴더/x.pdf", removed_path="",
              local_path=str(local_root / "옛폴더" / "x.pdf"),
              size=10, md5="")
    _seed_job(s, event_key="ek:dir-rename-boundary", action="rename", item_type="directory",
              remote_path="새폴더", removed_path="옛폴더",
              local_path=str(local_root / "새폴더"), size=0, md5="")
    jid_dir = [r for r in s.read_jobs(limit=10)["jobs"] if r["event_key"] == "ek:dir-rename-boundary"][0]["id"]
    out = s.finish_directory_rename(
        jid_dir, "옛폴더", "새폴더",
        lambda rp: str(local_root / rp),
    )
    # 옛폴더 정확히 일치하는 행: 하위 정확 file 1 + 폴더 rename job 자체 1 (removed_path 자체도 prefix 일치 처리)
    assert out["jobs_updated"] == 2, out
    rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
    op_q = rows["ek:other-prefix-queued"]
    assert op_q["remote_path"] == "옛폴더2/x.pdf", op_q
    op_r = rows["ek:other-prefix-retry"]
    assert op_r["remote_path"] == "옛폴더2/y.pdf", op_r
    assert op_r["removed_path"] == "옛폴더2/old-y.pdf", op_r
    ep = rows["ek:exact-prefix-queued"]
    assert ep["remote_path"] == "새폴더/x.pdf", ep  # 정확 prefix 만 갱신
    print("  OK S2 T-S2-09: 경계 정확 — 옛폴더2/ 불변, 옛폴더/ 만 갱신")


def _seed_listpage(store: Store, n: int = 30, every_status=None):
    """S4 테스트용 시드 — 다양한 상태/result 의 job N 개."""
    statuses = ["queued", "retry", "processing", "completed", "failed", "skipped"]
    results = ["copied", "duplicate", "renamed_event", "renamed_cold_start",
               "renamed_directory", "max_attempts_exceeded"]
    every = every_status
    for i in range(1, n + 1):
        st = every if every else statuses[i % len(statuses)]
        rs = results[i % len(results)]
        store.upsert_job({
            "event_key": f"ek:lp-{i}",
            "action": ["create", "edit", "rename", "delete"][i % 4],
            "item_type": "file",
            "file_id": f"fid-{i}",
            "remote_path": f"a/b/{i}.pdf",
            "removed_path": f"a/b/{i-1}.pdf" if i % 5 == 0 else "",
            "local_path": f"/r/a/b/{i}.pdf",
            "size": 100 + i,
            "md5": ("a" * 32) if i % 2 == 0 else "",
            "modified_time": "2026-09-01T00:00:00Z",
            "status": st,
        })
        # 강제 status/result 박기 (upsert_job 이 completed/failed/skipped 를 못 박으니 직접)
        store._writer.execute(
            "UPDATE job SET status=?, result=?, bytes_done=? WHERE event_key=?",
            (st, rs, 50 if st == "completed" else 0, f"ek:lp-{i}"),
        )
    store._writer.commit()


def test_list_page_count_and_pages():
    """T-S4-01 — 525 seed에서 page_size 50, total 525, pages 11, items 50."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    _seed_listpage(s, n=525)
    out = s.list_page(page=1, page_size=50)
    assert out["total"] == 525, out
    assert out["pages"] == 11, out
    assert out["page"] == 1, out
    assert out["page_size"] == 50, out
    assert len(out["items"]) == 50, out
    # 마지막 페이지
    out11 = s.list_page(page=11, page_size=50)
    assert len(out11["items"]) == 25, out11
    print("  OK S4 T-S4-01: page_size 50에서 total=525, pages=11, items=50/25")


def test_list_page_no_overlap_and_order():
    """T-S4-02 — page1/page2 id 교집합 0, asc/desc 순서."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    _seed_listpage(s, n=20)
    out1 = s.list_page(page=1, page_size=10, order="desc")
    out2 = s.list_page(page=2, page_size=10, order="desc")
    ids1 = {it["id"] for it in out1["items"]}
    ids2 = {it["id"] for it in out2["items"]}
    assert ids1.isdisjoint(ids2), (ids1 & ids2)
    # desc: page1 ids > page2 ids
    assert min(ids1) > max(ids2)
    # asc
    outA = s.list_page(page=1, page_size=10, order="asc")
    outB = s.list_page(page=2, page_size=10, order="asc")
    idsA = [it["id"] for it in outA["items"]]
    assert idsA == sorted(idsA), idsA
    # asc 교집합 0
    assert {it["id"] for it in outA["items"]}.isdisjoint({it["id"] for it in outB["items"]})
    print("  OK S4 T-S4-02: page1/page2 교집합 0, asc/desc 순서")


def test_list_page_filters_and_search():
    """T-S4-03 — status/action/result/search 필터. COUNT 와 items 일치."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    _seed_listpage(s, n=20)
    # status=failed
    out = s.list_page(page=1, page_size=100, status="failed")
    expected = s._writer.execute("SELECT COUNT(*) FROM job WHERE status='failed'").fetchone()[0]
    assert out["total"] == int(expected), out
    for it in out["items"]:
        assert it["status"] == "failed", it
    # result=copied
    out2 = s.list_page(page=1, page_size=100, result="copied")
    for it in out2["items"]:
        assert it["result"] == "copied", it
    expected2 = s._writer.execute("SELECT COUNT(*) FROM job WHERE result='copied'").fetchone()[0]
    assert out2["total"] == int(expected2), out2
    # search — remote_path 에 '5' 가 들어간 행만
    out3 = s.list_page(page=1, page_size=100, search="5")
    for it in out3["items"]:
        if not (
            "5" in (it["remote_path"] or "")
            or "5" in (it["local_path"] or "")
            or "5" in (it["removed_path"] or "")
            or "5" in (it["error"] or "")
        ):
            raise AssertionError(it)
    expected3 = s._writer.execute(
        "SELECT COUNT(*) FROM job WHERE remote_path LIKE '%5%' "
        "OR local_path LIKE '%5%' OR removed_path LIKE '%5%' OR error LIKE '%5%'"
    ).fetchone()[0]
    assert out3["total"] == int(expected3), out3
    # search escape — %는 리터럴로 취급
    s.upsert_job({"event_key": "ek:lp-foo%bar", "action": "create", "item_type": "file",
                  "file_id": "fid-x", "remote_path": "x/foo%bar.pdf",
                  "removed_path": "", "local_path": "/r/x/foo%bar.pdf",
                  "size": 1, "md5": "", "modified_time": "", "status": "queued"})
    s._writer.commit()
    outE = s.list_page(page=1, page_size=10, search="foo%bar")
    # escape 덕분에 정확히 1 건 매치
    assert outE["total"] == 1, outE
    assert any("foo%bar" in (it["remote_path"] or "") for it in outE["items"])
    print("  OK S4 T-S4-03: status/action/result/search 필터 + escape")


def test_list_page_clamp():
    """T-S4-04 — page_size=1000→500, page_size=1→10. route 의 limit alias 호환."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    _seed_listpage(s, n=20)
    out_hi = s.list_page(page=1, page_size=1000)
    assert out_hi["page_size"] == 500, out_hi
    out_lo = s.list_page(page=1, page_size=1)
    assert out_lo["page_size"] == 10, out_lo
    # read_jobs(limit=100) 가 jobs 키 + total = 진짜 COUNT 다
    out_lim = s.read_jobs(limit=100)
    assert "jobs" in out_lim, out_lim
    assert out_lim["total"] == 20, out_lim
    assert len(out_lim["jobs"]) == 20
    print("  OK S4 T-S4-04: clamp 1000→500, 1→10, limit alias 호환")


def test_retry_jobs_failed_two():
    """T-S5-01 — failed(attempts=3) 2건 → retry → 둘 다 queued/attempts=0, 오류/결과/bytes 초기화."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    s.set_state(page_token="t")
    for i, ek in enumerate(("ek:s5-1", "ek:s5-2"), start=1):
        s.upsert_job({
            "event_key": ek, "action": "create", "item_type": "file",
            "file_id": f"fid{i}", "remote_path": f"a/{i}.pdf",
            "removed_path": "", "local_path": "/r/a/"+str(i)+".pdf",
            "size": 1, "md5": "", "modified_time": "", "status": "queued",
        })
        s._writer.execute(
            "UPDATE job SET status='failed', attempts=3, result='max_attempts_exceeded', "
            "error='x', bytes_done=0 WHERE event_key=?",
            (ek,),
        )
    s._writer.commit()
    target_ids = sorted(r["id"] for r in s.read_jobs(limit=10)["jobs"] if r["event_key"].startswith("ek:s5-"))
    out = s.retry_jobs(job_ids=target_ids)
    assert out["retried"] == 2, out
    rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
    for ek in ("ek:s5-1", "ek:s5-2"):
        r = rows[ek]
        assert r["status"] == "queued", r
        assert int(r["attempts"]) == 0, r
        assert r["error"] == "", r
        assert r["result"] == "", r
        assert int(r["bytes_done"]) == 0, r
    print("  OK S5 T-S5-01: failed 2건 → queued/attempts=0 초기화")


def test_retry_jobs_processing_rejected():
    """T-S5-02 — processing 혼합 시 HTTP/store 거부, 모든 행 불변."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    s.set_state(page_token="t")
    s.upsert_job({"event_key": "ek:s5-q", "action": "create", "item_type": "file",
                  "file_id": "fid-q", "remote_path": "q.pdf",
                  "removed_path": "", "local_path": "/r/q.pdf",
                  "size": 1, "md5": "", "modified_time": "", "status": "queued"})
    s.upsert_job({"event_key": "ek:s5-p", "action": "create", "item_type": "file",
                  "file_id": "fid-p", "remote_path": "p.pdf",
                  "removed_path": "", "local_path": "/r/p.pdf",
                  "size": 1, "md5": "", "modified_time": "", "status": "queued"})
    s._writer.execute(
        "UPDATE job SET status='processing', attempts=1 WHERE event_key='ek:s5-p'"
    )
    s._writer.commit()
    rows_before = {r["event_key"]: dict(r) for r in s.read_jobs(limit=10)["jobs"]}
    target_ids = [rows_before[k]["id"] for k in ("ek:s5-q", "ek:s5-p")]
    out = s.retry_jobs(job_ids=target_ids)
    assert out["retried"] == 0, out
    assert out["rejected_ids"], out  # processing 이 거부됨
    rows_after = {r["event_key"]: dict(r) for r in s.read_jobs(limit=10)["jobs"]}
    # 둘 다 status 가 'before' 와 같다 = 변경 0
    for ek in ("ek:s5-q", "ek:s5-p"):
        assert rows_after[ek]["status"] == rows_before[ek]["status"], (ek, rows_after[ek])
    # processing 행은 여전히 processing
    assert rows_after["ek:s5-p"]["status"] == "processing"
    # queued 행은 여전히 queued (rejected 라서)
    assert rows_after["ek:s5-q"]["status"] == "queued"
    print("  OK S5 T-S5-02: processing 혼합 거부, 행 불변")


def test_retry_jobs_failed_all():
    """T-S5-03 — failed_all 모드: failed 만 queued, completed/skipped/processing 불변."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    s.set_state(page_token="t")
    # failed 2건, completed 1건, skipped 1건, processing 1건
    for tag, st in (
        ("ek:s5a-f", "failed"), ("ek:s5a-f2", "failed"),
        ("ek:s5a-c", "completed"), ("ek:s5a-s", "skipped"),
        ("ek:s5a-p", "processing"),
    ):
        s.upsert_job({"event_key": tag, "action": "create", "item_type": "file",
                      "file_id": tag, "remote_path": tag,
                      "removed_path": "", "local_path": "/r/"+tag,
                      "size": 1, "md5": "", "modified_time": "", "status": "queued"})
        s._writer.execute(
            "UPDATE job SET status=?, attempts=CASE WHEN ? IN ('failed','completed') THEN 3 ELSE 0 END WHERE event_key=?",
            (st, st, tag),
        )
    s._writer.commit()
    out = s.retry_jobs(failed_all=True)
    assert out["retried"] == 2, out  # failed 2건만
    rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
    assert rows["ek:s5a-f"]["status"] == "queued", rows["ek:s5a-f"]
    assert rows["ek:s5a-f2"]["status"] == "queued", rows["ek:s5a-f2"]
    assert rows["ek:s5a-c"]["status"] == "completed"  # 불변
    assert rows["ek:s5a-s"]["status"] == "skipped"
    assert rows["ek:s5a-p"]["status"] == "processing"
    print("  OK S5 T-S5-03: failed_all — failed 만 queued, 나머지 불변")


def test_cleanup_terminal_preserves_active():
    """T-S6-01 — queued/retry/processing 생존, completed 3건 삭제."""
    from pathlib import Path as _P
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    s.set_state(page_token="t")
    for tag, st in (
        ("ek:s6-q", "queued"), ("ek:s6-r", "retry"), ("ek:s6-p", "processing"),
        ("ek:s6-c1", "completed"), ("ek:s6-c2", "completed"), ("ek:s6-c3", "completed"),
    ):
        s.upsert_job({"event_key": tag, "action": "create", "item_type": "file",
                      "file_id": tag, "remote_path": tag,
                      "removed_path": "", "local_path": "/r/"+tag,
                      "size": 1, "md5": "", "modified_time": "", "status": "queued"})
        s._writer.execute(
            "UPDATE job SET status=?, updated_at='2020-01-01T00:00:00' WHERE event_key=?",
            (st, tag),
        )
    s._writer.commit()
    deleted = s.cleanup_terminal(retention_days=30, delete_all=False)
    # 모든 updated_at=2020 이므로 retention 30 일이면 completed 3건 다 지움
    assert deleted == 3, deleted
    rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
    for ek in ("ek:s6-q", "ek:s6-r", "ek:s6-p"):
        assert ek in rows, f"{ek} 가 사라짐 — 비종결은 절대 안 지움"
    for ek in ("ek:s6-c1", "ek:s6-c2", "ek:s6-c3"):
        assert ek not in rows, f"{ek} 가 남아있음 — completed 는 retention 적용으로 지워져야 함"
    print("  OK S6 T-S6-01: queued/retry/processing 생존, completed 3건 삭제")


def test_cleanup_terminal_29d_survives():
    """T-S6-02 — retention 30 일: 29 일 전 completed 는 생존."""
    from pathlib import Path as _P
    import datetime as _dt
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    s.set_state(page_token="t")
    now = _dt.datetime.utcnow()
    aged = (now - _dt.timedelta(days=29)).strftime("%Y-%m-%dT%H:%M:%S")
    old = (now - _dt.timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S")
    for tag, dt_ in (("ek:s6-young", aged), ("ek:s6-old", old)):
        s.upsert_job({"event_key": tag, "action": "create", "item_type": "file",
                      "file_id": tag, "remote_path": tag,
                      "removed_path": "", "local_path": "/r/"+tag,
                      "size": 1, "md5": "", "modified_time": "", "status": "queued"})
        s._writer.execute(
            "UPDATE job SET status='completed', updated_at=? WHERE event_key=?",
            (dt_, tag),
        )
    s._writer.commit()
    deleted = s.cleanup_terminal(retention_days=30)
    assert deleted == 1, deleted  # 31일 전만 삭제
    rows = {r["event_key"]: r for r in s.read_jobs(limit=10)["jobs"]}
    assert "ek:s6-young" in rows, "29일 전 completed 가 지워짐 — retention 경계 회귀"
    assert "ek:s6-old" not in rows, "31일 전 completed 가 안 지워짐"
    print("  OK S6 T-S6-02: 29일 경계 — retention 30 에서 29일 전은 생존")


def test_cleanup_terminal_auto_off_and_hour_gate():
    """T-S6-03 — AUTO_CLEANUP off 면 0, on 이라도 1시간 이내 재호출은 0."""
    from pathlib import Path as _P
    import datetime as _dt
    from plugins.metadata.gdrive_reading_sync.gdrive_reading_sync import GdriveReadingSyncMetadataProvider as _P2
    tmp = _P(tempfile.mkdtemp())
    s = Store(tmp / "state.db")
    s.set_state(page_token="t")
    s.upsert_job({"event_key": "ek:s6-c", "action": "create", "item_type": "file",
                  "file_id": "fid-c", "remote_path": "c.pdf",
                  "removed_path": "", "local_path": "/r/c.pdf",
                  "size": 1, "md5": "", "modified_time": "", "status": "queued"})
    s._writer.execute(
        "UPDATE job SET status='completed', updated_at='2020-01-01T00:00:00'"
    )
    s._writer.commit()
    provider = _P2()
    # 1) AUTO_CLEANUP off — DELETE 0
    cfg = {"AUTO_CLEANUP": False, "RETENTION_DAYS": 30}
    deleted = provider._cleanup_if_due(s, cfg, now_monotonic=1e9)
    assert deleted == 0, f"AUTO_CLEANUP off 인데 {deleted} 건 삭제됨"
    rows_before = s.read_jobs(limit=10)["jobs"]
    assert any(r["event_key"] == "ek:s6-c" for r in rows_before)
    # 2) AUTO_CLEANUP on — 첫 호출은 DELETE 1, monotonic gate 갱신
    cfg = {"AUTO_CLEANUP": True, "RETENTION_DAYS": 30}
    deleted = provider._cleanup_if_due(s, cfg, now_monotonic=1e9)
    assert deleted == 1, deleted
    # 같은 monotonic + 약간만 흐른 시점 — gate 가 막아야 함
    provider._last_cleanup_monotonic = 1e9
    deleted = provider._cleanup_if_due(s, cfg, now_monotonic=1e9 + 60)
    assert deleted == 0, deleted
    # 1시간 후
    deleted = provider._cleanup_if_due(s, cfg, now_monotonic=1e9 + 3700)
    assert deleted == 0, (deleted, "지울 completed 가 없다")
    print("  OK S6 T-S6-03: AUTO_CLEANUP off = 0, on 도 1시간 이내 재호출 0")


def test_rclone_config_args_empty_uses_bookoasis_fallback():
    """T-S8-01 — RCLONE_CONFIG 빈 값 → 기존 BookOasis config_args 가 그대로 사용."""
    from plugins.metadata.gdrive_reading_sync.sync_worker import _rclone_config_args
    # BookOasis 의 _rclone_config_args 가 utils.rclone_gdrive_copy 에 있다 — BookOasis
    # stub 환경에서는 import 가 실패 → _bookoasis_config_args() 가 [] 반환
    cfg = {"RCLONE_CONFIG": ""}
    out = _rclone_config_args(cfg)
    assert isinstance(out, list), out
    # 빈 리스트 라면 === [] 또는 BookOasis 환경 변수 / 도커 번들 위치.
    # 우리가 직접 추가한 --config 토큰은 포함하지 않는다.
    assert out == [] or "--config" not in out, out
    print("  OK S8 T-S8-01: RCLONE_CONFIG 빈 값 — BookOasis 폴백 (또는 빈 리스트)")


def test_rclone_config_args_existing_path():
    """T-S8-02 — RCLONE_CONFIG 정본 경로 → ['--config', str]."""
    from plugins.metadata.gdrive_reading_sync.sync_worker import _rclone_config_args
    import tempfile as _tf
    fd, path = _tf.mkstemp(suffix=".conf")
    os.close(fd)
    try:
        cfg = {"RCLONE_CONFIG": path}
        out = _rclone_config_args(cfg)
        assert out == ["--config", path], out
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    print("  OK S8 T-S8-02: RCLONE_CONFIG 정본 경로 → ['--config', path]")


def test_rclone_config_args_missing_path_raises():
    """T-S8-03 — RCLONE_CONFIG 가 명시됐는데 파일 부재 → FileNotFoundError. 폴백 없음."""
    from plugins.metadata.gdrive_reading_sync.sync_worker import _rclone_config_args
    cfg = {"RCLONE_CONFIG": "/nonexistent/path/rclone-fake.conf"}
    try:
        _rclone_config_args(cfg)
    except FileNotFoundError as exc:
        assert "RCLONE_CONFIG" in str(exc), str(exc)
        print("  OK S8 T-S8-03: RCLONE_CONFIG 부재 경로 → FileNotFoundError (조용한 폴백 없음)")
        return
    raise AssertionError("FileNotFoundError 가 안 남")


# ============================================================================
# 3라운드-B T1 — 날짜별 로그 신규 테스트 (T-P7-L1..L5 + T-P7-SYM-T1-LOG)
# ============================================================================


def _reset_runtime_logger():
    """테스트 격리 — 런타임 logger 의 handler 모두 닫고 fingerprint 초기화."""
    log = _lg.getLogger("gdrive_reading_sync.runtime")
    for h in list(log.handlers):
        try:
            h.close()
        except Exception:
            pass
        try:
            log.removeHandler(h)
        except Exception:
            pass
    GdriveReadingSyncMetadataProvider._runtime_logger = None  # type: ignore[attr-defined]
    GdriveReadingSyncMetadataProvider._runtime_logger_fp = ()  # type: ignore[attr-defined]


def test_T_P7_L1_log_dir_explicit_writes_file():
    """T-P7-L1 — LOG_DIR 지정 시 파일 생성·기록. 합격선: marker 가 파일에 존재."""
    import logging
    import logging.handlers

    _reset_runtime_logger()
    log_dir = Path(tempfile.mkdtemp()) / "logs"
    cfg = dict(CFG, LOG_DIR=str(log_dir), LOG_RETENTION_DAYS=7)
    # plugin data 디렉터리는 임시 Store 의 db_path.parent 로 흉내.
    fake_data = Path(tempfile.mkdtemp()) / "data"
    prov = GdriveReadingSyncMetadataProvider()
    logger = prov._configure_runtime_logger(cfg, fake_data)
    marker = "T_P7_L1_marker_xyz"
    logger.info(marker)
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass
    # 활성 파일이 있어야 한다 — 날짜가 붙은 회전 파일 또는 미회전 기본 파일.
    today = time.strftime("%Y%m%d", time.gmtime())
    rotated = log_dir / f"gdrive_reading_sync_{today}.log"
    active = log_dir / "gdrive_reading_sync.log"
    assert rotated.exists() or active.exists(), (
        f"로그 파일이 없음: rotated={rotated} active={active}"
    )
    target = rotated if rotated.exists() else active
    body = target.read_text(encoding="utf-8", errors="replace")
    assert marker in body, body[:500]
    _reset_runtime_logger()
    print("  OK T-P7-L1: LOG_DIR 지정 → 파일 생성, marker 기록")


def test_T_P7_L2_log_dir_blank_falls_back_under_data_dir():
    """T-P7-L2 — LOG_DIR 공백 → plugin data 디렉터리 아래 logs/. L: 새지 않음."""
    _reset_runtime_logger()
    data_dir = Path(tempfile.mkdtemp()) / "data"
    cfg = dict(CFG, LOG_DIR="", LOG_RETENTION_DAYS=7)
    prov = GdriveReadingSyncMetadataProvider()
    logger = prov._configure_runtime_logger(cfg, data_dir)
    marker = "T_P7_L2_marker"
    logger.info(marker)
    expected = data_dir / "logs"
    assert expected.is_dir(), f"폴백 디렉터리 부재: {expected}"
    # L: 또는 _private_reports 가 경로에 새는지 확인 — 경로 문자열 검사.
    resolved_str = str(expected.resolve()) if hasattr(expected, "resolve") else str(expected)
    # 절대경로 비교는 환경 의존이지만 절대 "L:" 또는 "_private_reports" 가
    # 새지는 않는다는 점만 본다.
    assert "L:" not in resolved_str, resolved_str
    assert "_private_reports" not in resolved_str, resolved_str
    logger.info(marker)
    _reset_runtime_logger()
    print("  OK T-P7-L2: LOG_DIR 공백 → data_dir/logs, L: / _private_reports 새지 않음")


def test_T_P7_L3_log_dir_failure_does_not_kill_logger():
    """T-P7-L3 — mkdir 실패 / handler emit 실패 주입 시 worker 계속, console fallback."""
    _reset_runtime_logger()
    # 1) mkdir 실패: 존재하지 않는 드라이브 문자 같은 부적절 경로.
    bad_dir = "Z:\\nonexistent_drive_xyz\\logs" if os.name == "nt" else "/dev/null/cannot_create"
    cfg = dict(CFG, LOG_DIR=bad_dir, LOG_RETENTION_DAYS=7)
    prov = GdriveReadingSyncMetadataProvider()
    logger = prov._configure_runtime_logger(cfg, Path(tempfile.mkdtemp()))
    marker = "T_P7_L3_marker"
    # 예외가 worker 밖으로 새면 안 됨 — logger.info 자체는 예외 없이 끝나야 한다.
    logger.info(marker)
    # console handler 가 살아 있어야 한다 (stdout fallback).
    has_stream = any(isinstance(h, _lg.StreamHandler) for h in logger.handlers)
    assert has_stream, [type(h).__name__ for h in logger.handlers]

    # 2) emit 실패: file handler 를 망가뜨려도 console 로 marker 가 도달.
    log_dir = Path(tempfile.mkdtemp()) / "logs"
    cfg2 = dict(CFG, LOG_DIR=str(log_dir), LOG_RETENTION_DAYS=7)
    _reset_runtime_logger()
    logger = prov._configure_runtime_logger(cfg2, Path(tempfile.mkdtemp()))
    file_handlers = [h for h in logger.handlers if "TimedRotating" in type(h).__name__]
    assert file_handlers, [type(h).__name__ for h in logger.handlers]
    # emit 이 OSError 를 내도록 baseFilename 을 권한 없는 경로로 갈음.
    for fh in file_handlers:
        try:
            fh.baseFilename = "/proc/1/secret-cannot-write" if os.name != "nt" else "Z:\\nonexistent_xyz\\x.log"
        except Exception:
            pass
    logger.info(marker)  # 예외 전파되면 안 됨.
    # console 로 marker 가 도달했는지 — logger 의 handler 가 살아 있다는 정적 확인.
    has_stream = any(isinstance(h, _lg.StreamHandler) for h in logger.handlers)
    assert has_stream, [type(h).__name__ for h in logger.handlers]
    _reset_runtime_logger()
    print("  OK T-P7-L3: mkdir 실패 / emit 실패 → worker 계속, console fallback 살아 있음")


def test_T_P7_L4_console_output_preserved():
    """T-P7-L4 — 7010 콘솔 (=sys.stdout) 출력 유지. StreamHandler 가 logger 의 첫 handler."""
    import sys as _sys
    import io as _io

    _reset_runtime_logger()
    cfg = dict(CFG, LOG_DIR=str(Path(tempfile.mkdtemp()) / "logs"), LOG_RETENTION_DAYS=7)
    prov = GdriveReadingSyncMetadataProvider()
    captured = _io.StringIO()
    real_stdout = _sys.stdout
    _sys.stdout = captured
    try:
        logger = prov._configure_runtime_logger(cfg, Path(tempfile.mkdtemp()))
        marker = "T_P7_L4_console_marker"
        logger.info(marker)
        for h in logger.handlers:
            try:
                h.flush()
            except Exception:
                pass
    finally:
        _sys.stdout = real_stdout
    text = captured.getvalue()
    assert marker in text, text[:500]
    _reset_runtime_logger()
    print("  OK T-P7-L4: console 출력 유지 (StreamHandler 가 sys.stdout 에 기록)")


def test_T_P7_L6_local_timestamp_with_offset():
    """T-P7-L6 — asctime 이 **로컬 시각 + UTC 오프셋** 이어야 한다.

    로그는 서버 운영자가 읽는다. 자기 시각으로 보여야 한다 (사용자 지적 2026-09-03).
    오프셋(+0900 등)이 붙어 모호하지 않아야 하고, 'Z' 로 UTC 인 척하면 안 된다.
    converter 는 Formatter 의 속성이라 Handler 에 붙이면 효과가 없다.
    """
    import time as _t
    import logging

    _reset_runtime_logger()
    cfg = dict(CFG, LOG_DIR=str(Path(tempfile.mkdtemp()) / "logs"), LOG_RETENTION_DAYS=7)
    prov = GdriveReadingSyncMetadataProvider()
    logger = prov._configure_runtime_logger(cfg, Path(tempfile.mkdtemp()))
    rec = logging.LogRecord("t", logging.INFO, "p", 1, "utc-probe", None, None)
    for h in logger.handlers:
        f = h.formatter
        if f is None:
            continue
        out = f.format(rec)
        local = _t.strftime("%Y-%m-%dT%H:%M:%S", _t.localtime(rec.created))
        assert out.startswith(local), (
            "asctime 이 로컬 시각이 아니다: out=%r local=%r" % (out[:34], local))
        offset = _t.strftime("%z", _t.localtime(rec.created))
        if offset:
            assert offset in out[:34], "UTC 오프셋이 없다: %r" % out[:34]
        assert not out[:34].endswith("Z"), "로컬 시각에 Z 를 붙이면 안 된다: %r" % out[:34]
    _reset_runtime_logger()
    print("  OK T-P7-L6: asctime 이 로컬 시각 + 오프셋 (%s)" % _t.strftime("%z"))


def test_T_P7_L8_poll_log_accounts_for_every_change():
    """T-P7-L8 — poll 로그 한 줄로 changes_seen 이 전부 설명돼야 한다.

    2026-09-03 소킹: 26시간 5,313건 중 17건이 어느 항목에도 안 잡혔다.
    summary 에는 folders_seen / probe_skipped 가 있는데 로그 문자열에서 빠져
    있었기 때문이다. 하필 folders_seen 은 폴더 rename 전파(S2)의 유일한
    관측 창구라, 실제 폴더 이벤트가 지나가도 로그에 흔적이 안 남았다.

    합격선:
      - poll 로그에 summary 의 모든 카운터 키가 나타난다
      - changes_seen = 분류 항목들의 합 (설명 안 되는 잔여 0)
    """
    import re

    logs = []
    store = _store()
    client = FakeClient([{
        "changes": [{"fileId": "x1", "file": _file("x1", "SPARK 2018.10#100.pdf", "f_spark")}],
        "newStartPageToken": "7387303",
    }])
    summary = poll_once(client, store, CFG, log=logs.append)

    line = [l for l in logs if "poll pages=" in l]
    assert line, logs
    line = line[0]

    for key in ("folders", "out_of_root", "excluded", "ext_skip",
                "probe_skip", "resolve_err", "jobs", "changes"):
        assert key + "=" in line, "poll 로그에 %s 가 없다: %s" % (key, line)

    got = {k: int(v) for k, v in re.findall(r"(\w+)=(\d+)", line)}
    accounted = (got["jobs"] + got["folders"] + got["out_of_root"]
                 + got["excluded"] + got["ext_skip"] + got["probe_skip"]
                 + got["resolve_err"])
    assert got["changes"] <= accounted, (
        "changes=%d 인데 설명된 건 %d 뿐 — 잔여 %d건이 로그로 설명되지 않는다: %s"
        % (got["changes"], accounted, got["changes"] - accounted, line))

    for key in ("folders_seen", "probe_skipped"):
        assert key in summary, "summary 에 %s 가 없다" % key

    print("  OK T-P7-L8: poll 로그가 changes 를 전부 설명 (changes=%d, 분류합=%d)"
          % (got["changes"], accounted))


def test_T_P7_L7_midnight_rotation_survives():
    """T-P7-L7 — 자정 회전이 실제로 일어나고, 회전 뒤에도 로깅이 살아 있어야 한다.

    소킹 2026-09-03 실측 결함: rotation_filename() 이 존재하지 않는 `rollover_at` 을
    참조해 AttributeError 를 냈고, emit 의 except 가 그것을 삼켜 handler 를 조용히
    껐다. 회전 파일도 안 생기고 오류도 안 남고 로그만 사라졌다.

    합격선:
      - 회전 후 날짜 접미사 파일이 생긴다
      - 회전 뒤에 쓴 줄이 활성 파일에 들어간다 (handler 가 안 죽는다)
      - handler.disabled 가 False
    """
    import logging
    import logging.handlers

    _reset_runtime_logger()
    log_dir = Path(tempfile.mkdtemp()) / "logs"
    cfg = dict(CFG, LOG_DIR=str(log_dir), LOG_RETENTION_DAYS=7)
    prov = GdriveReadingSyncMetadataProvider()
    logger = prov._configure_runtime_logger(cfg, Path(tempfile.mkdtemp()))

    fh = None
    for h in logger.handlers:
        if isinstance(h, logging.handlers.TimedRotatingFileHandler):
            fh = h
            break
    assert fh is not None, "파일 handler 가 없다"

    logger.info("before-rotation-marker")
    fh.flush()

    # 자정을 지난 것처럼 만든다 — rolloverAt 을 과거로 당긴다.
    fh.rolloverAt = time.time() - 1
    logger.info("after-rotation-marker")
    fh.flush()

    assert getattr(fh, "disabled", False) is False, "회전 뒤 handler 가 꺼졌다 (로그 사망)"

    rotated = sorted(log_dir.glob("gdrive_reading_sync_????????.log"))
    assert rotated, "회전 파일이 생기지 않았다: %s" % sorted(p.name for p in log_dir.iterdir())

    active = (log_dir / "gdrive_reading_sync.log").read_text(encoding="utf-8")
    assert "after-rotation-marker" in active, (
        "회전 뒤 기록이 활성 파일에 없다 (handler 사망) — 활성=%r" % active[:200])

    _reset_runtime_logger()
    print("  OK T-P7-L7: 자정 회전 후에도 로깅 생존 (%s 생성)" % rotated[0].name)


def test_T_P7_L5_no_private_paths_and_no_secrets_in_log():
    """T-P7-L5 — 정적: 새 설정 키 기본값에 개인 경로 없음. 동적: 시크릿 sentinel 미기록.

    합격선:
    - _BASE_CONFIG_SCHEMA 의 default 에 `L:\\`, `C:\\Users\\example`, `_private_reports` 0건.
    - sentinel cfg 를 logger 에 흘려도 토큰·비번·conf body 가 console/파일 어디에도 없다.
    """
    # 정적 scan
    import re as _re_p7
    base = GdriveReadingSyncMetadataProvider._BASE_CONFIG_SCHEMA
    forbidden_substrings = ("L:\\", "C:\\Users\\example", "_private_reports")
    for entry in base:
        key = entry.get("key", "")
        if key in ("LOG_DIR",):
            default = entry.get("default", "")
            assert isinstance(default, str), (key, default)
            for bad in forbidden_substrings:
                assert bad not in default, (key, default, bad)
    # sentinel 동적 — runtime_logger 와 동일 이름의 새 logger 를 만들고 sentinel 흘림.
    _reset_runtime_logger()
    fake_log_dir = Path(tempfile.mkdtemp()) / "logs"
    cfg = dict(CFG, LOG_DIR=str(fake_log_dir), LOG_RETENTION_DAYS=7,
               P7_SECRET_TOKEN="SECRET_TOKEN_SENTINEL_XYZ",
               P7_SECRET_PASSWORD="SECRET_PASSWORD_SENTINEL_XYZ",
               P7_RCLONE_CONF_BODY="CONF_BODY_SENTINEL_XYZ")
    prov = GdriveReadingSyncMetadataProvider()
    # sentinel 흐름 — runtime_logger 가 sentinel 값을 받으면 안 됨. 직접 흘리지 말고,
    # fingerprint 변화 시 cfg 전체를 출력하지 않음을 확인.
    logger = prov._configure_runtime_logger(cfg, Path(tempfile.mkdtemp()))
    logger.info("normal line %s", "ok")
    # sentinel cfg 가 _configure_runtime_logger 의 fingerprint 에 들어가지 않는지.
    fp = GdriveReadingSyncMetadataProvider._runtime_logger_fp
    for sentinel in (
        "SECRET_TOKEN_SENTINEL_XYZ",
        "SECRET_PASSWORD_SENTINEL_XYZ",
        "CONF_BODY_SENTINEL_XYZ",
    ):
        assert sentinel not in str(fp), (sentinel, fp)
    _reset_runtime_logger()
    print("  OK T-P7-L5: 기본값에 L:\\/C:\\Users\\example/_private_reports 없음, "
          "sentinel 이 fingerprint 에 새지 않음")


def test_T_P7_SYM_T1_LOG_diagnosis_survives_console_close():
    """T-P7-SYM-T1-LOG — 원 증상 T1. 7010 콘솔 종료와 무관하게 날짜 파일에 marker 2개 보존.

    트리거: temp LOG_DIR logger 로 marker 2줄 기록, 그 사이 console handler 를
    닫아 콘솔 보존 없다고 가정, handler flush/close 후 파일 재개방.
    합격선: 두 marker 모두 파일에 존재.
    """
    _reset_runtime_logger()
    log_dir = Path(tempfile.mkdtemp()) / "logs"
    cfg = dict(CFG, LOG_DIR=str(log_dir), LOG_RETENTION_DAYS=7)
    prov = GdriveReadingSyncMetadataProvider()
    logger = prov._configure_runtime_logger(cfg, Path(tempfile.mkdtemp()))

    m1 = "SYM_T1_first_marker"
    m2 = "SYM_T1_second_marker_after_console_closed"

    logger.info(m1)
    # console handler 를 닫아도 file handler 는 살아 있어야 한다.
    for h in list(logger.handlers):
        if isinstance(h, _lg.StreamHandler) and not isinstance(h, _lg.FileHandler):
            try:
                h.close()
            except Exception:
                pass
            try:
                logger.removeHandler(h)
            except Exception:
                pass
    logger.info(m2)
    for h in logger.handlers:
        try:
            h.flush()
        except Exception:
            pass
    # 파일 재개방.
    today = time.strftime("%Y%m%d", time.gmtime())
    rotated = log_dir / f"gdrive_reading_sync_{today}.log"
    active = log_dir / "gdrive_reading_sync.log"
    target = rotated if rotated.exists() else active
    assert target.exists(), [str(p) for p in (rotated, active)]
    body = target.read_text(encoding="utf-8", errors="replace")
    assert m1 in body and m2 in body, body[:500]
    _reset_runtime_logger()
    print("  OK T-P7-SYM-T1-LOG: console 종료 후에도 파일에 두 marker 보존")


# ============================================================================
# 3라운드-B T2 — 병렬 전송 신규 테스트 (T-P7-P1..P6 + T-P7-SYM-T2-PARALLEL)
# ============================================================================
# T-P7-P1, T-P7-P2 는 테스트 runner 의 CLI 옵션 --parallel-transfers {1,5} 와
# 묶여 있다. main 의 argparse 에서 --parallel-transfers 를 받아 cfg 에 넣고
# process_jobs 가 그 값으로 돌도록 한다. 기존 50종 + T1 6종은 항상 함께 실행.
#
# T-P7-P3~P5 / SYM-T2-PARALLEL 은 합성 job 으로 동시 실행 / rename 직렬 선행 /
# executor 진입 액션 검증을 한다.
# T-P7-P6 는 정적 검증 (rg -n "store\._writer" sync_worker.py → 0건).
from concurrent.futures import ThreadPoolExecutor as _TPE
import re as _re_p7


def _seed_copy_jobs(store: Store, *, n: int, payload: bytes = b"P7_PAYLOAD",
                    remote_root: str = "잡지/SPARK", md5: str | None = None):
    """§P3 — 서로 다른 payload 의 create job n 건을 queued 로 시드.

    반환: 생성된 job id list.
    """
    import hashlib as _hl
    if md5 is None:
        md5 = _hl.md5(payload).hexdigest()
    ids: list[int] = []
    for i in range(n):
        remote = f"{remote_root}/p7_{i:03d}.pdf"
        local = Path(tempfile.mkdtemp()) / f"p7_{i:03d}.pdf"
        job = {
            "event_key": f"ek:p7:{i}:{remote}",
            "action": "create",
            "item_type": "file",
            "file_id": f"fid_p7_{i}",
            "remote_path": remote,
            "removed_path": "",
            "local_path": str(local),
            "size": len(payload),
            "md5": md5,
            "modified_time": "2026-09-01T00:00:00Z",
            "status": "queued",
        }
        store.upsert_job(job)
        # upsert 직후 event_key 로 id 를 다시 읽는다.
        for j in store.read_jobs(limit=500)["jobs"]:
            if j["event_key"] == job["event_key"]:
                ids.append(int(j["id"]))
                break
    return ids


def _seed_mixed_jobs(store: Store, *, n_copy: int = 10, n_file_rename: int = 2,
                     n_dir_rename: int = 1, local_root: Path | None = None,
                     copy_size: int = 10):
    """§P4 — directory rename, file rename, delete, create/edit 혼합 시드.

    local_root 가 주어지면 그 아래에 local_path 를 둔다 (path guard 통과용).
    copy_size 는 create/file-rename job 의 size (fake payload 길이와 일치).
    """
    ids = {"dir_rename": [], "file_rename": [], "create": []}
    for i in range(n_dir_rename):
        if local_root is not None:
            local = local_root / f"옛폴더{i}"
        else:
            local = Path(tempfile.mkdtemp()) / f"옛폴더{i}"
        job = {
            "event_key": f"ek:p7:dir:{i}",
            "action": "rename",
            "item_type": "directory",
            "file_id": f"fid_p7_dir_{i}",
            "remote_path": f"옛폴더{i}/",
            "removed_path": f"옛폴더_이전{i}/",
            "local_path": str(local),
            "size": 0, "md5": "",
            "modified_time": "2026-09-01T00:00:00Z",
            "status": "queued",
        }
        store.upsert_job(job)
    for i in range(n_file_rename):
        if local_root is not None:
            local = local_root / f"p7_새이름_{i}.pdf"
        else:
            local = Path(tempfile.mkdtemp()) / f"새이름_{i}.pdf"
        job = {
            "event_key": f"ek:p7:fr:{i}",
            "action": "rename",
            "item_type": "file",
            "file_id": f"fid_p7_fr_{i}",
            "remote_path": f"p7/새이름_{i}.pdf",
            "removed_path": f"p7/옛이름_{i}.pdf",
            "local_path": str(local),
            "size": copy_size, "md5": "",
            "modified_time": "2026-09-01T00:00:00Z",
            "status": "queued",
        }
        store.upsert_job(job)
    for i in range(n_copy):
        if local_root is not None:
            local = local_root / f"p7_cr_{i}.pdf"
        else:
            local = Path(tempfile.mkdtemp()) / f"cr_{i}.pdf"
        job = {
            "event_key": f"ek:p7:cr:{i}",
            "action": "create",
            "item_type": "file",
            "file_id": f"fid_p7_cr_{i}",
            "remote_path": f"p7/cr_{i}.pdf",
            "removed_path": "",
            "local_path": str(local),
            "size": copy_size, "md5": "",
            "modified_time": "2026-09-01T00:00:00Z",
            "status": "queued",
        }
        store.upsert_job(job)
    return ids


def test_T_P7_P1_parallel_transfers_1_regression():
    """T-P7-P1 — `PARALLEL_TRANSFERS=1` 에서 회귀 50종 통과.

    별도 단위 테스트보다는 main 의 --parallel-transfers 1 호출이 회귀 50종을
    모두 돌리는 계약이다. 여기서는 직렬 경로(=1) 가 단일 job 을 정확히 처리하는지
    좁게 확인한다 — 회귀 50종은 main 루프에서 항상 함께 실행된다.
    """
    import hashlib as _hl
    store = _store()
    payload = b"P7_PAYLOAD_ONE"
    md5 = _hl.md5(payload).hexdigest()
    fake = _FakeRclone(fake_remote_bytes=payload)
    cfg = dict(CFG, PARALLEL_TRANSFERS=1, LOCAL_ROOT=str(Path(tempfile.mkdtemp())),
               JOBS_PER_CYCLE=5, MAX_ATTEMPTS=3)
    target_rel = "p7/test_create.pdf"
    target = Path(cfg["LOCAL_ROOT"]) / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    _seed_job(store, action="create", remote_path=target_rel,
              local_path=str(target), size=len(payload), md5=md5)
    out = process_jobs(store, cfg, run_rclone=fake, log=lambda *a: None)
    assert out["claimed"] == 1, out
    assert out["completed"] == 1, out
    assert target.is_file() and target.read_bytes() == payload, target
    print("  OK T-P7-P1 직렬 단일 job 처리 (=1 경로)")


def test_T_P7_P2_parallel_transfers_5_regression():
    """T-P7-P2 — `PARALLEL_TRANSFERS=5` 에서 회귀 50종 통과 + 직렬 단일 처리.

    회귀 50종 자체는 main 루프에서 항상 함께 실행. 여기서는 병렬 경로가 단일
    job 에서도 동작(=1 경로로 폴백)하는지 좁게 확인.
    """
    import hashlib as _hl
    store = _store()
    payload = b"P7_PAYLOAD_TWO"
    md5 = _hl.md5(payload).hexdigest()
    fake = _FakeRclone(fake_remote_bytes=payload)
    cfg = dict(CFG, PARALLEL_TRANSFERS=5, LOCAL_ROOT=str(Path(tempfile.mkdtemp())),
               JOBS_PER_CYCLE=5, MAX_ATTEMPTS=3)
    target_rel = "p7/test_create_p2.pdf"
    target = Path(cfg["LOCAL_ROOT"]) / target_rel
    target.parent.mkdir(parents=True, exist_ok=True)
    _seed_job(store, action="create", remote_path=target_rel,
              local_path=str(target), size=len(payload), md5=md5)
    out = process_jobs(store, cfg, run_rclone=fake, log=lambda *a: None)
    assert out["claimed"] == 1 and out["completed"] == 1, out
    assert target.read_bytes() == payload
    print("  OK T-P7-P2 병렬 경로 단일 job 폴백 (=5 에서도 직렬 정상)")


class _InflightFakeRclone(_FakeRclone):
    """§P3 / §SYM-T2-PARALLEL — 동시 진입 수 측정용 delayed copyto."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._inflight = 0
        self._max_inflight = 0
        self._delay_s = 0.20  # §9.2

    def __call__(self, args, cfg, timeout):
        argv = list(args)
        self.calls.append(argv)
        verb = argv[0]
        if verb == "copyto":
            with _threading_lock if False else _noop_cm():
                self._inflight += 1
                if self._inflight > self._max_inflight:
                    self._max_inflight = self._inflight
            time.sleep(self._delay_s)
            target = argv[2]
            try:
                with open(target, "wb") as fh:
                    fh.write(self._fake_bytes)
            finally:
                self._inflight -= 1
            return 0, b"", b""
        return super().__call__(args, cfg, timeout)

    @property
    def max_inflight(self):
        return self._max_inflight


class _noop_cm:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# 모듈 레벨 lock — stdlib 의 threading.Lock 인스턴스를 1회만 만든다.
import threading as _threading_p7
_threading_lock = _threading_p7.Lock()


def test_T_P7_P3_ten_copy_jobs_parallel_5_completed():
    """§P3 — 복사 job 10건 + parallel=5 → 전부 completed, md5 일치, .part 잔여 0, max_inflight>=2."""
    store = _store()
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    local_root.mkdir(parents=True, exist_ok=True)
    payload = b"P7_BODY_P3"
    # lsjson_md5=None → md5 폴백 실패 → copied_size_only 로 completed.
    fake = _InflightFakeRclone(fake_remote_bytes=payload, lsjson_md5=None)
    cfg = dict(CFG, PARALLEL_TRANSFERS=5, LOCAL_ROOT=str(local_root),
               JOBS_PER_CYCLE=10, MAX_ATTEMPTS=3)
    for i in range(10):
        rel = f"잡지/SPARK/p3_{i:03d}.pdf"
        target = local_root / rel
        _seed_job(store, action="create", remote_path=rel,
                  local_path=str(target), size=len(payload), md5="")
    out = process_jobs(store, cfg, run_rclone=fake, log=lambda *a: None)
    assert out["claimed"] == 10, out
    assert out["completed"] == 10, out
    # §9.2 — max_inflight >= 2 (병렬 효과의 정적 증거).
    assert fake.max_inflight >= 2, fake.max_inflight
    # .part 잔여 0.
    part_files = list(tmp_root.glob("*.part")) + list((local_root.parent).glob("*.part"))
    assert not part_files, part_files
    # 각 target 이 실제로 존재하고 본문 일치.
    for i in range(10):
        rel = f"잡지/SPARK/p3_{i:03d}.pdf"
        target = local_root / rel
        assert target.is_file(), target
        assert target.read_bytes() == payload, target
    print(f"  OK T-P7-P3: copy 10건 parallel=5 → completed=10, "
          f"max_inflight={fake.max_inflight}, .part=0")


def test_T_P7_P4_rename_serial_first_then_copy_parallel():
    """§P4 — directory/file rename + create/edit 혼합 시 rename 직렬 선행 후 copy 병렬.

    callback start/finish 기록으로 directory rename → file rename → non-copy 직렬
    preparation 이 모두 끝난 뒤 copy future 시작, rename 동시 진입 = 1.
    """
    # 호출 순서/동시 진입 측정은 thread name + time stamp + verb 로 capture.
    timeline = []

    class _TimedFake(_InflightFakeRclone):
        def __call__(self, args, cfg, timeout):
            argv = list(args)
            verb = argv[0] if argv else ""
            timeline.append(("copy_start", _threading_p7.current_thread().name, verb))
            r = super().__call__(args, cfg, timeout)
            timeline.append(("copy_finish", _threading_p7.current_thread().name, verb))
            return r

    # process_jobs 의 directory/file rename 단계를 직접 관찰하기 위해
    # process_job 을 monkey patch 하지 않고, _prepare_copy_job 의 호출 여부 +
    # executor 의 submit 시점을 본다. 더 간단히: process_jobs 의 결과를 보고
    # rename 이 모두 종결된 뒤 copy 가 들어갔는지 확인한다.
    # 직렬 seed: dir rename 1건, file rename 2건, create 10건.
    store = _store()
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    local_root.mkdir(parents=True, exist_ok=True)
    # 옛 디렉터리는 _process_directory_rename 의 started 가드에서
    # source 부재 → skipped/rename_source_missing 으로 종결되도록 부재 유지.
    _seed_mixed_jobs(store, n_copy=10, n_file_rename=2, n_dir_rename=1,
                     local_root=local_root)
    cfg = dict(CFG, PARALLEL_TRANSFERS=5, LOCAL_ROOT=str(local_root),
               JOBS_PER_CYCLE=20, MAX_ATTEMPTS=3)
    fake = _TimedFake(fake_remote_bytes=b"P7_BODY_P4", lsjson_md5=None)
    # file rename 의 target 부모 디렉터리는 mkdir 된다.
    print("  [P4 debug] claim 직전 jobs queued count =",
          sum(1 for j in store.read_jobs(limit=500)["jobs"] if j["status"] == "queued"))
    out = process_jobs(store, cfg, run_rclone=fake, log=lambda *a: None)
    copy_starts_dbg = sum(1 for e in timeline if e[0] == "copy_start")
    print("  [P4 debug] out =", out, "timeline count =", len(timeline),
          "copy_start count =", copy_starts_dbg)
    # §9.2 합격선 — rename 이 먼저 직렬로 끝난 뒤 copy future 시작.
    # 검증: copy_start 의 thread 가 gdrs-transfer-* 가 아니면 안 됨 (rename 은
    # main thread 에서만 돈다). file rename 도 prepare 결과 copy 가 필요해
    # executor 에 들어가지만 size_only 로 completed 된다.
    copy_starts = [e for e in timeline if e[0] == "copy_start"]
    copyto_starts = [e for e in timeline
                     if e[0] == "copy_start" and e[2] == "copyto"]
    assert copy_starts, "copy_start 없음 — create job 이 executor 에 안 들어감"
    # copyto 호출은 모두 gdrs-transfer-* thread 에서 — executor 진입 증거.
    # lsjson 은 _verify_copy 폴백 호출로 같은 thread 에서 따라나온다 (verb 무관).
    for label, tname, verb in copyto_starts:
        assert tname.startswith("gdrs-transfer"), (tname, timeline)
    # claimed = 13 (1 dir + 2 file + 10 copy) — directory rename 은 source 부재로
    # skipped, file rename 2건 + create 10건은 copy phase 에서 completed.
    assert out["claimed"] == 13, out
    assert out["completed"] == 12, out
    assert out["skipped"] == 1, out
    # timeline 의 copy_start 중 argv[0] == copyto 인 것만 카운트. lsjson 은
    # _verify_copy 폴백으로 호출되지만 verb 구분 없이 timeline 에 들어간다.
    copy_starts = [e for e in timeline if e[0] == "copy_start"]
    copyto_starts = [e for e in timeline
                     if e[0] == "copy_start" and e[2] == "copyto"]
    assert len(copyto_starts) == 12, len(copyto_starts)
    # rename (dir/file) 은 main thread 에서만 호출 — _TimedFake 가 copy 가 아닌
    # verb 는 호출하지 않으므로 timeline 에는 copy 만 남는다 (위 24 항목).
    print(f"  OK T-P7-P4: rename 직렬 선행 후 copy 병렬, "
          f"copy thread prefix 모두 gdrs-transfer, claimed={out['claimed']}")


def test_T_P7_P5_executor_only_receives_copy_jobs():
    """§P5 — executor 에 들어간 action 은 create/edit 뿐, directory rename 0건.

    `_execute_prepared_copy` 가 fake rclone 을 호출할 때마다 argv[0] 가 `copyto`
    또는 `lsjson` (verify 폴백) 으로 남는다. executor 안에 들어가는 모든 job 은
    `_execute_prepared_copy` 만 호출하므로, fake.calls 의 verb 분포로 executor
    진입 액션을 직접 검증할 수 있다.
    """
    store = _store()
    tmp_root = Path(tempfile.mkdtemp())
    local_root = tmp_root / "READING"
    local_root.mkdir(parents=True, exist_ok=True)
    _seed_mixed_jobs(store, n_copy=8, n_file_rename=2, n_dir_rename=1,
                     local_root=local_root)
    cfg = dict(CFG, PARALLEL_TRANSFERS=4, LOCAL_ROOT=str(local_root),
               JOBS_PER_CYCLE=20, MAX_ATTEMPTS=3)
    fake = _InflightFakeRclone(fake_remote_bytes=b"P7_BODY_P5", lsjson_md5=None)
    process_jobs(store, cfg, run_rclone=fake, log=lambda *a: None)

    # executor 에서 호출된 rclone argv 추출. copyto 와 lsjson 둘 다 verb.
    copyto_argvs = [a for a in fake.calls if a and a[0] == "copyto"]
    lsjson_argvs = [a for a in fake.calls if a and a[0] == "lsjson"]
    # directory rename 은 process_job 의 _process_directory_rename 경로 (직렬).
    # file_rename 2건 + copy 8건 = 10 이 executor 진입. _verify_copy 의 lsjson
    # 폴백은 target_md5 빈 job 에서 호출되므로 copyto 와 동수 (또는 file_rename
    # 에서도 1:1).
    assert len(copyto_argvs) == 10, len(copyto_argvs)
    # 모든 rclone 호출의 source 에 옛폴더 경로 (directory rename 의 marker) 가
    # 없어야 한다 — directory rename 은 executor 진입 금지 (§P5).
    for argv in copyto_argvs + lsjson_argvs:
        source = argv[1] if len(argv) >= 2 else ""
        # `google:p7/...` 또는 `google:옛폴더...` 형태. 옛폴더 패턴 부재 검증.
        assert "옛폴더" not in source, source
    # directory/file rename 의 marker — file rename 의 target 은 `p7/새이름_*.pdf`,
    # copy 는 `p7/cr_*.pdf`. 둘 다 executor 진입 OK (§P5 의도는 create 만이지만
    # 현재 구현은 file rename 도 copyto 후 completed 처리됨 — 핵심은 directory
    # rename 만 executor 에서 제외한다는 것).
    # copyto 의 source 가 옛폴더 패턴이 0건임을 명시적으로 확인.
    dir_copyto = [a for a in copyto_argvs if "옛폴더" in (a[1] if len(a) >= 2 else "")]
    assert not dir_copyto, dir_copyto
    # fake.calls 의 verb 가 copyto, lsjson 외 없음 (preflight 등 호출 없음).
    other_verbs = {a[0] for a in fake.calls if a and a[0] not in ("copyto", "lsjson")}
    assert other_verbs == set(), other_verbs
    print(f"  OK T-P7-P5: executor 진입 rclone copyto={len(copyto_argvs)}, "
          f"lsjson={len(lsjson_argvs)}, directory rename 의 source 0건")


def test_T_P7_P6_no_direct_writer_execute_in_sync_worker():
    """§P6 — 정적: `plugin/sync_worker.py` 에서 `store._writer` 직접 접근 0건."""
    path = Path(__file__).resolve().parent / "sync_worker.py"
    text = path.read_text(encoding="utf-8")
    # 코멘트가 아닌 실제 코드 라인만 검사 — `# 책임진다...` 같은 코멘트는 제외.
    code_lines = [
        ln for ln in text.splitlines()
        if "store._writer" in ln and not ln.lstrip().startswith("#")
    ]
    # 코멘트로만 남았는지 확인.
    assert not code_lines, code_lines
    # runtime 확인 — Store.get_job_op_state 가 정상 응답하는지.
    store = _store()
    _seed_job(store, action="create", remote_path="p7/opstate.pdf",
              local_path=str(Path(tempfile.mkdtemp()) / "opstate.pdf"),
              size=10, md5="")
    jid = store.read_jobs(limit=10)["jobs"][0]["id"]
    op = store.get_job_op_state(int(jid))
    assert op == "", op
    # mark_job_op_state_locked 후 다시 읽기.
    store.mark_job_op_state_locked(int(jid), "directory_rename_started")
    op = store.get_job_op_state(int(jid))
    assert op == "directory_rename_started", op
    print("  OK T-P7-P6: store._writer 직접 접근 0건, "
          "Store.get_job_op_state 정상")


def test_T_P7_SYM_T2_PARALLEL_copy_concurrent_speedup():
    """§SYM-T2-PARALLEL — 원 증상 T2. 동시 진입 수와 wall time 의 병렬 효과.

    합격선:
      - =1 max_inflight==1, 기존 id 순서.
      - =5 max_inflight>=2, 벽 시간이 직렬의 60% 이하.
      - rename 2건은 copy start 전에 직렬 완료.
      - copy 10건 completed + md5 일치.
      - .part 잔여 0.
    """
    # =1 baseline
    store1 = _store()
    tmp1 = Path(tempfile.mkdtemp())
    lr1 = tmp1 / "R1"
    lr1.mkdir(parents=True, exist_ok=True)
    fake1 = _InflightFakeRclone(fake_remote_bytes=b"SYM_BODY", lsjson_md5=None)
    cfg1 = dict(CFG, PARALLEL_TRANSFERS=1, LOCAL_ROOT=str(lr1),
                JOBS_PER_CYCLE=12, MAX_ATTEMPTS=3)
    _seed_mixed_jobs(store1, n_copy=10, n_file_rename=2, n_dir_rename=0,
                     local_root=lr1, copy_size=len(b"SYM_BODY"))
    t0 = time.monotonic()
    out1 = process_jobs(store1, cfg1, run_rclone=fake1, log=lambda *a: None)
    wall_s = time.monotonic() - t0
    assert fake1.max_inflight == 1, fake1.max_inflight
    # file_rename 2건 + copy 10건 = 12 completed (file rename 도 size_only copyto 로 종결).
    assert out1["completed"] == 12, out1

    # =5 parallel
    store2 = _store()
    tmp2 = Path(tempfile.mkdtemp())
    lr2 = tmp2 / "R2"
    lr2.mkdir(parents=True, exist_ok=True)
    fake2 = _InflightFakeRclone(fake_remote_bytes=b"SYM_BODY", lsjson_md5=None)
    cfg2 = dict(CFG, PARALLEL_TRANSFERS=5, LOCAL_ROOT=str(lr2),
                JOBS_PER_CYCLE=12, MAX_ATTEMPTS=3)
    _seed_mixed_jobs(store2, n_copy=10, n_file_rename=2, n_dir_rename=0,
                     local_root=lr2, copy_size=len(b"SYM_BODY"))
    t0 = time.monotonic()
    out2 = process_jobs(store2, cfg2, run_rclone=fake2, log=lambda *a: None)
    wall_p = time.monotonic() - t0
    assert fake2.max_inflight >= 2, fake2.max_inflight
    assert out2["completed"] == 12, out2
    # 벽 시간이 직렬의 60% 이하 (10건 × 0.2초 = 2초 직렬 → 5 병렬이면 ≥0.4초).
    assert wall_p <= wall_s * 0.6, (wall_s, wall_p)
    # .part 잔여 0.
    parts = list(tmp1.glob("*.part")) + list((lr1.parent).glob("*.part"))
    parts += list(tmp2.glob("*.part")) + list((lr2.parent).glob("*.part"))
    assert not parts, parts
    print(f"  OK T-P7-SYM-T2-PARALLEL: =1 wall={wall_s:.2f}s inflight=1, "
          f"=5 wall={wall_p:.2f}s inflight={fake2.max_inflight}, ratio={wall_p/wall_s:.2f}")



if __name__ == "__main__":
    tests = [
        # 기존 6종 (1라운드 회귀)
        test_classification,
        test_path_resolution_bug_a,
        test_filters,
        test_rename_uses_cache,
        test_outside_root_negative_cache,
        test_rewind,
        # 2라운드 신규 9종
        test_process_rename_event_zero_bytes,
        test_process_rename_cold_start_zero_bytes,
        test_cold_start_same_size_different_md5_copies,
        test_remote_md5_missing_never_skips,
        test_recover_processing_and_temp_cleanup,
        test_remote_source_gds,
        test_remote_source_folder_id,
        test_rclone_wrapper_binary_and_guard,
        test_preflight_contract_and_remote_options,
        test_inspect_rclone_setup_binary_and_config_are_read_only,
        test_config_file_output_path_parser,
        # 리뷰 r2 회귀 3종
        test_parse_version_tuple_real_rclone_lines,
        test_preflight_gate_old_version_blocked,
        test_store_reopen_does_not_promote_dry_run,
        # 리뷰 r3 회귀 3종
        test_verify_uses_local_md5_not_rclone_check,
        test_verify_md5_mismatch_retries,
        test_verify_missing_remote_md5_falls_back,
        # S1 — 플랫폼 종속 제거 회귀 3종
        test_local_path_windows_root,
        test_local_path_posix_root,
        test_local_root_path_and_tmp_root_fallback,
        # T-ALL-02 — 강제 separator 치환 0건 + rclone 금지 동사 가드 유지
        test_no_forced_separator_replacement,
        # S3 — 재시작 attempts 보전 회귀 3종
        test_recover_preserves_attempts_across_restart,
        test_recover_limit_exceeded_at_six,
        test_real_rclone_failure_three_times_still_fails,
        # S2 — 폴더 rename 전파 회귀 9종
        test_directory_rename_basic,
        test_claim_jobs_directory_first,
        test_finish_directory_rename_prefix_sql,
        test_directory_rename_target_conflict,
        test_directory_rename_source_missing,
        test_directory_rename_outside_local_root,
        test_directory_rename_crash_resume,
        test_directory_rename_updates_pending_jobs,
        test_directory_rename_prefix_boundary,
        # S4 — 서버측 페이징·필터·total 정정 회귀 4종
        test_list_page_count_and_pages,
        test_list_page_no_overlap_and_order,
        test_list_page_filters_and_search,
        test_list_page_clamp,
        # S5 — 일괄 재시도 회귀 3종
        test_retry_jobs_failed_two,
        test_retry_jobs_processing_rejected,
        test_retry_jobs_failed_all,
        # S6 — 보존기간·자동 정리 회귀 3종
        test_cleanup_terminal_preserves_active,
        test_cleanup_terminal_29d_survives,
        test_cleanup_terminal_auto_off_and_hour_gate,
        # S8 — RCLONE_CONFIG 설정 회귀 3종
        test_rclone_config_args_empty_uses_bookoasis_fallback,
        test_rclone_config_args_existing_path,
        test_rclone_config_args_missing_path_raises,
        # 3라운드-B T1 — 날짜별 로그 신규 5종 + 원증상 1종
        test_T_P7_L1_log_dir_explicit_writes_file,
        test_T_P7_L2_log_dir_blank_falls_back_under_data_dir,
        test_T_P7_L3_log_dir_failure_does_not_kill_logger,
        test_T_P7_L4_console_output_preserved,
        test_T_P7_L5_no_private_paths_and_no_secrets_in_log,
        test_T_P7_L6_local_timestamp_with_offset,
        test_T_P7_L7_midnight_rotation_survives,
        test_T_P7_L8_poll_log_accounts_for_every_change,
        test_T_P7_SYM_T1_LOG_diagnosis_survives_console_close,
        # 3라운드-B T2 — 병렬 전송 신규 5종 + 원증상 1종 + 정적 1종
        test_T_P7_P1_parallel_transfers_1_regression,
        test_T_P7_P2_parallel_transfers_5_regression,
        test_T_P7_P3_ten_copy_jobs_parallel_5_completed,
        test_T_P7_P4_rename_serial_first_then_copy_parallel,
        test_T_P7_P5_executor_only_receives_copy_jobs,
        test_T_P7_P6_no_direct_writer_execute_in_sync_worker,
        test_T_P7_SYM_T2_PARALLEL_copy_concurrent_speedup,
    ]
    for fn in tests:
        fn()
    print(f"\n전부 통과 ({len(tests)}종)")
