"""从解析结构中启发式抽取文档元数据（摘要、作者）。

纯函数、不依赖 LLM，便于离线单测。启发式结果可能遗漏或误判：
所有字段都应视为"可空"，缺失时保留空值，不得编造。
"""
from __future__ import annotations

import re

MAX_ABSTRACT_CHARS = 2000

_ABSTRACT_HEAD = re.compile(r"^(abstract|摘\s*要)\s*[:：]?\s*", re.IGNORECASE)
_AUTHOR_SPLIT = re.compile(r"\s*,\s*|\s+and\s+|\s*、\s*|\s*;\s*|\s*；\s*")
# 人名 token：首字母大写的字母串，允许缩写点与连字符
_NAME_TOKEN = re.compile(r"^[A-Z][A-Za-z.\-']*$")
# 机构行特征：出现这些词时不当作作者
_AFFILIATION = re.compile(
    r"university|department|institute|laboratory|lab\.?|college|school\s+of|"
    r"大学|学院|研究所|实验室|学会|公司",
    re.IGNORECASE,
)
# 常见章节标题，避免被误当作作者行
_HEADING_WORD = re.compile(
    r"^(abstract|introduction|contents|chapter|摘要|引言|目录)\b", re.IGNORECASE
)


def extract_abstract(parsed: dict) -> str:
    """定位首页摘要：支持"Abstract"独立成块或与正文同块两种排版。"""
    pages = parsed.get("pages", [])
    if not pages:
        return ""
    blocks = pages[0].get("blocks", [])
    for idx, block in enumerate(blocks):
        text = block.get("text", "").strip()
        match = _ABSTRACT_HEAD.match(text)
        if not match:
            continue
        rest = text[match.end():].strip()
        if rest:
            return rest[:MAX_ABSTRACT_CHARS]
        # 标题独立成块：取下一个正文块
        for nxt in blocks[idx + 1:]:
            if nxt.get("kind") == "text" and nxt.get("text", "").strip():
                return nxt["text"].strip()[:MAX_ABSTRACT_CHARS]
        return ""
    return ""


def extract_authors(parsed: dict) -> str:
    """在首页主标题之后的紧邻块中，保守识别人名行；不确定时返回空串。"""
    pages = parsed.get("pages", [])
    if not pages:
        return ""
    blocks = pages[0].get("blocks", [])
    for block in blocks[1:6]:
        if block.get("kind") != "text":
            continue
        text = block.get("text", "").strip()
        if not text or len(text) > 160 or "\n" in text:
            continue
        if _AFFILIATION.search(text) or _HEADING_WORD.search(text):
            continue
        # 含句点（缩写点除外难以区分）保守起见，出现句点且不像人名缩写则跳过
        parts = [p.strip() for p in _AUTHOR_SPLIT.split(text) if p.strip()]
        if not 1 <= len(parts) <= 12:
            continue
        # 每个 part 是一个完整人名，可含空格（名+姓），其每个词都符合人名模式
        if all(_is_person_name(p) for p in parts):
            return ", ".join(parts)
    return ""


def _is_person_name(part: str) -> bool:
    words = part.split()
    return bool(words) and all(_NAME_TOKEN.match(w) for w in words)


# 公式常用运算符、括号与希腊字母（含大写）
_FORMULA_SYMBOLS = set(
    "=+-*/^_()[]{}≈≠≤≥±×∈→←∑∫√∏∂∇∞∝∪∩⊂⊃∀∃·"
    "αβγδεζηθικλμνξοπρστυφχψωΓΔΘΛΞΠΣΦΨΩ"
    "⁰¹²³⁴⁵⁶⁷⁸⁹₀₁₂₃₄₅₆₇₈₉"
)


def looks_like_formula(text: str) -> bool:
    """保守识别独立成行的显示公式；行内公式与普通句子不命中。"""
    stripped = text.strip()
    if len(stripped) < 4:
        return False
    chars = [c for c in stripped if not c.isspace()]
    op_count = sum(1 for c in chars if c in _FORMULA_SYMBOLS)
    if op_count < 2:
        return False
    # 自然语言长词多则为正文句
    if len(re.findall(r"[A-Za-z]{4,}", stripped)) > 3:
        return False
    return (op_count / len(chars)) >= 0.2
