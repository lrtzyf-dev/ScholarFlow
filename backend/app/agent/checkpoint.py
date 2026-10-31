"""Versioned execution state and durable boundaries around external operations."""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable, Literal
from uuid import uuid4
import time

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ..execution_mode import ExecutionMode


class CheckpointNeedsReview(ValueError):
    pass


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    phase: Literal["setup", "model", "tools", "finalize", "deliver", "completed"] = "setup"
    question: str
    execution_mode: ExecutionMode = "execute"
    reviewed_plan_id: str | None = None
    reviewed_plan: str = ""
    conversation_id: str | None = None
    paper_id: str | None = None
    workspace: dict | None = None
    skills: list[str] = Field(default_factory=list)
    messages: list[dict] = Field(default_factory=list)
    todo: list[dict] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    evidence_counter: int = Field(default=0, ge=0)
    usage: dict[str, int] = Field(default_factory=lambda: {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    })
    step: int = Field(default=0, ge=0)
    max_steps: int = Field(default=6, ge=1)
    calls: list[dict] = Field(default_factory=list)
    tool_index: int = Field(default=0, ge=0)
    operations: dict[str, Any] = Field(default_factory=dict)
    pending_operation: dict | None = None
    resolution: Literal["retry", "skip"] | None = None
    answer: str = ""
    result: dict | None = None
    record_id: str = Field(default_factory=lambda: uuid4().hex)
    elapsed_ms: int = 0

    @model_validator(mode="after")
    def consistent_execution(self):
        if self.phase == "tools" and (not self.calls or self.tool_index > len(self.calls)):
            raise ValueError("工具执行位置与检查点不一致")
        if self.pending_operation:
            pending = self.pending_operation
            if (not isinstance(pending.get("key"), str) or pending.get("kind") not in ("tool", "hook")
                    or not isinstance(pending.get("name"), str) or not isinstance(pending.get("arguments"), dict)):
                raise ValueError("未决操作格式不兼容")
        if self.phase in ("deliver", "completed") and not self.result:
            raise ValueError("交付检查点缺少结果")
        ids = [entry.get("id") for entry in self.evidence]
        if len(ids) != len(set(ids)):
            raise ValueError("检查点证据编号重复")
        return self

    def snapshot(self) -> dict:
        return self.model_dump(mode="json")


class Checkpointer:
    def __init__(self, state: AgentState, persist: Callable[[dict], None] | None):
        self.state = state
        self.persist = persist
        self.started = time.monotonic()
        self.base_elapsed = state.elapsed_ms

    def update_elapsed(self):
        self.state.elapsed_ms = self.base_elapsed + int((time.monotonic() - self.started) * 1000)

    def save(self):
        self.update_elapsed()
        if self.persist:
            self.persist(self.state.snapshot())

    def perform(self, key: str, kind: str, name: str, arguments: dict,
                action: Callable[[], Any], skipped: Any):
        state = self.state
        if key in state.operations:
            return deepcopy(state.operations[key])
        if state.pending_operation:
            if state.pending_operation.get("key") != key or not state.resolution:
                raise CheckpointNeedsReview("上一步执行结果未确认，请检查后选择重试或跳过")
            resolution = state.resolution
            state.pending_operation = None
            state.resolution = None
            if resolution == "skip":
                state.operations[key] = deepcopy(skipped)
                self.save()
                return deepcopy(skipped)
        state.pending_operation = {"key": key, "kind": kind, "name": name,
                                   "arguments": deepcopy(arguments)}
        # This commit must succeed before starting an operation with side effects.
        self.save()
        result = action()
        state.operations[key] = deepcopy(result)
        state.pending_operation = None
        self.save()
        return result


def recovery_info(snapshot: dict | None) -> dict:
    if not snapshot:
        return {"available": False, "reason": "此任务没有执行检查点，可选择从头重新运行"}
    try:
        state = AgentState.model_validate(snapshot)
    except ValueError:
        return {"available": False, "reason": "执行检查点格式不兼容，无法安全恢复"}
    return {
        "available": state.phase != "completed",
        "phase": state.phase,
        "step": state.step + 1,
        "completed_tools": sum(key.startswith("tool/") for key in state.operations),
        "pending_operation": state.pending_operation,
        "reason": "上一步执行结果待确认" if state.pending_operation else "",
    }
