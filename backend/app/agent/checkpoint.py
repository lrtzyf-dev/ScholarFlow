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


class PlanResponseDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["direct_response", "clarification"]
    applied_input_ids: list[str] = Field(default_factory=list)


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    phase: Literal["setup", "model", "tools", "paused", "summarize", "finalize", "deliver", "completed"] = "setup"
    question: str
    execution_mode: ExecutionMode = "execute"
    reviewed_plan_id: str | None = None
    reviewed_plan: str = ""
    conversation_id: str | None = None
    paper_id: str | None = None
    workspace: dict | None = None
    runtime_config: dict | None = None
    source_revision_ids: list[str] = Field(default_factory=list, max_length=16)
    source_reads: list[dict] = Field(default_factory=list)
    source_attribution_reviews: dict[str, dict] = Field(default_factory=dict)
    source_attribution_repair_contexts: list[str] = Field(default_factory=list)
    source_attribution_feedback_indices: list[int] = Field(default_factory=list)
    answer_quality_repairs: dict[str, dict] = Field(default_factory=dict)
    citation_audit_batches: dict[str, dict] = Field(default_factory=dict)
    goal_id: str | None = None
    attempt_id: str | None = None
    writer_token: str | None = None
    previous_record_id: str | None = None
    delivery_only: bool = False
    source_attempt_id: str | None = None
    applied_input_ids: list[str] = Field(default_factory=list)
    tool_failures: list[dict] = Field(default_factory=list)
    plan_generation: int = Field(default=0, ge=0)
    usage_baseline: dict[str, int] = Field(default_factory=dict)
    skills: list[str] = Field(default_factory=list)
    skill_manifest: list[dict] = Field(default_factory=list)
    planning_started: bool = False
    # None preserves legacy saved plans; new runs distinguish private acceptance
    # checks from a plan explicitly requested by the model through update_plan.
    plan_source: Literal["model", "requirements"] | None = None
    plan_response: PlanResponseDecision | None = None
    messages: list[dict] = Field(default_factory=list)
    provider_context: dict = Field(default_factory=dict)
    context_archive: dict[str, dict] = Field(default_factory=dict)
    context_manifest: dict = Field(default_factory=dict)
    memory_fingerprint: str | None = None
    memory_generation: int = Field(default=0, ge=0)
    memory_stage_log: list[dict] = Field(default_factory=list)
    memory_side_effects: dict[str, dict] = Field(default_factory=dict)
    # Same stage lifecycle as memory revocation. A durable progress anchor
    # prevents opaque native responses from causing an endless renew loop.
    context_stage_progress: str | None = None
    resource_pause: dict | None = None
    progress_seen: list[str] = Field(default_factory=list)
    readonly_cache: dict[str, dict] = Field(default_factory=dict)
    todo: list[dict] = Field(default_factory=list)
    trace: list[dict] = Field(default_factory=list)
    evidence: list[dict] = Field(default_factory=list)
    evidence_counter: int = Field(default=0, ge=0)
    usage: dict[str, int] = Field(default_factory=lambda: {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    })
    step: int = Field(default=0, ge=0)
    max_steps: int = Field(default=6, ge=1)
    budget_chunk_steps: int | None = Field(default=None, ge=1)
    continuation_segments: int = Field(default=0, ge=0)
    segment_result_fingerprints: list[str] = Field(default_factory=list)
    last_segment_fingerprint: str | None = None
    no_progress_segments: int = Field(default=0, ge=0)
    incomplete_finalizations: int = Field(default=0, ge=0)
    budget_exhausted: bool = False
    summary_failed: bool = False
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
        if self.phase == "paused" and (not self.budget_exhausted or self.step < self.max_steps):
            raise ValueError("暂停检查点缺少已耗尽的执行预算")
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

    def perform_events(self, key, kind, name, arguments, action, skipped):
        """Keep the same side-effect receipt boundary while forwarding events.

        An action may return a synchronous denial or a generator whose return
        value is its outcome. Disconnecting during an event leaves it pending.
        """
        from types import GeneratorType
        state = self.state
        if key in state.operations:
            return deepcopy(state.operations[key])
        if state.pending_operation:
            if state.pending_operation.get("key") != key or not state.resolution:
                raise CheckpointNeedsReview("上一步执行结果未确认，请检查后选择重试或跳过")
            resolution = state.resolution
            state.pending_operation = state.resolution = None
            if resolution == "skip":
                state.operations[key] = deepcopy(skipped)
                self.save()
                return deepcopy(skipped)
        state.pending_operation = {"key": key, "kind": kind, "name": name, "arguments": deepcopy(arguments)}
        self.save()
        result = action()
        if isinstance(result, GeneratorType):
            result = yield from result
        state.operations[key] = deepcopy(result)
        state.pending_operation = None
        self.save()
        return result


