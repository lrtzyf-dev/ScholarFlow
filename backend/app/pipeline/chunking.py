"""
结构切块：
- 标题启发式（常见章节名 / 数字编号 + 字号偏大）
- 200–400 字块，段落边界切分，记录父块（节）
- 表格、图注独立成块；References 区域整体缓冲后抽为参考文献条目
"""
from __future__ import annotations

import re

from ..parsing.metadata import looks_like_formula

# 数字编号标题（1 / 1.2 / 3.2.1）或常见英文章节名
NUMBERED_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,3})\s+\S.{1,60}$")
NAMED_HEADING = re.compile(
    r"^(abstract|introduction|related work|background|method|methods|methodology|"
    r"experiments|results|discussion|conclusion|conclusions|references|appendix|"
    r"acknowledgments|摘要|关键词|引言|绪论|背景|相关工作|方法|实验|结果|讨论|结论|参考文献|致谢|附录)\.?\s*$",
    re.IGNORECASE,
)
# 数字编号参考文献条目（[1] / 1 / (1) 后接空格+正文）
REF_ENTRY = re.compile(r"^\s*[\[(]?(\d{1,3})[\])]?\s+\S")

# 作者-年份格式：出版年份（可带 a/b 后缀，紧跟句号，前非数字/连字符，后非数字）
PUB_YEAR = re.compile(r"(?<![\d–\-])((?:19|20)\d{2}[a-z]?)\.(?!\d)")
# 参考文献区域结束信号：附录/致谢/字母编号章节（如 "A Additional Details"）
_REF_SECTION_AFTER = re.compile(r"^(?:appendix|附录|acknowledg|[A-E]\s+[A-Za-z])", re.IGNORECASE)
# 页码范围跨行：上一行以 –/- 结尾，下一行数字开头，需合并
_REF_RANGE_END = re.compile(r"[–\-]\s*$")
# 作者起始回溯：作者逗号（逗号后接大写名）
_REF_AUTHOR_COMMA = re.compile(r"[A-Z][A-Za-z\.\-]*,\s*[A-Z]")
_REF_SENT_BOUND = re.compile(r"\.\s+")
# 连写作者（无空格 "NameandName"）
_REF_CONCAT_AUTHOR = re.compile(r"[A-Z][A-Za-z]+and[A-Z]")


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


def _merge_ref_lines(region: list[dict]) -> tuple[list[str], list[dict]]:
    """页码范围跨行合并，返回 (合并行, 各行元信息)。"""
    lines: list[str] = []
    metas: list[dict] = []
    for r in region:
        if lines and _REF_RANGE_END.search(lines[-1]) and re.match(r"^\d", r["text"]):
            lines[-1] = lines[-1] + r["text"]
        else:
            lines.append(r["text"])
            metas.append({"page": r["page"], "bbox": r["bbox"]})
    return lines, metas


def _build_numbered_refs(region: list[dict]) -> list[dict]:
    """数字编号条目：按编号去重，分栏重复跳过，编号连续。"""
    entries: dict[int, dict] = {}
    current = None
    for r in region:
        m = REF_ENTRY.match(r["text"])
        if m:
            n = int(m.group(1))
            if n in entries:
                # 分栏重复的编号行：跳过，后续行不再追加
                current = None
                continue
            entries[n] = {"parts": [r["text"]], "page": r["page"], "bbox": r["bbox"]}
            current = n
        elif current is not None:
            entries[current]["parts"].append(r["text"])
    refs = []
    for n in sorted(entries):
        e = entries[n]
        raw = " ".join(e["parts"]).strip()
        refs.append({"ref_index": n, "raw_text": raw, "title": raw[:200],
                     "page": e["page"], "bbox": e["bbox"]})
    return refs


