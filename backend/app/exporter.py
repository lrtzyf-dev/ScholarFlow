"""多格式导出：综述/答案导出 Markdown、Word；参考文献导出 BibTeX、RIS。

复用论文元数据（标题、作者、年份、arXiv id），不引入网络依赖。
"""
from __future__ import annotations

import io
import re
import unicodedata

from sqlalchemy.orm import Session

from .models import Paper


def _year(published_at: str | None) -> str:
    if not published_at:
        return ""
    m = re.search(r"\d{4}", str(published_at))
    return m.group(0) if m else ""


def collect_papers(db: Session, evidence: list[dict]) -> list[dict]:
    """从证据里去重收集论文元数据。"""
    meta: list[dict] = []
    seen: set[str] = set()
    for ev in evidence:
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


def build_markdown(question: str, answer: str, papers: list[dict]) -> str:
    lines = [f"# {question}", "", answer, ""]
    if papers:
        lines += ["## 参考文献", ""]
        for i, p in enumerate(papers, 1):
            authors = p.get("authors", "")
            year = f", {p['year']}" if p.get("year") else ""
            url = f". {p['url']}" if p.get("url") else ""
            lines.append(f"[{i}] {authors}{year}. {p['title']}{url}")
    return "\n".join(lines)


def build_docx(question: str, answer: str, papers: list[dict]) -> bytes:
    from docx import Document
    from docx.shared import Pt

    doc = Document()
    doc.add_heading(question, level=1)
    for para in answer.split("\n"):
        para = para.strip()
        if not para:
            continue
        if para.startswith("# "):
            doc.add_heading(para[2:], level=2)
        elif para.startswith("## "):
            doc.add_heading(para[3:], level=2)
        elif para.startswith("### "):
            doc.add_heading(para[4:], level=3)
        else:
            doc.add_paragraph(para)
    if papers:
        doc.add_heading("参考文献", level=2)
        for i, p in enumerate(papers, 1):
            authors = p.get("authors", "")
            year = f", {p['year']}" if p.get("year") else ""
            doc.add_paragraph(f"[{i}] {authors}{year}. {p['title']}.", style="List Number")
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
