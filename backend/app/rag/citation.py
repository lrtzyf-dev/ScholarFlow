"""
引用解析与引用闸门：
- 答案中的 [^eN] 标记必须落在证据包范围内
- 非空内容先收为候选，由类型化范围审查判断是否需要来源；
  标题、短句、表格和过程前缀本身不能豁免科学事实
- 列表和表格可继承当前块的有效引用；分隔线、脚注定义和代码示例不作论断
- 输出 claims：每句论断、证据 id、supported
"""
from __future__ import annotations

import re

MARKER_RE = re.compile(r"\[\^e(\d+)\]")


def prose_for_citations(text: str, *, preserve_inline: bool = False) -> str:
    """Hide literal Markdown examples before checking source references.

    Backtick spans and fenced code display syntax, not source promises. Keep
    unmatched backticks as prose and honor matching fence characters/lengths.
    Auditing keeps inline values/identifiers, such as ``99.8%``, but never
    treats a marker inside a code span as a citation for its surrounding text.
    """
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*", line):
                fence = None
            lines.append("\n" if line.endswith("\n") else "")
            continue
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)", line)
        if opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = (opening[1][0], len(opening[1]))
            lines.append("\n" if line.endswith("\n") else "")
        else:
            lines.append(line)
    prose = "".join(lines)
    out, cursor = [], 0
    while cursor < len(prose):
        if prose[cursor] != "`":
            out.append(prose[cursor])
            cursor += 1
            continue
        run = re.match(r"`+", prose[cursor:])[0]
        end = re.search(r"(?<!`)" + re.escape(run) + r"(?!`)", prose[cursor + len(run):])
        if end:
            if preserve_inline:
                literal = prose[cursor + len(run):cursor + len(run) + end.start()]
                out.append(MARKER_RE.sub("", literal))
            else:
                out.append(" ")
            cursor += len(run) + end.end()
        else:
            out.append(run)
            cursor += len(run)
    return re.sub(r"(\\+)(\[\^e\d+\])", lambda match: match[1] + (
        " " * len(match[2]) if len(match[1]) % 2 else match[2]), "".join(out))

# 句末标点后的 [^eN] 标记归属本句（避免被切到下一句）
SENT_RE = re.compile(r"[^。！？!?\n]+[。！？!?]?(?:\s*\[\^e\d+\])*")

# Markdown 结构行（非论断）
HEADING_RE = re.compile(r"^\s*#{1,6}\s")
# A hash inside prose is literal content, including quoted source titles.
# Only a heading at the start of its own line is Markdown structure. Never
# repair malformed inline headings by deleting the rest of a source claim.
HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
FOOTNOTE_DEF_RE = re.compile(r"^\s*\[\^e\d+\]\s*:")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
# 行首 Markdown 强调/代码标记（判断元话语前缀前先剥离）
_LEAD_EMPHASIS_RE = re.compile(r"^[*_`\s]+")

# 免责说明、括号备注、过渡/元话语、标题行不需要引用
_EXEMPT_PREFIX = ("（", "(", "免责", "配置真实", "无法", "未检索", "知识库")
# 过程性、框架性语句（非可证伪的文献事实），不强制逐句引用
_META_PREFIX = _EXEMPT_PREFIX + (
    "说明", "坦白", "提示", "注意", "推荐", "建议", "我", "如果", "需要",
    "先说", "因此", "这是", "上述", "可行", "工作量",
    "本轮", "所以", "研究问题", "数据", "这", "与", "诚实", "本地", "而",
)

_MEANINGFUL_STRIP_RE = re.compile(r"[\s，。！？、；,.!?;:：()（）#*_|>\-`]")

