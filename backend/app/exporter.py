"""多格式导出：综述/答案导出 Markdown、Word；参考文献导出 BibTeX、RIS。

复用论文元数据（标题、作者、年份、arXiv id），不引入网络依赖。
"""
from __future__ import annotations

import io
import json
import re
import unicodedata

from sqlalchemy.orm import Session

from .models import Paper


def _year(published_at: str | None) -> str:
    if not published_at:
        return ""
    m = re.search(r"\d{4}", str(published_at))
    return m.group(0) if m else ""


def collect_papers(db: Session, evidence: list[dict], *, preserve_evidence: bool = False) -> list[dict]:
    """从证据里去重收集论文元数据。"""
    meta: list[dict] = []
    seen: set[str] = set()
    for ev in evidence:
        if preserve_evidence:
            p = db.get(Paper, ev.get("paper_id")) if ev.get("paper_id") else None
            meta.append({**ev, "title": ev.get("source_title") or (p.title if p else ev.get("paper_title")) or "来源",
                         "authors": p.authors or "" if p else "", "year": _year(p.published_at) if p else "",
                         "arxiv_id": p.arxiv_id or "" if p else "",
                         "url": ev.get("url") or (f"https://arxiv.org/abs/{p.arxiv_id}" if p and p.arxiv_id else "")})
            continue
        pid = ev.get("paper_id")
        if pid and pid not in seen:
            seen.add(pid)
            p = db.query(Paper).filter(Paper.id == pid).first()
            if p:
                meta.append({
                    "title": p.title or ev.get("paper_title", ""),
                    "authors": p.authors or "",
                    "year": _year(p.published_at),
                    "arxiv_id": p.arxiv_id or "",
                    "url": f"https://arxiv.org/abs/{p.arxiv_id}" if p.arxiv_id else "",
                })
        elif not pid and ev.get("paper_title") not in seen:
            seen.add(ev.get("paper_title", ""))
            meta.append({"title": ev.get("paper_title", ""), "authors": "",
                         "year": "", "arxiv_id": "", "url": ""})
    return meta


def _cite_key(p: dict, i: int) -> str:
    first = ""
    if p.get("authors"):
        first = re.split(r"[，,、;；]", p["authors"])[0].strip()
    ascii_first = re.sub(r"[^a-zA-Z]", "", unicodedata.normalize("NFKD", first))
    return f"{ascii_first or 'ref'}{p.get('year') or i}"


def _reference_id(p: dict) -> str | None:
    value = str(p.get("id") or "")
    return value if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", value) else None


def build_markdown(question: str, answer: str, papers: list[dict]) -> str:
    lines = [f"# {question}", "", answer, ""]
    if papers:
        lines += ["## 参考文献", ""]
        for i, p in enumerate(papers, 1):
            authors = p.get("authors", "")
            year = f", {p['year']}" if p.get("year") else ""
            url = f". {p['url']}" if p.get("url") else ""
            reference = _reference_id(p)
            lines.append(f"[^{reference}]: {authors}{year}. {p['title']}{url}" if reference else f"[{i}] {authors}{year}. {p['title']}{url}")
            if p.get("revision_id"):
                lines.append(f"    来源版本：{p['revision_id']}；定位：{p.get('locator_id') or '整份资料'}；核验状态：{p.get('verification', 'unverified')}")
            if p.get('source_fingerprint'):
                lines.append("    来源指纹：" + p['source_fingerprint'])
            if p.get('location'):
                lines.append("    原文位置：" + json.dumps(p['location'], ensure_ascii=False, sort_keys=True))
            if p.get("quote"):
                lines.append("    原文：" + p["quote"].replace("\n", "\n    "))
            if p.get('revision_id'):
                lines.append("    原件未包含；需在有访问权限的研序实例中按来源指纹查找原件。")
            lines.append("")
    return "\n".join(lines)


