"""论文标识符提取与校验（arXiv 编号、DOI）。

从参考文献原文中提取稳定标识符，用于同篇论文的精确匹配。
所有提取结果都经过结构与取值有效性校验，避免把 OCR 误识别的
数字串（如 8817.34762、7495.35319）当作有效编号。
"""
from __future__ import annotations

import datetime
import re
from typing import Optional

# 新格式 arXiv 编号：YYMM.NNNNN（序号 4~5 位，可带 vN 版本后缀）
_ARXIV_NEW_RE = re.compile(r"(\d{2})(\d{2})\.(\d{4,5})(?:v\d+)?")
# 旧格式：分类/7 位数字（如 cs.AI/0102033、math.CO/0506217）
_ARXIV_OLD_RE = re.compile(r"[a-z][a-z0-9\-]*(?:\.[A-Z]{2})?/(\d{7})")
# DOI：10.登记号/后缀
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)

# arXiv 最早发布于 1991 年；年份上限动态取当前年 + 1，容忍跨年与轻微 OCR 误差。
_MIN_ARXIV_YEAR = 1991


def _max_year() -> int:
    return datetime.datetime.now().year + 1


def _valid_new_arxiv(year2: str, month: str) -> bool:
    """校验新格式编号的年份与月份，拒绝 8817/7495 这类无效年份。"""
    full_year = 2000 + int(year2)
    if not (_MIN_ARXIV_YEAR <= full_year <= _max_year()):
        return False
    return 1 <= int(month) <= 12


def extract_arxiv_id(text: str) -> Optional[str]:
    """从文本中提取并校验一个有效 arXiv 编号（去版本后缀的规范形式）。

    优先识别 URL（arxiv.org/abs|pdf/...），再匹配裸编号；
    无效年份/月份的数字串返回 None。
    """
    if not text:
        return None
    # 去掉 URL 前缀干扰，保留编号部分
    candidate = text
    url_match = re.search(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+)", text, re.IGNORECASE)
    if url_match:
        candidate = url_match.group(1)
    new_match = _ARXIV_NEW_RE.search(candidate)
    if new_match and _valid_new_arxiv(new_match.group(1), new_match.group(2)):
        return f"{new_match.group(1)}{new_match.group(2)}.{new_match.group(3)}"
    old_match = _ARXIV_OLD_RE.search(candidate)
    if old_match:
        return old_match.group(0)
    # 裸编号在整段文本里再校验一次（覆盖无 URL 场景）
    new_match = _ARXIV_NEW_RE.search(text)
    if new_match and _valid_new_arxiv(new_match.group(1), new_match.group(2)):
        return f"{new_match.group(1)}{new_match.group(2)}.{new_match.group(3)}"
    return None


def extract_doi(text: str) -> Optional[str]:
    """从文本中提取并规范化一个 DOI（小写，去除尾部标点）。"""
    if not text:
        return None
    match = _DOI_RE.search(text)
    if not match:
        return None
    doi = match.group(0).rstrip(".,;)]}")
    return doi.lower()


def extract_identifiers(text: str) -> dict:
    """同时提取 arXiv 编号与 DOI，返回结构化结果（缺失为 None）。"""
    return {"arxiv_id": extract_arxiv_id(text), "doi": extract_doi(text)}
