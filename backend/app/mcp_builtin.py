"""研序内置 MCP 服务器（in-process，随软件本体分发，无需外部运行时）。

工具均为免 Key 的公开学术资源 / 网页抓取：
- arxiv_search：arXiv 论文检索
- semantic_scholar_search：Semantic Scholar 检索（含摘要/开放PDF）
- crossref_search：Crossref 出版元数据检索（DOI/期刊）
- web_fetch：抓取网页并提取正文文本

通过 in-process Client 连接（Client(server=MCPServer)），不启动子进程。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

import httpx

from mcp.server.mcpserver import MCPServer

_UA = "ScholarFlow/1.0 (research assistant; +https://scholarflow.local)"
ATOM = "{http://www.w3.org/2005/Atom}"

server = MCPServer("builtin")


def _dumps(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _err(source: str, exc: Exception) -> str:
    """把网络/限流错误转成结构化 JSON（不抛异常，Agent 可据此降级或提示）。"""
    code = getattr(getattr(exc, "response", None), "status_code", None)
    hint = "请求过于频繁，已被限流，请稍后重试" if code in (429, 403) else "网络请求失败，请检查网络后重试"
    return _dumps({"source": source, "error": f"{type(exc).__name__}: {exc}",
                   "status_code": code, "hint": hint, "papers": []})


@server.tool()
async def arxiv_search(query: str, max_results: int = 8) -> str:
    """在 arXiv 上检索论文，返回标题/作者/摘要/发布日期/链接。

    参数：
        query: 检索关键词（可含英文术语，如 "large language model agent"）
        max_results: 返回条数（1-30）
    """
    max_results = max(1, min(int(max_results or 8), 30))
    q = urllib.parse.quote(query)
    url = (
        f"https://export.arxiv.org/api/query?search_query=all:{q}"
        f"&start=0&max_results={max_results}&sortBy=relevance"
    )
    try:
        async with httpx.AsyncClient(
            timeout=25, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return _err("arxiv", exc)
    root = ET.fromstring(resp.text)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        link = (entry.findtext(f"{ATOM}id") or "").strip()
        pdf = ""
        for l in entry.findall(f"{ATOM}link"):
            if l.get("title") == "pdf":
                pdf = l.get("href", "")
        authors = [a.findtext(f"{ATOM}name") or "" for a in entry.findall(f"{ATOM}author")]
        papers.append({
            "title": (entry.findtext(f"{ATOM}title") or "").replace("\n", " ").strip(),
            "authors": ", ".join(authors),
            "abstract": (entry.findtext(f"{ATOM}summary") or "").replace("\n", " ").strip(),
            "published": (entry.findtext(f"{ATOM}published") or "")[:10],
            "url": link,
            "pdf": pdf,
        })
    return _dumps({"source": "arxiv", "query": query, "count": len(papers), "papers": papers})


@server.tool()
async def semantic_scholar_search(query: str, limit: int = 8) -> str:
    """在 Semantic Scholar 上检索论文，返回标题/作者/年份/摘要/DOI/开放PDF。

    参数：
        query: 检索关键词
        limit: 返回条数（1-30）
    """
    limit = max(1, min(int(limit or 8), 30))
    fields = "title,authors,year,abstract,externalIds,url,openAccessPdf,venue,citationCount"
    params = {"query": query, "limit": limit, "fields": fields}
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    try:
        async with httpx.AsyncClient(
            timeout=25, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return _err("semantic_scholar", exc)
    data = resp.json()
    papers = []
    for p in data.get("data", []) or []:
        authors = ", ".join(a.get("name", "") for a in (p.get("authors") or []))
        ext = p.get("externalIds") or {}
        oa = p.get("openAccessPdf") or {}
        papers.append({
            "title": p.get("title", ""),
            "authors": authors,
            "year": p.get("year", ""),
            "venue": p.get("venue", ""),
            "abstract": (p.get("abstract") or ""),
            "doi": ext.get("DOI", ""),
            "citations": p.get("citationCount", 0),
            "url": p.get("url", ""),
            "pdf": oa.get("url", ""),
        })
    return _dumps({"source": "semantic_scholar", "query": query,
                   "count": len(papers), "papers": papers})


@server.tool()
async def crossref_search(query: str, rows: int = 8) -> str:
    """在 Crossref 上检索出版元数据（期刊/会议/DOI），适合查正式出版信息与引用。

    参数：
        query: 检索关键词
        rows: 返回条数（1-30）
    """
    rows = max(1, min(int(rows or 8), 30))
    url = "https://api.crossref.org/works"
    try:
        async with httpx.AsyncClient(
            timeout=25, follow_redirects=True, headers={"User-Agent": _UA}
        ) as client:
            resp = await client.get(url, params={"query": query, "rows": rows})
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        return _err("crossref", exc)
    items = (resp.json().get("message", {}) or {}).get("items", []) or []
    papers = []
    for it in items:
        def _names(seq):
            out = []
            for x in seq or []:
                out.append(" ".join(filter(None, [x.get("given"), x.get("family")])))
            return ", ".join(out)

        issued = ((it.get("issued", {}) or {}).get("date-parts", [[None]])[0])
        papers.append({
            "title": (it.get("title") or [""])[0],
            "authors": _names(it.get("author")),
            "year": issued[0] if issued else None,
            "venue": (it.get("container-title") or [""])[0],
            "type": it.get("type", ""),
            "doi": it.get("DOI", ""),
            "publisher": it.get("publisher", ""),
            "url": it.get("URL", ""),
        })
    return _dumps({"source": "crossref", "query": query,
                   "count": len(papers), "papers": papers})


@server.tool()
async def web_fetch(url: str) -> str:
    """抓取一个网页并提取可读正文文本（用于阅读在线资料/文档/网页）。

    参数：
        url: 完整网址（http/https）
    """
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; ScholarFlow/1.0)"},
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as exc:
        return _err("web", exc)
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "<html" not in resp.text[:2000].lower():
        return _dumps({"url": url, "content_type": ctype, "text": resp.text[:8000]})
    html = resp.text
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    import html as _html

    text = _html.unescape(html)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return _dumps({"url": url, "content_type": "text/html", "text": text.strip()[:8000]})


def build_builtin_servers() -> dict:
    """返回内置服务器名 -> MCPServer 实例。"""
    return {"builtin": server}
