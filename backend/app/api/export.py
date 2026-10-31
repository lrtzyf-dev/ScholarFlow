"""多格式导出端点：返回可下载文件。"""
from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..db import get_db
from ..exporter import (
    build_bibtex, build_docx, build_markdown, build_ris, collect_papers,
)

router = APIRouter(prefix="/api/export", tags=["export"])


class ExportRequest(BaseModel):
    question: str
    answer: str
    evidence: list[dict]
    format: str  # md / docx / bibtex / ris


def _content_disposition(base: str, ext: str) -> str:
    fname = f"ScholarFlow-{base}.{ext}"
    # RFC 5987：非 ASCII 文件名用 filename* 编码，同时给 ASCII 回退
    return f"attachment; filename=\"ScholarFlow.{ext}\"; filename*=UTF-8''{quote(fname)}"


@router.post("")
def export(req: ExportRequest, db: Session = Depends(get_db)):
    papers = collect_papers(db, req.evidence)
    fmt = req.format.lower()
    if fmt == "md":
        text = build_markdown(req.question, req.answer, papers)
        return Response(
            content=text.encode("utf-8"),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": _content_disposition("综述", "md")},
        )
    if fmt == "docx":
        data = build_docx(req.question, req.answer, papers)
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": _content_disposition("综述", "docx")},
        )
    if fmt == "bibtex":
        text = build_bibtex(papers)
        return Response(
            content=text.encode("utf-8"),
            media_type="application/x-bibtex; charset=utf-8",
            headers={"Content-Disposition": _content_disposition("参考文献", "bib")},
        )
    if fmt == "ris":
        text = build_ris(papers)
        return Response(
            content=text.encode("utf-8"),
            media_type="application/x-research-info-systems; charset=utf-8",
            headers={"Content-Disposition": _content_disposition("参考文献", "ris")},
        )
    return Response(content=json.dumps({"error": f"unsupported format: {fmt}"}),
                    status_code=400, media_type="application/json")
