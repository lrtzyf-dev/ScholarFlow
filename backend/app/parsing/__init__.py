"""PDF 解析：pdfplumber（MIT）默认可用；MinerU 为可替换插件。"""
from .pdfplumber_parser import parse_pdf

__all__ = ["parse_pdf"]