# 元/过程/证据状态陈述：描述检索过程、证据局限或文档质量，而非可证伪的外部事实。
# 这类句子即便带引用标记，也不应被当作"需要文档证据支撑的事实论断"。
_META_PATTERNS = [
    # 第一人称过程叙述（我检索了 / 我另外搜索 / 我没有找到 / 我只能确认）
    r"我(?:另外|又|重新|已|曾)?(?:检索|搜索|查找|查询|尝试|查看|看|注意|没有|没找到|找不到|只能|认为|建议|推测|不做|不会|给出|确认|发现)",
    r"检索了|搜索了|关键词|返回的(?:是|仍)|没有出现|没有找到|没有检索到|未检索到",
    # 证据状态 / 局限（证据中没有、原文被截断、任务书没有给出、文档中未找到）
    r"(?:证据|原文|片段|文献|现有|当前|任务书|文档|文中|材料|本书)[^。]{0,14}(?:没有|未|缺失|无法|不完整|截断|不含|只)",
    r"无法(?:确认|给出|提取|可靠|判断|读出|回答|得到)",
    r"(?:不能|无法|不会)(?:推测|猜测|外推|断言)",
    r"只能(?:确认|给出|看到|得到)",
    r"缺失|未提及|没有明确|不完整|被截断|不做推测|不能推测|没有给出|未找到|未检索到|没检索到",
    # 建议性兜底（如需…以官方通知为准）
    r"以[^。]{0,12}(?:为准|通知为准)|如需[^。]{0,12}建议",
    # 文档 / 提取质量（文本层错乱、水印穿插、第8页只有零散残片）
    r"(?:文本层|版式|排版|水印|页眉|提取|解析)[^。]{0,12}(?:错乱|乱|问题|噪声|残片|无法|错误|拆|穿插|截断)",
    r"残片|零散|无法提取|乱序|错乱|被切碎|重排",
    # 引导 / 结论
    r"^(?:结论|综上|因此|所以|说明|坦白|提示|注意|建议|推荐|这意味着|需要说明)",
]
_META_RE = re.compile("|".join(_META_PATTERNS))


def is_meta_statement(text: str) -> bool:
    """Legacy presentation hint only; never proof of citation/audit exemption."""
    if not text:
        return True
    stripped = _LEAD_EMPHASIS_RE.sub("", text).strip()
    if not stripped:
        return True
    if stripped.endswith("?") or stripped.endswith("？"):
        return True
    if stripped.startswith(_META_PREFIX):
        return True
    return bool(_META_RE.search(stripped))


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in SENT_RE.findall(text) if s.strip()]


def parse_claims(answer: str, evidence_ids: list[str]) -> list[dict]:
    """Audit the same list/table citation scope accepted by citation_gate.

    A final marker on a bullet can cover several facts. Previously only its
    last sentence reached semantic auditing, leaving earlier numbers unchecked.
    Inheritance never crosses the current bullet/table block into later prose.
    """
    claims = []
    def collect(text, inherited=()):
        for sent in split_sentences(text):
            markers = [f"e{n}" for n in MARKER_RE.findall(sent)] or list(inherited)
            if not markers:
                continue
            valid = list(dict.fromkeys(m for m in markers if m in evidence_ids))
            clean = MARKER_RE.sub("", sent).strip()
            if _meaningful_len(clean):
                claims.append({"claim": clean, "evidence_ids": valid,
                               "supported": bool(valid) and all(m in evidence_ids for m in markers)})
    lines = prose_for_citations(answer, preserve_inline=True).splitlines()
    previous, inherited, in_list, i = "", [], False, 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line:
            # Markdown commonly separates a cited introduction from its list
            # with a blank line. The gate uses the nearest nonempty line.
            in_list, inherited = False, []
            continue
        if HEADING_RE.match(line) or HR_RE.match(line) or FOOTNOTE_DEF_RE.match(line):
            if HEADING_RE.match(line):
                collect(line)
            in_list, inherited, previous = False, [], line
            continue
        if line.startswith("|"):
            rows = [line]
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip()); i += 1
            markers = _valid_markers(" ".join(rows), evidence_ids)
            for index, row in enumerate(rows):
                if TABLE_SEP_RE.match(row):
                    continue
                collect(row, _valid_markers(row, evidence_ids) or markers)
            in_list, inherited, previous = False, [], rows[-1]
            continue
        if LIST_ITEM_RE.match(line):
            if not in_list:
                inherited = _valid_markers(previous, evidence_ids)
            in_list = True
            collect(LIST_ITEM_RE.sub("", line, count=1), _valid_markers(line, evidence_ids) or inherited)
            previous = line
        else:
            in_list, inherited = False, []
            paragraph = [line]
            while i < len(lines):
                candidate = lines[i].strip()
                if (not candidate or HEADING_RE.match(candidate) or HR_RE.match(candidate)
                        or FOOTNOTE_DEF_RE.match(candidate) or candidate.startswith("|") or LIST_ITEM_RE.match(candidate)):
                    break
                paragraph.append(candidate)
                i += 1
            collect(" ".join(paragraph).strip())
            previous = paragraph[-1]
    return claims


