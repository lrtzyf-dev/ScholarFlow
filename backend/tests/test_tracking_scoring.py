"""追踪推荐评分纯函数测试。"""
from app.tracking.scoring import (
    build_reasons,
    detect_github_url,
    feedback_adjust,
    score_candidate,
)


def test_detect_github_url():
    text = "Code is available at https://github.com/owner/repo for reproduction."
    assert detect_github_url(text) == "https://github.com/owner/repo"
    assert detect_github_url("no code link here") is None
    assert detect_github_url(None) is None


def test_score_candidate_weights_and_github_bonus():
    base = score_candidate(0.8, 0.5, has_github=False)
    # 0.6*0.8 + 0.4*0.5 = 0.48+0.20 = 0.68
    assert base == 0.68
    with_github = score_candidate(0.8, 0.5, has_github=True)
    assert with_github == 0.78
    # 上限为 1
    assert score_candidate(1.0, 1.0, has_github=True) == 1.0


def test_build_reasons_are_explainable():
    reasons = build_reasons(0.82, 0.76, "Attention Is All You Need", True)
    assert any("0.82" in r for r in reasons)
    assert any("Attention Is All You Need" in r for r in reasons)
    assert any("开源代码" in r for r in reasons)
    # 本地相关度不足时不显示本地理由
    weak = build_reasons(0.3, 0.2, "Some Paper", False)
    assert all("Some Paper" not in r for r in weak)


def test_feedback_adjust():
    assert feedback_adjust(0.5, "up") == 0.65
    assert feedback_adjust(0.5, "down") == 0.35
    assert feedback_adjust(0.5, None) == 0.5
    # 不越界
    assert feedback_adjust(0.95, "up") == 1.0
    assert feedback_adjust(0.05, "down") == 0.0
