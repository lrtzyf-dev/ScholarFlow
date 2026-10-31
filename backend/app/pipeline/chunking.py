"""
结构切块：
- 标题启发式（常见章节名 / 数字编号 + 字号偏大）
- 200–400 字块，段落边界切分，记录父块（节）
- 表格、图注独立成块；References 段落抽为参考文献条目
"""
from __future__ import annotations

import re

# 数字编号标题（1 / 1.2 / 3.2.1）或常见英文章节名
NUMBERED_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+\S.{1,60}$")
NAMED_HEADING = re.compile(
    r"^(abstract|introduction|related work|background|method|methods|methodology|"
    r"experiments|results|discussion|conclusion|conclusions|references|appendix|"
    r"acknowledgments|摘要|关键词|引言|绪论|背景|相关工作|方法|实验|结果|讨论|结论|参考文献|致谢|附录)\.?\s*$",
    re.IGNORECASE,
)
REF_ENTRY = re.compile(r"^\s*[\[(]?(\d{1,3})[\])]?\s+\S")


def _is_heading(block: dict, body_size: float) -> bool:
    text = block["text"].strip()
    if block["kind"] in ("table",):
        return False
    if len(text) > 80:
        return False
    if NAMED_HEADING.match(text):
        return True
    if NUMBERED_HEADING.match(text):
        return True
    if body_size and block.get("size", 0) >= body_size + 1.5 and len(text) <= 40:
        return True
    return False


def _median(nums: list[float]) -> float:
    if not nums:
        return 0.0
    nums = sorted(nums)
    mid = len(nums) // 2
    return nums[mid] if len(nums) % 2 else (nums[mid - 1] + nums[mid]) / 2


def chunk_parsed(parsed: dict, max_chars: int = 380) -> tuple[list[dict], list[dict]]:
    """返回 (chunks, references)。chunk 字段：page/bbox/section/kind/content/parent_index。"""
    all_sizes = [b["size"] for p in parsed["pages"] for b in p["blocks"] if b["kind"] == "text" and b["size"]]
    body_size = _median(all_sizes)

    chunks: list[dict] = []
    references: list[dict] = []
    section = ""
    parent_index = 0
    in_refs = False
    buf: list[str] = []
    buf_meta: dict = {}

    def flush() -> None:
        nonlocal buf, buf_meta
        if not buf:
            return
        content = "\n".join(buf).strip()
        if content:
            chunks.append(
                {
                    "chunk_index": len(chunks),
                    "parent_index": parent_index,
                    "page": buf_meta.get("page", 1),
                    "bbox": buf_meta.get("bbox"),
                    "section": section,
                    "kind": buf_meta.get("kind", "text"),
                    "content": content,
                }
            )
        buf, buf_meta = [], {}

    # 展平为行级流：同一块内多行也能识别标题/参考文献条目（合成 PDF 常见）
    line_stream: list[dict] = []
    for page in parsed["pages"]:
        for block in page["blocks"]:
            raw_text = block["text"].strip()
            if not raw_text:
                continue
            if block["kind"] in ("table", "caption"):
                line_stream.append({"page": page["page"], "bbox": block["bbox"], "kind": block["kind"],
                                    "size": block.get("size", 0.0), "text": raw_text})
                continue
            for ln in raw_text.split("\n"):
                ln = ln.strip()
                if ln:
                    line_stream.append({"page": page["page"], "bbox": block["bbox"], "kind": block["kind"],
                                        "size": block.get("size", 0.0), "text": ln})

    for item in line_stream:
        text = item["text"]

        if _is_heading(item, body_size):
            flush()
            section = text[:200]
            parent_index = len(chunks)
            in_refs = bool(re.search(r"(?:^|\s)(references|参考文献)\.?$", section, re.IGNORECASE))
            continue

        if in_refs:
            # 参考文献条目按行收集
            m = REF_ENTRY.match(text)
            if m:
                references.append({"ref_index": int(m.group(1)), "raw_text": text, "title": text[:200]})
            elif references:
                references[-1]["raw_text"] += " " + text
                references[-1]["title"] = references[-1]["raw_text"][:200]
            continue

        if item["kind"] in ("table", "caption"):
            flush()
            chunks.append(
                {
                    "chunk_index": len(chunks),
                    "parent_index": parent_index,
                    "page": item["page"],
                    "bbox": item["bbox"],
                    "section": section,
                    "kind": item["kind"],
                    "content": text,
                }
            )
            continue

        if not buf:
            buf_meta = {"page": item["page"], "bbox": item["bbox"], "kind": "text"}
        buf.append(text)
        if sum(len(x) for x in buf) >= max_chars:
            flush()

    flush()
    # 回填父块索引后修正：parent_index 指向本节首块，保持已设置值即可
    return chunks, references
