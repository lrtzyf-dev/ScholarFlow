"""
正文引用标记解析（纯函数，离线可测）。

识别顺序编码制的正文引用，如：
- [11] 单引用
- [12, 13] 多引用
- [12-14] / [12–14] 范围引用
- (11) 圆括号
- [11, 13-15] 混合

仅做确定性文本解析，不依赖 LLM。作者-年份制（如 (Smith, 2020)）不在此处理。
"""
from __future__ import annotations

import re

# 一对方括号/圆括号内是数字，且可由逗号或连字符继续
_CITATION = re.compile(r"[\[（(](\d+(?:\s*[,\-–—]\s*\d+)*)[\]）)]")
_PART_SPLIT = re.compile(r"\s*,\s*")
_RANGE_SPLIT = re.compile(r"\s*[\-–—]\s*")


def extract_citation_numbers(text: str) -> list[int]:
    """提取文本中出现的全部引用编号（去重、升序）。"""
    numbers: set[int] = set()
    for group in _CITATION.findall(text or ""):
        for part in _PART_SPLIT.split(group):
            if not part:
                continue
            if _RANGE_SPLIT.search(part):
                ends = _RANGE_SPLIT.split(part)
                if len(ends) != 2:
                    continue
                start, end = int(ends[0]), int(ends[1])
                if start <= end:
                    numbers.update(range(start, end + 1))
            else:
                numbers.add(int(part))
    return sorted(numbers)


def citation_map(
    chunks: list[dict], max_ref_index: int
) -> dict[int, list[dict]]:
    """
    建立引用编号 -> 正文出现位置（chunk 级）的映射。
    仅保留编号在参考文献范围内的条目，避免把 [2023] 之类误判为引用。
    返回 {ref_index: [{page, bbox}...]}，按出现顺序。
    """
    mapping: dict[int, list[dict]] = {}
    for chunk in chunks:
        for number in extract_citation_numbers(chunk.get("content", "")):
            if number < 1 or number > max_ref_index:
                continue
            loc = {"page": chunk.get("page", 1), "bbox": chunk.get("bbox")}
            if loc not in mapping.setdefault(number, []):
                mapping[number].append(loc)
    return mapping
