"""研序内置 MCP 服务器（in-process，随软件本体分发，无需外部运行时）。

工具均为免 Key 的公开学术资源 / 网页抓取：
- arxiv_search：arXiv 论文检索
- semantic_scholar_search：Semantic Scholar 检索（含摘要/开放PDF）
- crossref_search：Crossref 出版元数据检索（DOI/期刊）
- web_fetch：抓取网页并提取正文文本

通过 in-process Client 连接（Client(server=MCPServer)），不启动子进程。
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

import httpx

from app.netguard import SSRFError, request_guard_hook
from mcp.server.mcpserver import MCPServer

_UA = "ScholarFlow/1.0 (research assistant; +https://scholarflow.local)"
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_WEB_TEXT_CHARS = 8000
REQUEST_TIMEOUT_SECONDS = 25.0

server = MCPServer("builtin")


def _dumps(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


class _ToolError(ValueError):
    def __init__(self, code: str, status_code: int | None = None):
        self.code, self.status_code = code, status_code
        super().__init__(code)


_ERROR_HINTS = {
    "invalid_request": "请求参数无效，请检查搜索词、条数或网页地址。",
    "blocked_target": "目标或重定向地址不是允许访问的公网地址，未读取其内容。",
    "authentication": "远端服务要求有效鉴权，当前请求未获授权。",
    "forbidden": "远端服务拒绝访问，不能将拒绝访问页面作为正文。",
    "not_found": "远端资源不存在，未获得可读取内容。",
    "rate_limited": "远端服务已限流，请稍后重试。",
    "timeout": "请求超时，未自动重试。",
    "unavailable": "远端服务暂时不可用，未自动重试。",
    "http_error": "远端服务返回非成功状态，未将响应作为资料。",
    "network_error": "网络请求失败，请检查网络后重试。",
    "invalid_response": "远端返回的 JSON、XML 或字段结构无效，不能作为检索结果。",
    "upstream_error": "远端服务报告执行失败，未获得可用检索结果。",
    "response_too_large": "远端响应超过本地大小限制，未将不完整响应作为资料。",
    "incomplete_response": "远端没有返回完整的成功响应，未将其作为资料。",
    "unsupported_content_type": "此内容不是可直接读取的网页文本，请通过文件或文档解析入口处理。",
    "empty_content": "网页没有可读取的正文，不能声称已经阅读原文。",
}


def _blocked_target_diagnostic(exc: SSRFError) -> tuple[str, str]:
    # Interpret only messages emitted by our local guard. Never reflect the
    # hostname, URL, resolved address, or an upstream exception into the result.
    message = str(exc)
    if message.startswith("无法解析主机名："):
        return "dns_resolution_failed", "当前电脑无法解析远端域名，未发出请求。请检查 DNS 或代理的域名解析设置后重试。"
    if message.startswith("禁止访问内网/保留地址："):
        try:
            address = ipaddress.ip_address(message.split("：", 1)[1])
            if address.version == 4 and address in ipaddress.ip_network("198.18.0.0/15"):
                return "reserved_test_address", (
                    "当前 DNS 返回 198.18.0.0/15 保留测试地址，可能来自代理的 fake-IP 模式。"
                    "请求已阻止；请让该服务域名使用真实公网 IP 解析后重试。")
        except ValueError:
            pass
        return "non_public_address", "目标或重定向解析到内网、回环或保留地址，已阻止访问。"
    return "non_public_target", _ERROR_HINTS["blocked_target"]


def _err(source: str, exc: Exception) -> str:
    """只返回稳定诊断；远端错误正文、URL 参数和异常详情不进入结果。"""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    reason, blocked_hint = None, None
    if isinstance(exc, _ToolError):
        code, status = exc.code, exc.status_code
    elif isinstance(exc, SSRFError):
        code, status = "blocked_target", 400
        reason, blocked_hint = _blocked_target_diagnostic(exc)
    elif isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        code = "timeout"
    elif isinstance(exc, httpx.HTTPStatusError):
        code = {401: "authentication", 403: "forbidden", 404: "not_found", 408: "timeout",
                429: "rate_limited", 504: "timeout"}.get(status, "unavailable" if status >= 500 else "http_error")
    elif isinstance(exc, httpx.InvalidURL):
        code = "invalid_request"
    elif isinstance(exc, httpx.HTTPError):
        code = "network_error"
    else:
        code = "invalid_response"
    hint = blocked_hint or _ERROR_HINTS[code]
    result = {"source": source, "error": hint, "code": code, "status": "failed",
              "status_code": status, "hint": hint, "papers": [], "automatic_retry": False}
    if reason:
        result["reason_code"] = reason
    return _dumps(result)


async def _guard_request(request: httpx.Request) -> None:
    # DNS resolution is synchronous; keep it off the shared MCP event loop so
    # the request deadline and other conversations can run while DNS is slow.
    await asyncio.to_thread(request_guard_hook, request)


async def _read_response(url: str, params: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True, max_redirects=5, trust_env=False,
        event_hooks={"request": [_guard_request]},
        headers={"User-Agent": _UA, "Accept-Encoding": "identity"},
    ) as client:
        async with client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise _ToolError("incomplete_response", response.status_code)
            size = response.headers.get("content-length")
            if size is not None:
                if not size.isascii() or not size.isdigit():
                    raise _ToolError("invalid_response", response.status_code)
                if len(size) > 10 or int(size) > MAX_RESPONSE_BYTES:
                    raise _ToolError("response_too_large", response.status_code)
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise _ToolError("response_too_large", response.status_code)
                payload.extend(chunk)
            # HTTPX already decoded the stream; do not decode Content-Encoding twice.
            return httpx.Response(response.status_code, content=bytes(payload), request=response.request,
                                  headers={"content-type": response.headers.get("content-type", "")})


async def _get_response(url: str, params: dict | None = None) -> httpx.Response:
    # Socket idle timeouts alone do not bound an endless stream of small chunks.
    return await asyncio.wait_for(_read_response(url, params), timeout=REQUEST_TIMEOUT_SECONDS)


def _search_inputs(query: str, limit: int, default: int) -> int:
    if (not isinstance(query, str) or not query.strip() or len(query) > 2048
            or any(ord(c) < 32 for c in query) or isinstance(limit, bool)):
        raise _ToolError("invalid_request")
    if not isinstance(limit, (int, str)):
        raise _ToolError("invalid_request")
    try:
        return max(1, min(int(limit or default), 30))
    except (ValueError, TypeError):
        raise _ToolError("invalid_request") from None


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise _ToolError("invalid_response")
    return value


def _array(value) -> list:
    if not isinstance(value, list):
        raise _ToolError("invalid_response")
    return value


def _text(value, *, required=False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise _ToolError("invalid_response")
    return value


def _number(value):
    if value is not None and (type(value) is not int or value < 0):
        raise _ToolError("invalid_response")
    return value


def _unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise _ToolError("invalid_response")
        result[key] = value
    return result


def _invalid_json_constant(value):
    raise _ToolError("invalid_response")


def _json_object(response: httpx.Response) -> dict:
    data = _object(json.loads(response.content, object_pairs_hook=_unique_object, parse_constant=_invalid_json_constant))
    if "error" in data:
        raise _ToolError("upstream_error", response.status_code)
    return data


def _search_result(source: str, query: str, papers: list) -> str:
    return _dumps({"source": source, "query": query, "count": len(papers), "papers": papers,
                   "status": "ok" if papers else "empty", "trust": "untrusted_external",
                   "authorizes_actions": False})


@server.tool()
async def arxiv_search(query: str, max_results: int = 8) -> str:
    """在 arXiv 上检索论文，返回标题/作者/摘要/发布日期/链接。

    参数：
        query: 检索关键词（可含英文术语，如 "large language model agent"）
        max_results: 返回条数（1-30）
    """
    try:
        max_results = _search_inputs(query, max_results, 8)
        resp = await _get_response("https://export.arxiv.org/api/query", {
            "search_query": f"all:{query}", "start": 0, "max_results": max_results, "sortBy": "relevance"})
        if (b"\x00" in resp.content or b"<!DOCTYPE" in resp.content.upper()
                or b"<!ENTITY" in resp.content.upper()):
            raise _ToolError("invalid_response")
        root = ET.fromstring(resp.content)
        if root.tag != f"{ATOM}feed":
            raise _ToolError("invalid_response")
        entries = root.findall(f"{ATOM}entry")
        if len(entries) > max_results:
            raise _ToolError("invalid_response")
        papers = []
        for entry in entries:
            link = _text(entry.findtext(f"{ATOM}id"), required=True).strip()
            if urllib.parse.urlsplit(link).path.startswith("/api/errors"):
                raise _ToolError("upstream_error", resp.status_code)
            pdf = next((item.get("href", "") for item in entry.findall(f"{ATOM}link") if item.get("title") == "pdf"), "")
            authors = [_text(a.findtext(f"{ATOM}name"), required=True) for a in entry.findall(f"{ATOM}author")]
            papers.append({
                "title": _text(entry.findtext(f"{ATOM}title"), required=True).replace("\n", " ").strip(),
                "authors": ", ".join(authors),
                "abstract": _text(entry.findtext(f"{ATOM}summary")).replace("\n", " ").strip(),
                "published": _text(entry.findtext(f"{ATOM}published"))[:10], "url": link, "pdf": pdf,
            })
        return _search_result("arxiv", query, papers)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, ET.ParseError, TimeoutError) as exc:
        return _err("arxiv", exc)


@server.tool()
async def semantic_scholar_search(query: str, limit: int = 8) -> str:
    """在 Semantic Scholar 上检索论文，返回标题/作者/年份/摘要/DOI/开放PDF。

    参数：
        query: 检索关键词
        limit: 返回条数（1-30）
    """
    try:
        limit = _search_inputs(query, limit, 8)
        fields = "title,authors,year,abstract,externalIds,url,openAccessPdf,venue,citationCount"
        resp = await _get_response("https://api.semanticscholar.org/graph/v1/paper/search",
                                   {"query": query, "limit": limit, "fields": fields})
        items = _array(_json_object(resp).get("data"))
        if len(items) > limit:
            raise _ToolError("invalid_response")
        papers = []
        for raw in items:
            p = _object(raw)
            authors = ", ".join(_text(_object(a).get("name")) for a in _array(p.get("authors") if p.get("authors") is not None else []))
            ext = _object(p.get("externalIds") if p.get("externalIds") is not None else {})
            oa = _object(p.get("openAccessPdf") if p.get("openAccessPdf") is not None else {})
            papers.append({
                "title": _text(p.get("title"), required=True), "authors": authors,
                "year": _number(p.get("year")), "venue": _text(p.get("venue")),
                "abstract": _text(p.get("abstract")), "doi": _text(ext.get("DOI")),
                "citations": _number(p.get("citationCount", 0)), "url": _text(p.get("url")),
                "pdf": _text(oa.get("url")),
            })
        return _search_result("semantic_scholar", query, papers)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, RecursionError, TimeoutError) as exc:
        return _err("semantic_scholar", exc)


@server.tool()
async def crossref_search(query: str, rows: int = 8) -> str:
    """在 Crossref 上检索出版元数据（期刊/会议/DOI），适合查正式出版信息与引用。

    参数：
        query: 检索关键词
        rows: 返回条数（1-30）
    """
    try:
        rows = _search_inputs(query, rows, 8)
        resp = await _get_response("https://api.crossref.org/works", {"query": query, "rows": rows})
        data = _json_object(resp)
        if data.get("status", "ok") != "ok":
            raise _ToolError("upstream_error", resp.status_code)
        items = _array(_object(data.get("message")).get("items"))
        if len(items) > rows:
            raise _ToolError("invalid_response")
        papers = []
        for raw in items:
            it = _object(raw)
            names = []
            for author in _array(it.get("author") if it.get("author") is not None else []):
                author = _object(author)
                names.append(_text(author.get("name")) or " ".join(filter(None, [_text(author.get("given")), _text(author.get("family"))])))
            titles = [_text(value) for value in _array(it.get("title", []))]
            venues = [_text(value) for value in _array(it.get("container-title", []))]
            issued = _object(it.get("issued") if it.get("issued") is not None else {})
            dates = _array(issued.get("date-parts", []))
            first_date = _array(dates[0]) if dates else []
            year = _number(first_date[0]) if first_date else None
            papers.append({
                "title": titles[0] if titles else "", "authors": ", ".join(names), "year": year,
                "venue": venues[0] if venues else "", "type": _text(it.get("type")),
                "doi": _text(it.get("DOI")), "publisher": _text(it.get("publisher")), "url": _text(it.get("URL")),
            })
        return _search_result("crossref", query, papers)
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, RecursionError, TimeoutError) as exc:
        return _err("crossref", exc)


@server.tool()
async def web_fetch(url: str) -> str:
    """抓取一个网页并提取可读正文文本（用于阅读在线资料/文档/网页）。

    参数：
        url: 完整网址（http/https）
    """
    try:
        if (not isinstance(url, str) or not url.strip() or len(url) > 8192
                or any(ord(c) < 32 for c in url)):
            raise _ToolError("invalid_request")
        url = url.strip()
        if "://" not in url:
            url = "https://" + url
        try:
            parsed = urllib.parse.urlsplit(url)
            if (parsed.scheme.lower() not in ("http", "https") or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or any(c.isspace() for c in url) or "\\" in url):
                raise ValueError()
            parsed.port
        except ValueError:
            raise _ToolError("invalid_request") from None
        resp = await _get_response(url)
        ctype = resp.headers.get("content-type", "")
        media = ctype.split(";", 1)[0].strip().lower()
        if media and not (media.startswith("text/") or media in ("application/json", "application/xml", "application/xhtml+xml")
                          or media.endswith(("+json", "+xml"))):
            raise _ToolError("unsupported_content_type", resp.status_code)
        text = resp.text
        if "\x00" in text:
            raise _ToolError("unsupported_content_type", resp.status_code)
        if "html" in media or "<html" in text[:2000].lower():
            text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", text)
            text = re.sub(r"(?s)<!--.*?-->", " ", text)
            text = re.sub(r"(?s)<[^>]+>", " ", text)
            import html as _html

            text = _html.unescape(text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
            ctype = "text/html"
        if not text.strip():
            raise _ToolError("empty_content", resp.status_code)
        truncated = len(text) > MAX_WEB_TEXT_CHARS
        return _dumps({"url": url, "content_type": ctype, "text": text[:MAX_WEB_TEXT_CHARS],
                       "status": "partial" if truncated else "ok", "truncated": truncated,
                       "output_is_partial": truncated, "text_characters": len(text),
                       "returned_characters": min(len(text), MAX_WEB_TEXT_CHARS),
                       "source_completeness": "truncated" if truncated else "unknown",
                       "original_page_coverage_verified": False, "trust": "untrusted_external",
                       "authorizes_actions": False})
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, TimeoutError) as exc:
        return _err("web", exc)


def build_builtin_servers() -> dict:
    """返回内置服务器名 -> MCPServer 实例。"""
    return {"builtin": server}
