"""把引用图数据转为可渲染的拓扑图脚本（Mermaid / Graphviz DOT）。

纯函数、不依赖 LLM，输出确定性文本，便于离线单测与嵌入文档。
节点 id 为 hex（安全），仅标题需要转义。
"""
from __future__ import annotations

import re


def _safe_label(text: str, limit: int = 60) -> str:
    """清洗节点标题：去引号/括号冲突，折叠空白与换行，限制长度。"""
    text = re.sub(r"\s+", " ", (text or "").strip())
    text = text.replace('"', "'").replace("[", "(").replace("]", ")")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "未命名"


def _dedup_edges(edges: list[dict]) -> list[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[str, str]] = []
    for edge in edges:
        pair = (edge["source"], edge["target"])
        if pair in seen or pair[0] == pair[1]:
            continue
        seen.add(pair)
        pairs.append(pair)
    return pairs


def to_mermaid(nodes: list[dict], edges: list[dict]) -> str:
    """生成 Mermaid flowchart TD 脚本。"""
    lines = ["flowchart TD"]
    for node in nodes:
        label = _safe_label(node.get("title", ""))
        lines.append(f'    {node["id"]}["{label}"]')
    for source, target in _dedup_edges(edges):
        lines.append(f"    {source} --> {target}")
    if not nodes:
        lines.append('    empty["暂无节点"]')
    return "\n".join(lines)


def to_dot(nodes: list[dict], edges: list[dict]) -> str:
    """生成 Graphviz DOT 脚本。"""
    lines = ["digraph ScholarFlow {", "  rankdir=TB;"]
    for node in nodes:
        label = _safe_label(node.get("title", ""))
        lines.append(f'  {node["id"]} [label="{label}"];')
    for source, target in _dedup_edges(edges):
        lines.append(f"  {source} -> {target};")
    lines.append("}")
    return "\n".join(lines)


FORMATS = ("mermaid", "dot")


def render_diagram(nodes: list[dict], edges: list[dict], fmt: str = "mermaid") -> str:
    """按格式分发；未知格式回退为 mermaid。"""
    if fmt == "dot":
        return to_dot(nodes, edges)
    return to_mermaid(nodes, edges)
