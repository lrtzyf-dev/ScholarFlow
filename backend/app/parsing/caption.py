"""Figure Caption 生成。

真实 LLM 模式下由模型基于数据摘要生成学术图注；离线（MockLLM）时
不冒充模型生成，而是用基于真实统计的确定性模板，并显式标注 mode。
"""
from __future__ import annotations

from ..llm import MockLLM, get_llm


def template_caption(parsed: dict, chart_type: str) -> str:
    """离线确定性图注，所有数字均来自真实统计。"""
    count = parsed["row_count"]
    numeric = parsed["numeric_columns"]
    if not numeric:
        return f"图：{chart_type} 基于 {count} 条记录的分类展示，无数值统计列。"
    parts = []
    for col in numeric[:3]:
        stat = parsed["stats"][col]
        parts.append(
            f"{col}（均值 {stat['mean']:.2f}，范围 {stat['min']:g}–{stat['max']:g}）"
        )
    return f"图：{chart_type} 展示了 {count} 条记录的 " + "、".join(parts) + "。"


def _build_prompt(parsed: dict, chart_type: str) -> str:
    numeric = parsed["numeric_columns"]
    summary = "\n".join(
        f"- {col}: mean={parsed['stats'][col]['mean']:.3f}, "
        f"min={parsed['stats'][col]['min']}, max={parsed['stats'][col]['max']}"
        for col in numeric[:5]
    )
    return (
        "你是学术写作助手。请根据以下数据摘要，用一句符合学术规范的英文 "
        f"Figure Caption 描述这张 {chart_type}（记录数 {parsed['row_count']}）。"
        f"只描述数据中真实存在的内容，不要编造数据。\n{summary}"
    )


def generate_caption(parsed: dict, chart_type: str, llm=None) -> dict:
    """返回 {caption, mode}；mode 为 template（离线）或 llm（真实模型）。"""
    llm = llm or get_llm()
    if isinstance(llm, MockLLM):
        return {"caption": template_caption(parsed, chart_type), "mode": "template"}
    response = llm.chat(
        [{"role": "user", "content": _build_prompt(parsed, chart_type)}],
        temperature=0.3,
    )
    return {"caption": response.content.strip(), "mode": "llm"}
