"""arXiv 最新论文抓取（同步，可注入 transport，解析逻辑独立可离线测试）。"""
from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

import httpx

from .scoring import detect_github_url

_UA = "ScholarFlow/1.0 (research assistant; +https://scholarflow.local)"
ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_ID = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?$")


def _build_url(topic: str, max_results: int) -> str:
    q = urllib.parse.quote(topic)
    return (
        f"https://export.arxiv.org/api/query?search_query=all:{q}"
        f"&start=0&max_results={max_results}"
        "&sortBy=submittedDate&sortOrder=descending"
    )


def parse_arxiv_feed(xml_text: str) -> list[dict]:
    """解析 Atom feed 为候选论文字典，无需网络。"""
    root = ET.fromstring(xml_text)
    candidates = []
    for entry in root.findall(f"{ATOM}entry"):
        entry_url = (entry.findtext(f"{ATOM}id") or "").strip()
        id_match = _ARXIV_ID.search(entry_url)
        authors = [
            (a.findtext(f"{ATOM}name") or "").strip()
            for a in entry.findall(f"{ATOM}author")
        ]
        abstract = (entry.findtext(f"{ATOM}summary") or "").replace("\n", " ").strip()
        candidates.append({
            "arxiv_id": id_match.group(1) if id_match else None,
            "title": (entry.findtext(f"{ATOM}title") or "").replace("\n", " ").strip(),
            "authors": ", ".join(filter(None, authors)),
            "abstract": abstract,
            "published_at": (entry.findtext(f"{ATOM}published") or "")[:10],
            "url": entry_url,
            "github_url": detect_github_url(abstract),
        })
    return candidates


def fetch_arxiv(topic: str, max_results: int = 10) -> list[dict]:
    """抓取 arXiv 最新论文；网络/HTTP 错误向上抛出，由调用方结构化处理。"""
    with httpx.Client(
        timeout=25, follow_redirects=True, headers={"User-Agent": _UA}
    ) as client:
        resp = client.get(_build_url(topic, max_results))
        resp.raise_for_status()
    return parse_arxiv_feed(resp.text)
