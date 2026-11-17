"""CSV 数据可视化 API：解析 CSV、生成 Figure Caption。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..parsing.caption import generate_caption
from ..parsing.csv_parser import parse_csv

router = APIRouter(prefix="/api/visualization", tags=["visualization"])


class CsvRequest(BaseModel):
    content: str


class CaptionRequest(BaseModel):
    data: dict
    chart_type: str = "bar chart"


@router.post("/parse")
def parse_csv_api(req: CsvRequest):
    return parse_csv(req.content)


@router.post("/caption")
def caption_api(req: CaptionRequest):
    return generate_caption(req.data, req.chart_type)
