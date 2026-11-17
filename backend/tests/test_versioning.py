"""语义版本去重纯函数测试，全程无外部依赖。"""
from app.pipeline.versioning import (
    VERSION_SIMILARITY_THRESHOLD,
    extract_arxiv_id,
    extract_arxiv_version,
    is_newer_version,
    is_same_work,
    normalize_arxiv_id,
    text_jaccard,
)


def test_extract_full_arxiv_id_keeps_version():
    assert extract_arxiv_id("https://arxiv.org/pdf/2401.12345v7") == "2401.12345v7"
    assert extract_arxiv_id("https://arxiv.org/abs/2401.12345") == "2401.12345"
    assert extract_arxiv_id("garbage") is None


# ---------- arXiv ID 归一化 ----------

def test_normalize_strips_version_from_bare_id():
    assert normalize_arxiv_id("2401.12345v7") == "2401.12345"
    assert normalize_arxiv_id("2401.12345v1") == "2401.12345"


def test_normalize_abs_and_pdf_urls():
    assert normalize_arxiv_id("http://arxiv.org/abs/2401.12345v7") == "2401.12345"
    assert normalize_arxiv_id("https://arxiv.org/pdf/2401.12345v2") == "2401.12345"
    assert normalize_arxiv_id("https://arxiv.org/abs/0704.0001") == "0704.0001"


def test_normalize_legacy_id_format():
    assert normalize_arxiv_id("cs.AI/0102033v3") == "cs.ai/0102033"
    assert normalize_arxiv_id("math/0211159") == "math/0211159"


def test_normalize_returns_none_for_garbage():
    assert normalize_arxiv_id("") is None
    assert normalize_arxiv_id(None) is None
    assert normalize_arxiv_id("not-an-arxiv-link") is None


# ---------- 版本号提取 ----------

def test_extract_version():
    assert extract_arxiv_version("2401.12345v7") == 7
    assert extract_arxiv_version("https://arxiv.org/abs/2401.12345v1") == 1


def test_extract_version_none_when_absent():
    assert extract_arxiv_version("2401.12345") is None
    assert extract_arxiv_version("garbage") is None


# ---------- 文本相似度 ----------

def test_jaccard_identical_and_disjoint():
    assert text_jaccard("same text here", "same text here") == 1.0
    assert text_jaccard("alpha beta", "gamma delta") == 0.0


def test_jaccard_partial_overlap():
    score = text_jaccard("a b c", "a b d")
    # 交集 {a,b}=2，并集 {a,b,c,d}=4
    assert score == 0.5


def test_jaccard_empty_is_zero():
    assert text_jaccard("", "a b") == 0.0


# ---------- 同作品判定 ----------

def test_is_same_work_threshold():
    base = "the transformer uses multi-head attention with eight heads and six layers"
    assert is_same_work(base, base) is True
    assert is_same_work(base, "a completely unrelated discussion of marine biology") is False


def test_threshold_is_between_zero_and_one():
    assert 0.0 < VERSION_SIMILARITY_THRESHOLD < 1.0


def test_threshold_calibrated_to_real_data():
    # 真实同论文 v1/v7 Jaccard=0.84，不同论文最高 0.12；阈值须落在二者之间
    assert VERSION_SIMILARITY_THRESHOLD == 0.7


def test_text_fallback_catches_high_but_rejects_low():
    # 构造 Jaccard 约 0.8 的文本（10 个共享词 + 2 个独有词 = 10/12 ≈ 0.83）
    shared = " ".join(f"w{i}" for i in range(10))
    near = shared + " x1 x2"
    assert is_same_work(shared, near) is True
    # 几乎不共享的文本应判为不同论文
    assert is_same_work(shared, "totally different vocabulary marine biology ocean") is False


# ---------- 版本新旧比较（Q8） ----------

def test_is_newer_when_incoming_higher():
    assert is_newer_version(7, 1) is True
    assert is_newer_version(2, 1) is True


def test_is_not_newer_when_incoming_lower_or_equal():
    assert is_newer_version(1, 7) is False
    assert is_newer_version(3, 3) is False


def test_existing_version_missing_not_treated_as_newer():
    # Q8 核心：现有记录无版本号（None）时，上传 v1 不能误判为更新
    assert is_newer_version(1, None) is False
    assert is_newer_version(7, None) is False
    assert is_newer_version(None, None) is False
    assert is_newer_version(None, 1) is False
