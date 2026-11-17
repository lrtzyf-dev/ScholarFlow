"""Independent DeepSeek Search/Fetch/OCR protocol, not hosted model tools.

Contract: cached official Search/OCR guides, checked 2026-11-14. Callers must
authorize external transmission *before* invoking this adapter and supply a
dedicated, explicit credential. No chat configuration, filesystem access,
permission grants, automatic paid retries, or source-store writes occur here.

All returned text is untrusted data. Search summaries are discovery material;
web content has unknown original-page coverage. OCR coordinates stay in the
provider's 0..1000 space until a separate source mapper verifies dimensions.
OCR reference tokens never become chat usage or token-based OCR charges.
"""
from __future__ import annotations

import ipaddress
import hashlib
import json
import time
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx

from ..call_ledger import tracked_call


ENDPOINTS = MappingProxyType({name: f"https://api.deepseek.com/v1/tools/{name}" for name in ("search", "fetch", "ocr")})
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
OCR_PURPOSES = {"text_recognition", "rich_layout_recognition"}
_MESSAGES = {
    "invalid_request": "请求参数或文件无法处理，请检查格式、加密状态及页数限制。",
    "authentication": "独立工具的 DeepSeek 密钥无效或缺失，请检查该工具的凭据。",
    "insufficient_balance": "DeepSeek 账号余额不足。",
    "forbidden": "DeepSeek 拒绝访问该网页，请检查目标地址和访问策略。",
    "not_found": "目标网页没有可提取的正文或当前不可访问。",
    "timeout": "DeepSeek 工具请求超时；未自动重试，请检查任务状态后决定是否重试。",
    "too_large": "文件或请求超过允许大小，请缩小后再提交。",
    "rate_limited": "DeepSeek 工具当前达到账号限流，请稍后再试。",
    "unavailable": "DeepSeek 工具暂时不可用，请稍后检查。",
    "redirect": "工具接口返回重定向，已停止，密钥未被转发。",
    "invalid_response": "工具返回的数据不符合协议，无法作为可靠来源使用。",
    "non_json": "工具没有返回有效的 JSON 结果。",
    "response_too_large": "工具响应超过本地大小限制，结果未被当作完整来源。",
    "empty_content": "网页没有返回正文，不能用摘要代替原文证据。",
    "interrupted": "连接在结果确认前中断；未自动重试，实际完成和费用需核实。",
    "upstream_error": "DeepSeek 工具报告执行失败，未自动重试。",
    "authorization_revoked": "独立服务授权已撤销或发生变化，已停止本地传输；请求若已发出，服务端可能继续执行或计费，未自动重试。",
}
_STATUS = {400: "invalid_request", 401: "authentication", 402: "insufficient_balance", 403: "forbidden",
           404: "not_found", 408: "timeout", 413: "too_large", 429: "rate_limited", 500: "unavailable",
           502: "unavailable", 503: "unavailable", 504: "timeout"}
_ERROR_CODES = {"invalid_request_error": "invalid_request", "authentication_error": "authentication",
                "insufficient_balance": "insufficient_balance", "rate_limit_error": "rate_limited",
                "gateway_timeout_error": "timeout", "internal_server_error": "unavailable",
                "service_unavailable_error": "unavailable"}


@dataclass(frozen=True)
class ToolCallContext:
    run_id: str | None = None
    attempt_id: str | None = None
    parent_call_id: str | None = None
    request_id: str | None = None
    retry_of_call_id: str | None = None


class DeepSeekToolsError(RuntimeError):
    """Only stable, safe diagnostics; never provider error text or a request."""
    def __init__(self, service, code, *, status_code=None, call_id=None,
                 may_be_billed=None, retry_after_seconds=None):
        self.service, self.code = service, code
        self.status_code, self.call_id = status_code, call_id
        self.may_be_billed = may_be_billed
        self.upstream_may_continue = service == "ocr" and may_be_billed is None
        self.retryable = code in ("rate_limited", "unavailable", "timeout")
        self.retry_after_seconds = retry_after_seconds
        super().__init__(_MESSAGES.get(code, _MESSAGES["upstream_error"]))

    def to_dict(self):
        return {"service": self.service, "code": self.code, "message": str(self),
                "status_code": self.status_code, "call_id": self.call_id,
                "retryable": self.retryable, "automatic_retry": False,
                "retry_after_seconds": self.retry_after_seconds, "may_be_billed": self.may_be_billed,
                "upstream_may_continue": self.upstream_may_continue}


