"""语义版本去重：arXiv ID 归一化 + 文本相似度。

用于识别同一论文的不同版本（如 arXiv v1/v7），弥补 content_hash 精确去重
无法跨版本识别的缺口。纯函数设计，便于独立测试与复用。
"""
from __future__ import annotations

import re

# 新格式 2401.12345（4 位年.4~5 位序号）或旧格式 cs.AI/0102033，可选版本后缀 vN
_ARXIV_RE = re.compile(
    r"([a-z]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(v\d+)?",
    re.IGNORECASE,
)

# 版本相似度阈值：基于 5 篇真实论文实测校准——同论文 v1/v7 为 0.84，
# 不同论文两两最高仅 0.12，取 0.7 作为二者之间的工程分界，留有充分裕度。
# 样本仍较小（同作品对仅 1 组），并非大规模统计结论，后续可随数据调整。
VERSION_SIMILARITY_THRESHOLD = 0.7


def normalize_arxiv_id(value: str | None) -> str | None:
    """提取基础 arXiv ID（剥离 URL 前缀与版本号）；无法识别返回 None。"""
    if not value:
        return None
    match = _ARXIV_RE.search(value)
    if not match:
        return None
    return match.group(1).lower()


def extract_arxiv_id(value: str | None) -> str | None:
    """提取完整 arXiv ID（含版本号，若有），剥离 URL 前缀；无法识别返回 None。"""
    if not value:
        return None
    match = _ARXIV_RE.search(value)
    if not match:
        return None
    return (match.group(1) + (match.group(2) or "")).lower()


def extract_arxiv_version(value: str | None) -> int | None:
    """提取 arXiv 版本号整数（v7 -> 7）；无版本或无法识别返回 None。"""
    if not value:
        return None
    match = _ARXIV_RE.search(value)
    if not match or not match.group(2):
        return None
    return int(match.group(2)[1:])


def text_jaccard(text_a: str, text_b: str) -> float:
    """归一化小写文本的 token 集合 Jaccard 相似度，范围 0~1。"""
    tokens_a = set(re.findall(r"\w+", text_a.lower()))
    tokens_b = set(re.findall(r"\w+", text_b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def is_same_work(
    text_a: str,
    text_b: str,
    threshold: float = VERSION_SIMILARITY_THRESHOLD,
) -> bool:
    """文本相似度达到阈值即判定为同一作品的不同版本。"""
    return text_jaccard(text_a, text_b) >= threshold


def is_newer_version(incoming_version: int | None, existing_version: int | None) -> bool:
    """仅当双方版本号都明确且 incoming 更高时才判定为更新。

    existing 版本缺失（None）时无法证明 incoming 更新，返回 False，
    避免上传旧版（如 v1）而现有记录无版本号时被误标为 newer。
    """
    return (
        incoming_version is not None
        and existing_version is not None
        and incoming_version > existing_version
    )
