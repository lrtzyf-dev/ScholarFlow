"""元数据启发式抽取测试。"""
from reportlab.pdfgen import canvas

from app.parsing import parse_pdf
from app.parsing.metadata import extract_abstract, extract_authors, looks_like_formula


def _make_pdf(path, lines):
    """lines: (text, size, y_gap)，用较大间距模拟标题/作者/段落的分块排版。"""
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    y = 780
    for text, size, gap in lines:
        c.setFont("Helvetica", size)
        c.drawString(50, y, text)
        y -= gap
    c.showPage()
    c.save()


def test_abstract_inline(tmp_path):
    pdf = tmp_path / "a.pdf"
    _make_pdf(pdf, [
        ("Some Paper Title Here", 14, 40),
        ("Abstract: We present a novel method for retrieval.", 10, 38),
        ("1 Introduction", 11, 38),
    ])
    parsed = parse_pdf(pdf)
    assert extract_abstract(parsed).startswith("We present")


def test_abstract_standalone_block(tmp_path):
    pdf = tmp_path / "b.pdf"
    _make_pdf(pdf, [
        ("Some Paper Title Here", 14, 40),
        ("Abstract", 11, 38),
        ("This is the abstract body sentence.", 10, 38),
        ("1 Introduction", 11, 38),
    ])
    parsed = parse_pdf(pdf)
    assert extract_abstract(parsed) == "This is the abstract body sentence."


def test_abstract_missing_returns_empty(tmp_path):
    pdf = tmp_path / "c.pdf"
    _make_pdf(pdf, [
        ("Some Paper Title Here", 14, 40),
        ("1 Introduction", 11, 38),
        ("Just some body text without any abstract marker.", 10, 38),
    ])
    parsed = parse_pdf(pdf)
    assert extract_abstract(parsed) == ""


def test_extract_authors(tmp_path):
    pdf = tmp_path / "d.pdf"
    _make_pdf(pdf, [
        ("Some Paper Title Here", 14, 40),
        ("Alice Smith, Bob Jones and Carol Wei", 10, 38),
        ("1 Introduction", 11, 38),
    ])
    parsed = parse_pdf(pdf)
    authors = extract_authors(parsed)
    assert "Alice Smith" in authors
    assert "Bob Jones" in authors
    assert "Carol Wei" in authors


def test_affiliation_not_treated_as_authors(tmp_path):
    pdf = tmp_path / "e.pdf"
    _make_pdf(pdf, [
        ("Some Paper Title Here", 14, 40),
        ("Department of Computer Science, Stanford University", 10, 38),
        ("1 Introduction", 11, 38),
    ])
    parsed = parse_pdf(pdf)
    assert extract_authors(parsed) == ""


def test_looks_like_formula_positive():
    assert looks_like_formula("E = mc^2")
    assert looks_like_formula("f(x) = x^2 + 3x − 1")
    assert looks_like_formula("∇·E = ρ/ε₀")


def test_looks_like_formula_negative():
    assert not looks_like_formula("x = 1")  # 过短
    assert not looks_like_formula(
        "We propose a model that uses attention to encode sentences."
    )
    assert not looks_like_formula(
        "the result is statistically significant under the chosen threshold."
    )
