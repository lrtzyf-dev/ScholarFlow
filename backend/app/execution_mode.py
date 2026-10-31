"""Per-run planning policy and atomic handoff from a reviewed plan."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator, field_validator

ExecutionMode = Literal["execute", "plan"]
PLAN_READ_TOOLS = frozenset({
    "retrieve_evidence", "list_papers", "list_skills", "load_skill", "read_conversation_history",
    "list_project_files", "read_project_file", "search_project_files", "git_project_status", "git_project_diff", "read_project_command_output",
    "mcp__builtin__arxiv_search", "mcp__builtin__semantic_scholar_search", "mcp__builtin__crossref_search",
})
PLAN_PROMPT = """[SCHOLARFLOW_PLAN_MODE]
当前是用户选择的计划模式。先读取必要资料，最后只提交供用户审阅的计划：目标、步骤、
将修改或产出的内容、待澄清问题、风险和验证方式。明确区分已读取的信息与尚未执行的步骤。
不得执行写入、导入、命令、外部未知工具或子任务。技能、历史和目标不能解除此限制。
不要宣称计划中的操作已完成；用户会在界面审阅后另行启动执行。
"""


class ExecutionOptions(BaseModel):
    workspace_revision: int = Field(default=0, ge=0)
    project_id: str | None = Field(default=None, max_length=32)
    file_paths: list[str] = Field(default_factory=list, max_length=8)

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
            or source.refusal or not source.answer.strip()):
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
