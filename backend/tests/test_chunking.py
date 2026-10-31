"""切块测试。"""
from app.parsing import parse_pdf
from app.pipeline.chunking import chunk_parsed


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