def pending_resume_assessment(state: AgentState, db=None, run_id=None) -> dict:
    """Only durable adapters may turn an unknown receipt into safe replay."""
    if not state.pending_operation:
        return {"safe": False}
    parent_id = run_id or (state.runtime_config or {}).get("run_id")
    if not parent_id:
        return {"safe": False}
    if state.pending_operation.get("name") in {"start_research_child", "resume_research_child"}:
        from ..research_children import resume_safety
    elif state.pending_operation.get("name") == "spawn_subagent":
        from ..subtask_store import resume_safety
    else:
        return {"safe": False}
    if db is not None:
        return resume_safety(db, parent_run_id=parent_id, checkpoint=state.snapshot())
    from ..db import SessionLocal
    with SessionLocal() as owned:
        return resume_safety(owned, parent_run_id=parent_id, checkpoint=state.snapshot())


def permit_durable_child_resume(state: AgentState, db=None, run_id=None) -> bool:
    if state.resolution:
        return False
    assessment = pending_resume_assessment(state, db, run_id)
    if assessment.get("safe"):
        # Re-dispatch the same parent operation; persistent children, rather
        # than the parent, decide which successful reads/results can be reused.
        state.resolution = "retry"
        return True
    return False


def recovery_info(snapshot: dict | None) -> dict:
    if not snapshot:
        return {"available": False, "reason": "此任务没有执行检查点，可选择从头重新运行"}
    try:
        state = AgentState.model_validate(snapshot)
    except ValueError:
        return {"available": False, "reason": "执行检查点格式不兼容，无法安全恢复"}
    child_recovery = pending_resume_assessment(state)
    requires_review = bool(state.pending_operation and not child_recovery.get("safe"))
    return {
        "available": (state.phase != "completed" or is_partial_delivery(state)
                      or state.execution_mode == "execute" and bool((state.result or {}).get("budget_exhausted"))),
        "kind": "resources" if state.resource_pause else "continue_missing" if is_partial_delivery(state) else "checkpoint",
        "phase": state.phase,
        "step": state.step + 1,
        "completed_tools": sum(key.startswith("tool/") for key in state.operations),
        "pending_operation": state.pending_operation if requires_review else None,
        "automatic_child_recovery": child_recovery if child_recovery.get("safe") else None,
        "reason": "上一步执行结果待确认" if requires_review else (state.resource_pause or {}).get("message", ""),
        "resource_pause": deepcopy(state.resource_pause),
    }


def is_partial_delivery(state: AgentState) -> bool:
    result = state.result or {}
    return (state.execution_mode == "execute" and state.phase in ("deliver", "completed")
            and result.get("completed") is False
            and result.get("outcome") in ("partial", "blocked", None)
            and bool(result.get("incomplete_steps")))


def prepare_resume(snapshot: dict, additional_steps: int = 6) -> tuple[AgentState, bool]:
    """Prepare new work, while an interrupted delivery remains a delivery replay.

    Only a previously committed QA creates a new answer identity. Saved tool
    receipts and step positions remain valid and are never regenerated.
    """
    state = AgentState.model_validate(snapshot)
    permit_durable_child_resume(state)
    resource_paused = bool(state.resource_pause)
    if (state.resource_pause or {}).get('kind') == 'skill_changed':
        from ..skill_store import reload_checkpoint_skills
        reload_checkpoint_skills(state)
    state.resource_pause = None
    state.source_attempt_id = state.attempt_id
    state.delivery_only = state.phase == "deliver"
    partial = is_partial_delivery(state) and state.phase == "completed"
    budget = state.phase == "paused" or (state.phase == "completed"
                                         and bool((state.result or {}).get("budget_exhausted")))
    if partial or budget:
        if state.pending_operation:
            raise CheckpointNeedsReview("仍有未确认操作，不能直接开始补缺尝试")
        if state.result and state.phase == "completed":
            state.previous_record_id, state.record_id = state.record_id, uuid4().hex
            state.usage_baseline = deepcopy(state.usage)
        state.max_steps = max(state.max_steps, state.step) + additional_steps
        state.phase = "model"
        state.budget_exhausted = False
        state.summary_failed = False
        state.incomplete_finalizations = 0
        state.no_progress_segments = 0
        state.answer, state.result = "", None
        state.calls, state.tool_index = [], 0
        if partial:
            missing = [item.get("title", str(item)) if isinstance(item, dict) else str(item)
                       for item in (snapshot.get("result") or {}).get("incomplete_steps", [])]
            previous_answer = (snapshot.get("result") or {}).get("answer") or snapshot.get("answer")
            if previous_answer:
                state.messages.append({"role": "assistant", "content": previous_answer})
            state.messages.append({"role": "user", "content":
                "继续原任务，只补齐以下未完成要求，保留已验证成果，不重复已成功的写入或工具操作："
                + "；".join(missing)})
    return state, partial or budget or resource_paused


def delivery_usage(state: AgentState) -> dict[str, int]:
    return {key: max(0, value - state.usage_baseline.get(key, 0))
            for key, value in state.usage.items()}
