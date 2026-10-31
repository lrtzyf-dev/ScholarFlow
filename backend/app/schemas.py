"""Pydantic v2 请求/响应模型。"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from .execution_mode import ExecutionOptions


class PaperOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    arxiv_id: str | None
    title: str
    authors: str
    abstract: str
    page_count: int
    status: str
    error: str
    created_at: datetime
    # 命中相同内容哈希时为 True：本次未重复建论文，返回的是已存在记录
    duplicate: bool = False


class ChunkOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chunk_index: int
    page: int
    section: str
    kind: str
    content: str
    bbox: list[float] | None = None


class PageOut(BaseModel):
    page: int
    width: float
    height: float
    text: str
    blocks: list[dict]


class ChatRequest(ExecutionOptions):
    question: str
    paper_id: str | None = None  # 空 = 全库检索
    skills: list[str] | None = None  # 用户手动指定的技能名称（注入上下文）
    conversation_id: str | None = None  # 同一会话用于多轮记忆
    images: list[dict] | None = None  # 多模态图片：[{"media_type":"image/png","data":"<base64>"}]
    run_id: str | None = Field(default=None, pattern=r"^chat_[A-Za-z0-9_-]{8,64}$")


class ApprovalDecision(BaseModel):
    approval_id: str
    decision: str  # approve / deny
    scope: str = "once"  # once / session


class EvidenceOut(BaseModel):
    id: str
    paper_id: str
    paper_title: str
    chunk_id: str
    page: int
    bbox: list[float] | None
    quote: str
    score: float


class ClaimOut(BaseModel):
    claim: str
    supported: bool
    evidence_ids: list[str]


class HealthOut(BaseModel):
    status: str
    llm_mode: str
    embedding_mode: str
    llm_model: str
    paper_count: int
