"""Per-run planning policy and atomic handoff from a reviewed plan."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator, field_validator

ExecutionMode = Literal["execute", "plan"]
PLAN_RESPONSE_TOOL_NAME = "respond_in_plan_mode"
# A local control decision, offered only while planning. It carries the answer
# in the same provider turn; it is not an external action or a provider field.
PLAN_RESPONSE_TOOL_SPEC = {
    "name": PLAN_RESPONSE_TOOL_NAME,
    "description": "仅在计划模式下，本轮实际需要普通答复或澄清而不是交付可执行计划时使用。kind为direct_response时answer是完整直接答复；kind为clarification时answer是需要用户补充的信息或问题。若用户本轮要求可供审阅后执行的计划，必须使用update_plan登记步骤，不能用本工具替代或宣称计划已完成。已有公开计划不能降级为直接答复；未完成的用户验收要求、工具失败和权限限制不会被本决定取消。必须单独调用，不能和其他工具放在同一批次。",
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "kind": {"type": "string", "enum": ["direct_response", "clarification"]},
        "answer": {"type": "string", "minLength": 1, "maxLength": 100000},
    }, "required": ["kind", "answer"]},
}
PLAN_READ_TOOLS = frozenset({
    "update_plan",
    "list_attached_sources", "read_attached_source",
    "list_research_artifacts", "list_research_artifact_versions", "read_research_artifact",
    "list_evidence_matrices", "read_evidence_matrix",
    "retrieve_evidence", "list_papers", "list_skills", "load_skill", "read_conversation_history",
    "list_project_files", "read_project_file", "search_project_files", "git_project_status", "git_project_diff", "read_project_command_output", "list_project_worktrees", "review_project_worktree_removal", "review_project_worktree_archive",
    "mcp__builtin__arxiv_search", "mcp__builtin__semantic_scholar_search", "mcp__builtin__crossref_search",
})
PLAN_PROMPT = """[SCHOLARFLOW_PLAN_MODE]
当前是用户选择的计划模式。根据本轮请求决定是否需要资料；已有信息足以制定计划时直接规划，
不要为规划强行列举文献库、扫描项目或读取文件。用户明确要求不读取时，保留未知项和假设，
只基于用户已提供的内容规划。仅在确有必要且获用户授权的范围内读取资料。
最后只提交供用户审阅的计划：目标、步骤、
将修改或产出的内容、待澄清问题、风险和验证方式。明确区分已读取的信息与尚未执行的步骤。
如果本轮要交付可执行计划，必须先调用 update_plan 登记真实步骤，再输出计划说明，才能供用户审阅后启动执行。
普通问答或澄清不需要制造计划；结合本轮完整请求与上下文决定是否交付计划，不按关键词分流。
本轮不交付计划时，单独调用 respond_in_plan_mode，把完整答复放入 answer，并以 kind 明确声明
direct_response 或 clarification；不要先输出答复正文再调用。澄清不会将未完成任务标为完成。
没有该明确决定的计划正文，仍必须由 update_plan 登记真实步骤才能视为可审阅计划。
不得执行写入、导入、命令、外部未知工具或子任务。技能、历史和目标不能解除此限制。
不要宣称计划中的操作已完成；用户会在界面审阅后另行启动执行。
"""


class ExecutionOptions(BaseModel):
    workspace_revision: int = Field(default=0, ge=0)
    project_id: str | None = Field(default=None, max_length=32)
    file_paths: list[str] = Field(default_factory=list, max_length=8)
    source_revision_ids: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("source_revision_ids")
    @classmethod
    def fixed_sources(cls, value):
        from .source_runtime import revision_ids
        return revision_ids(value)

    @field_validator("file_paths")
    @classmethod
    def bounded_paths(cls, value):
        if any(len(path) > 1024 for path in value):
            raise ValueError("文件相对路径过长")
        return list(dict.fromkeys(value))
    execution_mode: ExecutionMode = "execute"
    reviewed_plan_id: str | None = Field(default=None, min_length=1, max_length=64)
    retry_plan_run_id: str | None = Field(default=None, min_length=1, max_length=96)

    @model_validator(mode="after")
    def valid_mode(self):
        if self.execution_mode == "plan" and (self.reviewed_plan_id or self.retry_plan_run_id):
            raise ValueError("计划模式不能执行已审阅计划")
        if self.retry_plan_run_id and not self.reviewed_plan_id:
            raise ValueError("重试计划必须指定原计划")
        return self


class PlanConflict(ValueError):
    pass


def claim_reviewed_plan(db, plan_id, conversation_id, run_id, retry_of=None):
    from . import run_store, task_runner
    from .models import ForegroundRun, QARecord, TaskRecord

    if not plan_id:
        return ""
    source = db.get(QARecord, plan_id)
    if (source is None or source.conversation_id != conversation_id or source.execution_mode != "plan"
            or source.refusal or not source.answer.strip() or not source.plan_steps):
        raise PlanConflict("计划不存在、不可执行或不属于当前会话，请重新载入")
    claimed = source.plan_run_id
    if claimed and claimed != run_id:
        if retry_of != claimed:
            raise PlanConflict("该计划已提交执行，请查看原轮次或任务；中断后可明确选择重试")
        foreground = db.get(ForegroundRun, claimed)
        task = db.get(TaskRecord, claimed)
        if (claimed in run_store.active_runs() or claimed in task_runner.running_task_ids()
                or (foreground and foreground.status == "running")
                or (task and task.status in ("pending", "running"))):
            raise PlanConflict("原执行仍在运行或收尾，请结束后再重试")
        if not foreground and not task:
            raise PlanConflict("无法核对原执行状态，请重新生成计划")
    elif retry_of and not claimed:
        raise PlanConflict("原执行已发生变化，请刷新计划")
    source.plan_run_id = run_id
    db.flush()
    return source.answer


def reviewed_plan_text(db, plan_id, conversation_id, run_id):
    from .models import QARecord
    source = db.get(QARecord, plan_id) if plan_id else None
    if not source or source.conversation_id != conversation_id or source.plan_run_id != run_id:
        raise PlanConflict("已审阅计划与执行记录不匹配")
    return source.answer
