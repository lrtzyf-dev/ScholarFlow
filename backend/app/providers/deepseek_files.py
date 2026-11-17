"""DeepSeek Files transport, official OpenAI-compatible contract 2026-11-15."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import re
import time
import httpx
from ..call_ledger import tracked_call
from ..request_resources import request_admission
from .deepseek_tools import _DeadlineStream

ENDPOINT = "https://api.deepseek.com/files"
PURPOSES = {"user_data", "text_recognition", "rich_layout_recognition"}


class FilesError(ValueError):
    def __init__(self, code, *, status_code=None, dispatched=False, call_id=None, retry_after_seconds=None):
        self.code, self.status_code, self.dispatched, self.call_id = code, status_code, dispatched, call_id
        self.retry_after_seconds = retry_after_seconds
        super().__init__({"authorization": "云文件授权已变化或撤销。", "http": "云文件服务拒绝请求，请核对权限、额度或文件格式。",
            "transport": "云文件请求中断，远端结果待核对，未自动重试。", "contract": "云文件返回结构不符合已核实协议，结果待核对。",
            "deadline": "云文件操作超时，远端结果待核对，未自动重试。"}.get(code, "云文件操作未完成。"))


def remote_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"file-[A-Za-z0-9_-]{1,200}", value):
        raise FilesError("contract")
    return value


def metadata(value):
    if (not isinstance(value, dict) or value.get("object") != "file" or type(value.get("bytes")) is not int
            or value["bytes"] < 0 or type(value.get("created_at")) is not int or value["created_at"] < 0
            or not isinstance(value.get("filename"), str) or value.get("purpose") not in PURPOSES):
        raise FilesError("contract")
    result = {k: value[k] for k in ("id", "object", "bytes", "created_at", "filename", "purpose") if k in value}
    result["id"] = remote_id(value.get("id"))
    if "expires_at" in value:
        if type(value["expires_at"]) is not int or value["expires_at"] <= value["created_at"]:
            raise FilesError("contract")
        result["expires_at"] = value["expires_at"]
    return result


class FilesClient:
    def __init__(self, key, *, authorization_check, synthetic=False, transport=None):
        self.key, self.authorization_check, self.synthetic, self.transport = key, authorization_check, synthetic, transport
        self.dispatched = False
        self.last_call_id = None

    def _request(self, method, suffix="", *, data=None, files=None, params=None, seconds=120, parse=lambda value: value):
        url = ENDPOINT + suffix
        with request_admission(provider="api.deepseek.com", endpoint=url,
                credential_fingerprint=hashlib.sha256(self.key.encode()).hexdigest(), service="files_" + method.lower()) as admission:
            with tracked_call(provider="deepseek", service="files_" + method.lower(), endpoint=url, synthetic=self.synthetic) as call:
                self.last_call_id = call.id
                admission.bind_call(call.id)
                start = time.monotonic()
                def check():
                    try:
                        allowed = self.authorization_check() is True
                    except Exception:
                        allowed = False
                    if not allowed:
                        raise FilesError("authorization", dispatched=self.dispatched, call_id=call.id)
                    if time.monotonic() - start > seconds:
                        raise FilesError("deadline", dispatched=self.dispatched, call_id=call.id)
                try:
                    check()
                    with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
                                      trust_env=False, transport=self.transport) as client:
                        request = client.build_request(method, url, headers={"Authorization": "Bearer " + self.key,
                            "Accept": "application/json", "Accept-Encoding": "identity"}, data=data, files=files, params=params)
                        request.stream = _DeadlineStream(request.stream, check)
                        check()
                        self.dispatched = True
                        with closing(request.stream), closing(client.send(request, stream=True)) as response:
                            if not 200 <= response.status_code < 300:
                                retry = response.headers.get("retry-after", "")
                                delay = min(int(retry), 3600) if retry.isascii() and retry.isdigit() and len(retry) <= 6 else None
                                raise FilesError("http", status_code=response.status_code, dispatched=True, call_id=call.id, retry_after_seconds=delay)
                            raw = bytearray()
                            for part in response.iter_bytes():
                                check()
                                if len(raw) + len(part) > 2 * 1024 * 1024:
                                    raise FilesError("contract", dispatched=True, call_id=call.id)
                                raw.extend(part)
                            check()
                    result = json.loads(raw)
                    if not isinstance(result, dict) or result.get("error") is not None:
                        raise FilesError("contract", dispatched=True, call_id=call.id)
                    result = parse(result)
                    call.finish()
                    return result
                except FilesError as exc:
                    exc.dispatched = exc.dispatched or self.dispatched
                    exc.call_id = exc.call_id or call.id
                    call.finish(state="unknown" if exc.dispatched else "failed", error_kind=exc.code)
                    raise
                except (httpx.HTTPError, ValueError, UnicodeError):
                    call.finish(state="unknown" if self.dispatched else "failed", error_kind="transport")
                    raise FilesError("transport", dispatched=self.dispatched, call_id=call.id) from None

    def upload(self, raw, filename, purpose, ttl):
        # HTTPX emits data fields before files. Never place purpose after file.
        return self._request("POST", data={"purpose": purpose, "expires_after[anchor]": "created_at",
            "expires_after[seconds]": str(ttl)}, files={"file": (filename, raw, "application/octet-stream")},
            seconds=600 if purpose == "user_data" else 120, parse=metadata)

    def retrieve(self, file_id):
        return self._request("GET", "/" + remote_id(file_id), parse=metadata)

    def delete(self, file_id):
        file_id = remote_id(file_id)
        def parse(result):
            if result.get("id") != file_id or result.get("deleted") is not True:
                raise FilesError("contract", dispatched=True, call_id=self.last_call_id)
            return {"id": file_id, "deleted": True}
        try:
            return self._request("DELETE", "/" + file_id, parse=parse)
        except FilesError as exc:
            if exc.status_code == 404:
                return {"id": file_id, "deleted": True, "absent": True}
            raise

    def list(self, *, purpose, after=None):
        params = {"purpose": purpose, "limit": 100, "order": "desc"}
        if after:
            params["after"] = remote_id(after)
        def parse(value):
            if not isinstance(value.get("data"), list) or len(value["data"]) > 100 or type(value.get("has_more")) is not bool:
                raise FilesError("contract", dispatched=True, call_id=self.last_call_id)
            return {"data": [metadata(v) for v in value["data"]], "has_more": value["has_more"],
                    "next_cursor": remote_id(value.get("last_id")) if value["has_more"] else None}
        return self._request("GET", params=params, parse=parse)
