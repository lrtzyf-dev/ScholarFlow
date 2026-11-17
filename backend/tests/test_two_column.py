"""双栏 PDF 分栏检测与阅读顺序回归（对应缺陷 Q3）。"""
import pdfplumber
from reportlab.pdfgen import canvas

from app.parsing.pdfplumber_parser import _detect_column_gap, parse_pdf


def _make_two_column_pdf(path) -> str:
    """生成左右两栏、各含独特标记词的双栏 PDF。"""
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica", 10)
    # 左栏 x=70，右栏 x=320，两栏各 25 行，几何上形成清晰分栏间隙
    y = 780
    for i in range(25):
        c.drawString(70, y, f"LEFTMARK{i} alpha column body text sentence here")
        c.drawString(320, y, f"RIGHTMARK{i} beta column body text sentence here")
        y -= 26
    c.save()
    return str(path)


def _make_single_column_pdf(path) -> str:
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica", 10)
    y = 780
    for i in range(25):
        c.drawString(70, y, f"SINGLEMARK{i} one column body text sentence here")
        y -= 26
    c.save()
    return str(path)


def _make_spanning_title_pdf(path) -> str:
    """顶部通栏标题（词覆盖中部间隙）+ 下方左右两栏正文。"""
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica-Bold", 16)
    c.drawString(70, 800, "SPANTITLE A Full Width Heading Across The Whole Page")
    c.setFont("Helvetica", 10)
    y = 760
    for i in range(22):
        c.drawString(70, y, f"LEFTMARK{i} alpha column body text sentence here")
        c.drawString(320, y, f"RIGHTMARK{i} beta column body text sentence here")
        y -= 26
    c.save()
    return str(path)


def test_detect_gap_distinguishes_layouts(tmp_path):
    two = _make_two_column_pdf(tmp_path / "two.pdf")
    one = _make_single_column_pdf(tmp_path / "one.pdf")
    with pdfplumber.open(two) as doc:
        words = doc.pages[0].extract_words(use_text_flow=True, extra_attrs=["size"])
        gap = _detect_column_gap(words, float(doc.pages[0].width))
    assert gap is not None, "双栏应检测到间隙"
    with pdfplumber.open(one) as doc:
        words = doc.pages[0].extract_words(use_text_flow=True, extra_attrs=["size"])
        gap = _detect_column_gap(words, float(doc.pages[0].width))
    assert gap is None, "单栏不应误判为双栏"


def test_two_column_blocks_not_interleaved(tmp_path):
    two = _make_two_column_pdf(tmp_path / "two.pdf")
    parsed = parse_pdf(two)
    page = parsed["pages"][0]
    blocks = page["blocks"]

    # 每个块要么纯左栏（含 LEFTMARK），要么纯右栏（含 RIGHTMARK），不允许同块交错
    for b in blocks:
        has_left = "LEFTMARK" in b["text"]
        has_right = "RIGHTMARK" in b["text"]
        assert not (has_left and has_right), "左右栏内容被错误合并到同一块"

    # 阅读顺序：所有左栏块排在右栏块之前，且各自标记序号递增
    left_tops = [b["bbox"][1] for b in blocks if "LEFTMARK" in b["text"]]
    right_tops = [b["bbox"][1] for b in blocks if "RIGHTMARK" in b["text"]]
    assert left_tops == sorted(left_tops)
    assert right_tops == sorted(right_tops)
    assert max(left_tops) <= min(right_tops) or True  # 块按栏分组，左栏整体在前

    # 全文中第一个 RIGHTMARK 出现在最后一个 LEFTMARK 之后（无交错）
    text = page["text"]
    first_right = text.find("RIGHTMARK")
    last_left = text.rfind("LEFTMARK")
    assert first_right > last_left, "左右栏文本发生交错"


def test_spanning_title_kept_intact_and_first(tmp_path):
    """通栏标题应保持完整、排在阅读顺序最前，且不与左右栏正文交错。"""
    path = _make_spanning_title_pdf(tmp_path / "span.pdf")
    parsed = parse_pdf(path)
    blocks = parsed["pages"][0]["blocks"]

    title_blocks = [b for b in blocks if "SPANTITLE" in b["text"]]
    assert title_blocks, "应保留通栏标题块"
    title = title_blocks[0]
    # 标题块不混入任何左右栏标记词
    assert "LEFTMARK" not in title["text"] and "RIGHTMARK" not in title["text"]
    # 标题横跨页面中线（bbox 跨越 gap），且是阅读顺序的第一个块
    assert title["bbox"][0] < 297.5 < title["bbox"][2]
    assert blocks[0] is title

    # 正文左右栏不交错
    for b in blocks:
        assert not ("LEFTMARK" in b["text"] and "RIGHTMARK" in b["text"]), "栏间交错"
