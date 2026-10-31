"""
MinerU 解析插件（占位，W2 接入）。
商业版注意：MinerU 为 AGPL-3.0，通过独立进程/HTTP 调用隔离，避免协议传染。
接入方式：pip install mineru，按官方 CLI/API 解析 PDF，再把 content_list.json
转换为与 pymupdf_parser.parse_pdf 相同的结构（pages/blocks/bbox/kind）。
"""
from __future__ import annotations

from pathlib import Path


class MinerUNotInstalled(RuntimeError):
    pass


def parse_pdf(pdf_path: str | Path, out_path: str | Path | None = None) -> dict:
    raise MinerUNotInstalled(
        "MinerU 解析器尚未安装（W2 接入）。请先使用默认 PyMuPDF 解析器；"
        "扫描件需要 OCR 时再 pip install mineru 并实现本适配。"
    )
