"""Resolve and describe the user-selected local paper scope.

The paper selector is a user boundary, not a hint for the model.  Keep the
lookup in one small module so foreground runs, background tasks, and direct
agent invocations enforce the same rules.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..models import Chunk, Paper


@dataclass
class PaperScopeError(ValueError):
    """A selected paper cannot be used as a local retrieval scope."""

    message: str
    status_code: int = 409

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class PaperScope:
    paper_id: str
    title: str
    status: str
    page_count: int
    retrieval_ready: bool = True
    unavailable_reason: str | None = None

    def as_dict(self) -> dict[str, str | int | bool | None]:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "status": self.status,
            "page_count": self.page_count,
            "retrieval_ready": self.retrieval_ready,
            "unavailable_reason": self.unavailable_reason,
        }


def describe(db: Session, paper_id: str | None) -> PaperScope | None:
    """Validate identity without making retrieval a prerequisite for chatting.

    A selected source remains selected even while parsing or awaiting OCR.
    Missing IDs still fail, so an invalid selection never becomes full-library
    retrieval. Tools must call the stricter ``resolve`` before reading it.
    """
    if not paper_id:
        return None
    paper = db.get(Paper, paper_id)
    if paper is None:
        raise PaperScopeError(f"所选论文不存在，无法检索：{paper_id}", status_code=404)
    unavailable_reason = None
    if paper.status != "parsed":
        if paper.status == "needs_ocr":
            unavailable_reason = (
                "所选论文是扫描件且尚未识别出有效正文（状态：待 OCR），"
                "无法基于原文回答；请重新上传更清晰的文件后再检索。"
            )
        else:
            unavailable_reason = (
                f"所选论文尚未解析完成，当前状态为「{paper.status}」，"
                "请等待解析完成后再检索"
            )
    elif db.query(Chunk.id).filter(Chunk.paper_id == paper.id).first() is None:
        unavailable_reason = "所选论文没有可检索的原文索引，请重新解析该论文"
    return PaperScope(
        paper_id=paper.id,
        title=(paper.title or "未命名论文").strip() or "未命名论文",
        status=paper.status,
        page_count=int(paper.page_count or 0),
        retrieval_ready=unavailable_reason is None,
        unavailable_reason=unavailable_reason,
    )


def resolve(db: Session, paper_id: str | None) -> PaperScope | None:
    """Require the selected source to be indexed before an actual retrieval."""
    scope = describe(db, paper_id)
    if scope and not scope.retrieval_ready:
        raise PaperScopeError(scope.unavailable_reason or "所选论文当前不可检索", status_code=409)
    return scope


def system_block(scope: PaperScope | None) -> str:
    """Return an explicit, model-visible retrieval boundary."""
    if scope is None:
        return (
            "# 本轮文献检索范围\n"
            "用户未选择单篇论文；本轮本地 retrieve_evidence 可以检索全部已解析论文。\n"
            "这只是需要文献检索时的可用范围，不代表用户要求检索或核对文献库；普通聊天不要列出或检索论文。\n"
            "如果用户随后明确选择论文，以最新一轮请求中的范围为准。"
        )
    availability = (
        "本轮所选论文的原文检索可用。\n" if scope.retrieval_ready else
        "本轮所选论文当前不可检索原文：" + (scope.unavailable_reason or "尚未准备就绪")
        + "\n普通问答、寒暄、模型身份等仍可直接回应；需要这篇论文原文时，说明当前限制，"
          "不能声称已经读取或核实其内容，不能自动改查其他论文。\n"
    )
    return (
        "# 本轮文献检索范围（服务端已锁定）\n"
        f"用户选择的论文标题：{scope.title}\n"
        f"用户选择的论文 ID：{scope.paper_id}\n"
        f"解析状态：{scope.status}；页数：{scope.page_count}\n"
        + availability +
        "本轮所有本地 retrieve_evidence 调用都会被服务端强制限制到这篇论文。"
        "不要用工具参数切换到其他 paper_id；需要检索时省略 paper_id，或使用上述 ID。"
        "如果证据为空，必须说明这篇选定论文中没有找到相关原文，不能改查其他论文。"
    )
