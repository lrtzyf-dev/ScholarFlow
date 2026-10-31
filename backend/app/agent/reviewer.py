"""一键审我的稿件：以审稿人视角审查用户自己的综述/草稿。

审查维度：
1. 引用完整性：关键论断是否都有引用；
2. 引用真实性：文内引用标记与参考文献是否一一对应、有无悬空引用；
3. 逻辑与论证：论断有无矛盾、跳跃，结论是否有支撑；
4. 结构：章节是否完整、段落组织是否合理。

在线（有 LLM）：审稿人式结构化评审；离线：规则机械检查 + 提示。
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session

from ..llm import get_llm
from ..rag.citation import split_sentences

# 常见引用标记：[e1] [^1] [1] （作者，2024） (Author, 2024)
_CITE_MARK = re.compile(r"(?:\[\^?e?\d+\]|（[^（）]{1,30}?[，,]\s*\d{4}）|\([^()]{1,40}?[，,]\s*\d{4}\))")
# 参考文献条目：[1] ... 或 1. ...
_REF_LINE = re.compile(r"^\s*(?:\[\d+\]|\d+[.、])\s*\S")
# 章节标题
_SECTION = re.compile(r"^\s{0,3}#{1,6}\s+\S|^\s{0,3}(?:摘要|引言|前言|相关工作|研究背景|方法|实验|结果|讨论|结论|参考文献|abstract|introduction|related work|method|experiment|result|discussion|conclusion|references)\s*[:：]?\s*$",
                      re.IGNORECASE | re.MULTILINE)


def _mechanical_review(draft: str) -> dict:
    """规则机械检查：引用覆盖率、悬空引用、结构。"""
    # 分离正文与参考文献
    ref_split = re.split(r"\n\s*(?:#{1,6}\s*)?参考文献\s*[:：]?\s*\n", draft, flags=re.IGNORECASE)
    body = ref_split[0]
    ref_text = ref_split[1] if len(ref_split) > 1 else ""
    ref_numbers = {int(n) for n in re.findall(r"^\s*(?:\[(\d+)\]|(\d+)[.、])", ref_text, re.MULTILINE) for n in n if n}

    sentences = split_sentences(body)
    # 只看有实质内容的陈述句（>=10 字）
    substantive = [s for s in sentences if len(re.sub(r"\s", "", s)) >= 10]
    uncited = [s for s in substantive if not _CITE_MARK.search(s)]
    cited_count = len(substantive) - len(uncited)

    # 文内数字型引用
    in_text_nums = {int(n) for n in re.findall(r"\[\^?e?(\d+)\]", body)}
    dangling = sorted(n for n in in_text_nums if ref_numbers and n not in ref_numbers)

    sections = {m.group(0).strip().lstrip("#").strip() for m in _SECTION.finditer(draft)}
    coverage = round(cited_count / len(substantive), 3) if substantive else 0.0

    issues = []
    if uncited:
        issues.append({"severity": "high", "dimension": "引用完整性",
                       "title": f"{len(uncited)} 个关键论断没有引用",
                       "detail": "以下论断缺少出处，投稿前需补引用：" + "；".join(
                           f"“{s[:40]}…”" for s in uncited[:5])})
    if dangling:
        issues.append({"severity": "high", "dimension": "引用真实性",
                       "title": f"{len(dangling)} 个文内引用在参考文献中找不到对应",
                       "detail": "悬空引用编号：" + ", ".join(f"[{n}]" for n in dangling)})
    if ref_numbers and not in_text_nums:
        issues.append({"severity": "low", "dimension": "引用真实性",
                       "title": "有参考文献列表，但正文未发现数字型引用标记",
                       "detail": "请确认正文是否正确标注了引用。"})
    if len(sections) < 3:
        issues.append({"severity": "mid", "dimension": "结构",
                       "title": "章节结构不完整",
                       "detail": "未识别到完整的章节标题（如摘要、引言、相关工作、方法、结论）。"})
    if not substantive:
        issues.append({"severity": "mid", "dimension": "内容",
                       "title": "正文内容过少或无法识别",
                       "detail": "请粘贴完整的稿件正文。"})

    return {
        "issues": issues,
        "stats": {
            "substantive_sentences": len(substantive),
            "cited_sentences": cited_count,
            "uncited_sentences": len(uncited),
            "citation_coverage": coverage,
            "reference_count": len(ref_numbers),
            "sections_found": sorted(sections),
        },
    }


_REVIEWER_PROMPT = """你是资深学术期刊审稿人。请审查以下稿件，从四个维度给出严格、可执行的评审意见：
1. 引用完整性：关键论断是否都有出处；
2. 引用真实性：引用是否与论断匹配、有无悬空或张冠李戴；
3. 逻辑与论证：论断有无矛盾、跳跃，结论是否有前文支撑；
4. 结构与表达：章节是否完整、段落组织、语言是否客观准确。

只输出 JSON：
{
  "overall": "总体评价（2-3句）",
  "issues": [
    {"severity": "high|mid|low", "dimension": "维度", "title": "问题标题", "detail": "具体位置与修改建议"}
  ],
  "strengths": ["稿件的优点"],
  "verdict": "major_revision|minor_revision|accept"
}
"""


def review_draft(db: Session, draft: str, llm=None) -> dict:
    """审稿主入口，返回结构化评审结果。"""
    mech = _mechanical_review(draft)
    llm = llm or get_llm()
    is_mock = type(llm).__name__ == "MockLLM"

    if is_mock:
        return {
            "overall": "（离线模式）以下为规则机械检查结果，未做语义层面的逻辑评审；"
                       "配置大模型后可获得审稿人式的完整意见。",
            "issues": mech["issues"],
            "strengths": [],
            "verdict": "undetermined",
            "stats": mech["stats"],
            "mode": "mechanical",
        }

    try:
        raw = llm.chat(
            [{"role": "system", "content": _REVIEWER_PROMPT},
             {"role": "user", "content": f"稿件：\n{draft[:8000]}"}],
            temperature=0.2,
        )
    except Exception as exc:  # noqa: BLE001
        return {"overall": "审稿调用失败，仅给出机械检查结果。",
                "issues": mech["issues"], "strengths": [], "verdict": "undetermined",
                "stats": mech["stats"], "mode": "mechanical",
                "error": f"{type(exc).__name__}: {exc}"}

    m = re.search(r"\{.*\}", raw.content, re.DOTALL)
    if not m:
        return {"overall": "审稿结果无法解析，仅给出机械检查结果。",
                "issues": mech["issues"], "strengths": [], "verdict": "undetermined",
                "stats": mech["stats"], "mode": "mechanical"}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"overall": "审稿结果无法解析，仅给出机械检查结果。",
                "issues": mech["issues"], "strengths": [], "verdict": "undetermined",
                "stats": mech["stats"], "mode": "mechanical"}

    # 合并机械检查发现的高严重问题（避免 LLM 漏掉悬空引用）
    existing = {(i.get("title")) for i in data.get("issues", [])}
    for mi in mech["issues"]:
        if mi["severity"] == "high" and mi["title"] not in existing:
            data.setdefault("issues", []).insert(0, mi)
    data["stats"] = mech["stats"]
    data["mode"] = "llm"
    return data
