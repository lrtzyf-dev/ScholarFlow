"""推荐评分与可解释理由（纯函数，不做网络/模型 IO）。

embedder 编码与相似度计算在编排层完成，这里只接收已算好的相似度，
保证评分逻辑确定、可离线测试。
"""
from __future__ import annotations

import re

# 主题相关度与本地库相关度的权重
TOPIC_WEIGHT = 0.6
LOCAL_WEIGHT = 0.4
# 检测到开源代码仓库的加分（易于复现）
GITHUB_BONUS = 0.1
# 点赞/踩对排序分的影响
FEEDBACK_DELTA = 0.15
# 判定与本地库相关的阈值
LOCAL_RELEVANT = 0.5

_GITHUB_URL = re.compile(r"https?://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+")


def detect_github_url(text: str | None) -> str | None:
    """从摘要/评论中提取 GitHub 仓库地址；找不到返回 None，不编造。"""
    if not text:
        return None
    match = _GITHUB_URL.search(text)
    return match.group(0).rstrip(".,;)") if match else None


def score_candidate(topic_sim: float, local_max_sim: float, has_github: bool) -> float:
    """综合主题相关度、本地库相关度与开源加分，限制在 [0,1]。"""
    base = TOPIC_WEIGHT * topic_sim + LOCAL_WEIGHT * local_max_sim
    if has_github:
        base += GITHUB_BONUS
    return round(min(max(base, 0.0), 1.0), 4)


def build_reasons(
    topic_sim: float,
    local_max_sim: float,
    local_paper_title: str | None,
    has_github: bool,
) -> list[str]:
    """生成可解释、可溯源的推荐理由，全部来自真实计算。"""
    reasons = [f"与订阅主题相关度 {topic_sim:.2f}"]
    if local_paper_title and local_max_sim >= LOCAL_RELEVANT:
        reasons.append(f"与本地库《{local_paper_title}》相关度 {local_max_sim:.2f}")
    if has_github:
        reasons.append("检测到开源代码仓库，便于复现")
    return reasons


def feedback_adjust(score: float, feedback: str | None) -> float:
    """点赞/踩调整排序分；无反馈保持原值。"""
    if feedback == "up":
        return round(min(score + FEEDBACK_DELTA, 1.0), 4)
    if feedback == "down":
        return round(max(score - FEEDBACK_DELTA, 0.0), 4)
    return score
