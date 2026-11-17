"""引用含金量分析（确定性启发式，不依赖 LLM）。

分析每条参考文献在正文中的引用上下文，区分实质讨论与应付式罗列。
所有判定给出可解释信号，便于复核，避免黑箱。
"""
from __future__ import annotations

import re

from ..parsing.citation_marks import extract_citation_numbers
from ..rag.citation import split_sentences

# 同一句中出现这么多引用编号，视为密集罗列
DENSE_THRESHOLD = 4
# 去掉引用标记后，讨论文本的有效长度阈值
DISCUSSION_LEN = 10

DISCUSSION_TERMS = (
    "方法", "提出", "表明", "证明", "结果", "相比", "比较", "改进", "局限",
    "问题", "性能", "评估", "优于", "不足", "发现", "采用", "利用", "思路",
    "框架", "模型", "算法", "实验", "提升", "显著", "缺点", "优势", "准确率",
    "method", "propose", "proposes", "show", "shows", "result", "results",
    "compare", "compared", "improve", "however", "approach", "model",
    "algorithm", "performance", "outperform", "outperforms", "limitation",
)

_CITATION_PAREN = re.compile(r"[\[（(]\d+(?:\s*[,\-–—]\s*\d+)*[\]）)]")
_EN_WORD = re.compile(r"[a-zA-Z]+")
_CJK_CHAR = re.compile(r"[\u4e00-\u9fff]")


def _effective_length(text: str) -> int:
    """中文按字符、英文按单词统计有效长度。"""
    return len(_CJK_CHAR.findall(text)) + len(_EN_WORD.findall(text))


def score_occurrence(sentence: str) -> dict:
    """对一次引用出现（所在句子）打分，返回分数与命中信号。"""
    co_cited = len(extract_citation_numbers(sentence))
    discussion = _CITATION_PAREN.sub("", sentence)
    term_hits = sum(1 for term in DISCUSSION_TERMS if term in discussion.lower() or term in discussion)
    length = _effective_length(discussion)

    score = 0
    signals: list[str] = []
    if co_cited >= DENSE_THRESHOLD:
        score -= 2
        signals.append("密集罗列")
    elif co_cited <= 2:
        score += 1
        signals.append("少量同引")
    if term_hits >= 2:
        score += 2
        signals.append("含讨论词")
    elif term_hits == 1:
        score += 1
    if length >= DISCUSSION_LEN:
        score += 1
    else:
        score -= 1
        signals.append("上下文过短")
    return {"score": score, "co_cited": co_cited, "signals": signals}


def analyze_reference(ref_index: int, text: str) -> dict:
    """分析单条参考文献的全部引用出现，给出含金量标签。"""
    sentences = [
        s for s in split_sentences(text)
        if ref_index in extract_citation_numbers(s)
    ]
    if not sentences:
        return {
            "ref_index": ref_index, "label": "unmentioned", "score": 0,
            "occurrences": 0, "signals": [],
        }

    occurrences = [score_occurrence(s) for s in sentences]
    best = max(o["score"] for o in occurrences)
    signals = sorted({sig for o in occurrences for sig in o["signals"]})

    if best >= 2:
        label = "substantive"
    elif best <= 0:
        label = "passing"
    else:
        label = "neutral"
    return {
        "ref_index": ref_index,
        "label": label,
        "score": best,
        "occurrences": len(occurrences),
        "signals": signals,
    }


def analyze_paper_references(text: str, ref_count: int) -> list[dict]:
    """对一篇论文的 1..ref_count 条参考文献批量分析。"""
    return [analyze_reference(i, text) for i in range(1, ref_count + 1)]