def _meaningful_len(text: str) -> int:
    """去掉空白与常见 Markdown/标点符号后的实质长度。"""
    return len(_MEANINGFUL_STRIP_RE.sub("", text))


def _valid_markers(text: str, evidence_ids: list[str]) -> list[str]:
    return [f"e{n}" for n in MARKER_RE.findall(text) if f"e{n}" in evidence_ids]


def citation_gate(answer: str, evidence_ids: list[str]) -> dict:
    """返回 {passed, unsupported, invalid_markers, marked_claims, total_claims, grounded_ratio}。

    passed 仍为严格口径（无无效标记且全部实质论断被引用）；
    grounded_ratio 供 finalize 阶段做"高比例溯源即放行"的判定。
    """
    answer = prose_for_citations(answer, preserve_inline=True)
    invalid_markers = [
        m for m in (f"e{n}" for n in MARKER_RE.findall(answer)) if m not in evidence_ids
    ]

    marked_claims = 0
    unsupported: list[str] = []
    total = 0

    lines = answer.splitlines()
    n = len(lines)
    i = 0
    while i < n:
        raw = lines[i]
        line = raw.strip()
        i += 1
        if not line:
            continue
        # Only literal structural syntax is intrinsically nonfactual. Heading
        # text may contain a result; its scope is decided like other content.
        if HR_RE.match(line) or FOOTNOTE_DEF_RE.match(line):
            continue
        if HEADING_RE.match(raw):
            clean = MARKER_RE.sub("", line).strip()
            if _meaningful_len(clean):
                total += 1
                if _valid_markers(line, evidence_ids):
                    marked_claims += 1
                else:
                    unsupported.append(clean[:80])
            continue

        # Unmarked tables are still source candidates. Labels can be exempted
        # by the scope review; values cannot disappear because a row is short.
        if line.startswith("|"):
            block = [line]
            while i < n and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            content_rows = [b for b in block if not TABLE_SEP_RE.match(b)]
            joined = " ".join(block)
            block_markers = _valid_markers(joined, evidence_ids)
            for row in content_rows:
                clean = MARKER_RE.sub("", row).strip()
                if _meaningful_len(clean):
                    total += 1
                    if block_markers:
                        marked_claims += 1
                    else:
                        unsupported.append(clean[:80])
            continue

        # 3) 列表块：引导句（紧邻的非空行）带有效标记时，列表项整体继承
        if LIST_ITEM_RE.match(raw):
            items = [line]
            while i < n and LIST_ITEM_RE.match(lines[i]):
                items.append(lines[i].strip())
                i += 1
            intro_markers: list[str] = []
            for j in range(i - len(items) - 1, -1, -1):
                prev = lines[j].strip()
                if prev:
                    intro_markers = _valid_markers(prev, evidence_ids)
                    break
            intro_grounded = bool(intro_markers)
            for it in items:
                body = LIST_ITEM_RE.sub("", it, count=1).strip()
                if not _meaningful_len(MARKER_RE.sub("", body)):
                    continue
                markers = _valid_markers(it, evidence_ids)
                total += 1
                if intro_grounded or markers:
                    marked_claims += 1
                else:
                    unsupported.append(MARKER_RE.sub("", body)[:80])
            continue

        # 4) 普通段落：聚合连续散文行，再逐句判定
        para_lines = [line]
        while i < n:
            nxt_raw = lines[i]
            nxt = nxt_raw.strip()
            if (
                not nxt
                or HEADING_RE.match(nxt_raw)
                or HR_RE.match(nxt)
                or FOOTNOTE_DEF_RE.match(nxt)
                or nxt.startswith("|")
                or LIST_ITEM_RE.match(nxt_raw)
            ):
                break
            para_lines.append(nxt)
            i += 1
        paragraph = " ".join(para_lines).strip()
        for sent in split_sentences(paragraph):
            clean = MARKER_RE.sub("", sent).strip()
            if not _meaningful_len(clean):
                continue
            total += 1
            markers = _valid_markers(sent, evidence_ids)
            if markers:
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
        "grounded_ratio": (marked_claims / total if total else 1.0),
    }


def strip_markers(text: str) -> str:
    return MARKER_RE.sub("", text)