def build_docx(question: str, answer: str, papers: list[dict]) -> bytes:
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from markdown_it import MarkdownIt
    from .word_math import latex_math

    doc = Document()
    zoom = doc.settings.element.find(qn('w:zoom'))
    if zoom is not None and zoom.get(qn('w:percent')) is None:
        zoom.set(qn('w:percent'), '100')
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.27), Inches(11.69)
    section.left_margin = section.right_margin = Inches(0.75)
    normal = doc.styles['Normal']
    normal.font.name, normal.font.size = 'Arial', Pt(11)
    normal.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), '宋体')
    normal.paragraph_format.space_after = Pt(7)
    # Word's default template/theme can otherwise supply a blue title rule
    # and colored headings. Keep research exports readable across machines.
    for name in ('Title', 'Subtitle', *(f'Heading {i}' for i in range(1, 10))):
        style = doc.styles[name]
        style.font.color.rgb = RGBColor(0, 0, 0)
        style.font.name = 'Arial'
        style.font.underline = False
        style.element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Microsoft YaHei')
        properties = style.element.get_or_add_pPr()
        for child in list(properties):
            if child.tag in {qn('w:pBdr'), qn('w:shd')}:
                properties.remove(child)
    # Also remove template-level title-adjacent rules, without touching any
    # data table borders or mathematical structure.
    for properties in doc.element.iter(qn('w:pPr')):
        for border in list(properties.findall(qn('w:pBdr'))):
            properties.remove(border)
    doc.add_heading(question, level=0)
    math_expressions = {}
    math_originals = {}
    math_prefix = 'SFMATH' + __import__('uuid').uuid4().hex + 'X'
    # Protect complete formulas before Markdown can reinterpret underscores,
    # backslashes or line breaks. Fenced and inline code remain literal.
    pattern = re.compile(r'(^ {0,3}(`{3,}|~{3,})[^\n]*\n[\s\S]*?^ {0,3}\2[ \t]*$|`+[^`\n]*`+)|(\$\$[\s\S]+?\$\$|(?<![\\$])\$[^$\n]+?(?<!\\)\$(?!\$)|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])', re.MULTILINE)
    def protect(match):
        if match.group(1): return match.group(0)
        expression = match.group(3)
        token = math_prefix + str(len(math_expressions)) + 'END'
        math_expressions[token] = expression[2:-2] if expression.startswith(('$$', '\\(', '\\[')) else expression[1:-1]
        math_originals[token] = expression
        return token
    protected_answer = pattern.sub(protect, answer)
    reference_anchors = {_reference_id(p): f"SFRef_{index}" for index, p in enumerate(papers, 1) if _reference_id(p)}
    def original_code(text):
        return re.sub(math_prefix + r'\d+END', lambda match: math_originals[match[0]], text)

    def math(paragraph, expression):
        try:
            paragraph._p.append(latex_math(expression.strip()))
        except Exception:
            paragraph.add_run('公式原文（未转换）：' + expression)

    def plain(paragraph, text, bold=False, italic=False):
        # Math is processed only in prose, never inside a code span or fence.
        parts = re.split('(' + math_prefix + r'\d+END)', text)
        for part in parts:
            if part in math_expressions:
                math(paragraph, math_expressions[part])
            else:
                cursor = 0
                for match in re.finditer(r'\[\^([A-Za-z][A-Za-z0-9_-]{0,99})\]', part):
                    run = paragraph.add_run(part[cursor:match.start()]); run.bold, run.italic = True if bold else None, True if italic else None
                    reference = match[1]
                    if reference in reference_anchors:
                        link = OxmlElement('w:hyperlink'); link.set(qn('w:anchor'), reference_anchors[reference])
                        run_node = OxmlElement('w:r'); properties = OxmlElement('w:rPr')
                        style = OxmlElement('w:rStyle'); style.set(qn('w:val'), 'Hyperlink'); properties.append(style)
                        run_node.append(properties); label = OxmlElement('w:t'); label.text = '[' + reference + ']'
                        run_node.append(label); link.append(run_node); paragraph._p.append(link)
                    else:
                        paragraph.add_run(match[0])
                    cursor = match.end()
                run = paragraph.add_run(part[cursor:]); run.bold, run.italic = True if bold else None, True if italic else None

    def inline(paragraph, token):
        bold = italic = False
        for child in token.children or []:
            if child.type == 'strong_open': bold = True
            elif child.type == 'strong_close': bold = False
            elif child.type == 'em_open': italic = True
            elif child.type == 'em_close': italic = False
            elif child.type == 'code_inline':
                run = paragraph.add_run(original_code(child.content)); run.font.name = 'Consolas'
            elif child.type in ('softbreak', 'hardbreak'):
                paragraph.add_run().add_break()
            elif child.type == 'image':
                paragraph.add_run('[图片：' + child.content + '，原始链接见 Markdown]')
            elif child.type == 'text': plain(paragraph, child.content, bold, italic)
            elif child.type == 'link_open':
                href = child.attrGet('href') or ''
                if href.startswith(('https://', 'http://')):
                    paragraph.add_run('(' + href + ') ')

    tokens = MarkdownIt('commonmark', {'html': False}).enable('table').parse(protected_answer)
    paragraph, table, cell = None, None, None
    table_row = -1
    list_types = []
    in_cell = False
    for token in tokens:
        if token.type == 'heading_open':
            paragraph = doc.add_heading('', level=min(6, int(token.tag[1:])))
        elif token.type in ('bullet_list_open', 'ordered_list_open'):
            list_types.append(token.type)
        elif token.type in ('bullet_list_close', 'ordered_list_close'):
            if list_types: list_types.pop()
        elif token.type == 'paragraph_open':
            if in_cell and cell is not None:
                paragraph = cell.paragraphs[0] if not cell.paragraphs[0].text else cell.add_paragraph()
            else:
                style = ('List Number' if list_types[-1] == 'ordered_list_open' else 'List Bullet') if list_types else None
                paragraph = doc.add_paragraph(style=style)
                if len(list_types) > 1: paragraph.paragraph_format.left_indent = Inches(0.25 * len(list_types))
        elif token.type == 'inline':
            if paragraph is None: paragraph = doc.add_paragraph()
            inline(paragraph, token)
        elif token.type in ('heading_close', 'paragraph_close'):
            paragraph = None
        elif token.type in ('fence', 'code_block'):
            paragraph = doc.add_paragraph()
            run = paragraph.add_run(original_code(token.content).rstrip('\n')); run.font.name = 'Consolas'; run.font.size = Pt(9)
            paragraph = None
        elif token.type == 'table_open':
            # Determine column count from the first table row's opening cells.
            start = next(i for i, candidate in enumerate(tokens) if candidate is token)
            count = 0
            for following in tokens[start + 1:]:
                if following.type in ('th_open', 'td_open'): count += 1
                if following.type == 'tr_close': break
            table = doc.add_table(rows=0, cols=max(1, count)); table.style = 'Table Grid'; table.autofit = False
            table_properties = table._tbl.tblPr
            borders = table_properties.find(qn('w:tblBorders'))
            if borders is None:
                borders = OxmlElement('w:tblBorders'); table_properties.append(borders)
            for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
                border = OxmlElement('w:' + edge)
                for key, value in {'val': 'single', 'sz': '4', 'color': 'D9D9D9'}.items():
                    border.set(qn('w:' + key), value)
                borders.append(border)
            margins = OxmlElement('w:tblCellMar')
            for side, size in (('top', '80'), ('bottom', '80'), ('left', '100'), ('right', '100')):
                margin = OxmlElement('w:' + side); margin.set(qn('w:w'), size); margin.set(qn('w:type'), 'dxa'); margins.append(margin)
            table_properties.append(margins)
            width = int((section.page_width - section.left_margin - section.right_margin) / max(1, count))
            for column in table.columns: column.width = width
            table_row = -1
        elif token.type == 'tr_open' and table is not None:
            table_row += 1; cells = table.add_row().cells; cell_index = 0
            for current in cells: current.width = table.columns[0].width
            if table_row == 0:
                header = OxmlElement('w:tblHeader'); table.rows[-1]._tr.get_or_add_trPr().append(header)
        elif token.type in ('th_open', 'td_open') and table is not None:
            cell = table.rows[-1].cells[cell_index]; cell_index += 1
            paragraph = cell.paragraphs[0]; in_cell = True
        elif token.type in ('th_close', 'td_close'):
            if cell is not None:
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                for item in cell.paragraphs:
                    item.paragraph_format.space_after = Pt(3)
                    item.paragraph_format.space_before = Pt(3)
                    if table_row == 0:
                        for run in item.runs: run.bold = True
                if table_row == 0:
                    fill = OxmlElement('w:shd'); fill.set(qn('w:fill'), 'EFEFEF'); cell._tc.get_or_add_tcPr().append(fill)
            in_cell = False; paragraph = None
        elif token.type == 'table_close':
            table = cell = None
    if papers:
        doc.add_heading("参考文献", level=2)
        for i, p in enumerate(papers, 1):
            authors = p.get("authors", "")
            year = f", {p['year']}" if p.get("year") else ""
            reference = _reference_id(p)
            paragraph = doc.add_paragraph(f"[{reference or i}] {authors}{year}. {p['title']}." + (f" {p['url']}" if p.get('url') else ''))
            if reference:
                start = OxmlElement('w:bookmarkStart'); start.set(qn('w:id'), str(i)); start.set(qn('w:name'), reference_anchors[reference])
                end = OxmlElement('w:bookmarkEnd'); end.set(qn('w:id'), str(i))
                paragraph._p.insert(0, start); paragraph._p.append(end)
            if p.get('revision_id'):
                verification = {'source_frozen_semantics_unverified': '来源版本已固定，结论仍待核验',
                    'unverified': '未核验', 'verified': '已核验', 'supported': '支持', 'conflict': '存在冲突',
                    'not_found': '本次未找到', 'not_processed': '未处理'}.get(p.get('verification'), p.get('verification') or '未核验')
                doc.add_paragraph(f"来源版本：{p['revision_id']}；定位：{p.get('locator_id') or '整份资料'}；核验状态：{verification}")
            if p.get('quote'): doc.add_paragraph('原文：' + p['quote'])
            if p.get('source_fingerprint'): doc.add_paragraph('来源指纹：' + p['source_fingerprint'])
            if p.get('location'): doc.add_paragraph('原文位置：' + json.dumps(p['location'], ensure_ascii=False, sort_keys=True))
            if p.get('revision_id'): doc.add_paragraph('原件未包含；需在有访问权限的研序实例中按来源指纹查找原件。')
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_bibtex(papers: list[dict]) -> str:
    entries = []
    for i, p in enumerate(papers, 1):
        key = _cite_key(p, i)
        kind = "article" if p.get("arxiv_id") else "misc"
        fields = [f"  title = {{{p.get('title','')}}}",
                  f"  author = {{{p.get('authors','')}}}"]
        if p.get("year"):
            fields.append(f"  year = {{{p['year']}}}")
        if p.get("arxiv_id"):
            fields.append(f"  journal = {{arXiv preprint arXiv:{p['arxiv_id']}}}")
            fields.append(f"  eprint = {{{p['arxiv_id']}}}")
            fields.append("  archivePrefix = {arXiv}")
        if p.get("url"):
            fields.append(f"  url = {{{p['url']}}}")
        entries.append(f"@{kind}{{{key},\n" + ",\n".join(fields) + "\n}")
    return "\n\n".join(entries)


def build_ris(papers: list[dict]) -> str:
    records = []
    for p in papers:
        lines = ["TY  - JOUR", f"TI  - {p.get('title','')}"]
        for a in re.split(r"[，,、;；]", p.get("authors", "")):
            a = a.strip()
            if a:
                lines.append(f"AU  - {a}")
        if p.get("year"):
            lines.append(f"PY  - {p['year']}")
        if p.get("url"):
            lines.append(f"UR  - {p['url']}")
        if p.get("arxiv_id"):
            lines.append(f"AN  - arXiv:{p['arxiv_id']}")
        lines.append("ER  - ")
        records.append("\n".join(lines))
    return "\n\n".join(records)