def _build_year_refs(region: list[dict]) -> list[dict]:
    """作者-年份条目：年份锚点 + 作者起始切分，ref_index 连续 1..N。"""
    lines, metas = _merge_ref_lines(region)
    # 每行在拼接 text 中的字符偏移
    offsets: list[int] = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln) + 1
    text = " ".join(lines)
    anchors = list(PUB_YEAR.finditer(text))

    starts: list[int] = []
    for i, anc in enumerate(anchors):
        seg_start = 0 if i == 0 else anchors[i - 1].end()
        seg = text[seg_start:anc.start()]
        s = None
        cm = _REF_AUTHOR_COMMA.search(seg)
        if cm:
            before = seg[:cm.start()]
            bounds = list(_REF_SENT_BOUND.finditer(before))
            s = seg_start + (bounds[-1].end() if bounds else 0)
        if s is None:
            cc = _REF_CONCAT_AUTHOR.search(seg)
            if cc:
                s = seg_start + cc.start()
        if s is None:
            s = seg_start
        starts.append(s)

    # 相邻相同起始去重
    cleaned = []
    for s, anc in zip(starts, anchors):
        if cleaned and s == cleaned[-1][0]:
            continue
        cleaned.append((s, anc))

    refs = []
    for k, (s, _anc) in enumerate(cleaned):
        end = cleaned[k + 1][0] if k + 1 < len(cleaned) else len(text)
        raw = text[s:end].strip()
        # 起始字符偏移映射回行，取该行元信息
        line_idx = max(j for j, off in enumerate(offsets) if off <= s)
        meta = metas[line_idx]
        refs.append({"ref_index": k + 1, "raw_text": raw, "title": raw[:200],
                     "page": meta["page"], "bbox": meta["bbox"]})
    return refs


def _numbered_series_top(region: list[dict]) -> int | None:
    """判断数字编号是否形成从 1 开始、缺号率 <10% 的连续序列。"""
    nums = set()
    for r in region:
        m = REF_ENTRY.match(r["text"])
        if m:
            nums.add(int(m.group(1)))
    if not nums:
        return None
    top = max(nums)
    if top < 5:
        return None
    missing = [n for n in range(1, top + 1) if n not in nums]
    if 1 in nums and len(missing) <= top * 0.1:
        return top
    return None


def _extract_references(region: list[dict]) -> list[dict]:
    """对缓冲好的参考文献区域统一抽条目：连续数字编号优先，否则年份锚点。"""
    if not region:
        return []
    if _numbered_series_top(region) is not None:
        return _build_numbered_refs(region)
    return _build_year_refs(region)


def chunk_parsed(parsed: dict, max_chars: int = 380) -> tuple[list[dict], list[dict]]:
    """返回 (chunks, references)。chunk 字段：page/bbox/section/kind/content/parent_index。"""
    all_sizes = [b["size"] for p in parsed["pages"] for b in p["blocks"] if b["kind"] == "text" and b["size"]]
    body_size = _median(all_sizes)

    chunks: list[dict] = []
    references: list[dict] = []
    section = ""
    parent_index = 0
    in_refs = False
    ref_region: list[dict] = []
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

    def flush_refs() -> None:
        nonlocal ref_region
        if ref_region:
            references.extend(_extract_references(ref_region))
            ref_region = []

    # 展平为行级流：同一块内多行也能识别标题/参考文献条目（合成 PDF 常见）
    line_stream: list[dict] = []
    for page in parsed["pages"]:
        for block in page["blocks"]:
            raw_text = block["text"].strip()
            if not raw_text:
                continue
            if block["kind"] in ("table", "caption", "figure"):
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

        if in_refs:
            # 参考文献区域内：仅严格命名标题（非 references）才作为退出标题。
            # 不能用通用 _is_heading，否则字号偏大的续行会被误判而中断区域。
            is_named = bool(NAMED_HEADING.match(text))
            is_ref_heading = bool(re.search(r"(references|参考文献)\.?$", text, re.IGNORECASE))
            if is_named and not is_ref_heading:
                flush()
                flush_refs()
                section = text[:200]
                parent_index = len(chunks)
                in_refs = False
                continue
            # 字母编号的附录/致谢标题：区域结束
            if _REF_SECTION_AFTER.match(text):
                flush_refs()
                in_refs = False
                section = text[:200]
                continue
            # 缓冲整个参考文献区域，结束后统一切分
            ref_region.append({"page": item["page"], "bbox": item["bbox"], "text": text})
            continue

        if _is_heading(item, body_size):
            flush()
            section = text[:200]
            parent_index = len(chunks)
            in_refs = bool(re.search(r"(?:^|\s)(references|参考文献)\.?$", section, re.IGNORECASE))
            continue

        if item["kind"] in ("table", "caption", "figure"):
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

        # 行级显示公式：独立成块，避免混入正文
        if looks_like_formula(text):
            flush()
            chunks.append(
                {
                    "chunk_index": len(chunks),
                    "parent_index": parent_index,
                    "page": item["page"],
                    "bbox": item["bbox"],
                    "section": section,
                    "kind": "formula",
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
    flush_refs()
    return chunks, references
