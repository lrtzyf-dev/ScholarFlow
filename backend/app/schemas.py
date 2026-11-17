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
    # 语义版本去重：同一论文不同版本（如 arXiv v1/v7）时为 True，两条记录均保留
    version_duplicate: bool = False
    existing_id: str | None = None
    incoming_version: int | None = None
    existing_version: int | None = None
    newer: bool = False
    recommend: str | None = None
    source_id: str | None = None
    source_revision_id: str | None = None
    source_conversation_id: str | None = None
    source_media_type: str | None = None
    document_format: str | None = None


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
    resume_checkpoint: bool = False
    resume_request_id: str | None = Field(default=None, min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_-]+$")
    expected_run_revision: int | None = Field(default=None, ge=0, strict=True)
    additional_steps: int = Field(default=6, ge=1, le=24)


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
    source_id: str | None = None
    revision_id: str | None = None
    locator_id: str | None = None
    source_title: str | None = None
    source_kind: str | None = None
    source_location: dict | None = None


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
