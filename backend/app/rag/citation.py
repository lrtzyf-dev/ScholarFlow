"""
引用解析与引用闸门：
- 答案中的 [^eN] 标记必须落在证据包范围内
- 实质性论断句（长度足够）必须带引用，否则判为无支撑
- 输出 claims：每句论断、证据 id、supported
"""
from __future__ import annotations

import re

MARKER_RE = re.compile(r"\[\^e(\d+)\]")
# 句末标点后的 [^eN] 标记归属本句（避免被切到下一句）
SENT_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?(?:\s*\[\^e\d+\])*")

# 免责说明、括号备注、标题行不需要引用
_EXEMPT_PREFIX = ("（", "(", "免责", "配置真实", "无法", "未检索", "知识库")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_RE.findall(text) if s.strip()]


def parse_claims(answer: str, evidence_ids: list[str]) -> list[dict]:
    claims = []
    for sent in split_sentences(answer):
        markers = [f"e{n}" for n in MARKER_RE.findall(sent)]
        if not markers:
            continue
        valid = [m for m in markers if m in evidence_ids]
        claims.append(
            {
                "claim": MARKER_RE.sub("", sent).strip(),
                "evidence_ids": valid,
                "supported": bool(valid) and len(valid) == len(markers),
            }
        )
    return claims


def citation_gate(answer: str, evidence_ids: list[str]) -> dict:
    """返回 {passed, unsupported, invalid_markers, marked_claims, total_claims}。"""
    invalid_markers = [m for m in (f"e{n}" for n in MARKER_RE.findall(answer)) if m not in evidence_ids]

    marked_claims = 0
    unsupported: list[str] = []
    total = 0
    for sent in split_sentences(answer):
        clean = MARKER_RE.sub("", sent).strip()
        # 去掉空白与常见标点后再判断长度，避免中文短句被漏判
        meaningful = re.sub(r"[\s，。！？、；,.!?;:：()（）]", "", clean)
        if len(meaningful) < 10:
            continue
        if clean.startswith(_EXEMPT_PREFIX):
            continue
        total += 1
        markers = [f"e{n}" for n in MARKER_RE.findall(sent)]
        if markers and all(m in evidence_ids for m in markers):
            marked_claims += 1
        else:
            unsupported.append(clean[:80])

    passed = not invalid_markers and (total == 0 or marked_claims == total)
    return {
        "passed": passed,
        "unsupported": unsupported,
        "invalid_markers": invalid_markers,
        "marked_claims": marked_claims,
        "total_claims": total,
    }


def strip_markers(text: str) -> str:
    return MARKER_RE.sub("", text)
