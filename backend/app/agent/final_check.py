"""产物自检闭环：定稿前对整稿做一次综合校验，输出可核对的检查报告。

检查项：
1. 论断引用完整性：每个实质性论断都有有效引用（机械闸门）；
2. 引用语义审计：引用证据是否真的支撑论断（在线 LLM / 离线弱审计）；
3. 引用-参考文献对应：文内引用与参考文献一一对应，无悬空；
4. 关键文献覆盖：检索到的关键文献是否都在正文被引用。

报告给出 passed、逐项结果与 must_fix 待修复清单。
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from ..llm import get_llm
from ..rag.citation import MARKER_RE, citation_gate
from .audit import audit_answer


def _reference_section(answer: str, evidence: list[dict]) -> dict:
    """检查文内引用与参考文献列表是否一一对应。"""
    in_text = sorted({int(n) for n in MARKER_RE.findall(answer)})
    # 参考文献列表：答案末尾若有"参考文献"小节，按 [N] 编号
    m = re.search(r"参考文献\s*[:：]?\s*\n(.*)$", answer, re.DOTALL)
    listed = sorted({int(n) for n in re.findall(r"^\s*(?:\[(\d+)\]|(\d+)[.、])",
                                               m.group(1) if m else "", re.MULTILINE) for n in n if n})
    if not listed:
        return {"present": False, "in_text": in_text, "listed": [],
                "orphan_listed": [], "missing_in_list": []}
    missing_in_list = [n for n in in_text if n not in listed]
    orphan_listed = [n for n in listed if n not in in_text]
    return {"present": True, "in_text": in_text, "listed": listed,
            "orphan_listed": orphan_listed, "missing_in_list": missing_in_list}


def _paper_coverage(answer: str, evidence: list[dict]) -> dict:
    """关键文献覆盖：被检索到的论文，哪些在正文被引用。"""
    cited_ids = {int(n) for n in MARKER_RE.findall(answer)}
    cited_evidence = {e["id"] for e in evidence if _num_of(e["id"]) in cited_ids}
    papers: dict[str, dict] = {}
    for e in evidence:
        pid = e.get("paper_id") or e.get("paper_title") or "unknown"
        slot = papers.setdefault(pid, {"title": e.get("paper_title", ""), "cited": False})
        if e["id"] in cited_evidence:
            slot["cited"] = True
    uncited = [p["title"] for p in papers.values() if not p["cited"]]
    total = len(papers)
    return {"total_papers": total, "cited_papers": total - len(uncited),
            "uncited_papers": uncited}


def _num_of(eid: str) -> int:
    m = re.search(r"e(\d+)$", str(eid))
    return int(m.group(1)) if m else -1


def final_check(db: Session, answer: str, evidence: list[dict], llm=None) -> dict:
    llm = llm or get_llm()
    evidence_ids = [e["id"] for e in evidence]
    checks: list[dict] = []
    must_fix: list[dict] = []

    # 1. 论断引用完整性
    gate = citation_gate(answer, evidence_ids)
    gate_ok = gate["passed"]
    checks.append({
        "name": "论断引用完整性",
        "passed": gate_ok,
        "detail": f"{gate['marked_claims']}/{gate['total_claims']} 个论断有有效引用；"
                  f"无效引用标记 {len(gate['invalid_markers'])} 个。",
        "data": gate,
    })
    if not gate_ok:
        for s in gate["unsupported"][:6]:
            must_fix.append({"check": "论断引用完整性", "item": f"论断无引用：{s}"})
        for m in gate["invalid_markers"]:
            must_fix.append({"check": "论断引用完整性", "item": f"无效引用标记：{m}"})

    # 2. 引用语义审计
    audit = audit_answer(db, answer, evidence, llm=llm)
    audit_ok = audit["passed"]
    checks.append({
        "name": "引用语义审计",
        "passed": audit_ok,
        "detail": f"模式：{audit['mode']}；不被证据支撑的论断 {len(audit['unsupported'])} 条。",
        "data": {"mode": audit["mode"], "unsupported": audit["unsupported"]},
    })
    if not audit_ok:
        for u in audit["unsupported"][:6]:
            must_fix.append({"check": "引用语义审计",
                             "item": f"引用不支撑：{u.get('text','')[:50]}（{u.get('reason','')}）"})

    # 3. 引用-参考文献对应
    refmap = _reference_section(answer, evidence)
    ref_ok = True
    if refmap["present"]:
        ref_ok = not refmap["missing_in_list"] and not refmap["orphan_listed"]
        checks.append({
            "name": "引用-参考文献对应",
            "passed": ref_ok,
            "detail": f"文内 {len(refmap['in_text'])} 处、参考文献 {len(refmap['listed'])} 条；"
                      f"正文有但列表缺 {len(refmap['missing_in_list'])}；列表有但正文未引 {len(refmap['orphan_listed'])}。",
            "data": refmap,
        })
        for n in refmap["missing_in_list"]:
            must_fix.append({"check": "引用-参考文献对应", "item": f"引用 [{n}] 在参考文献列表中缺失"})
    else:
        checks.append({
            "name": "引用-参考文献对应",
            "passed": True,
            "detail": "未检测到独立参考文献列表（答案可能直接以证据编号呈现）。",
            "data": refmap,
        })

    # 4. 关键文献覆盖
    coverage = _paper_coverage(answer, evidence)
    cov_ok = coverage["total_papers"] == 0 or coverage["cited_papers"] >= coverage["total_papers"]
    # 未引用某篇检索到的论文属于提示项（可能确实无关），不直接判失败
    checks.append({
        "name": "关键文献覆盖",
        "passed": True,
        "detail": f"已引用 {coverage['cited_papers']}/{coverage['total_papers']} 篇检索到的论文；"
                  f"未引用 {len(coverage['uncited_papers'])} 篇（请确认是否确为无关）。",
        "data": coverage,
    })
    for t in coverage["uncited_papers"][:5]:
        must_fix.append({"check": "关键文献覆盖（提示）",
                         "item": f"检索到但未引用：《{t}》"})

    passed = all(c["passed"] for c in checks)
    return {"passed": passed, "checks": checks, "must_fix": must_fix,
            "summary": ("全部检查通过，可以定稿导出。" if passed
                        else f"有 {len(must_fix)} 项待处理，建议修复后再定稿。")}
