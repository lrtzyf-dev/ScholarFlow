"""配置诊断端点：只读返回 doctor 报告。"""
from __future__ import annotations

from fastapi import APIRouter

from ..diagnostics import run_diagnostics

router = APIRouter(prefix="/api/debug", tags=["diagnostics"])


@router.get("/config")
def debug_config():
    return run_diagnostics()
