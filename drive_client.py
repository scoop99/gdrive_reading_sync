# -*- coding: utf-8 -*-
"""Google Drive Changes API minimal client (stdlib urllib only).

인증 토큰은 utils.rclone_gdrive_copy.get_access_token() 이 발급한다.
GET 전용 — Drive 에 쓰는 호출은 이 파일에 없다.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request


API_ROOT = "https://www.googleapis.com/drive/v3"

# changes / files.get 공통 필드. size·md5 는 2단계 중복 판정(D1)에 그대로 쓴다.
FILE_FIELDS = "id,name,mimeType,size,md5Checksum,modifiedTime,createdTime,parents,trashed"


class DriveAPIError(RuntimeError):
    """Drive API 가 2xx 가 아니거나 응답을 해석할 수 없을 때."""


class DriveClient:
    def __init__(self, access_token: str):
        self._token = access_token

    def _request(self, endpoint: str, params: dict | None = None) -> dict:
        qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{API_ROOT}/{endpoint}"
        if qs:
            url = f"{url}?{qs}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {self._token}")
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            raise DriveAPIError(f"HTTP {exc.code} on {endpoint}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise DriveAPIError(f"URL error on {endpoint}: {exc.reason}") from exc
        try:
            return json.loads(body.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DriveAPIError(f"non-JSON response from {endpoint}") from exc

    def start_page_token(self) -> str:
        data = self._request(
            "changes/startPageToken",
            params={"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"},
        )
        return str(data.get("startPageToken") or "")

    def list_changes(self, page_token: str) -> dict:
        return self._request(
            "changes",
            params={
                "pageToken": page_token,
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                "includeRemoved": "true",
                "spaces": "drive",
                "fields": f"newStartPageToken,nextPageToken,changes(fileId,removed,time,file({FILE_FIELDS}))",
                "pageSize": 200,
            },
        )

    def get_file(self, file_id: str) -> dict:
        return self._request(
            f"files/{file_id}",
            params={
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
                "fields": FILE_FIELDS,
            },
        )
