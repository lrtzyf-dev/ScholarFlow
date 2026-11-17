"""Future Work 提取与时间线判断（确定性、可溯源，不依赖 LLM）。

提取各论文未来工作条目，再用更晚论文的正文内容匹配，
启发式标记"疑似已被后续工作处理"的旧 idea，不武断下结论。
"""
from __future__ import annotations

import re

FUTURE_MARKERS = (
    "future work", "future direction", "future research", "in the future",
    "as future", "未来工作", "未来研究", "未来的工作", "展望", "今后",
)
ADDRESS_TERMS = (
    "we address", "we resolve", "we propose", "we implement", "we extend",
    "we overcome", "we present", "this paper presents", "this work presents",
    "解决", "实现", "提出", "改进", "扩展",
)
# 与更晚论文内容匹配的 token 重合阈值
OVERLAP_THRESHOLD = 0.5

_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")
_YEAR = re.compile(r"(?:19|20)\d{2}")


def year_of(published_at: str | None) -> int | None:
    match = _YEAR.search(published_at or "")
    return int(match.group(0)) if match else None


def _tokens(text: str) -> set[str]:
    text = (text or "").lower()
    toks = {w for w in _WORD.findall(text) if len(w) > 2}
    cjk = _CJK.findall(text)
    toks.update(cjk)
    toks.update(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return toks


_SENT_SPLIT = re.compile(r"(?<=[。！？!?\.])\s+|\n+")


def _split_sentences(text: str) -> list[str]:
    """支持中英文句点的句子切分。"""
    return [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]


def _has_marker(sentence: str) -> bool:
    lowered = sentence.lower()
    return any(marker in lowered or marker in sentence for marker in FUTURE_MARKERS)


def extract_directions(paper: dict) -> list[dict]:
    """从单篇论文 chunks 提取未来工作条目（句子级，含位置）。"""
    directions = []
    for chunk in paper["chunks"]:
        for sentence in _split_sentences(chunk.get("content", "")):
            if _has_marker(sentence):
                directions.append({
                    "paper_id": paper["id"],
                    "paper_title": paper["title"],
                    "year": paper["year"],
                    "quote": sentence,
                    "page": chunk.get("page"),
                    "bbox": chunk.get("bbox"),
                })
    return directions


def _overlap(idea_tokens: set[str], text: str) -> float:
    target = _tokens(text)
    if not idea_tokens or not target:
        return 0.0
    return len(idea_tokens & target) / max(3, min(len(idea_tokens), len(target)))


def build_future_work(papers: list[dict]) -> dict:
    """papers: [{id, title, year, chunks}]，返回开放与疑似已处理方向。"""
    all_directions = []
    for paper in papers:
        all_directions.extend(extract_directions(paper))

    for direction in all_directions:
        idea_tokens = _tokens(direction["quote"])
        resolved_by = []
        for later in papers:
            if direction["year"] is None or later["year"] is None:
                continue
            if later["year"] <= direction["year"] or later["id"] == direction["paper_id"]:
                continue
            later_text = "\n".join(c.get("content", "") for c in later["chunks"])
            has_address = any(
                term in later_text.lower() or term in later_text for term in ADDRESS_TERMS
            )
            if has_address and _overlap(idea_tokens, later_text) >= OVERLAP_THRESHOLD:
                resolved_by.append({
                    "paper_id": later["id"],
                    "paper_title": later["title"],
                    "year": later["year"],
                })
        direction["status"] = "possibly_addressed" if resolved_by else "open"
        direction["resolved_by"] = resolved_by

    return {
        "open": [d for d in all_directions if d["status"] == "open"],
        "possibly_addressed": [d for d in all_directions if d["status"] == "possibly_addressed"],
        "total": len(all_directions),
    }
