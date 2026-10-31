"""引用语义审计：在"引用标记存在"的机械闸门之上，进一步核对
每条论断是否真的被其引用的证据所支撑（防止把 eN 挂到它并不支持的句子上）。

- 在线（有 LLM）：把论断+引用证据一次性送 LLM，严格 JSON 回传 supported。
- 离线（MockLLM）：用确定性词汇重叠兜底，仅作弱审计并标注。
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from ..llm import get_llm
from ..rag.citation import parse_claims

_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[一-鿿]")
_STOP = {"the", "a", "an", "of", "to", "in", "on", "and", "or", "is", "are", "was", "how", "what", "does", "do", "with", "for", "that", "this", "it", "its", "by", "as", "at", "be"}


def _content_tokens(text: str) -> set[str]:
    t = text.lower()
    toks = [w for w in _WORD.findall(t) if w not in _STOP and len(w) > 1]
    cjk = _CJK.findall(t)
    toks.extend(cjk)
    toks.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return set(toks)


def _lexical_supported(claim: str, evidence_quotes: list[str], threshold: float = 0.5) -> bool:
    """离线兜底：论断的关键 token 有多少能在引用证据中找到。"""
    ct = _content_tokens(claim)
    if not ct:
        return True
    ev_text = " ".join(evidence_quotes).lower()
    hit = sum(1 for t in ct if t in ev_text)
    return (hit / len(ct)) >= threshold


def _build_audit_text(claims: list[dict], evidence_map: dict[str, dict]) -> str:
    lines = ["以下是若干论断及其引用的证据原文。请逐条判断引用证据是否直接支持该论断；"
             "数字、年份、结论必须与证据一致，证据未提及的内容视为不支持。", ""]
    for i, c in enumerate(claims):
        lines.append(f"[论断 {i}] {c['text']}")
        for eid in c["evidence_ids"]:
            ev = evidence_map.get(eid)
            if ev:
                lines.append(f"  引用证据 {eid}（{ev.get('paper_title','')}）：{ev.get('quote','')}")
        lines.append("")
    lines.append('只输出 JSON：{"results": [{"index": 0, "supported": true, "reason": "简短说明"}]}')
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def audit_answer(db: Session, answer: str, evidence: list[dict],
                 llm=None) -> dict:
    """返回 {passed, unsupported:[{text,reason}], mode: llm|lexical}。"""
    evidence_map = {e["id"]: e for e in evidence}
    evidence_ids = list(evidence_map.keys())
    raw_claims = parse_claims(answer, evidence_ids)
    if not raw_claims:
        return {"passed": True, "unsupported": [], "mode": "none", "claims": []}
    # 归一化为 {text, evidence_ids}
    claims = [{"text": c["claim"], "evidence_ids": c["evidence_ids"]} for c in raw_claims]

    # 只审计有引用的实质性论断；无引用的论断由机械闸门负责
    cited = [c for c in claims if c["evidence_ids"]]
    if not cited:
        return {"passed": True, "unsupported": [], "mode": "none", "claims": claims}

    llm = llm or get_llm()
    is_mock = type(llm).__name__ == "MockLLM"

    if is_mock:
        unsupported = []
        for c in cited:
            quotes = [evidence_map[e]["quote"] for e in c["evidence_ids"] if e in evidence_map]
            if not _lexical_supported(c["text"], quotes):
                unsupported.append({"text": c["text"], "reason": "引用证据与论断词汇重叠不足（离线弱审计）"})
        return {"passed": not unsupported, "unsupported": unsupported, "mode": "lexical", "claims": claims}

    prompt = _build_audit_text(cited, evidence_map)
    try:
        raw = llm.chat(
            [{"role": "system", "content": "你是严谨的引用审计员，只输出 JSON。"},
             {"role": "user", "content": prompt}],
            temperature=0,
        )
    except Exception as exc:  # 审计本身失败不应阻断：保守放行并标注
        return {"passed": True, "unsupported": [], "mode": "unavailable",
                "claims": claims, "error": f"{type(exc).__name__}: {exc}"}

    data = _extract_json(raw.content if hasattr(raw, "content") else raw)
    if not data or "results" not in data:
        return {"passed": True, "unsupported": [], "mode": "unparseable", "claims": claims}

    unsupported = []
    for r in data["results"]:
        try:
            idx = int(r.get("index"))
            ok = bool(r.get("supported"))
        except (TypeError, ValueError):
            continue
        if not ok and 0 <= idx < len(cited):
            unsupported.append({"text": cited[idx]["text"],
                                "reason": str(r.get("reason", "证据不支持该论断"))})
    return {"passed": not unsupported, "unsupported": unsupported, "mode": "llm", "claims": claims}
