"""切块测试。"""
import pytest

from app.parsing import InvalidPdfError, parse_pdf
from app.pipeline.chunking import chunk_parsed


def test_broken_pdf_raises_invalid_pdf_error(tmp_path):
    # 损坏 PDF 在解析层即抛 InvalidPdfError（客户端错误），而非底层 PdfiumError
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4" + bytes((i % 256 for i in range(200))))
    with pytest.raises(InvalidPdfError):
        parse_pdf(broken)


def test_chunk_parsed_has_sections_and_refs(sample_pdf):
    parsed = parse_pdf(sample_pdf)
    chunks, refs = chunk_parsed(parsed)
    assert parsed["page_count"] == 2
    assert len(chunks) >= 2
    # 至少识别出 Introduction / Model Architecture 等章节
    sections = {c["section"] for c in chunks if c["section"]}
    assert any("Introduction" in s or "Model" in s for s in sections)
    # 参考文献条目被抽出
    assert len(refs) >= 2
    assert refs[0]["ref_index"] == 1
    # 每个块都带页码
    assert all(c["page"] >= 1 for c in chunks)
