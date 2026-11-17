"""Durable research executions on the shared logical run/attempt/goal ledger.

Adapters own domain validation and outputs. This module owns fenced persistence,
model identity, cancellation and explicit recovery of uncertain paid requests.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import threading
from uuid import uuid4

from sqlalchemy import select, text

from . import cancel_store, run_lifecycle, run_store, source_store as sources
from .agent.checkpoint import AgentState, Checkpointer, CheckpointNeedsReview, recovery_info, prepare_resume
from .db import SessionLocal
from .model_profiles import capture_run_config, public_runtime_config
from .runtime_context import bind_runtime_config
from .runtime_models import RuntimeRun, RunAttempt
from .source_runtime import conversation_scope
from .timeutil import utcnow

_guard = threading.RLock()
_threads = {}
_slots = threading.Semaphore(2)
_stopping = threading.Event()
JOB_KEY = "research_job"
KINDS = ("framework", "translation", "quality", "matrix")


class ResearchStopped(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def adapter(kind):
    if kind == "framework":
        from .writing import framework_run
        return framework_run
    if kind == "translation":
        from .translate import translation_run
        return translation_run
    if kind == "quality":
        from .writing import quality_run
        return quality_run
    if kind == "matrix":
        from . import matrix_run
        return matrix_run
    raise sources.SourceError("此研究任务类型尚未开放")


def descriptor(state):
    job = state.context_archive.get(JOB_KEY)
    if not isinstance(job, dict) or job.get("version") != 1 or digest(job.get("inputs")) != job.get("input_sha256"):
        raise sources.SourceCorruption("研究任务固定输入校验失败，未重新发送请求")
    return job


def checked_payload(state,job):
    payload=(state.result or {}).get("payload") or job.get("last_payload")
    expected=job.get("last_payload_sha256")
    if payload is not None and expected is not None and digest(payload)!=expected:
        raise sources.SourceCorruption("研究任务保存结果校验失败，未重新付费调用")
    return payload,expected


def checked(db, run_id, conversation_id, *, writing=False):
    conversation_scope(db, conversation_id, writing=writing)
    run = db.get(RuntimeRun, run_id, populate_existing=True)
    if run is None or run.conversation_id != conversation_id or run.kind not in KINDS:
        raise sources.SourceNotFound("当前会话没有此研究任务")
    attempt = run_lifecycle.current_attempt(db, run_id)
    if attempt is None: raise sources.SourceCorruption("研究任务缺少持久执行尝试")
    state = AgentState.model_validate(attempt.checkpoint)
    job = descriptor(state)
    if job["kind"] != run.kind: raise sources.SourceCorruption("研究任务类型与检查点不一致")
    checked_payload(state,job)
    return run, attempt, state


def create(db, *, kind, run_id, conversation_id, inputs, request_id, parent_binding=None):
    """Caller holds coordinated admission + BEGIN IMMEDIATE; no provider calls."""
    conversation_scope(db, conversation_id)
    if parent_binding is not None:
        from .research_children import assert_parent
        parent, parent_attempt = assert_parent(db, parent_binding)
        if parent.conversation_id != conversation_id:
            raise sources.SourceConflict("研究任务与父执行会话不一致")
        stored = (parent_attempt.checkpoint or {}).get("runtime_config") or {}
        if parent_binding.get("resource_budget_id") != stored.get("resource_budget_id"):
            raise sources.SourceConflict("父预算绑定不匹配")
    handler = adapter(kind)
    inputs = handler.validate_inputs(inputs)
    existing = db.get(RuntimeRun, run_id)
    if existing:
        run, attempt, state = checked(db, run_id, conversation_id, writing=True)
        if run.kind != kind or descriptor(state)["input_sha256"] != digest(inputs):
            raise sources.SourceConflict("同一研究任务编号不能用于不同输入")
        if run_lifecycle.admitted_request(db, run_id, request_id) is None:
            raise sources.SourceConflict("任务已存在，请继续原任务或新建任务")
        return run, attempt, False
    run_lifecycle.assert_conversation_idle(db, conversation_id, trusted_parent=parent_binding)
    binding = (handler.capture_config(db, conversation_id) if hasattr(handler, "capture_config") else
        deepcopy(parent_binding) if parent_binding is not None else capture_run_config(db, conversation_id))
    if parent_binding is not None:
        binding.pop("writer_token", None)
        binding.pop("research_parent", None)
        binding.update(parent_run_id=parent_binding["run_id"], resource_budget_id=parent_binding.get("resource_budget_id"),
            resource_policy=deepcopy(parent_binding.get("resource_policy", {})),
            approval_mode=parent_binding.get("approval_mode", "ask"))
    from .resource_policy import freeze_budget
    binding = freeze_budget(db, run_id=run_id, binding=binding)
    question = handler.goal_description(inputs)
    attempt, fresh = run_lifecycle.admit_attempt(db, run_id, conversation_id, question,
        kind=kind, request_id=request_id)
    run = db.get(RuntimeRun, run_id)
    state = AgentState(question=question, conversation_id=conversation_id, goal_id=run.goal_id,
        runtime_config={**binding, "run_id": run_id, "goal_id": run.goal_id, "attempt_id": attempt.id, "conversation_id": conversation_id},
        attempt_id=attempt.id, writer_token=attempt.writer_token,
        context_archive={JOB_KEY: {"version": 1, "kind": kind, "inputs": inputs,
            "input_sha256": digest(inputs), "stage": "queued", "events": [], **handler.snapshot_config(binding)}})
    if hasattr(handler, "initialize"): handler.initialize(db, state)
    handler.authorize(db, state)
    attempt.checkpoint = state.snapshot()
    db.flush()
    return run, attempt, fresh


def active():
    with _guard: return list(_threads)


def authorize(run_id, attempt_id, writer_token):
    if _stopping.is_set() or cancel_store.is_cancelled(run_id):
        raise ResearchStopped("研究任务已停止，已保存的结果与调用回执保留")
    with SessionLocal() as db:
        attempt = run_lifecycle.assert_writer(db, run_id, attempt_id, writer_token)
        state = AgentState.model_validate(attempt.checkpoint)
        from .research_children import authorize_state
        authorize_state(db, state)
        conversation_scope(db, state.conversation_id)
        handler = adapter(descriptor(state)["kind"])
        validate_binding(db, state, handler)
        handler.authorize(db, state)
    return True


def validate_binding(db, state, handler):
    if hasattr(handler, "validate_binding"): handler.validate_binding(db, state.runtime_config)
    else: run_lifecycle.validate_binding(db, state.runtime_config)


class Execution:
    def __init__(self, run_id, state):
        self.run_id, self.state = run_id, state
        self.checkpointer = Checkpointer(state, self._persist)

    def _persist(self, snapshot):
        # Preserve a known paid response even when cancellation arrived during
        # HTTP. No new requests follow until authorize succeeds. Deleted or
        # superseded attempts cannot be recreated by this writer.
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            if snapshot.get("phase") == "completed":
                from .research_children import authorize_state
                authorize_state(db, AgentState.model_validate(snapshot))
            run_lifecycle.save_checkpoint(db, self.run_id, snapshot)
            db.commit()

    def authorize(self):
        return authorize(self.run_id, self.state.attempt_id, self.state.writer_token)

    def save(self):
        self.checkpointer.save()

    def set_payload(self, payload):
        job=descriptor(self.state)
        job["last_payload"],job["last_payload_sha256"]=deepcopy(payload),digest(payload)
        self.save()

    def activity(self, stage, message):
        job = descriptor(self.state)
        job["stage"] = stage
        job["events"] = [*job.get("events", []), {"stage": stage, "text": message, "at": utcnow().isoformat() + "Z"}][-30:]
        self.save()

    def perform(self, key, name, arguments, action):
        self.authorize()
        from .ledger_models import CallRecord
        with SessionLocal() as db:
            previous = set(db.scalars(select(CallRecord.id).where(CallRecord.run_id == self.run_id,
                CallRecord.attempt_id == self.state.attempt_id)))
        def recorded():
            try:
                payload = action()
            except Exception as exc:
                from .goal_policy import GoalPolicyChanged
                if isinstance(exc, GoalPolicyChanged) and exc.denial.get("not_executed"):
                    with SessionLocal() as db:
                        new_calls = list(db.scalars(select(CallRecord).where(CallRecord.run_id == self.run_id,
                            CallRecord.attempt_id == self.state.attempt_id, CallRecord.id.not_in(previous))))
                    # A policy refusal before transport is a known non-dispatch.
                    # Never erase uncertainty if this operation also dispatched
                    # another call, even when a later preflight was refused.
                    if all(call.state == "failed" and call.error_kind == "GoalPolicyChanged" for call in new_calls):
                        self.state.pending_operation = self.state.resolution = None
                        self.save()
                from .request_resources import raise_if_resource_boundary
                try: raise_if_resource_boundary(exc)
                except Exception:
                    with SessionLocal() as db:
                        current = set(db.scalars(select(CallRecord.id).where(CallRecord.run_id == self.run_id,
                            CallRecord.attempt_id == self.state.attempt_id)))
                    # A verified admission refusal has not started a supplier
                    # call. It can resume without claiming an unknown charge.
                    if current == previous:
                        self.state.pending_operation = self.state.resolution = None
                    from .resource_budget import ResourceBudgetExceeded
                    self.state.resource_pause = {"kind": "budget" if isinstance(exc, ResourceBudgetExceeded) else "resources",
                        "message": "资源或预算限制已暂停任务，已保存的草稿与调用结果保留；调整限制后可继续。"}
                    self.save()
                raise
            return {"payload": payload, "sha256": digest(payload)}
        record = self.checkpointer.perform(key, "hook", name, arguments, recorded, skipped=None)
        if not isinstance(record, dict) or digest(record.get("payload")) != record.get("sha256"):
            raise sources.SourceCorruption("研究任务调用回执校验失败，未重新付费调用")
        return deepcopy(record["payload"])


def execute(run_id, attempt_id=None):
    with SessionLocal() as db:
        run = db.get(RuntimeRun, run_id)
        if run is None: return
        attempt = run_lifecycle.current_attempt(db, run_id)
        if attempt is None or attempt.id != (attempt_id or attempt.id) or attempt.status in run_lifecycle.TERMINAL: return
        state = AgentState.model_validate(attempt.checkpoint)
    context = Execution(run_id, state)
    context.authorize()
    with bind_runtime_config(state.runtime_config):
        context.activity("preparing", "正在准备固定输入与原文来源")
        payload, complete = adapter(descriptor(state)["kind"]).execute(context)
    context.authorize()
    job = descriptor(state)
    job["stage"] = "completed" if complete else "partial"
    job["last_payload"] = deepcopy(payload)
    job["last_payload_sha256"] = digest(payload)
    state.result = {"payload": payload, "completed": complete, "outcome": "completed" if complete else "partial",
        "incomplete_steps": [] if complete else [{"title": ("将提取预览合并到当前矩阵版本" if job["kind"] == "matrix" and payload.get("apply_conflict")
            else {"translation": "补齐翻译", "quality": "补齐引用讨论分析", "matrix": "补齐矩阵提取"}.get(job["kind"], "补齐框架生成或论点支持复核"))}],
        "stop_reason": "" if complete else "research_review_incomplete"}
    state.phase = "completed"
    context.save()


def _mark_failure(run_id, attempt_id, error):
    with SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        run = db.get(RuntimeRun, run_id)
        attempt = db.get(RunAttempt, attempt_id)
        if run is None or attempt is None or run.current_attempt_id != attempt_id: return
        status = "stopped" if isinstance(error, ResearchStopped) else "interrupted"
        state = AgentState.model_validate(attempt.checkpoint)
        if state.resource_pause and status != "stopped": status = "paused"
        from .goal_policy import GoalPolicyChanged
        message = str(error) if isinstance(error, (ResearchStopped, sources.SourceError, CheckpointNeedsReview, GoalPolicyChanged)) else "研究任务尚未结束；已保存结果保留。请核对模型、授权或存储，再选择继续；未确认的调用不会自动重发。"
        run_lifecycle.finish_attempt(db, run_id, attempt.id, attempt.writer_token, status, message)
        db.commit()


def _worker(run_id, attempt_id):
    status = "interrupted"
    try:
        from . import maintenance
        with maintenance.activity():
            while not _slots.acquire(timeout=.1):
                if _stopping.is_set() or cancel_store.is_cancelled(run_id): raise ResearchStopped("排队任务已停止，固定输入已保留")
            try: execute(run_id, attempt_id)
            finally: _slots.release()
        with SessionLocal() as db:
            attempt = db.get(RunAttempt, attempt_id)
            status = attempt.status if attempt else "interrupted"
    except Exception as error:
        try: _mark_failure(run_id, attempt_id, error)
        except Exception: pass
        status = "stopped" if isinstance(error, ResearchStopped) else "interrupted"
    finally:
        run_store.finish(run_id, status, attempt_id=attempt_id)
        with _guard: _threads.pop(run_id, None)


def start(run_id):
    if _stopping.is_set(): raise sources.SourceConflict("应用正在退出，研究任务已保存")
    with SessionLocal() as db:
        run = db.get(RuntimeRun, run_id); attempt = run_lifecycle.current_attempt(db, run_id)
        if run is None or attempt is None: raise sources.SourceNotFound("研究任务不存在")
        cid, ident = run.conversation_id, attempt.id
    with _guard:
        if run_id in _threads: return
    if not run_store.begin(run_id, cid, attempt_id=ident): raise sources.SourceConflict("此研究任务已有执行器")
    thread = threading.Thread(target=_worker, args=(run_id, ident), daemon=True, name="research-" + run_id[-8:])
    with _guard: _threads[run_id] = thread
    try: thread.start()
    except Exception:
        with _guard: _threads.pop(run_id, None)
        error = sources.SourceConflict("研究执行器未能启动，任务已保存，可核对后继续")
        _mark_failure(run_id, ident, error); run_store.finish(run_id, "interrupted", attempt_id=ident)
        raise error from None


def resume(db, run_id, *, conversation_id, request_id, expected_revision, retry_unknown=False, parent_binding=None):
    run, attempt, state = checked(db, run_id, conversation_id, writing=True)
    from .research_children import assert_linked_control, attach_attempt, parent_record
    link = assert_linked_control(db, run_id, parent_binding)
    previous = run_lifecycle.admitted_request(db, run_id, request_id)
    if previous: return run, previous, False
    if run_id in active() or run_id in run_store.active_runs(): raise sources.SourceConflict("研究任务仍在运行")
    if run.revision != expected_revision: raise sources.SourceConflict("研究任务已更新，请重新载入")
    if not recovery_info(attempt.checkpoint)["available"]: raise sources.SourceConflict("此研究任务已有完整结果，请新建任务")
    if state.pending_operation and not retry_unknown:
        raise sources.SourceConflict("上次接口调用结果未确认。请先核对，确认可能产生重复费用后再选择重试未确认调用")
    run_lifecycle.assert_conversation_idle(db, conversation_id, exclude_run_id=run_id, trusted_parent=parent_binding)
    if link:
        state.runtime_config = {**state.runtime_config, "research_parent": parent_record(parent_binding, link.id)}
    validate_binding(db, state, adapter(descriptor(state)["kind"]))
    adapter(descriptor(state)["kind"]).authorize(db, state)
    if state.pending_operation:
        # A domain draft can accompany an uncertain paid operation. The user
        # has explicitly authorized replay of this exact pending call.
        state.phase, state.result, state.resolution = "model", None, "retry"
        state.source_attempt_id, state.delivery_only = attempt.id, False
    else:
        state, _ = prepare_resume(state.snapshot())
    adapter(descriptor(state)["kind"]).prepare_resume(state)
    new, fresh = run_lifecycle.admit_attempt(db, run_id, conversation_id, state.question, kind=run.kind,
        checkpoint=state.snapshot(), resume=True, request_id=request_id, expected_revision=expected_revision)
    frozen = AgentState.model_validate(new.checkpoint)
    frozen.runtime_config = {**frozen.runtime_config, "run_id": run_id, "attempt_id": new.id, "conversation_id": conversation_id}
    new.checkpoint = frozen.snapshot()
    if link:
        attach_attempt(db, link, new, parent_binding)
    db.flush()
    return run, new, fresh


def cancel(db, run_id, conversation_id, *, parent_binding=None):
    run, attempt, state = checked(db, run_id, conversation_id)
    from .research_children import assert_linked_control
    assert_linked_control(db, run_id, parent_binding)
    if state.phase == "completed" and (state.result or {}).get("completed") is True:
        raise sources.SourceConflict("此研究任务已有完整结果，无需停止")
    if run_id in active():
        run_store.cancel(run_id)
        return "cancelling"
    run_lifecycle.finish_attempt(db, run_id, attempt.id, attempt.writer_token, "stopped", "用户停止；已保存结果保留")
    return "stopped"


def public(db, run_id, conversation_id, *, expected_kind=None):
    run, attempt, state = checked(db, run_id, conversation_id)
    if expected_kind is not None and run.kind != expected_kind:
        raise sources.SourceNotFound("当前会话没有此类型的研究任务")
    handler=adapter(run.kind)
    if hasattr(handler,"public_authorize"):handler.public_authorize(db,state)
    job = descriptor(state)
    payload,expected=checked_payload(state,job)
    from .call_ledger import summarize_calls
    from .research_models import ResearchChildLink
    link = db.scalar(select(ResearchChildLink).where(ResearchChildLink.child_run_id == run_id))
    return {"run_id": run.id, "kind": run.kind, "conversation_id": run.conversation_id, "revision": run.revision,
        "attempt_id": attempt.id, "attempt_number": attempt.number, "status": run.status, "outcome": run.outcome,
        "inputs": deepcopy(job["inputs"]), "stage": job["stage"], "events": deepcopy(job.get("events", [])),
        "model": public_runtime_config(state.runtime_config), "error": attempt.error,
        "resource_pause": {key: state.resource_pause.get(key) for key in ("kind", "message")} if state.resource_pause else None,
        "result": deepcopy(payload), "result_integrity": "checked" if expected else "legacy_unchecked" if payload is not None else "not_available", "requires_unknown_confirmation": bool(state.pending_operation),
        "can_resume": bool(not link and not job.get("resume_blocked_reason") and recovery_info(attempt.checkpoint)["available"] and run.status not in ("queued", "running") and run.id not in active()),
        "managed_by_parent": bool(link), "parent_run_id": link.parent_run_id if link else None,
        "resume_blocked_reason": job.get("resume_blocked_reason", "") or ("此研究任务由父会话管理，请回到所属对话继续或合并。" if link else ""),
        "source_count": len(job.get("source_snapshot", [])) if run.kind in ("framework", "quality", "matrix") else int(job["inputs"].get("source_ref") is not None), "usage": summarize_calls(run_id=run.id, db=db),
        "updated_at": run.updated_at.isoformat() + "Z"}


def recover_startup():
    _stopping.clear()
    with SessionLocal() as db:
        for run in db.scalars(select(RuntimeRun).where(RuntimeRun.kind.in_(KINDS), RuntimeRun.status.in_(("queued", "running")))):
            attempt = run_lifecycle.current_attempt(db, run.id)
            if attempt:
                run_lifecycle.finish_attempt(db, run.id, attempt.id, attempt.writer_token, "interrupted",
                    "应用曾退出。固定输入与已知结果保留；请核对后继续，未确认调用不会自动重发。")
        db.commit()


def wait_shutdown():
    _stopping.set()
    with _guard: threads = list(_threads.values())
    for thread in threads: thread.join(timeout=2)