class _InvalidResponse(Exception):
    def __init__(self, code="invalid_response"):
        self.code = code


class _DeadlineStream(httpx.SyncByteStream):
    """Check continuous uploads too; HTTPX socket timeouts cover each blocked I/O."""
    def __init__(self, stream, check):
        self._stream, self._check = stream, check

    def __iter__(self):
        for chunk in self._stream:
            self._check()
            yield chunk
        self._check()

    def close(self):
        self._stream.close()


def _input_text(value, label, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError(f"{label}格式或长度无效")
    return value


def _url(value, *, output=False):
    try:
        if (not isinstance(value, str) or not value or len(value) > 8192
                or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value):
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        parsed.port
        if any(c in parsed.hostname for c in "%/#?"):
            raise ValueError()
    except (ValueError, TypeError):
        if output:
            raise _InvalidResponse() from None
        raise ValueError("网页地址必须是无用户名密码的有效 HTTP/HTTPS URL") from None
    return value


def _string(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise _InvalidResponse()
    return value


def _count(value, minimum=0, maximum=10**18):
    if type(value) is not int or not minimum <= value <= maximum:
        raise _InvalidResponse()
    return value


def _regions(text):
    regions, warnings = [], []
    position = 0
    while len(regions) < 2000:
        start = text.find("<｜ref｜>", position)
        end = text.find("<｜/ref｜>", start + len("<｜ref｜>")) if start >= 0 else -1
        if end < 0:
            break
        after = end + len("<｜/ref｜>")
        box_start = after
        while box_start < len(text) and text[box_start].isspace():
            box_start += 1
        if not text.startswith("<｜box｜>", box_start):
            position = after
            continue
        box_start += len("<｜box｜>")
        box_end = text.find("<｜/box｜>", box_start)
        if box_end < 0:
            break
        position = box_end + len("<｜/box｜>")
        try:
            boxes = json.loads(text[box_start:box_end])
            if not isinstance(boxes, list) or not 1 <= len(boxes) <= 2000:
                raise ValueError()
            for box in boxes:
                if (not isinstance(box, list) or len(box) != 4
                        or any(type(n) not in (int, float) or not 0 <= n <= 1000 for n in box)
                        or box[0] > box[2] or box[1] > box[3]):
                    raise ValueError()
            regions.append({"text": text[start + len("<｜ref｜>"):end], "boxes": boxes, "coordinate_space": "normalized_0_1000",
                            "source_coordinates_verified": False})
        except (ValueError, TypeError, OverflowError, RecursionError):
            warnings.append("invalid_normalized_bbox")
    if len(regions) >= 2000:
        warnings.append("layout_region_limit")
    return regions, list(dict.fromkeys(warnings))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError()


class DeepSeekToolsClient:
    def __init__(self, api_key: str, *, transport: httpx.BaseTransport | None = None,
                 timeout_seconds=180.0, max_response_bytes=16 * 1024 * 1024,
                 max_file_bytes=MAX_FILE_BYTES, synthetic=False, authorization_check=None):
        # Explicit only; importing this module never reads chat settings/keys.
        self._api_key = _input_text(api_key.strip() if isinstance(api_key, str) else api_key, "工具密钥", 4096)
        if not self._api_key.isascii() or any(c.isspace() for c in self._api_key):
            raise ValueError("工具密钥必须是有效的单行凭据")
        if type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 180:
            raise ValueError("工具超时必须在 0 到 180 秒之间")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= MAX_RESPONSE_BYTES:
            raise ValueError("响应大小限制无效")
        if type(max_file_bytes) is not int or not 1 <= max_file_bytes <= MAX_FILE_BYTES:
            raise ValueError("文件大小限制无效")
        if type(synthetic) is not bool:
            raise ValueError("合成调用标志必须为布尔值")
        if authorization_check is not None and not callable(authorization_check):
            raise ValueError("授权检查必须是可调用函数")
        self._transport, self._timeout = transport, float(timeout_seconds)
        self._max_response, self._max_file, self._synthetic = max_response_bytes, max_file_bytes, synthetic
        self._authorization_check = authorization_check

    def __repr__(self):
        return "DeepSeekToolsClient(api_key=<redacted>, endpoint=official)"

    def search(self, query: str, *, count=10, ori_question=None, localization="", need_content=False,
               context: ToolCallContext | None = None):
        query = _input_text(query, "搜索词", 128)
        if type(count) is not int or not 1 <= count <= 50 or type(need_content) is not bool:
            raise ValueError("搜索条数或正文标志无效")
        body = {"query": query, "count": count, "need_content": need_content}
        if ori_question is not None:
            body["ori_question"] = _input_text(ori_question, "原始问题", 128)
        if not isinstance(localization, str):
            raise ValueError("本地化地址必须是 IP 地址")
        if localization:
            try:
                ipaddress.ip_address(localization)
            except ValueError:
                raise ValueError("本地化地址必须是 IP 地址") from None
            body["localization"] = localization
        return self._request("search", context=context, json_body=body,
                             parse=lambda data: self._search_result(data, count, need_content))

    def fetch(self, url: str, *, context: ToolCallContext | None = None):
        url = _url(url)
        result = self._request("fetch", context=context, json_body={"url": url}, parse=self._fetch_result)
        result.update(requested_url=url, resolved_url_verified=False)
        return result

    def ocr(self, file_bytes: bytes, *, filename: str, purpose="text_recognition",
            context: ToolCallContext | None = None):
        if not isinstance(file_bytes, bytes) or not 1 <= len(file_bytes) <= self._max_file:
            raise ValueError("OCR 文件不能为空或超过本地文件大小限制")
        filename = _input_text(filename, "文件名", 255)
        if filename in (".", "..") or any(c in filename for c in '/\\:"'):
            raise ValueError("OCR 文件名必须是安全的单个文件名")
        if not isinstance(purpose, str) or purpose not in OCR_PURPOSES:
            raise ValueError("OCR purpose 无效")
        result = self._request("ocr", context=context, form={"purpose": purpose},
                             files={"file": (filename, file_bytes, "application/octet-stream")},
                             parse=lambda data: self._ocr_result(data, purpose))
        result["input_file"] = {"sha256": hashlib.sha256(file_bytes).hexdigest(), "bytes": len(file_bytes),
                                "filename": filename, "format_verified": False}
        return result

    def _request(self, service, *, context, parse, json_body=None, form=None, files=None):
        from ..request_resources import request_admission
        if context is not None and not isinstance(context, ToolCallContext):
            raise ValueError("工具调用上下文格式无效")
        context = context or ToolCallContext()
        with request_admission(provider="api.deepseek.com", endpoint=ENDPOINTS[service],
                    credential_fingerprint=hashlib.sha256(self._api_key.encode()).hexdigest(),
                    service=f"independent_{service}", run_id=context.run_id) as admission, tracked_call(provider="deepseek", service=f"independent_{service}", endpoint=ENDPOINTS[service],
                          run_id=context.run_id, attempt_id=context.attempt_id, parent_call_id=context.parent_call_id,
                          request_id=context.request_id, retry_of_call_id=context.retry_of_call_id,
                          synthetic=self._synthetic) as call:
            start = time.monotonic()
            dispatched = False

            def check_deadline():
                if self._authorization_check is not None:
                    try:
                        check = (getattr(self._authorization_check, "during_response", self._authorization_check)
                                 if dispatched else self._authorization_check)
                        authorized = check()
                    except Exception:
                        authorized = False
                    if authorized is not True:
                        raise DeepSeekToolsError(service, "authorization_revoked", call_id=call.id,
                            may_be_billed=None if dispatched else False)
                if time.monotonic() - start >= self._timeout:
                    raise DeepSeekToolsError(service, "timeout", call_id=call.id)

            try:
                check_deadline()
                idle = min(30.0, self._timeout)
                with httpx.Client(transport=self._transport, follow_redirects=False, trust_env=False,
                                  timeout=httpx.Timeout(idle, connect=min(10.0, idle), pool=min(10.0, idle))) as client:
                    request = client.build_request("POST", ENDPOINTS[service], headers={"Authorization": f"Bearer {self._api_key}",
                            "Accept": "application/json", "Accept-Encoding": "identity"},
                            json=json_body, data=form, files=files)
                    request.stream = _DeadlineStream(request.stream, check_deadline)
                    # Manual streaming closes both resources on every path.
                    # Deadline checks run between actual chunks; a blocked
                    # socket is additionally bounded by the idle I/O timeout.
                    check_deadline()
                    admission.bind_call(call.id)
                    dispatched = True
                    with closing(request.stream), closing(client.send(request, stream=True)) as response:
                        if response.status_code != 200:
                            code = "redirect" if 300 <= response.status_code < 400 else _STATUS.get(response.status_code, "upstream_error")
                            retry_after = response.headers.get("retry-after", "")
                            delay = min(int(retry_after), 3600) if retry_after.isascii() and retry_after.isdigit() and len(retry_after) <= 6 else None
                            raise DeepSeekToolsError(service, code, status_code=response.status_code, call_id=call.id,
                                                     may_be_billed=False, retry_after_seconds=delay)
                        media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if media_type != "application/json" and not media_type.endswith("+json"):
                            raise _InvalidResponse("non_json")
                        if response.headers.get("content-encoding", "identity").lower() != "identity":
                            raise _InvalidResponse()
                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            if not content_length.isascii() or not content_length.isdigit():
                                raise _InvalidResponse()
                            if len(content_length) > 12 or int(content_length) > self._max_response:
                                raise _InvalidResponse("response_too_large")
                        content = bytearray()
                        for chunk in response.iter_bytes():
                            check_deadline()
                            if len(content) + len(chunk) > self._max_response:
                                raise _InvalidResponse("response_too_large")
                            content.extend(chunk)
                        check_deadline()
                try:
                    data = json.loads(content, parse_constant=_invalid_constant, object_pairs_hook=_unique_object)
                except (ValueError, UnicodeError, RecursionError):
                    raise _InvalidResponse("non_json") from None
                if not isinstance(data, dict):
                    raise _InvalidResponse()
                if "error" in data:
                    error = data["error"]
                    if (not isinstance(error, dict) or not isinstance(error.get("message"), str)
                            or not isinstance(error.get("type"), str) or error.get("param") is not None
                            or (error.get("code") is not None and not isinstance(error.get("code"), str))):
                        raise _InvalidResponse()
                    code = _ERROR_CODES.get(error.get("code"), "upstream_error")
                    raise DeepSeekToolsError(service, code, status_code=200, call_id=call.id, may_be_billed=False)
                result, usage = parse(data)
                call.finish(usage=usage)
                return {"provider": "deepseek", "integration": "independent_tool_api", "service": service,
                        "call_id": call.id, "trust": "untrusted_external", "authorizes_actions": False,
                        "response_complete": True, **result}
            except DeepSeekToolsError as error:
                call.finish(state="failed" if error.may_be_billed is False else "unknown", error_kind=error.code)
                raise error from None
            except _InvalidResponse as error:
                call.finish(state="unknown", error_kind=error.code)
                raise DeepSeekToolsError(service, error.code, call_id=call.id) from None
            except httpx.TimeoutException:
                call.finish(state="unknown", error_kind="timeout")
                raise DeepSeekToolsError(service, "timeout", call_id=call.id) from None
            except httpx.HTTPError:
                call.finish(state="unknown", error_kind="interrupted")
                raise DeepSeekToolsError(service, "interrupted", call_id=call.id) from None
            except BaseException as error:
                if isinstance(error, (KeyboardInterrupt, GeneratorExit)) or type(error).__name__ == "CancelledError":
                    # A local cancellation is not a provider cancellation,
                    # especially for OCR which continues after disconnect.
                    call.finish(state="unknown", error_kind="client_cancelled")
                raise

    @staticmethod
    def _search_result(data, count, need_content):
        if not isinstance(data.get("results"), list) or len(data["results"]) > count:
            raise _InvalidResponse()
        results = []
        for raw in data["results"]:
            if not isinstance(raw, dict):
                raise _InvalidResponse()
            item = {name: _string(raw.get(name)) for name in ("title", "snippet", "summary", "content")}
            item["url"] = _url(raw.get("url"), output=True)
            score = raw.get("rerank_score")
            if type(score) not in (int, float) or not 0 <= score <= 3:
                raise _InvalidResponse()
            item["rerank_score"] = score
            item["authority"] = _count(raw.get("authority"), -1, 4)
            for name in ("site_name", "site_icon", "date"):
                item[name] = _string(raw.get(name), nullable=True)
            if item["site_icon"]:
                _url(item["site_icon"], output=True)
            if item["date"]:
                try:
                    date = datetime.fromisoformat(item["date"].replace("Z", "+00:00"))
                    if date.tzinfo is None or date.utcoffset().total_seconds() != 0:
                        raise ValueError()
                except ValueError:
                    raise _InvalidResponse() from None
            if not need_content and item["content"]:
                raise _InvalidResponse()
            item.update(result_type="search_result", summary_is_source_text=False,
                        content_type="web_extracted_text" if item["content"] else "unavailable",
                        source_completeness="unknown" if item["content"] else "unavailable",
                        trust="untrusted_external", authorizes_actions=False)
            results.append(item)
        return ({"result_type": "search_results", "status": "ok" if results else "empty", "results": results,
                 "original_page_coverage_verified": False,
                 "billing": {"unit": "tool_search", "count": int(bool(results)), "amount": None}},
                {"tool_search": int(bool(results))})

    @staticmethod
    def _fetch_result(data):
        title = _string(data.get("title", ""))
        content = _string(data.get("content"))
        if not content.strip():
            raise _InvalidResponse("empty_content")
        return ({"result_type": "web_extracted_text", "status": "ok", "title": title, "content": content,
                 "content_format": "markdown", "source_completeness": "unknown",
                 "original_page_coverage_verified": False,
                 "billing": {"unit": "tool_fetch", "count": 1, "amount": None}}, {"tool_fetch": 1})

    @staticmethod
    def _ocr_result(data, purpose):
        ident = _string(data.get("id"))
        if not ident or len(ident) > 256 or data.get("object") != "ocr.result" or data.get("purpose") != purpose:
            raise _InvalidResponse()
        created = _count(data.get("created"))
        count = _count(data.get("page_count"), 1, 1000)
        raw_pages = data.get("pages")
        if not isinstance(raw_pages, list) or len(raw_pages) != count:
            raise _InvalidResponse()
        pages = []
        for page in raw_pages:
            if not isinstance(page, dict):
                raise _InvalidResponse()
            index = _count(page.get("index"), 0, count - 1)
            text, finish = _string(page.get("text")), page.get("finish_reason")
            if finish not in ("stop", "length"):
                raise _InvalidResponse()
            regions, warnings = _regions(text)
            pages.append({"index": index, "index_base": 0, "text": text, "finish_reason": finish,
                          "truncated": finish == "length", "result_type": "ocr_text", "regions": regions,
                          "layout_warnings": warnings, "trust": "untrusted_external", "authorizes_actions": False})
        if sorted(page["index"] for page in pages) != list(range(count)):
            raise _InvalidResponse()
        raw_usage = data.get("usage")
        if not isinstance(raw_usage, dict):
            raise _InvalidResponse()
        reference_usage = {name: _count(raw_usage.get(name)) for name in ("input_tokens", "output_tokens", "total_tokens")}
        truncated = any(page["truncated"] for page in pages)
        return ({"result_type": "ocr_document", "status": "partial" if truncated else "ok", "id": ident,
                 "created": created, "purpose": purpose, "page_count": count, "pages": pages,
                 "truncated": truncated, "source_completeness": "truncated" if truncated else "complete_returned_pages",
                 "original_page_coverage_verified": False, "reference_usage": reference_usage,
                 "reference_usage_is_billable": False,
                 "coordinates": {"space": "normalized_0_1000", "origin": "top_left", "scale": 1000,
                                 "source_dimensions": None, "mapped_to_source": False},
                 "billing": {"unit": "ocr_pages", "count": count, "amount": None}}, {"ocr_pages": count})
