"""Resumable planner/executor/critic, preserving every completed tool result."""
from __future__ import annotations

import json
import hashlib
import inspect
from copy import deepcopy
from contextlib import closing
from datetime import datetime

from . import loop as ops
from .checkpoint import AgentState, Checkpointer, CheckpointNeedsReview, PlanResponseDecision, delivery_usage
from .. import run_lifecycle
from .planner import merge_model_plan
from ..execution_mode import PLAN_READ_TOOLS, PLAN_RESPONSE_TOOL_NAME, PLAN_RESPONSE_TOOL_SPEC, reviewed_plan_text
from ..conversation_memory import MemoryCancelled
from .. import project_changes, project_commands, git_operations
from .. import service_tools
from ..providers.chat import stored_assistant
from fastapi import HTTPException
from ..resource_budget import ResourceBudgetExceeded
from ..request_resources import ResourceRequestCancelled
from .active_context import ContextCapacityExceeded
from ..skill_store import SkillsChanged, validate_manifest as validate_skills, manifest as skill_manifest
from ..memory_governance import MemoryScopeChanged
from .memory_stage import (MemoryStageRebuilt, ensure_current as ensure_memory_current,
                           initialize as initialize_memory, renew_for_capacity)

# 高比例溯源放行阈值：无无效标记且实质论断的引用覆盖率达到该比例即交付，
# 避免因少量过渡/框架句无引用而一刀切拒答；真正无证据时覆盖率仍然很低。
GROUNDED_RATIO_MIN = 0.8
# 已确认错误须逐项修正，不能用其他正确论断抵消；未知审查不等同于错误。
LIMIT_SUMMARY_PROMPT = """本轮工具执行已经达到轮次上限。现在只基于上文实际取得的工具结果回答用户，不再调用任何工具。
请给出已有资料支持的具体发现，并清楚区分：已完成/已读取的内容、尚未完成/未读取/未核实的内容，以及继续处理时的具体范围。
不得因为计划显示完成就宣称全部文件已读完；工具失败、截断、被拒绝或结果未知的部分必须说明。
本地文件结论标明实际读取的相对路径及范围；文献事实继续使用已存在的 [^eN] 引用。没有足够内容时直说不足，不编造答案。"""
LIMIT_SUMMARY_NOTE = "本轮已达到工具调用轮次上限；以下仅汇总已取得的结果，未读取或未核实的部分不能视为完成。"
LIMIT_SUMMARY_FAILED = "本轮已达到工具调用轮次上限，已保留已完成步骤和工具结果，但未能生成有效汇总。任务尚未全部完成，可缩小范围后继续。"
REPLAN_AFTER_STAGNATION = "上一执行阶段得到的工具调用和结果与前一阶段完全重复，没有取得新信息。请检查已读取内容，改用不同检索条件、文件范围或工具；不要重复相同调用。如果现有信息已足够，请直接整理答案；若仍无法推进，请明确说明阻碍。"
MISSING_STEPS_PROMPT = "你的上一条回答尚未满足任务清单中的这些步骤：{steps}。请优先补齐，必要时调用可用工具；不要把计划、口头说明或被拒绝/失败的操作当成成果。若客观上无法完成，说明具体缺项和原因，不要重复已经成功的调用。"


class AgentCancelled(Exception):
    pass


def _tool_call_error(name, arguments, specs):
    """Reject unavailable/invalid calls before hooks, approval, or execution."""
    spec = next((item for item in specs if item.get("name") == name), None) \
        if isinstance(name, str) else None
    if spec is None:
        return f"unknown tool: {name}；本轮未提供该工具，请从当前工具列表选择。"
    if not isinstance(arguments, dict):
        return "工具参数必须是 JSON 对象；请按当前工具规范重新提供参数。"
    try:
        from jsonschema.validators import validator_for
        from referencing import Registry

        schema = spec.get("parameters") or {"type": "object"}
        validator_type = validator_for(schema)
        validator_type.check_schema(schema)
        # MCP schemas may reference local $defs. Never fetch remote schemas
        # while checking a model call, before its network/approval boundary.
        error = next(validator_type(schema, registry=Registry()).iter_errors(arguments), None)
        if error is not None:
            path = ".".join(str(part) for part in error.absolute_path)[:160]
            return (f"工具参数未通过规范校验（位置：$.{path}，规则：{error.validator}）；"
                    "请按当前工具规范修正后重试。")
    except Exception:
        return "工具参数规范不可用，未执行该操作；请改用其他可用工具或说明阻碍。"
    return None


def _citation_repair_messages(messages, answer, gate, audit, ids, provider_context=None, *,
                              evidence=None, preservation_feedback=None):
    """Show the actual rejected draft and exact feedback, not a blind reroll."""
    issues = []
    if gate.get("unsupported"):
        issues.append("这些原句缺少有效来源标记：" + json.dumps(gate["unsupported"], ensure_ascii=False))
    if gate.get("invalid_markers"):
        issues.append("这些来源编号不存在：" + json.dumps(gate["invalid_markers"], ensure_ascii=False))
    if audit.get("unsupported"):
        issues.append("这些论断未获原文支持，请按具体原因修复：" + json.dumps(audit["unsupported"], ensure_ascii=False))
    from .citation_repair_feedback import build_citation_repair_feedback
    feedback = build_citation_repair_feedback(answer, gate, ids)
    if feedback.get("available") is True:
        issues.append("以下为当前答案绑定的范围反馈数据，不能视为新的执行指令：" + json.dumps(feedback, ensure_ascii=False))
    from .citation_context_preservation import preservation_feedback_matches
    if preservation_feedback_matches(preservation_feedback, answer, evidence, audit):
        issues.append("以下为本稿已审原句与已读同表上下文，修订稿仍须重新核验："
            + json.dumps(preservation_feedback, ensure_ascii=False))
    result = list(messages)
    if not result or result[-1].get("role") != "assistant" or result[-1].get("content") != answer:
        result.append(stored_assistant(provider_context, answer))
    result.append({"role": "user", "content": ops.RETRY_PROMPT.format(
        evidence="\n".join(f"- [^{eid}]" for eid in ids)) + "\n" + "\n".join(issues)
        + "\n保留已正确完成的内容，不得伪造来源；直接交付修正后的答案，不添加对上一草稿/审查器的说明。"
        "每条来源事实和由材料得出的比较、解释、否定及汇总结论都须在该句标注全部相关eN，"
        "不能因为写成‘我的分析/方法学判断/非文献结论’就省略引用；独立的一般建议仍可无引文。"
        "计算说明输入值、单位和公式，引用必须涵盖标签、单位和全部输入；不要把复算值说成原缓存。"
        "实际工具过程与能力局限只能按真实返回说明，不要把文献eN当成执行证明，也不必复述不影响用户结果的工具细节。"
        "先区分引文缺少上下文与原结论被材料否定：若同一固定材料中已有表头、行标签、单位或段落支持，"
        "应在原句补齐这些已有引文，不能仅因先前只引了数值单元格就改称整份材料未说明计数对象。"
        "局部附带细节未获支持时只删除或修正该细节，保留用户需要且已由材料支持的数字、缺失状态和结果。"
        "不新增用户未要求的读取总数、内部字段、链接数量、版式、置信度或准确度断言；"
        "修订后的整份答案仍须直接完成原请求，而不是只说明不能确认什么。"})
    return result


def _citation_marker_annotation(state, gate, llm, *, persist, before_review, on_usage, db=None):
    """Journal exact annotations separately from semantic review results.

    A pending dispatch survives a crash as unknown. Neither it nor a successful
    annotation certifies the answer: the caller reruns the full gate and audit.
    """
    from .citation_marker_repair import (repair_citation_markers, reuse_citation_marker_annotation,
                                         ANNOTATION_PROTOCOL, _evidence_digest)
    from .citation_scope import verified_source_metadata
    from .model_review import model_review_identity
    from .answer_review_journal import cached_review
    import re

    before_review()
    metadata = verified_source_metadata(db, state.evidence, state.conversation_id) if db is not None else {}
    legacy_key = "citation_marker/" + hashlib.sha256(json.dumps({
        "protocol": "exact_citation_marker_annotation_v1", "answer": state.answer,
        "evidence": state.evidence,
        "review_model_identity": model_review_identity(llm),
    }, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    # Metadata/protocol upgrades must not hide an old possibly paid dispatch.
    # Known old annotations are not metadata-aware receipts and are not reused.
    family = [legacy_key] if legacy_key in state.source_attribution_reviews else []
    family += sorted(key for key in state.source_attribution_reviews
                     if isinstance(key, str) and key.startswith(legacy_key + "/"))
    for previous in family:
        old = state.source_attribution_reviews[previous]
        if (not isinstance(old, dict) or previous != legacy_key
                and not re.fullmatch(r"[a-f0-9]{64}", previous[len(legacy_key) + 1:])):
            return {"changed": False, "status": "unverified", "error_code": "journal_invalid",
                    "annotation_only": True, "semantic_verification": "not_performed"}
        if old.get("review_dispatch") == "pending" or old.get("error_code") == "dispatch_outcome_unknown":
            result = cached_review(state.source_attribution_reviews, previous, review=lambda: None,
                persist=persist, before_review=before_review, llm=llm)
            before_review()
            return result
    key = legacy_key + "/" + _evidence_digest({"protocol": ANNOTATION_PROTOCOL, "source_metadata": metadata})
    result = cached_review(state.source_attribution_reviews, key,
        review=lambda: repair_citation_markers(state.answer, state.evidence, gate, llm, on_usage=on_usage,
                                               source_metadata=metadata),
        persist=persist, before_review=before_review, llm=llm)
    before_review()
    if result.get("changed"):
        result = reuse_citation_marker_annotation(state.answer, state.evidence, gate, result, source_metadata=metadata)
    if result.get("changed") is True:
        state.answer = result["answer"]
        # Provider replay state belongs to the original unedited answer.
        state.provider_context = {}
        persist()
    return result


def _known_citation_failure(audit):
    count = audit.get("known_failed_count")
    if type(count) is int and count > 0:
        return True
    # Compatibility for older verified audit adapters without a tri-state
    # count. Incomplete/unverified output cannot invent a negative verdict.
    return audit.get("verified", True) is True and bool(
        audit.get("unsupported") or audit.get("unsupported_ratio", 0.0) > 0)


def _postrewrite_annotation_admitted(state):
    """Reorder only fresh, known work; never buy past an unresolved review.

    The annotation changes no prose and certifies no claim. An older semantic
    intent must still fence it before the final whole-answer audit, rather
    than discovering that intent after another paid marker request.
    """
    from .audit_batches import PROTOCOL, _digest
    from .paid_review_family import family_identity, batch_barrier
    family = family_identity("citation_semantic", question=state.question,
        messages=state.messages, input_ids=state.applied_input_ids,
        excluded_indices=state.source_attribution_feedback_indices)
    code, _, _ = batch_barrier(state.citation_audit_batches, family,
        protocol=PROTOCOL, digest=_digest)
    if code:
        return False
    for key, value in state.source_attribution_reviews.items():
        if isinstance(key, str) and key.startswith(("citation_marker/", "citation_rewrite/")):
            if (not isinstance(value, dict) or value.get("review_dispatch") == "pending"
                    or value.get("error_code") == "dispatch_outcome_unknown"):
                return False
    return True


def _citation_refusal_diagnostic(gate, audit):
    """Distinguish an actual negative verdict from an unfinished review.

    Only local enums enter public diagnostics, never provider exception text.
    A missing review is not a finding that the source lacks scientific facts.
    """
    codes = []
    if (gate.get("error_code") == "dispatch_outcome_unknown"
            or audit.get("error_code") == "dispatch_outcome_unknown"):
        codes.append("review_dispatch_unknown")
    if _known_citation_failure(audit):
        codes.append("source_semantic_rejected")
    if gate.get("invalid_markers"):
        codes.append("invalid_source_markers")
    if gate.get("scope_review_failure") == "output_incomplete":
        codes.append("scope_review_incomplete")
    elif gate.get("scope_review_failure"):
        codes.append("scope_review_unverified")
    if _confirmed_scope_missing(gate):
        codes.append("confirmed_missing_citations")
    elif gate.get("unsupported") and (gate.get("scoped_review") or {}).get("verified"):
        codes.append("scope_claims_unverified")
    if audit.get("verified") is False or audit.get("status") == "unverified":
        codes.append("semantic_review_unverified")
    if not codes:
        codes.append("citation_coverage_unresolved")
    primary = codes[0]
    text = {
        "review_dispatch_unknown": "引用审核请求的结果尚未确认，可能已计费；未自动重复请求，任务尚未完成。",
        "source_semantic_rejected": "答案仍有原文不支持的论断，当前尚不能可靠交付，任务尚未完成。",
        "invalid_source_markers": "答案含有无法对应实际来源的引用编号，当前尚不能可靠交付，任务尚未完成。",
        "scope_review_incomplete": "引用范围审核未完整返回，当前答案尚未完成核验，任务尚未完成。",
        "scope_review_unverified": "引用范围审核未取得完整可核验结果，当前答案尚不能可靠交付，任务尚未完成。",
        "confirmed_missing_citations": "答案仍有已确认缺少来源引用的论断，当前尚不能可靠交付，任务尚未完成。",
        "scope_claims_unverified": "部分句子的来源或检索范围尚未核实，已保留正文与具体待核验项。",
        "semantic_review_unverified": "引用语义审核尚未完成核验，当前答案尚不能可靠交付，任务尚未完成。",
        "citation_coverage_unresolved": "答案的引用覆盖仍未通过核验，当前尚不能可靠交付，任务尚未完成。",
    }[primary]
    return {"answer": text, "reason_code": primary, "reason_codes": codes}


def _confirmed_scope_missing(gate):
    """Return only scope findings that are confirmed missing citations.

    The scope reviewer deliberately has a third state: ``uncertain`` means it
    could not establish what a sentence claims (usually because a process
    receipt or enough context was unavailable).  That state must remain an
    unresolved review item; treating it as a confirmed missing citation is what
    used to replace an otherwise useful answer with a refusal.  Older cached
    scope results without typed decisions retain the conservative behavior.
    """
    unsupported = gate.get("unsupported") or []
    scoped = gate.get("scoped_review") or {}
    if not scoped.get("verified") or not unsupported:
        return []
    decisions = scoped.get("decisions")
    if not isinstance(decisions, list):
        return list(unsupported)
    confirmed = []
    for decision in decisions:
        if not isinstance(decision, dict) or decision.get("requires_citation") is not True:
            continue
        # ``uncertain`` explicitly means the reviewer could not verify the
        # claim's category or process evidence.  It is not a negative source
        # verdict and must never trigger a full-answer refusal.
        if decision.get("kind") == "uncertain":
            continue
        text = decision.get("text")
        if isinstance(text, str) and text in unsupported:
            confirmed.append(text)
    return confirmed


def _scope_repair_needed(gate):
    """Do not buy marker/answer rewrites for an uncertain scope verdict."""
    if gate.get("invalid_markers"):
        return True
    if gate.get("scope_review_failure") or gate.get("error_code") == "dispatch_outcome_unknown":
        return False
    if (gate.get("scoped_review") or {}).get("verified"):
        return bool(_confirmed_scope_missing(gate))
    return bool(gate.get("unsupported"))


def _can_preserve_citation_draft(gate, audit):
    """Keep useful text as partial output without certifying failed claims.

    Invalid IDs or an answer whose cited claims are all confirmed false cannot
    be presented as usable source-backed output.  A small failed/unknown item
    in an otherwise cited answer remains a visible review issue, not a reason
    to erase all the text.  Completion is still blocked separately below.
    """
    if gate.get("invalid_markers") or not gate.get("marked_claims"):
        return False
    if float(gate.get("grounded_ratio", 0)) < GROUNDED_RATIO_MIN:
        return False
    failed = audit.get("known_failed_count", len(audit.get("unsupported") or []))
    cited = audit.get("factual_claims", gate.get("marked_claims", 0))
    return not (type(failed) is int and type(cited) is int and cited > 0 and failed >= cited)


def execute(db, question, paper_id, skills, conversation_id, images, run_id,
            checkpoint, save_checkpoint, execution_mode="execute", reviewed_plan_id=None, workspace=None,
            runtime_config=None, display_event_sink=None):
    """Keep finalization steering in the same execution, attempt and QA identity.

    A pass may return a committed model checkpoint before irreversible delivery.
    Iterate instead of recursing so repeated late corrections cannot grow the
    Python stack or create a second executor.
    """
    continuing = False
    while True:
        with closing(_execute_pass(db, question, paper_id, skills, conversation_id, images, run_id,
                checkpoint, save_checkpoint, execution_mode, reviewed_plan_id, workspace,
                runtime_config=runtime_config, internal_continue=continuing,
                display_event_sink=display_event_sink)) as source:
            next_checkpoint = yield from source
        if next_checkpoint is None:
            return
        checkpoint, continuing = next_checkpoint, True


def _execute_pass(db, question, paper_id, skills, conversation_id, images, run_id,
            checkpoint, save_checkpoint, execution_mode="execute", reviewed_plan_id=None, workspace=None,
            runtime_config=None, internal_continue=False, display_event_sink=None):
    state = (AgentState.model_validate(checkpoint) if checkpoint else AgentState(
        question=question, paper_id=paper_id, conversation_id=conversation_id,
        skills=skills or [], max_steps=max(1, ops.settings.max_agent_steps),
        execution_mode=execution_mode, reviewed_plan_id=reviewed_plan_id,
        workspace=workspace,
        runtime_config=runtime_config,
    ))
    if state.runtime_config is None:
        state.runtime_config = deepcopy(runtime_config)
    from ..history_revisions import answer_only, ANSWER_READ_TOOLS, ANSWER_ONLY_PROMPT
    revising_answer = answer_only(state.runtime_config)
    from ..source_runtime import validate_frozen_sources, validate_source_evidence
    validate_frozen_sources(db, conversation_id, state.runtime_config)
    bound_source_ids = (state.runtime_config or {}).get("source_revision_ids", [])
    if checkpoint and state.source_revision_ids != bound_source_ids:
        raise ValueError("执行检查点的来源版本与冻结范围不匹配")
    state.source_revision_ids = list(bound_source_ids)
    attempt = run_lifecycle.current_attempt(db, run_id) if run_id else None
    if attempt is not None:
        if state.attempt_id is None:
            state.attempt_id, state.writer_token = attempt.id, attempt.writer_token
        run_lifecycle.assert_writer(db, run_id, state.attempt_id, state.writer_token,
                                    allow_terminal=state.phase == "completed")
        from ..runtime_models import RuntimeRun
        state.goal_id = db.get(RuntimeRun, run_id).goal_id
        if state.runtime_config is not None:
            state.runtime_config.update(run_id=run_id, conversation_id=conversation_id,
                                        attempt_id=state.attempt_id, goal_id=state.goal_id)
    if (state.question, state.paper_id, state.conversation_id) != (question, paper_id, conversation_id):
        raise ValueError("检查点与任务不匹配")
    if (state.execution_mode, state.reviewed_plan_id) != (execution_mode, reviewed_plan_id):
        raise ValueError("检查点的执行模式或计划来源不匹配")
    if state.budget_chunk_steps is None:
        # Older checkpoints do not store a segment size. Use the smaller of
        # their saved budget and the current setting so legacy runs resume in
        # familiar-sized chunks instead of growing exponentially.
        state.budget_chunk_steps = min(state.max_steps, max(1, ops.settings.max_agent_steps))
    # Preserve the selected identity on fresh/resumed runs. Readiness is
    # enforced by retrieval itself, so a parsing paper cannot block unrelated
    # conversation or silently widen the selected search boundary.
    ops.describe_paper_scope(db, paper_id)
    from ..workspace import validate_scope
    validate_scope(db, conversation_id, state.workspace)
    if state.workspace != workspace:
        raise ValueError("检查点的项目文件快照不匹配")
    if state.reviewed_plan_id:
        source_text = reviewed_plan_text(db, state.reviewed_plan_id, conversation_id, run_id)
        if not state.reviewed_plan:
            state.reviewed_plan = source_text
    if state.pending_operation and not state.resolution:
        raise CheckpointNeedsReview("上一步执行结果未确认，请检查后选择重试或跳过")
    def persist_state(snapshot):
        if state.attempt_id:
            run_lifecycle.assert_writer(db, run_id, state.attempt_id, state.writer_token)
        if state.goal_id:
            from ..goal_ledger import sync_plan_requirements, apply_goal_to_plan
            goal = sync_plan_requirements(db, state.goal_id, state.todo)
            state.todo = apply_goal_to_plan(goal, state.todo)
            snapshot["todo"] = deepcopy(state.todo)
        if save_checkpoint:
            save_checkpoint(snapshot)
        else:
            if state.attempt_id:
                run_lifecycle.save_checkpoint(db, run_id, snapshot)
            db.commit()

    saver = Checkpointer(state, persist_state if save_checkpoint or state.attempt_id else None)
    git_owner = {'conversation_id': conversation_id, 'run_id': run_id or state.record_id}
    git_prepared = False
    from ..models import ForegroundRun, TaskRecord
    steering_enabled = bool(run_id and conversation_id and (
        db.get(ForegroundRun, run_id) is not None or db.get(TaskRecord, run_id) is not None))

    def log(kind, payload):
        if kind == 'skill_loaded':
            state.skill_manifest = [item for item in state.skill_manifest if item['identity'] != payload['skill']['identity']]
            state.skill_manifest.append(deepcopy(payload['skill']))
        event = {"kind": kind, "time": datetime.now().strftime("%H:%M:%S"), **deepcopy(payload)}
        state.trace.append(event)
        if kind == "answer" and state.phase == "setup":
            state.answer = payload.get("answer", "")
            state.result = dict(payload)
        return kind, event

    def review_progress(payload):
        # Review calls are synchronous. This optional public-only sink lets
        # an observer see completed batch counts while that call is running;
        # it never controls cancellation, checkpointing, or paid dispatches.
        if display_event_sink is not None:
            kind, event = log("review_progress", payload)
            display_event_sink(kind, event)

    def audit_with_progress(*args, **kwargs):
        # Several focused executor tests and integrations provide narrow fake
        # audit adapters. Keep those contracts working while the production
        # auditor opts into batch progress.
        try:
            parameters = inspect.signature(ops.audit_answer).parameters.values()
            accepts_progress = any(parameter.name == "on_progress"
                                   or parameter.kind is inspect.Parameter.VAR_KEYWORD
                                   for parameter in parameters)
        except (TypeError, ValueError):
            accepts_progress = False
        if accepts_progress:
            kwargs["on_progress"] = review_progress
        return ops.audit_answer(*args, **kwargs)

    last_identity_check = -1.0

    def check_cancel():
        nonlocal last_identity_check
        # Stream tokens need a cheap cancellation check. Durable writes and
        # actual side effects are fenced separately at every saver boundary.
        now = ops.time.monotonic()
        if state.attempt_id and now - last_identity_check >= 0.25:
            run_lifecycle.assert_writer(db, run_id, state.attempt_id, state.writer_token,
                                        allow_terminal=state.phase == "completed")
            last_identity_check = now
        if ops.cancel_store.is_cancelled(run_id):
            raise AgentCancelled()

    def record_model_usage(usage):
        if isinstance(usage, dict):
            for key in state.usage:
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    state.usage[key] += value

    def run_hook(key, name, payload, **kwargs):
        check_cancel()
        if revising_answer:
            return {"ran": 0, "blocked": False, "feedback": "历史答案修订停用命令钩子"}
        if state.execution_mode == "plan":
            return {"ran": 0, "blocked": False, "feedback": "计划模式停用命令钩子"}
        if goal_constraint("__command_hook__"):
            return {"ran": 0, "blocked": False, "feedback": "当前用户要求禁止写文件，命令钩子未执行"}
        retry_hook = bool(state.pending_operation and state.pending_operation.get("key") == key
                          and state.resolution == "retry")
        def invoke_hook():
            if queued_steering() or goal_constraint("__command_hook__"):
                return {"ran": 0, "blocked": False, "feedback": "用户补充或禁止写文件要求已阻止新的命令钩子"}
            def before_command(command):
                check_cancel()
                if queued_steering():
                    raise CheckpointNeedsReview("用户补充要求待处理，命令钩子已在下一条命令前停止；请先回原任务应用补充。")
                signature = call_signature("__command_hook__", {"event": name, "command": command})
                previous = state.memory_side_effects.get(signature, {})
                if previous.get("execution_unknown") and not retry_hook:
                    raise CheckpointNeedsReview("此前相同命令钩子的结果尚未确认，未自动重发；请核对后重试、跳过或停用该钩子。")
            def execute_command(command, command_payload, timeout, index):
                receipt_key = "hook-command/" + key + "/" + str(index) + "/" + call_signature(
                    "__command_hook_receipt__", {"command": command, "payload": command_payload, "timeout": timeout})
                if receipt_key in state.operations:
                    return {**deepcopy(state.operations[receipt_key]), "checkpoint_reused": True}
                result = ops.hooks_store._run_one(command, command_payload, timeout)
                if not result.get("execution_unknown"):
                    # Keep the outer event intent pending, but commit each
                    # known command result before starting the next command.
                    state.operations[receipt_key] = deepcopy(result)
                    saver.save()
                return result
            result = ops.hooks_store.run_hooks(name, payload, before_command=before_command,
                before_commands=lambda commands: [before_command(command) for command in commands],
                execute_command=execute_command, **kwargs)
            if result.get("execution_unknown"):
                for item in result.get("results", []):
                    if item.get("execution_unknown"):
                        signature = call_signature("__command_hook__", {"event": name, "command": item["command"]})
                        state.memory_side_effects[signature] = {"operation_key": key,
                            "user_input_count": len(state.applied_input_ids), "execution_unknown": True}
                # Keep perform()'s pending intent, instead of settling an
                # uncertain hook as an ordinary successful hook receipt.
                saver.save()
                raise CheckpointNeedsReview("命令钩子已启动但结果未确认；已保留任务，请核对后选择重试或跳过。")
            return result
        return saver.perform(
            key, "hook", name, payload,
            invoke_hook,
            {"ran": 0, "blocked": False, "feedback": "用户跳过了中断的钩子"},
        )

    def goal_snapshot_for_policy():
        if state.goal_id:
            from ..goal_ledger import snapshot_goal
            return snapshot_goal(db, state.goal_id)
        # Legacy/direct callers still receive the same pre-action constraint;
        # they do not gain a second persistent task or a forced visible plan.
        return {"id": None, "revision": 1, "revisions": [{"revision": 1, "action": "create",
            "source": "user", "source_message_id": "current-user", "changes": {"description": question}}]}

    def goal_constraint(name, arguments=None):
        from ..goal_candidates import tool_constraint
        return tool_constraint(goal_snapshot_for_policy(), name, arguments)

    def requirement_messages(messages=None):
        messages = state.messages if messages is None else messages
        if not state.goal_id:
            return messages
        snapshot = goal_snapshot_for_policy()
        coverage = snapshot["input_coverage"]
        candidates = coverage["candidates"]
        if len(candidates) < 2 and not coverage["active_file_constraint"] and not snapshot["incomplete_requirements"]:
            return messages
        visible, remaining = [], 8000
        for item in candidates:
            if len(visible) == 24 or len(item["quote"]) > min(remaining, 4000):
                continue
            visible.append({key: item[key] for key in ("source_message_id", "start", "end", "quote")})
            remaining -= len(item["quote"])
        context = {"goal_revision": snapshot["revision"], "offset_unit": "Unicode code points, end exclusive",
                   "candidates": visible, "omitted_candidate_count": len(candidates) - len(visible),
                   "outstanding": [{"id": item["id"], "title": item["title"],
                                    "user_result_contract": (item.get("tool_arguments") or {}).get("result_contract")}
                                   for item in snapshot["incomplete_requirements"]][:24]}
        position = 1 if messages and messages[0].get("role") == "system" else 0
        return [*messages[:position], {"role": "system", "content":
            "以下是应用保存的用户原话区间及未完成验收项。候选不等于新增用户要求；计划只是方法。"
            "复杂任务可在 update_plan 中可选提交 requirement_candidates 核对理解；普通答复不需要计划。"
            "完成前核对数量、交付和局限等全部原话；结构检查不代表语义已正确。\n"
            + json.dumps(context, ensure_ascii=False)}, *messages[position:]]

    def request_messages(messages, request_tools=None):
        validate_skills(state.skill_manifest)
        if state.phase not in ("deliver", "completed") and ensure_memory_current(db, state, run_id, images=images):
            if runtime_config is not None:
                runtime_config["memory_fingerprint"] = state.memory_fingerprint
            saver.save()
            raise MemoryStageRebuilt()
        from .active_context import compact_messages, _frozen_tool_block
        reason = None
        identity = None
        identity_reader = getattr(llm, "context_identity", None)
        def private_message(message):
            return (message.get("_provider") is not None or _frozen_tool_block(message)
                    or (message.get("role") == "assistant" and any(
                        key in message for key in ("reasoning_content", "reasoning"))))
        if callable(identity_reader) and any(private_message(m) for m in messages):
            identity = identity_reader()
            if any(private_message(m) and (
                    not isinstance(m.get("_provider"), dict) or any(m["_provider"].get(key) != identity.get(key)
                    for key in ("version", "scope", "protocol"))) for m in messages):
                reason = "provider_identity"
        try:
            if reason:
                raise ContextCapacityExceeded("供应商私有上下文与当前连接不匹配", stage_reason=reason)
            outgoing, manifest = compact_messages(messages, tools=request_tools,
                binding=state.runtime_config, workspace=state.workspace, archive=state.context_archive)
        except ContextCapacityExceeded as exc:
            if not exc.stage_reason:
                raise
            renew_for_capacity(db, state, run_id, reason=exc.stage_reason, tools=tool_specs,
                               messages_builder=requirement_messages, images=images, provider_identity=identity)
            if runtime_config is not None:
                runtime_config["memory_fingerprint"] = state.memory_fingerprint
            saver.save()
            raise MemoryStageRebuilt(exc.stage_reason) from None
        state.context_manifest = manifest
        saver.save()
        return outgoing

    def model_messages_with_requirements():
        return request_messages(requirement_messages(), tool_specs)

    def public_plan():
        if state.plan_source == "requirements":
            return []
        return [step for step in state.todo if not step.get("acceptance")]

    def todo(phase):
        ops.plan_advance(state.todo, phase)
        steps = public_plan()
        if steps:
            yield log("todo_update", {"text": "更新任务进度", "steps": steps})

    def queued_steering():
        if not steering_enabled:
            return False
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from ..run_inputs import RunInput
        # Used only to wake an obsolete approval wait. Application itself takes
        # place in the executor transaction and its durable checkpoint below.
        with Session(bind=db.get_bind()) as inbox_db:
            return inbox_db.scalar(select(RunInput.input_id).where(
                RunInput.run_id == run_id, RunInput.conversation_id == conversation_id,
                RunInput.kind == "steer", RunInput.status == "queued").limit(1)) is not None

    def apply_steering(*, abandon_calls=False):
        if not steering_enabled or state.pending_operation:
            return False
        from ..run_inputs import consume_steering_inputs
        incoming = consume_steering_inputs(db, run_id, conversation_id, attempt_id=state.attempt_id)
        if not incoming:
            return False
        if not state.goal_id:
            from ..goal_ledger import ensure_run_goal
            state.goal_id = ensure_run_goal(db, run_id, conversation_id, question)["goal_id"]
        if abandon_calls and state.phase == "tools":
            # OpenAI-compatible histories require a receipt for each declared
            # tool call, including actions withdrawn before their execution.
            for call in state.calls[state.tool_index:]:
                state.messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps({
                    "cancelled_by_user_steering": True,
                    "error": "用户补充已生效，该旧操作未开始，已交回模型重新决策。"}, ensure_ascii=False)})
            state.calls, state.tool_index = [], 0
            state.step += 1  # Never reuse operation keys from an abandoned batch.
        for row in incoming:
            if row["input_id"] not in state.applied_input_ids:
                from ..source_runtime import context_text as source_context_text
                source_binding = row.get("source_binding")
                if source_binding is not None:
                    state.runtime_config = {**(state.runtime_config or {}), **source_binding}
                    state.source_revision_ids = list(source_binding.get("source_revision_ids", []))
                state.messages.append({"role": "user", "content": row["content"] + source_context_text(state.runtime_config),
                                       "_user_steering": row["input_id"]})
                state.applied_input_ids.append(row["input_id"])
        for step in state.todo:
            if step.get("phase") == "final":
                step["evidence"] = [proof for proof in step.get("evidence", [])
                                    if proof.get("kind") != "delivery"]
                if step.get("status") == "done" and not step["evidence"]:
                    step["status"] = "pending"
        state.calls, state.tool_index = [], 0
        state.answer, state.result = "", None
        state.plan_response = None
        state.phase = "model"
        state.incomplete_finalizations = state.no_progress_segments = 0
        saver.save()  # Inbox application, goal revision and checkpoint commit together.
        for row in incoming:
            yield log("run_input", {"input": row, "revision": row["revision"]})
        yield log("answer_reset", {"text": "已应用你的补充，正在按更新后的要求继续"})
        return True

    def completion_payload(outcome, *, allow_partial=False):
        """Return structured tool output only when execution produced a usable result."""
        if not isinstance(outcome, dict) or not outcome.get("executed") or outcome.get("error"):
            return None
        raw = outcome.get("result")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            payload = None
        if isinstance(payload, dict):
            if payload.get("error") or any(payload.get(key) for key in (
                    "blocked_by_tool_policy", "blocked_by_plan_mode", "blocked_by_hook",
                    "blocked_by_live_policy", "denied_by_user", "execution_unknown")):
                return None
            if payload.get("limited") or payload.get("truncated") or payload.get("output_is_partial"):
                return None
            if not allow_partial and (payload.get("next_offset") is not None or payload.get("next_page")):
                return None
            if payload.get("status") in ("failed", "error", "blocked", "interrupted", "timeout", "timed_out", "output_limit",
                                          "pending", "proposed", "prepared", "running", "conflict", "cancelled", "rejected", "denied"):
                return None
        return payload

    def call_signature(name, arguments):
        from ..operation_receipts import signature
        return signature(name, arguments)

    def requirements_satisfied():
        if not state.goal_id:
            return True
        from ..goal_ledger import snapshot_goal
        return not snapshot_goal(db, state.goal_id).get("incomplete_requirements")

    def resolve_alternatives():
        if requirements_satisfied():
            for failure in state.tool_failures:
                if failure.get("status") == "open" and failure.get("alternative_proof"):
                    failure.update(status="resolved", resolution="verified_alternative")

    def prove_tool_outcome(name, arguments, outcome, operation_key, proof=None):
        signature = call_signature(name, arguments)
        if proof is not None:
            child_id = (proof.get("research_delivery") or {}).get("child_run_id")
            for failure in state.tool_failures:
                if failure.get("status") != "open":
                    continue
                if failure["signature"] == signature or (child_id and failure.get("research_child_id") == child_id):
                    failure.update(status="resolved", resolution="successful_retry", resolution_proof=deepcopy(proof))
                elif not failure.get("execution_unknown"):
                    for candidate in failure.get("alternatives", []):
                        if ops.plan_complete_one([deepcopy(candidate)], candidate.get("phase", "analyze"), proof):
                            failure["alternative_proof"] = deepcopy(proof)
                            break
            resolve_alternatives()
            return
        try:
            payload = json.loads(outcome.get("result", ""))
        except (ValueError, TypeError):
            payload = {}
        payload = payload if isinstance(payload, dict) else {}
        # Invalid model syntax is returned for correction, not made into a new
        # user obligation. Real failed/denied/unknown operations remain facts.
        if payload.get("invalid_tool_call") or ((payload.get("blocked_by_goal_constraint") or payload.get("user_steering_waiting"))
                                                  and not payload.get("execution_unknown")):
            return
        if payload.get("not_replayed") and any(item.get("operation_key") == payload.get("prior_operation_key")
                                                 for item in state.tool_failures):
            return  # The original uncertain operation remains the one to reconcile.
        error = outcome.get("error") or payload.get("error") or (
            "操作实际结果仍未知，需要核对；不能据正常返回或跳过认定任务完成" if payload.get("execution_unknown") else None)
        research_child_id = None
        if name in {"start_research_child", "resume_research_child", "read_research_child"}:
            # This path is reached only without a verified database delivery.
            # A successful HTTP/tool return is not a completed research task.
            research_child_id = payload.get("run_id")
            error = error or "科研子任务尚未交付可核验的完整成果，需继续完成或明确处理未完成部分"
        if not error:
            return
        if any(item.get("id") == f"failure/{operation_key}" for item in state.tool_failures):
            return
        state.tool_failures.append({"id": f"failure/{operation_key}", "signature": signature,
            "tool_name": name, "arguments": deepcopy(arguments), "phase": ops._tool_phase(name),
            "status": "open", "error": str(error)[:2000], "operation_key": operation_key,
            "research_child_id": research_child_id,
            "execution_unknown": bool(payload.get("execution_unknown")),
            "plan_generation": state.plan_generation, "alternatives": []})

    def register_alternatives():
        for failure in state.tool_failures:
            if failure.get("status") != "open" or failure.get("execution_unknown"):
                continue
            # Only an explicit subsequent update_plan changes the method. A
            # later unrelated successful tool cannot silently erase failures.
            failure["alternatives"] = [deepcopy(step) for step in state.todo
                if not step.get("acceptance") and step.get("phase") != "final"
                and (step.get("tool_name") and call_signature(step["tool_name"], step.get("tool_arguments")) != failure["signature"]
                     or not step.get("tool_name") and step.get("phase") in ("analyze", "write"))]

    try:
        check_cancel()
        from ..runtime_context import public_runtime_config
        yield log("runtime_config", {"runtime_config": public_runtime_config(state.runtime_config),
                                     "text": "本轮模型与权限配置已绑定"})
        yield log("execution_mode", {"execution_mode": state.execution_mode, "reviewed_plan_id": state.reviewed_plan_id,
                                     "text": "计划模式：仅分析与规划" if state.execution_mode == "plan" else "执行模式"})
        if state.phase == "setup":
            saver.save()
            prepared = yield from ops._prepare_agent(
                db, question, paper_id, skills, conversation_id, images, run_id,
                log=log, run_hook=run_hook,
                execution_mode=state.execution_mode, reviewed_plan=state.reviewed_plan,
                workspace=state.workspace,
            )
            if prepared is None:
                if state.result:
                    state.phase = "completed"
                    saver.save()
                return
            state.messages, state.todo = prepared
            initialize_memory(state)
            if runtime_config is not None:
                runtime_config["memory_fingerprint"] = state.memory_fingerprint
            if revising_answer:
                state.messages.insert(1 if state.messages and state.messages[0].get("role") == "system" else 0,
                                      {"role": "system", "content": ANSWER_ONLY_PROMPT})
            state.planning_started = bool(state.todo)
            state.phase = "model"
            saver.save()
        elif checkpoint:
            if state.todo:
                state.planning_started = True
                state.todo = ops.plan_add_acceptance_requirements(state.todo, question)
            yield log("status", {"text": "已应用补充，保留实际成果继续执行" if internal_continue
                                 else "已恢复执行上下文，保留已完成的工具结果"})
            if public_plan():
                yield log("plan", {"text": "恢复已保存计划", "steps": public_plan()})

        llm = ops.get_llm()
        if state.phase not in ("deliver", "completed") and ensure_memory_current(db, state, run_id, images=images):
            if runtime_config is not None:
                runtime_config["memory_fingerprint"] = state.memory_fingerprint
            saver.save()
            raise MemoryStageRebuilt()
        from .tool_availability import available_workspace_tools
        tool_specs = available_workspace_tools(ops.get_tool_specs(), state.workspace)
        if revising_answer:
            tool_specs = [spec for spec in tool_specs if spec["name"] in ANSWER_READ_TOOLS]
        if state.execution_mode == "plan":
            tool_specs = [spec for spec in tool_specs if spec["name"] in PLAN_READ_TOOLS]
            tool_specs.append(deepcopy(PLAN_RESPONSE_TOOL_SPEC))
        from .active_context import CONTEXT_TOOL, CONTEXT_TOOL_SPEC
        tool_specs.append(deepcopy(CONTEXT_TOOL_SPEC))
        from ..model_capability_policy import capability, notices, text_only_messages
        for notice in notices(state.runtime_config):
            yield log("status", {"text": notice, "reason": "model_capability_route"})
        if capability(state.runtime_config, "tools") == "unsupported":
            tool_specs = []
            state.messages = text_only_messages(state.messages)
            saver.save()
        # Permissions granted in a previous process/run do not authorize new work.
        session_allowed = set()
        policy_state = {
            "approval_mode": (state.runtime_config or {}).get("approval_mode", ops.settings.approval_mode),
            "policy_revision": (state.runtime_config or {}).get("policy_revision", 0),
        }

        def refresh_policy():
            from ..model_profiles import current_policy
            from sqlalchemy.orm import Session
            # Approval waits and tool boundaries must see committed changes from
            # another window, not this executor's ORM identity-map snapshot.
            with Session(bind=db.get_bind()) as policy_db:
                latest = current_policy(policy_db, conversation_id, state.runtime_config or policy_state)
            changed = latest != policy_state
            if changed:
                session_allowed.clear()
                policy_state.clear()
                policy_state.update(latest)
                if state.runtime_config is not None:
                    state.runtime_config.update(latest)
            return changed

        def needs_approval(name):
            from ..mcp_policy import approval_for
            refresh_policy()
            policy = approval_for(name)
            if policy == "always":
                return True
            if ops.tool_risk(name) == "safe":
                return False
            mode = policy_state["approval_mode"]
            if mode in ("always", "read_only"):
                return True
            if policy == "never":
                return False
            return mode != "full_access" and name not in session_allowed

        def authorize(name, approval_arguments, arguments, key):
            """Authorize one operation; a policy edit expires an earlier grant."""
            if not needs_approval(name):
                return True
            ph = run_hook(f"hook/{key}/permission", "PermissionRequest", {
                "tool_name": name, "arguments": approval_arguments,
                "reason": ops.approval_reason(name, arguments),
            }, matcher_value=name)
            if ph["ran"]:
                yield log("hook", {"text": f"PermissionRequest 钩子执行 {ph['ran']} 条", **ph})
            while needs_approval(name):
                pending = ops.approval_store.create(name, approval_arguments, ops.tool_risk(name),
                                                   ops.approval_reason(name, arguments), run_id=run_id)
                pending.refresh_policy = lambda: refresh_policy() or queued_steering()
                before_policy = dict(policy_state)
                try:
                    yield log("approval_request", {"text": "需要你的批准", **pending.to_request()})
                    check_cancel()
                    ops.approval_store.wait(pending.id)
                    check_cancel()
                    if (yield from apply_steering(abandon_calls=True)):
                        yield log("approval_result", {"approval_id": pending.id, "decision": "superseded",
                            "scope": "once", "text": "你的补充已生效，旧审批已撤销，正在重新决策"})
                        return None
                    refresh_policy()
                    if pending.decision == "recheck" or before_policy != policy_state:
                        yield log("approval_result", {"approval_id": pending.id, "decision": "superseded",
                            "scope": "once", "text": "会话权限已更新，此审批已失效，正在按新权限重新检查"})
                        continue
                    approved = pending.decision == "approve"
                    effective_scope = ("session" if approved and pending.scope == "session"
                                       and policy_state["approval_mode"] == "auto" else "once")
                    if effective_scope == "session":
                        session_allowed.add(name)
                    yield log("approval_result", {"approval_id": pending.id,
                        "decision": "approve" if approved else "deny", "scope": effective_scope,
                        "text": "已批准，继续执行" if approved else "你已拒绝该操作，跳过此工具"})
                    # The UI can update permissions while observing the result.
                    # Do not carry a grant across that generator suspension.
                    refresh_policy()
                    if approved and before_policy != policy_state:
                        continue
                    if approved and (yield from apply_steering(abandon_calls=True)):
                        return None
                    return approved
                finally:
                    ops.approval_store.discard(pending.id)
            return True

        while state.phase in ("model", "tools"):
            check_cancel()
            validate_skills(state.skill_manifest)
            if state.phase == "model":
                yield from apply_steering()
                if state.step >= state.max_steps:
                    segment_fingerprint = hashlib.sha256(
                        ":".join(sorted(set(state.segment_result_fingerprints))).encode("utf-8")
                    ).hexdigest()
                    novel = set(state.segment_result_fingerprints) - set(state.progress_seen)
                    if not novel:
                        state.no_progress_segments += 1
                    else:
                        state.no_progress_segments = 0
                    state.progress_seen = sorted(set(state.progress_seen) | set(state.segment_result_fingerprints))
                    state.last_segment_fingerprint = segment_fingerprint
                    state.segment_result_fingerprints = []
                    if state.no_progress_segments >= 2:
                        state.budget_exhausted = True
                        state.phase = "paused"
                        from ..foreground_run_store import finish as finish_foreground_run
                        finish_foreground_run(
                            db, run_id, "paused",
                            "连续执行阶段没有取得新结果，已保存检查点；请调整检索范围或任务要求后继续。",
                        )
                        saver.save()
                        yield from todo("read")
                        yield log("paused", {
                            "text": "连续两个执行阶段没有取得新结果，任务已安全暂停并保存检查点。调整检索范围或任务要求后，可继续当前任务。",
                            "step": state.step,
                            "max_steps": state.max_steps,
                            "completed_tools": sum(key.startswith("tool/") for key in state.operations),
                            "reason": "no_progress",
                        })
                        return
                    if state.no_progress_segments == 1:
                        state.messages.append({"role": "user", "content": REPLAN_AFTER_STAGNATION})
                    state.budget_exhausted = False
                    chunk_steps = state.budget_chunk_steps or max(1, ops.settings.max_agent_steps)
                    state.max_steps += chunk_steps
                    state.continuation_segments += 1
                    saver.save()
                    text = (
                        "上一阶段没有产生新结果，已要求模型调整方法并自动继续"
                        if state.no_progress_segments == 1
                        else "本段执行额度已用完，检查点已保存，正在自动继续当前任务"
                    )
                    yield log("status", {
                        "text": text,
                        "step": state.step,
                        "max_steps": state.max_steps,
                        "continuation_segments": state.continuation_segments,
                    })
                    continue
                yield log("step", {"text": f"第 {state.step + 1} 轮思考"})
                response = None
                try:
                    with closing(ops._stream_chat_with_retry(llm, model_messages_with_requirements(), tools=tool_specs)) as source:
                        for item in source:
                            check_cancel()
                            if item[0] == "delta":
                                yield log(f"{item[1]}_delta", {"text": item[2]})
                            else:
                                response = item[1]
                except AgentCancelled:
                    raise
                except (ResourceBudgetExceeded, ContextCapacityExceeded, ResourceRequestCancelled, SkillsChanged, MemoryScopeChanged, MemoryStageRebuilt):
                    raise
                except Exception as exc:
                    yield log("error", {"text": f"大模型调用失败：{type(exc).__name__}: {exc}"})
                    return  # Retain the model boundary so a later resume can retry it.
                check_cancel()
                if response is None:
                    yield log("error", {"text": "模型流结束但未返回完整结果，可从检查点重试"})
                    return
                validate_skills(state.skill_manifest)
                for key in state.usage:
                    state.usage[key] += int((response.usage or {}).get(key) or 0)
                if (yield from apply_steering()):
                    state.step += 1
                    saver.save()
                    continue
                if response.reasoning_content:
                    yield log("thinking", {"text": response.reasoning_content})
                if response.tool_calls and response.content:
                    if (state.execution_mode == "plan" and all(
                            call.get("name") == PLAN_RESPONSE_TOOL_NAME for call in response.tool_calls)):
                        # A response control is not public work narration. Its
                        # answer argument replaces any provisional content.
                        yield log("answer_reset", {"text": ""})
                    else:
                        # Provider content before a real tool action is a public
                        # progress update, independently from model reasoning.
                        yield log("commentary", {"text": response.content, "source": "model",
                                                 "draft_to_commentary": True})
                if not response.tool_calls:
                    state.answer = response.content or ""
                    state.provider_context = response.persisted_context()
                    if state.answer.strip() and state.execution_mode == "execute":
                        answer_proof = {"kind": "answer", "qa_id": state.record_id}
                        ops.plan_complete_one(state.todo, "analyze", answer_proof)
                        ops.plan_complete_one(state.todo, "write", answer_proof)
                        prove_tool_outcome("__answer__", {}, {}, "answer", answer_proof)
                    outstanding = [] if state.execution_mode == "plan" else [step for step in state.todo
                                   if step.get("status") != "done"
                                   and (step.get("phase") not in ("audit", "final") or step.get("tool_name"))]
                    if state.goal_id and state.execution_mode == "execute":
                        from ..goal_ledger import evaluate_input_contracts
                        coverage = evaluate_input_contracts(db, state.goal_id, state.answer, workspace=state.workspace)
                        outstanding.extend(item for item in coverage["incomplete_requirements"]
                                           if (item.get("acceptance") or "").startswith("user_contract:"))
                    if outstanding:
                        state.incomplete_finalizations += 1
                        if state.incomplete_finalizations >= 2:
                            state.phase = "finalize"
                            saver.save()
                            yield log("status", {
                                "text": "已尝试补齐未完成步骤；仍缺少可核对的完成依据，将保留为部分完成",
                                "incomplete_steps": outstanding,
                            })
                            yield from todo(outstanding[0].get("phase", "analyze"))
                            saver.save()
                            continue
                        if state.answer:
                            state.messages.append(response.assistant_message())
                        descriptions = "；".join(step.get("title", "未命名步骤") for step in outstanding)
                        state.messages.append({"role": "user", "content": MISSING_STEPS_PROMPT.format(
                            steps=descriptions)})
                        state.phase = "model"
                        saver.save()
                        yield log("answer_reset", {
                            "text": "仍有任务步骤未取得完成依据，正在继续补齐",
                            "incomplete_steps": outstanding,
                        })
                        yield from todo(outstanding[0].get("phase", "analyze"))
                        state.answer = ""
                        saver.save()
                        continue
                    state.incomplete_finalizations = 0
                    state.phase = "finalize"
                    if state.todo:
                        yield from todo("write")
                else:
                    first_valid_call = next((call for call in response.tool_calls
                                             if not _tool_call_error(call.get("name"), call.get("arguments", {}), tool_specs)), None)
                    if not state.planning_started and first_valid_call is not None:
                        state.planning_started = True
                        if first_valid_call["name"] != "update_plan":
                            # Real tools do not imply a user-facing multi-step
                            # plan. Keep only necessary acceptance checks private.
                            state.todo = ops.plan_add_acceptance_requirements([], question)
                            state.plan_source = "requirements"
                    state.calls = [dict(call, id=call.get("id") or f"call_{state.step}_{i}")
                                   for i, call in enumerate(response.tool_calls)]
                    state.tool_index = 0
                    state.messages.append(response.assistant_message(state.calls))
                    state.phase = "tools"
                saver.save()

            while state.phase == "tools" and state.tool_index < len(state.calls):
                check_cancel()
                validate_skills(state.skill_manifest)
                call = state.calls[state.tool_index]
                name, arguments = call["name"], call.get("arguments", {})
                key = f"{state.step}/{state.tool_index}"
                tool_key = f"tool/{key}"
                # A completed tool may still need its post hook/result bookkeeping.
                cached = tool_key in state.operations
                if not cached and (yield from apply_steering(abandon_calls=True)):
                    break
                invalid_call = None if cached else _tool_call_error(name, arguments, tool_specs)
                if name == CONTEXT_TOOL and not invalid_call:
                    from .active_context import read_archive
                    def authorize_archive(original_name, original_arguments):
                        from .tools import tool_policy_error
                        if tool_policy_error(original_name):
                            return False
                        if goal_constraint(original_name, original_arguments):
                            return False
                        if original_name == "read_attached_source":
                            from ..source_runtime import require_attached_revision
                            require_attached_revision(db, original_arguments.get("source_revision_id"),
                                conversation_id=conversation_id, binding=state.runtime_config)
                            return True
                        if original_name == "read_project_file":
                            from .. import workspace as workspace_module
                            workspace_module.validate_scope(db, conversation_id, state.workspace)
                            workspace_module.scoped_path(state.workspace, original_arguments.get("path", ""))
                            return True
                        return False
                    try:
                        archived = read_archive(state.context_archive, arguments, binding=state.runtime_config,
                            workspace=state.workspace, authorize=authorize_archive)
                    except Exception as exc:
                        archived = {"error": str(exc), "context_access_denied": True}
                    archived_text = json.dumps(archived, ensure_ascii=False)
                    state.messages.append({"role": "tool", "tool_call_id": call["id"], "content": archived_text})
                    state.tool_index += 1
                    saver.save()
                    yield log("tool_result", {"name": name, "preview": archived_text[:600]})
                    continue
                if state.execution_mode == "plan" and name == PLAN_RESPONSE_TOOL_NAME:
                    # This is a checkpointed local response decision, not a
                    # tool execution. No hook, approval, external call or tool
                    # success proof may be created from it.
                    reason = invalid_call
                    kind = arguments.get("kind") if isinstance(arguments, dict) else None
                    answer = arguments.get("answer") if isinstance(arguments, dict) else None
                    if not reason and (not isinstance(answer, str) or not answer.strip()):
                        reason = "直接答复或澄清必须提供非空 answer。"
                    if not reason and len(state.calls) != 1:
                        reason = "回应决定必须单独调用，不能和其他工具放在同一批次。"
                    if not reason and public_plan():
                        reason = "已登记的公开计划不能改为直接答复或澄清；请保留计划并说明实际状态。"
                    if not reason and (state.budget_exhausted or state.summary_failed):
                        reason = "执行预算或汇总状态未完成，不能通过回应决定标为完成。"
                    if not reason and kind == "direct_response" and (
                            not requirements_satisfied()
                            or any(step.get("acceptance") and step.get("status") != "done" for step in state.todo)
                            or any(failure.get("status") == "open" for failure in state.tool_failures)):
                        reason = "仍有未完成的用户验收要求或工具失败，不能改为已完成的直接答复。"
                    state.messages.append({"role": "tool", "tool_call_id": call["id"],
                        "content": json.dumps({"accepted": not bool(reason), "kind": kind,
                                               **({"error": reason} if reason else {})}, ensure_ascii=False)})
                    state.tool_index += 1
                    state.plan_response = None
                    if not reason:
                        state.plan_response = PlanResponseDecision(
                            kind=kind, applied_input_ids=list(state.applied_input_ids))
                        state.answer = answer
                        state.calls, state.tool_index = [], 0
                        state.step += 1
                        state.phase = "finalize"
                    event = log("status", {"text": reason or ("需要用户澄清后继续" if kind == "clarification"
                                                             else "已按本轮请求选择直接答复"),
                                           "response_kind": kind, "response_accepted": not bool(reason)})
                    saver.save()
                    yield event
                    if not reason:
                        yield log("answer_delta", {"text": state.answer})
                    continue
                if not invalid_call:
                    yield from todo(ops._tool_phase(name))
                if cached:
                    yield log("status", {"text": f"复用已保存的「{name}」结果，继续处理剩余步骤"})
                elif not invalid_call:
                    yield log("status", {"text": ops._narrate(name, arguments), "name": name})
                    yield log("tool_call", {"name": name, "arguments": arguments})
                skip_pending = bool(state.pending_operation and
                                    state.pending_operation.get("key") == tool_key and state.resolution == "skip")
                prior_unknown = deepcopy(state.pending_operation) if state.pending_operation and state.resolution == "retry" else None
                if not cached and (yield from apply_steering(abandon_calls=True)):
                    break
                from .tools import tool_policy_error
                disabled_reason = None if invalid_call else tool_policy_error(name)
                if revising_answer and name not in ANSWER_READ_TOOLS:
                    disabled_reason = "历史答案修订仅允许读取资料；执行新动作须另建执行任务"
                goal_denial = None if invalid_call or cached else goal_constraint(name, arguments)
                prior_memory_effect = state.memory_side_effects.get(call_signature(name, arguments))
                from ..operation_receipts import retry_permission, consume_retry
                retry_grant = retry_permission(state, call_signature(name, arguments))
                if prior_memory_effect and prior_memory_effect.get("execution_unknown") and retry_grant:
                    prior_memory_effect = None
                if prior_memory_effect and not state.memory_generation and not prior_memory_effect.get("execution_unknown"):
                    prior_memory_effect = None
                if (prior_memory_effect and not prior_memory_effect.get("execution_unknown")
                        and prior_memory_effect.get("user_input_count") != len(state.applied_input_ids)):
                    prior_memory_effect = None
                if invalid_call:
                    if state.pending_operation and state.pending_operation.get("key") == tool_key:
                        state.pending_operation = state.resolution = None
                    outcome = {"result": json.dumps({"error": invalid_call, "invalid_tool_call": True}, ensure_ascii=False),
                               "error": "", "executed": False}
                    state.operations[tool_key] = outcome
                    saver.save()
                    yield log("policy_block", {"name": name, "text": invalid_call})
                elif goal_denial:
                    if prior_unknown:
                        goal_denial.update(execution_unknown=True, prior_operation=prior_unknown,
                            hint="本次重试未执行；中断前操作是否已经发生仍须核对，不能将其视为未执行。")
                    if state.pending_operation and state.pending_operation.get("key") == tool_key:
                        state.pending_operation = state.resolution = None
                    outcome = {"result": json.dumps(goal_denial, ensure_ascii=False), "error": "", "executed": False}
                    state.operations[tool_key] = outcome
                    saver.save()
                    yield log("policy_block", {"name": name, "text": goal_denial["error"],
                                               "goal_revision": goal_denial["goal_revision"]})
                elif disabled_reason and not cached:
                    if state.pending_operation and state.pending_operation.get("key") == tool_key:
                        state.pending_operation = state.resolution = None
                    outcome = {"result": json.dumps({"error": disabled_reason, "blocked_by_tool_policy": True}, ensure_ascii=False),
                               "error": "", "executed": False}
                    state.operations[tool_key] = outcome
                    saver.save()
                    yield log("policy_block", {"name": name, "text": disabled_reason})
                elif prior_memory_effect and not cached:
                    unknown_effect = bool(prior_memory_effect.get("execution_unknown"))
                    outcome = {"result": json.dumps({"already_executed": not unknown_effect, "not_replayed": True,
                        "execution_unknown": unknown_effect,
                        "prior_operation_key": prior_memory_effect["operation_key"],
                        "instruction": "此前相同操作在中断后被跳过，实际结果仍未知，未自动重发。请先读取实际成果或核对外部系统；不能把跳过当作未执行或成功。" if unknown_effect else
                            "此相同副作用操作已有成功回执，上下文重建不能再次执行。请读取已有成果核对；确需再次执行时需用户补充要求。"}, ensure_ascii=False),
                        "error": "", "executed": False}
                    state.operations[tool_key] = outcome
                    saver.save()
                elif state.execution_mode == "plan" and name not in PLAN_READ_TOOLS:
                    if state.pending_operation and state.pending_operation.get("key") == tool_key:
                        state.pending_operation = state.resolution = None
                    outcome = {"result": json.dumps({"error": "计划模式禁止此操作，请仅提交计划等待用户审阅", "blocked_by_plan_mode": True}),
                               "error": "", "executed": False}
                    state.operations[tool_key] = outcome
                    saver.save()
                    yield log("policy_block", {"name": name, "text": f"计划模式已阻止「{name}」"})
                elif cached or skip_pending:
                    outcome = saver.perform(tool_key, "tool", name, arguments,
                                            lambda: None, _skipped_tool())
                else:
                    hk = run_hook(f"hook/{key}/pre", "PreToolUse",
                                  {"tool_name": name, "arguments": arguments}, matcher_value=name)
                    if hk["ran"]:
                        yield log("hook", {"text": f"PreToolUse 钩子执行 {hk['ran']} 条", **hk})
                    approved = not hk["blocked"]
                    denial = ""
                    change = None
                    command = None
                    git_operation = None
                    service_operation = None
                    service_binding = {**(state.runtime_config or {}), "conversation_id": conversation_id,
                        "run_id": run_id, "attempt_id": state.attempt_id}
                    approval_arguments = arguments
                    if hk["blocked"]:
                        denial = json.dumps({"error": hk.get("feedback", "钩子阻断"), "blocked_by_hook": True}, ensure_ascii=False)
                        yield log("hook_block", {"text": "PreToolUse 钩子阻断了该工具", "tool_name": name,
                                                 "feedback": hk.get("feedback", "")})
                    if approved and name in service_tools.DEEPSEEK_TOOL_NAMES:
                        try:
                            service_operation = service_tools.prepare_deepseek_tool(db, name, arguments,
                                conversation_id=conversation_id,
                                operation_key=f"agent:{run_id or state.record_id}:{tool_key}",
                                binding=service_binding)
                            approval_arguments = service_operation.approval_arguments
                        except (service_tools.config.ServiceConfigurationError, service_tools.source_store.SourceError, ValueError):
                            approved = False
                            denial = json.dumps({"error": "独立服务尚未配置，或当前附件、范围及授权已变化；请在设置中核对后再试。",
                                "code": "service_preparation_failed", "automatic_retry": False}, ensure_ascii=False)
                    if approved and name == "edit_project_file":
                        try:
                            change = project_changes.prepare(state.workspace, conversation_id, arguments,
                                run_id=run_id, operation_key=f"agent:{run_id or state.record_id}:{tool_key}")
                            approval_arguments = {k: change[k] for k in ('path', 'operation', 'summary')}
                            approval_arguments['change_id'] = change['id']
                            yield log('project_change', {**approval_arguments, 'text': f"文件变更：{change['path']}"})
                            saver.save()
                            if change['status'] not in ('proposed', 'conflict', 'applied'):
                                approved = False
                                denial = json.dumps({'error': '此文件修改已拒绝或中断，请重新读取后生成新的修改。'}, ensure_ascii=False)
                        except HTTPException as exc:
                            approved = False
                            denial = json.dumps({'error': exc.detail}, ensure_ascii=False)
                    if approved and name == 'run_project_command':
                        try:
                            command = project_commands.prepare(state.workspace, conversation_id, arguments,
                                run_id=run_id or state.record_id, operation_key=f'agent:{run_id or state.record_id}:{tool_key}',
                                retry=bool(state.pending_operation and state.pending_operation.get('key') == tool_key and state.resolution == 'retry'))
                            approval_arguments = {key: command[key] for key in ('command', 'cwd', 'summary', 'timeout_seconds', 'attempt')}
                            approval_arguments['command_id'] = command['id']
                            yield log('project_command', {**approval_arguments, 'status': command['status'], 'text': f"项目命令：{command['summary']}"})
                            saver.save()
                        except HTTPException as exc:
                            approved = False
                            denial = json.dumps({'error': exc.detail}, ensure_ascii=False)
                    if approved and name == 'git_project_write':
                        try:
                            git_operation = git_operations.prepare_agent(state.workspace, conversation_id, arguments,
                                owner=git_owner, operation_key=tool_key)
                            git_prepared = True
                            approval_arguments = {field: git_operation[field] for field in
                                ('project_id', 'action', 'paths', 'message', 'revision', 'branch', 'head')}
                            approval_arguments.update(operation_id=git_operation['id'],
                                patch_preview=git_operation['patch'][:2000], patch_truncated=len(git_operation['patch']) > 2000)
                            yield log('git_operation', {'operation_id': git_operation['id'], 'project_id': git_operation['project_id'],
                                'action': git_operation['action'], 'status': git_operation['status'], 'text': f"Git 操作：{git_operation['action']}"})
                            saver.save()
                        except HTTPException as exc:
                            approved = False
                            denial = json.dumps({'error': exc.detail}, ensure_ascii=False)
                    if approved and name == "spawn_subagent":
                        start_hk = run_hook(f"hook/{key}/sub_start", "SubagentStart", {"task": arguments})
                        if start_hk["ran"]:
                            yield log("hook", {"text": f"SubagentStart 钩子执行 {start_hk['ran']} 条", **start_hk})
                    if (approved and not (change and change['status'] == 'applied')
                            and not (command and command['status'] in project_commands.TERMINAL)
                            and not (git_operation and git_operation['status'] != 'prepared')):
                        approved = yield from authorize(name, approval_arguments, arguments, key)
                        if not approved:
                            if change:
                                project_changes.reject(change['id'])
                            if command:
                                project_commands.reject(command['id'])
                            if git_operation:
                                git_operation = git_operations.decide(git_operation['project_id'], git_operation['id'],
                                    git_operation['revision'], cancel=True, agent_owner=git_owner)
                            denial = json.dumps({"error": "用户拒绝了该工具的执行，请换一种方式。",
                                                 "denied_by_user": True}, ensure_ascii=False)
                            if git_operation:
                                denial = json.dumps({**git_operations.agent_result(git_operation),
                                    'error': '用户拒绝了该 Git 操作，未执行写入。', 'denied_by_user': True}, ensure_ascii=False)
                        if approved is None:
                            # authorize() already checkpointed the new request
                            # and completed all abandoned tool-call receipts.
                            saver.save()
                            continue
                    if not approved:
                        outcome = {"result": denial, "error": "", "executed": False}
                        state.operations[tool_key] = outcome
                        saver.save()
                    else:
                        check_cancel()
                        validate_skills(state.skill_manifest)
                        authorized_policy = dict(policy_state)

                        def invoke():
                            try:
                                check_cancel()
                                try:
                                    validate_skills(state.skill_manifest)
                                except SkillsChanged as exc:
                                    # Only this local check proves nothing in the current action was dispatched.
                                    if not prior_unknown:
                                        exc.skill_request_not_dispatched = True
                                    raise
                                refresh_policy()
                                if policy_state != authorized_policy and needs_approval(name):
                                    return {"result": json.dumps({"error": "会话权限在操作开始前发生改变，旧授权已失效，请重新请求执行。",
                                            "blocked_by_live_policy": True}, ensure_ascii=False),
                                            "error": "", "executed": False}
                                if queued_steering():
                                    return {"result": json.dumps({"error": "用户补充等待应用，旧操作尚未执行。",
                                        "user_steering_waiting": True, "execution_unknown": bool(prior_unknown),
                                        "prior_operation": prior_unknown}, ensure_ascii=False), "error": "", "executed": False}
                                current_denial = goal_constraint(name, arguments)
                                if current_denial:
                                    if prior_unknown:
                                        current_denial.update(execution_unknown=True, prior_operation=prior_unknown)
                                    return {"result": json.dumps(current_denial, ensure_ascii=False), "error": "", "executed": False}
                                reason = tool_policy_error(name)
                                if reason:
                                    return {"result": json.dumps({"error": reason, "blocked_by_tool_policy": True}, ensure_ascii=False),
                                            "error": "", "executed": False}
                                if retry_grant and not consume_retry(state, call_signature(name, arguments), tool_key, saver.save):
                                    return {"result": json.dumps({"error": "原操作的单次重试许可已消耗或失效，未重发。",
                                        "execution_unknown": True, "not_replayed": True}, ensure_ascii=False),
                                        "error": "", "executed": False}
                                if change:
                                    result = project_changes.apply(change['id'], run_id=run_id)
                                    return {"result": json.dumps(result, ensure_ascii=False), "error": "", "executed": True}
                                if command:
                                    result = project_commands.run(command['id'], run_id=run_id or state.record_id)
                                    if (result.get('status') in ('interrupted', 'timed_out', 'output_limit')
                                            or result.get('status') in ('failed', 'cancelled') and result.get('started_at')):
                                        result.update(execution_unknown=True, automatic_retry=False,
                                            hint='命令可能已经产生文件或外部操作，不能按失败自动重发；请先核对已有结果。')
                                    return {'result': json.dumps(result, ensure_ascii=False), 'error': '', 'executed': True}
                                if git_operation:
                                    result = git_operations.decide(git_operation['project_id'], git_operation['id'],
                                        git_operation['revision'], agent_owner=git_owner)
                                    result = git_operations.agent_result(result)
                                    if result.get('status') == 'interrupted':
                                        result.update(execution_unknown=True, automatic_retry=False)
                                    return {'result': json.dumps(result, ensure_ascii=False), 'error': '', 'executed': True}
                                if service_operation:
                                    from ..runtime_context import bind_runtime_config
                                    if name in ("deepseek_research", "deepseek_file_read"):
                                        def native_events():
                                            with bind_runtime_config(service_binding):
                                                result = yield from service_tools.execute_deepseek_tool_events(db, name, arguments,
                                                    conversation_id=conversation_id, project_scope=state.workspace,
                                                    prepared=service_operation)
                                            return {"result": result, "error": "", "executed": True}
                                        return native_events()
                                    with bind_runtime_config(service_binding):
                                        result = service_tools.execute_deepseek_tool(db, name, arguments,
                                            conversation_id=conversation_id, project_scope=state.workspace,
                                            prepared=service_operation)
                                    return {"result": result, "error": "", "executed": True}
                                scope = {"conversation_id": conversation_id} if name in ("read_conversation_history", "retrieve_evidence", "compare_papers") else {}
                                if name == 'read_project_command_output':
                                    scope = {'project_scope': state.workspace, 'conversation_id': conversation_id}
                                if name == 'spawn_subagent':
                                    scope = {'project_scope': state.workspace, 'conversation_id': conversation_id}
                                if name in ('load_skill', 'list_skills'):
                                    scope = {'project_scope': state.workspace}
                                if name in ("list_attached_sources", "read_attached_source", "verify_attached_table"):
                                    scope = {"conversation_id": conversation_id}
                                if name in ("list_project_files", "read_project_file", "search_project_files", "git_project_status", "git_project_diff", "list_project_worktrees", "create_project_worktree",
                                            "review_project_worktree_removal", "remove_project_worktree", "review_project_worktree_archive",
                                            "archive_project_worktree", "restore_project_worktree", "reconcile_project_worktree"):
                                    scope = {"project_scope": state.workspace}
                                from ..runtime_context import bind_runtime_config
                                with bind_runtime_config({**(state.runtime_config or {}),
                                                          "writer_token": state.writer_token}):
                                    if name == "read_attached_source":
                                        from .readonly_cache import attached_read
                                        result = attached_read(db, arguments, binding=state.runtime_config or {},
                                            conversation_id=conversation_id, cache=state.readonly_cache,
                                            action=lambda: ops.execute_tool(db, name, arguments, scope_paper_id=paper_id, **scope))
                                    else:
                                        from .readonly_cache import verified_read
                                        result = verified_read(db, name, arguments, binding=state.runtime_config or {},
                                            conversation_id=conversation_id, project_scope=state.workspace,
                                            scope_paper_id=paper_id, cache=state.readonly_cache,
                                            action=lambda: ops.execute_tool(db, name, arguments, scope_paper_id=paper_id, **scope))
                                if name in ("create_project_worktree", "remove_project_worktree", "archive_project_worktree",
                                            "restore_project_worktree", "reconcile_project_worktree"):
                                    try:
                                        receipt = json.loads(result)
                                    except (ValueError, TypeError):
                                        receipt = None
                                    if isinstance(receipt, dict) and receipt.get("status") == "interrupted":
                                        receipt.update(execution_unknown=True, automatic_retry=False)
                                        result = json.dumps(receipt, ensure_ascii=False)
                                return {"result": result, "error": "", "executed": True}
                            except Exception as exc:
                                from ..resource_budget import ResourceBudgetExceeded
                                from ..request_resources import ResourceRequestCancelled
                                from .active_context import ContextCapacityExceeded
                                if isinstance(exc, (ResourceBudgetExceeded, ResourceRequestCancelled, ContextCapacityExceeded, SkillsChanged, MemoryScopeChanged)):
                                    raise
                                error = f"工具「{name}」执行失败：{type(exc).__name__}: {exc}"
                                # Schema validation and policy refusals happen
                                # before invoke(). An unexpected exception
                                # after entering a guarded adapter cannot prove
                                # that its file/remote effect did not happen.
                                not_dispatched = any(getattr(exc, flag, False) is True for flag in (
                                    "tool_request_not_dispatched", "service_request_not_dispatched", "request_not_dispatched"))
                                if change and isinstance(exc, HTTPException):
                                    # This adapter already reconciles actual
                                    # bytes before exposing a conflict. Reuse
                                    # that journal instead of inventing an
                                    # unknown write for an unchanged file.
                                    with project_changes.SessionLocal() as receipt_db:
                                        receipt = project_changes.get(receipt_db, change['id'])
                                        not_dispatched = receipt.status in ('proposed', 'conflict', 'rejected')
                                unknown = ops.tool_risk(name) != "safe" and not not_dispatched
                                return {"result": json.dumps({"error": error, "execution_unknown": unknown,
                                    "not_executed": not_dispatched, "automatic_retry": not unknown,
                                    "hint": "操作结果未确认，请先核对已有成果或外部系统；不能自动重发或视为成功。" if unknown else
                                        "请调整参数、换一种方式或跳过该工具。"}, ensure_ascii=False),
                                        "error": error, "executed": True}

                        if service_operation and name in ("deepseek_research", "deepseek_file_read"):
                            def forward_native_events():
                                from types import GeneratorType
                                action = invoke()
                                if not isinstance(action, GeneratorType):
                                    return action
                                with closing(action):
                                    while True:
                                        try:
                                            event = next(action)
                                        except StopIteration as completed:
                                            return completed.value
                                        check_cancel()
                                        yield log(event["kind"], {k: v for k, v in event.items() if k != "kind"})
                                        saver.save()
                            outcome = yield from saver.perform_events(tool_key, "tool", name, arguments,
                                forward_native_events, _skipped_tool())
                        else:
                            outcome = saver.perform(tool_key, "tool", name, arguments, invoke, _skipped_tool())
                outcome['tool_name'] = name
                if tool_key in state.operations:
                    state.operations[tool_key] = deepcopy(outcome)
                if name == 'git_project_write':
                    try:
                        git_result = json.loads(outcome['result'])
                        if git_result.get('operation_id'):
                            yield log('git_operation', {'text': f"Git 操作结果：{git_result['action']} · {git_result['status']}",
                                **{field: git_result[field] for field in ('operation_id', 'project_id', 'action', 'status')}})
                    except (ValueError, KeyError, TypeError):
                        pass
                if name == 'run_project_command' and outcome.get('executed'):
                    try:
                        command_result = json.loads(outcome['result'])
                        if command_result.get('command_id'):
                            yield log('project_command', {'text': f"项目命令结束：{command_result['summary']} · {command_result['status']}",
                                **{key: command_result[key] for key in
                                ('command_id', 'command', 'cwd', 'summary', 'timeout_seconds', 'attempt', 'status', 'exit_code')}})
                    except (ValueError, KeyError, TypeError):
                        pass
                if outcome.get("error"):
                    yield log("error", {"text": outcome["error"], "source": "tool",
                                        "operation_key": tool_key, "failure_id": f"failure/{tool_key}"})
                result = outcome["result"]
                if outcome.get("executed"):
                    ah = run_hook(f"hook/{key}/post", "PostToolUse", {
                        "tool_name": name, "arguments": arguments, "result_preview": result[:600],
                    }, matcher_value=name)
                    if ah["ran"]:
                        yield log("hook", {"text": f"PostToolUse 钩子执行 {ah['ran']} 条", **ah})
                if name == "spawn_subagent" and outcome.get("executed"):
                    sh = run_hook(f"hook/{key}/sub_stop", "SubagentStop", {
                        "task": arguments, "result_preview": result[:600],
                    })
                    if sh["ran"]:
                        yield log("hook", {"text": f"SubagentStop 钩子执行 {sh['ran']} 条", **sh})
                    try:
                        yield log("subagents", {"text": "子 agent 已完成", "subagents": json.loads(result).get("subagents", [])})
                    except (ValueError, AttributeError):
                        pass
                raw_result = result
                if (ops.tool_risk(name) != "safe" and completion_payload(outcome) is not None):
                    state.memory_side_effects[call_signature(name, arguments)] = {
                        "operation_key": tool_key, "user_input_count": len(state.applied_input_ids)}
                elif ops.tool_risk(name) != "safe":
                    try:
                        unconfirmed = json.loads(raw_result)
                    except (ValueError, TypeError):
                        unconfirmed = None
                    if isinstance(unconfirmed, dict) and unconfirmed.get("execution_unknown"):
                        state.memory_side_effects[call_signature(name, arguments)] = {
                            "operation_key": unconfirmed.get("prior_operation_key") or tool_key,
                            "user_input_count": len(state.applied_input_ids),
                            "execution_unknown": True}
                if name == 'load_skill':
                    try:
                        loaded_skill = json.loads(result)
                        if isinstance(loaded_skill, dict) and loaded_skill.get('identity') and 'content' in loaded_skill:
                            yield log('skill_loaded', {'skill': skill_manifest(loaded_skill), 'text': f"已加载技能 {loaded_skill['name']} · {loaded_skill['revision'][:12]}"})
                    except ValueError:
                        pass
                result, state.evidence_counter, evidence = ops._renumber_evidence(result, state.evidence_counter)
                state.evidence.extend(evidence)
                if name == "update_plan":
                    plan_payload = completion_payload(outcome)
                    if isinstance(plan_payload, dict) and isinstance(plan_payload.get("plan_steps"), list):
                        if state.goal_id and arguments.get("requirement_candidates") is not None:
                            from ..goal_ledger import propose_requirement_candidates, GoalError
                            try:
                                propose_requirement_candidates(db, state.goal_id, arguments["requirement_candidates"])
                            except GoalError as exc:
                                result = json.dumps({**plan_payload, "candidate_error": str(exc),
                                    "candidate_status": "rejected", "requirements_changed": False}, ensure_ascii=False)
                        state.todo = ops.plan_add_acceptance_requirements(
                            merge_model_plan(state.todo, plan_payload["plan_steps"]), question)
                        state.plan_source = "model"
                        state.plan_generation += 1
                        register_alternatives()
                        saver.save()
                        yield log("plan", {"text": "模型已根据当前请求更新计划", "steps": public_plan()})
                if name == "read_project_file":
                    read_page = completion_payload(outcome, allow_partial=True)
                    if isinstance(read_page, dict):
                        ops.plan_record_file_read(state.todo, read_page.get("path", ""), read_page)
                if name == "read_attached_source":
                    source_page = completion_payload(outcome, allow_partial=True)
                    if isinstance(source_page, dict):
                        from ..source_coverage import record_source_read
                        record_source_read(state, source_page)
                usable_result = completion_payload(outcome)
                research_delivery = None
                if name in {"start_research_child", "resume_research_child", "read_research_child"}:
                    from ..research_children import verified_delivery
                    if isinstance(usable_result, dict):
                        research_delivery = verified_delivery(db, parent_run_id=run_id,
                            conversation_id=conversation_id, child_run_id=usable_result.get("run_id"),
                            payload=usable_result)
                    if research_delivery is None:
                        usable_result = None
                if name in ("edit_project_file", "git_project_write") and (
                        not isinstance(usable_result, dict) or usable_result.get("status") != "applied"):
                    usable_result = None
                if name == "run_project_command" and (
                        not isinstance(usable_result, dict) or usable_result.get("status") != "succeeded"
                        or usable_result.get("exit_code") != 0):
                    usable_result = None
                if usable_result is not None and name != "update_plan":
                    if state.goal_id:
                        from ..goal_ledger import record_contract_tool_result
                        record_contract_tool_result(db, state.goal_id, name, arguments, usable_result, tool_key)
                    state.incomplete_finalizations = 0
                    phase = ops._tool_phase(name)
                    proof = {"kind": "tool_result", "tool_name": name,
                             "arguments": deepcopy(arguments),
                             "operation_key": tool_key, "status": "completed"}
                    if research_delivery is not None:
                        proof["research_delivery"] = {key: value for key, value in research_delivery.items() if key != "evidence"}
                    if isinstance(usable_result, dict):
                        if usable_result.get("path"):
                            proof["path"] = usable_result["path"]
                        if usable_result.get("exit_code") is not None:
                            proof["exit_code"] = usable_result["exit_code"]
                    prove_tool_outcome(name, arguments, outcome, tool_key, proof)
                    ops.plan_complete_one(state.todo, phase, proof)
                    if research_delivery is not None and name != "start_research_child":
                        from ..research_children import matches_original_start
                        # A resume/read may deliver the exact child started by
                        # an earlier plan step. Bind it through the immutable
                        # request/input hash rather than broad phase equality.
                        for step in state.todo:
                            if step.get("tool_name") != "start_research_child" or step.get("status") == "done":
                                continue
                            original_arguments = step.get("tool_arguments")
                            if not matches_original_start(db, parent_run_id=run_id,
                                    conversation_id=conversation_id, child_run_id=research_delivery["child_run_id"],
                                    arguments=original_arguments, payload=usable_result):
                                continue
                            original_proof = {**deepcopy(proof), "tool_name": "start_research_child",
                                "arguments": deepcopy(original_arguments), "actual_tool_name": name,
                                "actual_arguments": deepcopy(arguments)}
                            ops.plan_complete_one([step], step.get("phase", phase), original_proof)
                            prove_tool_outcome("start_research_child", original_arguments, outcome, tool_key, original_proof)
                    if name == "edit_project_file" and isinstance(usable_result, dict) \
                            and usable_result.get("status") == "applied":
                        ops.plan_complete_requirement(state.todo, "project_file", {
                            "kind": "project_file", "path": usable_result.get("path"),
                            "change_id": usable_result.get("id"), "status": "applied"})
                    if (name == "run_project_command" and isinstance(usable_result, dict)
                            and usable_result.get("status") == "succeeded"
                            and usable_result.get("exit_code") == 0):
                        ops.plan_complete_requirement(state.todo, "successful_command", {
                            "kind": "command", "command_id": usable_result.get("command_id"),
                            "exit_code": usable_result.get("exit_code"), "status": "succeeded"})
                elif name != "update_plan":
                    prove_tool_outcome(name, arguments, outcome, tool_key)
                from .active_context import progress_fingerprint
                operation_fingerprint = progress_fingerprint(name, arguments, raw_result)
                if operation_fingerprint:
                    state.segment_result_fingerprints.append(operation_fingerprint)
                state.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                state.tool_index += 1
                saver.save()
                if evidence:
                    # Do not make a citation clickable until its quote and
                    # locator have passed the same immutable-source check used
                    # at delivery.
                    validate_source_evidence(db, conversation_id, evidence)
                    yield log("evidence", {"evidence": evidence})
                yield log("tool_result", {"name": name, "operation_key": tool_key, "preview": result[:600], **_external_papers(name, result)})
            if state.phase == "tools":
                state.calls = []
                state.tool_index = 0
                state.step += 1
                state.phase = "model"
                saver.save()

        check_cancel()
        if state.phase == "summarize":
            # The final answer has its own read-only boundary. Retrying this
            # phase after cancellation never replays already completed tools.
            saver.save()
            yield log("status", {"text": "已达到工具调用轮次上限，正在汇总已取得的结果",
                                 "stop_reason": "step_limit"})
            yield log("step", {"text": "汇总已取得的结果（不再调用工具）", "phase": "summary"})
            response = None
            try:
                with closing(ops._stream_chat_with_retry(
                    llm, request_messages([*state.messages, {"role": "user", "content": LIMIT_SUMMARY_PROMPT}]),
                    tools=None, attempts=1,
                )) as source:
                    for item in source:
                        check_cancel()
                        if item[0] == "final":
                            response = item[1]
                        elif item[0] == "delta" and item[1] == "thinking":
                            yield log("thinking_delta", {"text": item[2]})
                        # Buffer answer fragments until a complete response is
                        # known to contain text rather than another tool request.
            except (AgentCancelled, MemoryCancelled):
                raise
            except (ResourceBudgetExceeded, ContextCapacityExceeded, ResourceRequestCancelled, SkillsChanged, MemoryScopeChanged, MemoryStageRebuilt):
                raise
            except Exception:
                yield log("error", {"text": "最终汇总调用失败，已保留已完成步骤和工具结果"})
            check_cancel()
            if response is not None:
                validate_skills(state.skill_manifest)
                for key in state.usage:
                    state.usage[key] += int((response.usage or {}).get(key) or 0)
                if response.reasoning_content:
                    yield log("thinking", {"text": response.reasoning_content})
            state.summary_failed = bool(
                response is None or response.tool_calls or not (response.content or "").strip()
            )
            state.answer = "" if state.summary_failed else response.content
            state.provider_context = {} if state.summary_failed else response.persisted_context()
            state.phase = "finalize"
            saver.save()

        check_cancel()
        if state.phase == "finalize":
            validate_skills(state.skill_manifest)
            if ensure_memory_current(db, state, run_id, images=images):
                if runtime_config is not None:
                    runtime_config["memory_fingerprint"] = state.memory_fingerprint
                saver.save()
                raise MemoryStageRebuilt()
            if (yield from apply_steering(abandon_calls=True)):
                return state.snapshot()
            validate_source_evidence(db, conversation_id, state.evidence)
            ids = [e["id"] for e in state.evidence]
            if ids:
                # A resumed finalization may have no tool events to replay.
                # Publish only validated, already-scoped public evidence.
                yield log("evidence", {"evidence": state.evidence})
                yield log("review_progress", {"phase": "finalize", "status": "running",
                    "text": "正文已生成，正在核查引用", "completed": 0, "total": 0})
            gate = {"passed": False, "status": "not_applicable", "unsupported": [], "invalid_markers": [], "total_claims": 0, "marked_claims": 0}
            audit = {"passed": False, "status": "not_applicable", "unsupported": [], "mode": "none", "verified": False}
            online = ((state.runtime_config or {}).get("mode", ops.settings.llm_mode) in ("api", "litellm"))
            attribution = None
            attribution_missing = False
            from .source_attribution import quality_repair_context_key
            quality_key = quality_repair_context_key(question, state.messages,
                input_ids=state.applied_input_ids, excluded_indices=state.source_attribution_feedback_indices)
            quality_record = state.answer_quality_repairs.get(quality_key, {})
            quality_issues = deepcopy(quality_record.get("unresolved", []))
            quality_missing = bool(quality_issues)
            post_rewrite_quality = None
            if (not ids or quality_missing) and state.answer.strip() and not state.summary_failed:
                from .source_attribution import (SOURCE_REVIEW_CONTEXT_PROTOCOL, review_contract_id,
                                                 review_enabled, review_source_attribution,
                                                 without_unsupported_claims, repair_context_key, execution_receipt,
                                                 configuration_receipt, quality_repair_context_key)
                if review_enabled(llm):
                    from .model_review import model_review_identity
                    actual_execution_receipt = execution_receipt(state)
                    actual_configuration_receipt = configuration_receipt(state)
                    review_key = hashlib.sha256(json.dumps({"protocol": SOURCE_REVIEW_CONTEXT_PROTOCOL, "question": question,
                        "answer": state.answer, "messages": state.messages,
                        "excluded_indices": state.source_attribution_feedback_indices,
                        "runtime_receipt": actual_execution_receipt,
                        "runtime_configuration": actual_configuration_receipt,
                        "review_model_identity": model_review_identity(llm)},
                        ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
                    from .source_review_journal import cached_source_review
                    from .paid_review_family import family_identity
                    attribution = cached_source_review(state.source_attribution_reviews, review_key,
                        contract_id=review_contract_id(),
                        review=lambda: review_source_attribution(question, state.answer, state.messages,
                            llm, on_usage=record_model_usage,
                            excluded_indices=state.source_attribution_feedback_indices,
                            runtime_receipt=actual_execution_receipt,
                            runtime_configuration=actual_configuration_receipt),
                        persist=saver.save, before_review=check_cancel, llm=llm,
                        allow_dispatch=not state.budget_exhausted,
                        dispatch_family=family_identity("source_attribution", question=question,
                            messages=state.messages, input_ids=state.applied_input_ids,
                            excluded_indices=state.source_attribution_feedback_indices))
                    if not ids:
                        audit = attribution
                    yield log("audit", {"text": "材料归属与实际提供范围核验", "audit": attribution})
                    quality_key = quality_repair_context_key(question, state.messages,
                        input_ids=state.applied_input_ids, excluded_indices=state.source_attribution_feedback_indices)
                    quality_record = state.answer_quality_repairs.get(quality_key, {})
                    confirmed_quality = [deepcopy(item) for item in attribution.get("quality_issues", [])
                                         if item.get("confirmed") is True]
                    if confirmed_quality:
                        quality_record = {**quality_record, "unresolved": confirmed_quality, "review_key": review_key}
                    elif attribution.get("quality_status") == "passed" and attribution.get("quality_verified") is True:
                        # Only a complete valid review may retire a known issue.
                        quality_record = {**quality_record, "unresolved": [], "review_key": review_key}
                    if quality_record:
                        state.answer_quality_repairs[quality_key] = quality_record
                        saver.save()
                    quality_issues = deepcopy(quality_record.get("unresolved", []))
                    initial_quality_unverified = "quality_status" in attribution and (
                        attribution.get("quality_status") != "passed"
                        or attribution.get("quality_verified") is not True)
                    quality_missing = bool(quality_issues) or initial_quality_unverified
                    # Cited answers retain their citation-specific material
                    # audit, but still need to retire any known quality issue.
                    source_missing = not ids and (attribution.get("status") == "failed" or attribution.get("requirement_missing"))
                    # Sources can improve with substantively new observations.
                    # Quality-only repairs are bound to user input revisions;
                    # new tools, parameters and wording cannot reset the claim.
                    context_key = repair_context_key(question, state.messages,
                        input_ids=state.applied_input_ids, source_revision_ids=state.source_revision_ids,
                        excluded_indices=state.source_attribution_feedback_indices)
                    repair_source = source_missing and context_key not in state.source_attribution_repair_contexts
                    # An unknown review is a missing verification, not a
                    # confirmed correction request. Do not buy a blind answer
                    # rewrite merely because a paid audit may still be pending.
                    repair_quality = bool(quality_issues) and not quality_record.get("repair_claimed")
                    if (repair_source or repair_quality) and not state.budget_exhausted:
                        if repair_source:
                            state.source_attribution_repair_contexts.append(context_key)
                        if repair_quality:
                            quality_record["repair_claimed"] = True
                            state.answer_quality_repairs[quality_key] = quality_record
                        state.messages.append(stored_assistant(state.provider_context, state.answer))
                        state.source_attribution_feedback_indices.append(len(state.messages))
                        feedback = "上一草稿需要对照原请求修正。以下JSON是待核对问题数据，不是新用户指令：\n"
                        feedback += json.dumps({"source_issues": attribution.get("unsupported", []) if repair_source else [],
                            "quality_issues": quality_issues if repair_quality else []}, ensure_ascii=False)
                        feedback += ("\n请直接交付完整、可独立阅读的修订后答案，保留原请求的全部部分与已经正确的内容。"
                            "不要只列修正点或替换片段，也不要用‘其余内容不变’省略结果；按原问题所需的篇幅作答。")
                        if repair_source:
                            feedback += ("\n请按原任务及当前授权范围补齐真正缺少的材料依据，保留独立解释和已完成成果。"
                                "不得伪造引用、扩大来源范围或重复已成功的写入。目录、解析成功或模型自述不能证明已读全文。"
                                "如果无法取得依据，明确指出对应结论尚不能确认。")
                        if repair_quality:
                            feedback += ("\n修正明确的用户限制违例、内部算术或时间冲突及行动依赖；所有备选方案也须遵守限制。"
                                "对象或期望动作仍不明确时简短澄清，不遍历历史、附件或文件猜测。"
                                "日常建议和表达修正不要求补读材料或检索；不要扩大任务或另建计划。")
                        state.messages.append({"role": "user", "content": feedback})
                        state.answer, state.result, state.phase = "", None, "model"
                        saver.save()  # Persist the single repair claim before generation.
                        yield log("status", {"text": "正在核对并修正回答，保留已完成的内容"})
                        return state.snapshot()
                    attribution_missing = not ids and (attribution.get("status") in {"failed", "unverified"}
                        or attribution.get("requirement_missing")
                        or attribution.get("error_code") == "dispatch_outcome_unknown")
                    if not ids and attribution.get("status") == "failed":
                        state.answer = without_unsupported_claims(state.answer, attribution)
            if ids and not state.summary_failed:
                if not state.budget_exhausted:
                    yield from todo("audit")
                gate = ops.citation_gate(state.answer, ids)
                if online and not state.budget_exhausted and not gate["passed"]:
                    from .audit import review_citation_scope
                    check_cancel()
                    yield log("review_progress", {"phase": "citation_review", "stage": "scope_review",
                        "status": "running", "completed": 0, "total": gate.get("total_claims", 0)})
                    gate = review_citation_scope(question, state.answer, state.evidence, gate, llm,
                                                 on_usage=record_model_usage, messages=state.messages,
                                                 excluded_indices=state.source_attribution_feedback_indices,
                                                 review_journal=state.source_attribution_reviews, input_ids=state.applied_input_ids,
                                                 persist=saver.save, before_review=check_cancel)
                    check_cancel()
                    yield log("review_progress", {"phase": "citation_review", "stage": "scope_review",
                        "status": "completed" if (gate.get("scoped_review") or {}).get("verified") else "unverified",
                        "completed": gate.get("total_claims", 0), "total": gate.get("total_claims", 0)})
                yield log("critic", {"text": "引用闸门校验", "gate": gate})
                # Review the already cited claims before correction even when
                # a different sentence still lacks a marker. Otherwise the
                # only rewrite receives no feedback about false source claims.
                yield log("review_progress", {"phase": "citation_review", "stage": "semantic_audit",
                    "status": "running", "completed": 0, "total": gate.get("marked_claims", 0)})
                audit = audit_with_progress(db, state.answer, state.evidence, llm=llm,
                    messages=state.messages, excluded_indices=state.source_attribution_feedback_indices,
                    conversation_id=conversation_id, question=question, input_ids=state.applied_input_ids,
                    audit_journal=state.citation_audit_batches,
                    persist=saver.save, before_batch=check_cancel, on_usage=record_model_usage)
                yield log("review_progress", {"phase": "citation_review", "stage": "semantic_audit",
                    "status": audit.get("status", "unverified"),
                    "completed": audit.get("factual_claims", 0), "total": audit.get("factual_claims", 0),
                    "batch_count": audit.get("batch_count", 0)})
                yield log("audit", {"text": f"引用语义审计（{audit['mode']}）", "audit": audit})
                audit_hard_fail = _known_citation_failure(audit)
                marker_dispatch_unknown = gate.get("error_code") == "dispatch_outcome_unknown"
                if (online and not state.budget_exhausted and not gate["passed"] and _scope_repair_needed(gate) and not audit_hard_fail
                        and not marker_dispatch_unknown):
                    annotation = _citation_marker_annotation(state, gate, llm,
                        persist=saver.save, before_review=check_cancel, on_usage=record_model_usage, db=db)
                    marker_dispatch_unknown = annotation.get("error_code") == "dispatch_outcome_unknown"
                    if annotation.get("changed"):
                        gate = ops.citation_gate(state.answer, ids)
                        if not gate["passed"]:
                            from .audit import review_citation_scope
                            gate = review_citation_scope(question, state.answer, state.evidence, gate, llm,
                                on_usage=record_model_usage, messages=state.messages,
                                excluded_indices=state.source_attribution_feedback_indices,
                                review_journal=state.source_attribution_reviews, input_ids=state.applied_input_ids,
                                persist=saver.save, before_review=check_cancel)
                            marker_dispatch_unknown = gate.get("error_code") == "dispatch_outcome_unknown"
                        audit = audit_with_progress(db, state.answer, state.evidence, llm=llm,
                            messages=state.messages, excluded_indices=state.source_attribution_feedback_indices,
                            conversation_id=conversation_id, question=question, input_ids=state.applied_input_ids,
                            audit_journal=state.citation_audit_batches,
                            persist=saver.save, before_batch=check_cancel, on_usage=record_model_usage)
                        audit_hard_fail = _known_citation_failure(audit)
                        yield log("audit", {"text": "引用标记补齐后重新核验原文", "audit": audit})
                if (online and not state.budget_exhausted and not marker_dispatch_unknown
                        and (_scope_repair_needed(gate) or audit_hard_fail)):
                    check_cancel()
                    yield log("retry", {"text": "存在无引用或证据不支持的论断，要求重写"})
                    from .citation_context_preservation import build_preservation_feedback
                    preservation_feedback = build_preservation_feedback(db, state.answer, state.evidence,
                        audit, conversation_id=conversation_id)
                    retry_messages = _citation_repair_messages(state.messages, state.answer, gate, audit, ids,
                        state.provider_context, evidence=state.evidence, preservation_feedback=preservation_feedback)
                    from .citation_answer_rewrite import apply_citation_answer_rewrite
                    from .source_attribution import repair_context_key
                    transformed_messages = request_messages(retry_messages)
                    revision = apply_citation_answer_rewrite(state, llm=llm,
                        request_messages=transformed_messages,
                        input_identity={"observations": repair_context_key(question, state.messages,
                            input_ids=state.applied_input_ids, source_revision_ids=state.source_revision_ids,
                            excluded_indices=state.source_attribution_feedback_indices),
                            "memory_fingerprint": state.memory_fingerprint},
                        call=lambda: ops._chat_with_retry(llm, transformed_messages, tools=None, attempts=1),
                        persist=saver.save, before_review=check_cancel, on_usage=record_model_usage)
                    if revision.get("error_code"):
                        yield log("status", {"text": "修订结果尚未确认，已保留原稿与核验缺项，未自动重复提交",
                            "error_code": revision["error_code"]})
                    if revision.get("status") in {"applied", "already_applied"} and not revision.get("error_code"):
                        from .citation_table_context import apply_table_context
                        context_annotation = apply_table_context(state, db=db, input_key=quality_key,
                            persist=saver.save, before_review=check_cancel)
                        if context_annotation.get("changed"):
                            yield log("status", {"text": "已补齐已读小表的上下文引用，正在重新核对原文",
                                "annotation_only": True, "semantic_verification": "not_performed"})
                    gate = ops.citation_gate(state.answer, ids)
                    if not gate["passed"]:
                        from .audit import review_citation_scope
                        check_cancel()
                        gate = review_citation_scope(question, state.answer, state.evidence, gate, llm,
                                                     on_usage=record_model_usage, messages=state.messages,
                                                     excluded_indices=state.source_attribution_feedback_indices,
                                                     review_journal=state.source_attribution_reviews, input_ids=state.applied_input_ids,
                                                     persist=saver.save, before_review=check_cancel)
                        check_cancel()
                    # Exact annotations precede the purchased final audit.
                    # Auditing the intermediate rewrite and immediately
                    # changing its hash used to purchase that full audit twice.
                    # No old verdict certifies the new prose: every final
                    # claim still receives the ordinary full semantic review.
                    annotated_before_audit = False
                    if (revision.get("status") in {"applied", "already_applied"}
                            and not revision.get("error_code") and not gate["passed"]
                            and gate.get("error_code") != "dispatch_outcome_unknown"
                            and _postrewrite_annotation_admitted(state)):
                        annotation = _citation_marker_annotation(state, gate, llm,
                            persist=saver.save, before_review=check_cancel, on_usage=record_model_usage, db=db)
                        if annotation.get("changed"):
                            annotated_before_audit = True
                            gate = ops.citation_gate(state.answer, ids)
                            if not gate["passed"]:
                                gate = review_citation_scope(question, state.answer, state.evidence, gate, llm,
                                    on_usage=record_model_usage, messages=state.messages,
                                    excluded_indices=state.source_attribution_feedback_indices,
                                    review_journal=state.source_attribution_reviews, input_ids=state.applied_input_ids,
                                    persist=saver.save, before_review=check_cancel)
                    audit = audit_with_progress(db, state.answer, state.evidence, llm=llm,
                        messages=state.messages, excluded_indices=state.source_attribution_feedback_indices,
                        conversation_id=conversation_id, question=question, input_ids=state.applied_input_ids,
                        audit_journal=state.citation_audit_batches,
                        persist=saver.save, before_batch=check_cancel, on_usage=record_model_usage)
                    yield log("audit", {"text": "修订稿补标后重新核验原文" if annotated_before_audit
                        else f"重写后语义审计（{audit['mode']}）", "audit": audit})
                # The purchased rewrite may retain an unnecessary rejected
                # standalone line or exact complete ordinary sentence;
                # never regenerate the rest or infer approval from deletion.
                from .citation_answer_rewrite import has_applied_citation_rewrite
                if (online and not state.budget_exhausted and _known_citation_failure(audit)
                        and has_applied_citation_rewrite(state.source_attribution_reviews)):
                    from .citation_local_cleanup import apply_rejected_line_cleanup
                    cleanup = apply_rejected_line_cleanup(state, audit, input_key=quality_key,
                        persist=saver.save, before_review=check_cancel)
                    if cleanup.get("reason") == "no_safe_exact_deletion":
                        from .citation_local_cleanup import apply_rejected_sentence_cleanup
                        cleanup = apply_rejected_sentence_cleanup(state, audit, input_key=quality_key,
                            persist=saver.save, before_review=check_cancel)
                    if cleanup.get("changed"):
                        yield log("status", {"text": "已移除无法核实的独立句子，正在重新核对保留结果和原任务要求"})
                        gate = ops.citation_gate(state.answer, ids)
                        if not gate["passed"]:
                            from .audit import review_citation_scope
                            gate = review_citation_scope(question, state.answer, state.evidence, gate, llm,
                                on_usage=record_model_usage, messages=state.messages,
                                excluded_indices=state.source_attribution_feedback_indices,
                                review_journal=state.source_attribution_reviews, input_ids=state.applied_input_ids,
                                persist=saver.save, before_review=check_cancel)
                        audit = audit_with_progress(db, state.answer, state.evidence, llm=llm,
                            messages=state.messages, excluded_indices=state.source_attribution_feedback_indices,
                            conversation_id=conversation_id, question=question, input_ids=state.applied_input_ids,
                            audit_journal=state.citation_audit_batches,
                            persist=saver.save, before_batch=check_cancel, on_usage=record_model_usage)
                        yield log("audit", {"text": "局部修正后重新核验原文", "audit": audit})
            if not ids:
                from ..rag.citation import prose_for_citations
                citation_prose = prose_for_citations(state.answer)
                if "[^e" in citation_prose:
                    gate = ops.citation_gate(citation_prose, [])
            # 溯源比例判定：结构感知闸门已剔除标题/表格/脚注等非论断内容。
            # 仅在引用了不存在的证据、实质论断覆盖率过低、或语义审计明确否决时拒答。
            ratio = float(gate.get("grounded_ratio", 1.0))
            total_claims = int(gate.get("total_claims", 0))
            has_invalid = bool(gate.get("invalid_markers"))
            scope_review = gate.get("scoped_review") or {}
            confirmed_scope_missing = _confirmed_scope_missing(gate)
            confirmed_missing_citations = bool(confirmed_scope_missing)
            scope_dispatch_unknown = gate.get("error_code") == "dispatch_outcome_unknown"
            substantially_grounded = not scope_dispatch_unknown and not has_invalid and not confirmed_missing_citations and (
                total_claims == 0
                or (gate.get("marked_claims", 0) > 0 and ratio >= GROUNDED_RATIO_MIN)
            )
            # Any confirmed source error remains blocking after correction;
            # unavailable or incomplete reviews stay unknown, never false.
            audit_blocked = bool(online and _known_citation_failure(audit))
            refusal = 0
            if state.summary_failed:
                state.answer = LIMIT_SUMMARY_FAILED
                refusal = 1
                yield log("refuse", {"text": "最终汇总未返回有效答案，任务尚未全部完成"})
            elif ids and (has_invalid or not substantially_grounded or audit_blocked) and not _can_preserve_citation_draft(gate, audit):
                refusal_diagnostic = _citation_refusal_diagnostic(gate, audit)
                state.answer = refusal_diagnostic["answer"]
                refusal = 1
                yield log("refuse", {
                    "text": refusal_diagnostic["answer"],
                    "reason_code": refusal_diagnostic["reason_code"],
                    "reason_codes": refusal_diagnostic["reason_codes"],
                    "grounded_ratio": ratio,
                    "total_claims": total_claims,
                    "has_invalid": has_invalid,
                    "confirmed_missing_citations": confirmed_missing_citations,
                    "confirmed_scope_missing_count": len(confirmed_scope_missing),
                    "audit_blocked": audit_blocked,
                })
            elif not ids and has_invalid:
                state.answer = "回答包含没有对应原文证据的引用，无法可靠交付。请提供相关资料或调整检索范围后再试。"
                refusal = 1
                gate["status"] = "insufficient_evidence"
                yield log("refuse", {"text": "引用没有对应的原文证据"})
            elif not state.answer.strip():
                state.answer = "模型没有返回有效回答。请重试，或检查当前模型的接口配置。"
                refusal = 1
                yield log("refuse", {"text": "无有效答案，触发兜底"})
            citation_review_unresolved = bool(ids and (refusal or audit_blocked or gate.get("passed") is not True
                or audit.get("status") == "unverified" or audit.get("verified") is False
                or gate.get("scope_review_failure")))
            if citation_review_unresolved and not refusal:
                yield log("citation_review", {"status": "needs_review", "answer_preserved": True,
                    "text": "正文已保留，个别引用仍需核对；具体问题列在回答下方，未将待核验项视为已通过",
                    "known_failed_count": audit.get("known_failed_count", 0),
                    "unknown_count": audit.get("unknown_count", 0),
                    "confirmed_scope_missing_count": len(confirmed_scope_missing)})
            from .citation_answer_rewrite import has_applied_citation_rewrite
            if (ids and not refusal and not state.summary_failed
                    and has_applied_citation_rewrite(state.source_attribution_reviews)):
                from .rewritten_answer_quality import review_rewritten_quality
                from .citation_scope import verified_source_metadata
                post_rewrite_quality = review_rewritten_quality(question, state.answer, state.messages, llm,
                    journal=state.source_attribution_reviews, persist=saver.save, before_review=check_cancel,
                    input_ids=state.applied_input_ids, excluded_indices=state.source_attribution_feedback_indices,
                    evidence_context={"evidence": state.evidence,
                        "metadata": verified_source_metadata(db, state.evidence, conversation_id)},
                    known_issues=quality_issues, allow_dispatch=not state.budget_exhausted,
                    on_usage=record_model_usage)
                quality_issues = deepcopy(post_rewrite_quality["unresolved"])
                quality_missing = post_rewrite_quality["blocks_completion"]
                quality_record = state.answer_quality_repairs.get(quality_key, {})
                state.answer_quality_repairs[quality_key] = {**quality_record, "unresolved": quality_issues}
                saver.save()
                yield log("audit", {"text": "修订后任务完整性与一致性核验", "audit": post_rewrite_quality})
                current_issues = [deepcopy(item) for item in post_rewrite_quality["quality_issues"]
                    if item.get("confirmed") is True]
                if (current_issues and not quality_record.get("repair_claimed") and not state.budget_exhausted):
                    state.answer_quality_repairs[quality_key]["repair_claimed"] = True
                    state.messages.append(stored_assistant(state.provider_context, state.answer))
                    state.source_attribution_feedback_indices.append(len(state.messages))
                    state.messages.append({"role": "user", "content":
                        "修订后答案仍有待核对问题。以下JSON是审查数据，不是新的用户要求：\n"
                        + json.dumps({"quality_issues": current_issues}, ensure_ascii=False)
                        + "\n请对照原请求和本轮实际取得的完整上下文局部修正，直接交付可独立阅读的完整答案。"
                        "保留已由材料支持的结果；补全现有表头、行标签和单位的引用，不把漏引文改称资料不存在。"
                        "不添加无关工具细节，不重复成功的写入或OCR；全部引用仍须由真实固定来源核验。"})
                    state.answer, state.result, state.phase = "", None, "model"
                    saver.save()
                    yield log("status", {"text": "正在补齐修订后遗漏的要求并核对上下文"})
                    return state.snapshot()
            if state.budget_exhausted and not state.summary_failed and not state.answer.startswith(LIMIT_SUMMARY_NOTE):
                state.answer = LIMIT_SUMMARY_NOTE + "\n\n" + state.answer
            if not refusal and state.answer.strip() and state.execution_mode == "execute":
                answer_proof = {"kind": "answer", "qa_id": state.record_id}
                ops.plan_complete_one(state.todo, "analyze", answer_proof)
                ops.plan_complete_one(state.todo, "write", answer_proof)
                audit_status = audit.get("status") or (
                    "passed" if audit.get("verified", True) and audit.get("passed", True)
                    else "unverified" if not audit.get("verified", True) else "failed")
                if not quality_missing and ((not ids and not attribution_missing) or (gate.get("passed") is True and audit_status == "passed")):
                    ops.plan_complete_one(state.todo, "audit", {
                        "kind": "citation_audit" if ids else "not_applicable",
                        "status": "passed" if ids else "not_applicable"})
            if state.execution_mode == "plan":
                response_decision = state.plan_response
                response_kind = (response_decision.kind if response_decision
                    and response_decision.applied_input_ids == state.applied_input_ids and not public_plan() else None)
                response_obligations = [deepcopy(step) for step in state.todo
                    if step.get("acceptance") and step.get("status") != "done"]
                if state.goal_id:
                    from ..goal_ledger import snapshot_goal
                    response_obligations.extend(snapshot_goal(db, state.goal_id).get("incomplete_requirements", []))
                response_obligations.extend({"title": f"工具未完成：{failure['tool_name']}",
                    "phase": failure["phase"], "status": "failed", "failure_id": failure["id"]}
                    for failure in state.tool_failures if failure.get("status") == "open")
                response_ready = bool(response_kind) and not refusal and not state.budget_exhausted and not state.summary_failed
                if response_ready and response_kind == "direct_response" and not response_obligations:
                    plan_result = {"completed": True, "incomplete_steps": []}
                    task_outcome = "completed"
                elif response_ready and response_kind == "clarification":
                    plan_result = {"completed": False, "incomplete_steps": [*response_obligations, {
                        "title": "等待用户补充或澄清后继续", "phase": "analyze", "status": "pending",
                        "reason": "needs_clarification",
                    }]}
                    task_outcome = "partial"
                else:
                    response_kind = None
                    ready_plan = bool(public_plan()) and not refusal and not state.budget_exhausted and not state.summary_failed
                    plan_result = {"completed": False, "incomplete_steps": [] if ready_plan else [*response_obligations, {
                        "title": "尚未形成可供确认执行的结构化计划", "phase": "analyze", "status": "pending",
                        "reason": "plan_not_created",
                    }]}
                    task_outcome = "planned" if ready_plan else "blocked" if refusal else "partial"
                    if not ready_plan and not refusal:
                        state.answer += "\n\n本轮尚未形成可供确认执行的结构化计划，规划未完成；请补充任务要求或重新生成计划。"
            else:
                plan_result = ops.plan_finalize(state.todo, {
                    "kind": "delivery", "qa_id": state.record_id,
                    "refusal": bool(refusal), "budget_exhausted": state.budget_exhausted}) \
                    if not refusal and not state.budget_exhausted and not state.summary_failed else {
                        "completed": False,
                        "incomplete_steps": [{"title": step.get("title", ""),
                                              "phase": step.get("phase", "analyze"),
                                              "status": step.get("status", "pending")}
                                             for step in state.todo if step.get("status") != "done"],
                    }
                task_outcome = "completed" if plan_result["completed"] else "blocked" if refusal else "partial"
            # Serialize the last inbox check with reception's write transaction.
            # No network/model request or observer yield follows this fence
            # before QA + delivery checkpoint + completed state commit.
            if steering_enabled:
                if state.attempt_id:
                    run_lifecycle.assert_writer(db, run_id, state.attempt_id, state.writer_token, lock=True)
                else:
                    from sqlalchemy import update
                    owner = db.get(ForegroundRun, run_id)
                    if owner is not None:
                        db.execute(update(ForegroundRun).where(ForegroundRun.id == run_id)
                                   .values(revision=ForegroundRun.revision))
                    else:
                        db.execute(update(TaskRecord).where(TaskRecord.id == run_id)
                                   .values(updated_at=TaskRecord.updated_at))
                if (yield from apply_steering(abandon_calls=True)):
                    return state.snapshot()
            goal_snapshot = None
            if state.goal_id and state.execution_mode == "execute":
                from ..goal_ledger import sync_plan_requirements, finalize_goal, snapshot_goal, evaluate_input_contracts
                sync_plan_requirements(db, state.goal_id, state.todo)
                evaluate_input_contracts(db, state.goal_id, state.answer, workspace=state.workspace)
                open_tool_failures = any(item.get("status") == "open" for item in state.tool_failures)
                goal_snapshot = (snapshot_goal(db, state.goal_id)
                    if refusal or citation_review_unresolved or attribution_missing or quality_missing or open_tool_failures
                    else finalize_goal(db, state.goal_id))
                if goal_snapshot.get("requirements"):
                    missing = goal_snapshot.get("incomplete_requirements", [])
                    if missing:
                        plan_result = {"completed": False, "incomplete_steps": deepcopy(missing)}
                        task_outcome = "blocked" if refusal else "partial"
            if state.execution_mode == "execute":
                resolve_alternatives()
                failed = [{"title": f"工具未完成：{failure['tool_name']}", "phase": failure["phase"],
                           "status": "failed", "failure_id": failure["id"], "tool_name": failure["tool_name"]}
                          for failure in state.tool_failures if failure.get("status") == "open"]
                if failed:
                    plan_result = {"completed": False, "incomplete_steps": [*plan_result["incomplete_steps"], *failed]}
                    task_outcome = "blocked" if refusal else "partial"
            if attribution_missing:
                plan_result = {"completed": False, "incomplete_steps": [*plan_result["incomplete_steps"], {
                    "title": "补齐材料结论或执行情况的实际依据", "phase": "audit", "status": "pending",
                    "reason": "source_attribution_unverified",
                }]}
                task_outcome = "blocked" if refusal else "partial"
            if quality_missing:
                plan_result = {"completed": False, "incomplete_steps": [*plan_result["incomplete_steps"], {
                    "title": "修正回答中尚未解决的约束或一致性问题" if quality_issues else "核对修订后答案的任务完整性与一致性",
                    "phase": "audit", "status": "pending",
                    "reason": "answer_quality_unresolved", "issues": deepcopy(quality_issues),
                }]}
                task_outcome = "blocked" if refusal else "partial"
            if citation_review_unresolved:
                plan_result = {"completed": False, "incomplete_steps": [*plan_result["incomplete_steps"], {
                    "title": "修正或核验尚未通过的引用论断", "phase": "audit", "status": "pending",
                    "reason": "citation_review_unresolved", "known_failed_count": audit.get("known_failed_count", 0),
                    "unknown_count": audit.get("unknown_count", 0),
                }]}
                task_outcome = "blocked" if refusal else "partial"
            # Persist structured completion facts before the QA transaction.
            # A history reload must not infer success from the existence of an
            # answer or from localized progress text.
            requirement_coverage = None
            if goal_snapshot:
                coverage = goal_snapshot["input_coverage"]
                contracts = [item for item in goal_snapshot["requirements"]
                             if (item.get("acceptance") or "").startswith("user_contract:")]
                if contracts or coverage["model_candidates"]:
                    requirement_coverage = {"goal_revision": goal_snapshot["revision"],
                        "verification": "structural_only", "semantic_coverage": "unverified",
                        "contracts": [{key: item[key] for key in ("id", "title", "status", "source_message_id", "source_revision")}
                                      for item in contracts],
                        "candidate_count": len(coverage["candidates"]),
                        "text": "已核对可验证的数量与交付条件；原话候选及语义覆盖仍待核验，不能据结构通过认定全部理解正确。"}
                    log("requirement_coverage", requirement_coverage)
            log("task_outcome", {"outcome": task_outcome, "completed": plan_result["completed"],
                                 "incomplete_steps": plan_result["incomplete_steps"]})
            if state.execution_mode == "execute" and public_plan():
                delivery_text = ("全部任务完成" if plan_result["completed"]
                                 else "本轮已结束，保留未完成的任务进度")
                log("todo_update", {"text": delivery_text, "steps": public_plan()})
            check_cancel()
            claims = ops.parse_claims(state.answer, ids) if ids else []
            saver.update_elapsed()
            from ..conversation_store import ensure_conversation
            record = db.get(ops.QARecord, state.record_id)
            if record is None:
                record = ops.QARecord(id=state.record_id, conversation_id=conversation_id, paper_id=paper_id,
                    execution_mode=state.execution_mode,
                    reviewed_plan_id=state.reviewed_plan_id,
                    plan_steps=[dict(step, status="pending") for step in public_plan()] if task_outcome == "planned" else None,
                    plan_images=images if task_outcome == "planned" else None,
                    workspace=state.workspace,
                    runtime_config=state.runtime_config,
                    provider_context=state.provider_context,
                    question=question, answer=state.answer, claims=claims, evidence=state.evidence,
                    trace=state.trace, skills=state.skills, refusal=refusal, latency_ms=state.elapsed_ms,
                    in_memory=1 if getattr(ops.settings, "memory_capture", True) else 0, usage=delivery_usage(state))
                db.add(record)
                ensure_conversation(db, conversation_id)
            if state.execution_mode == "plan":
                state.todo = [dict(step, status="pending") for step in state.todo]
            state.result = {"answer": state.answer, "claims": claims, "evidence_ids": ids,
                "evidence": state.evidence, "gate": gate,
                "audit": {"mode": audit.get("mode", "none"), "passed": audit.get("passed", True),
                          "status": audit.get("status", "passed" if audit.get("passed", True) else "failed"),
                          "verified": audit.get("verified", True),
                          "unsupported_ratio": audit.get("unsupported_ratio", 0.0),
                          "unverified_count": audit.get("unverified_count", 0),
                          "known_failed_count": audit.get("known_failed_count", 0),
                          "unknown_count": audit.get("unknown_count", 0),
                          # Keep sentence-level review state visible to the
                          # client.  It is diagnostic metadata; it never
                          # substitutes for the answer or turns an unknown
                          # review into a negative verdict.
                          "unsupported": deepcopy(audit.get("unsupported", [])),
                          "unverified": deepcopy(audit.get("unverified", []))},
                "refusal": refusal, "budget_exhausted": state.budget_exhausted,
                "outcome": task_outcome, "completed": plan_result["completed"],
                "incomplete_steps": plan_result["incomplete_steps"],
                "stop_reason": "step_limit" if state.budget_exhausted else "refused" if refusal
                              else "answer_quality_unresolved" if quality_missing
                              else "citation_review_unresolved" if citation_review_unresolved
                              else "needs_clarification" if state.execution_mode == "plan" and response_kind == "clarification"
                              else "plan_not_created" if state.execution_mode == "plan" and task_outcome != "planned"
                                   and response_kind != "direct_response"
                              else "incomplete_requirements" if not plan_result["completed"]
                              else "completed",
                "latency_ms": state.elapsed_ms, "usage": delivery_usage(state), "qa_id": state.record_id,
                "cumulative_usage": deepcopy(state.usage), "attempt_id": state.attempt_id,
                "goal_id": state.goal_id,
                "goal_outcome": goal_snapshot.get("outcome") if goal_snapshot else None}
            state.result["runtime_config"] = public_runtime_config(state.runtime_config)
            if audit.get("error_code") in {"output_incomplete", "model_response_invalid", "http_error", "json",
                                           "provider_unavailable", "schema_results", "dispatch_outcome_unknown",
                                           "journal_invalid", "audit_input_changed", "multiple_batch_failures"}:
                state.result["audit"]["error_code"] = audit["error_code"]
            if requirement_coverage is not None:
                state.result["requirement_coverage"] = requirement_coverage
            if attribution is not None:
                state.result["source_attribution"] = attribution
                state.result["answer_quality"] = {"status": "failed" if quality_issues else attribution.get("quality_status", "unverified"),
                    "verified": attribution.get("quality_verified", False),
                    "known_failed_count": len(quality_issues),
                    "issues": deepcopy(quality_issues) if quality_missing else deepcopy(attribution.get("quality_issues", [])),
                    "protocol_errors": deepcopy(attribution.get("quality_protocol_errors", []))}
            elif quality_missing:
                state.result["answer_quality"] = {"status": "failed", "verified": False,
                    "known_failed_count": len(quality_issues), "issues": deepcopy(quality_issues)}
            if post_rewrite_quality is not None:
                state.result["answer_quality"] = {"status": post_rewrite_quality["quality_status"],
                    "verified": post_rewrite_quality["quality_verified"], "known_failed_count": len(quality_issues),
                    "issues": deepcopy(quality_issues), "reason": post_rewrite_quality["quality_reason"],
                    "protocol_errors": deepcopy(post_rewrite_quality.get("quality_protocol_errors", []))}
            state.result.setdefault("answer_quality", {"status": "not_assessed", "verified": False,
                "known_failed_count": 0, "issues": [], "reason":
                    "citation_review_blocked" if ids and refusal else "post_rewrite_check_not_required"})
            if state.execution_mode == "plan" and response_kind:
                state.result["response_kind"] = response_kind
            state.result["source_revision_ids"] = list(state.source_revision_ids)
            state.result.update(execution_mode=state.execution_mode, reviewed_plan_id=state.reviewed_plan_id,
                                plan_steps=public_plan() if state.execution_mode == "execute" or task_outcome == "planned" else [])
            state.phase = "deliver"
            from ..foreground_run_store import finish as finish_foreground_run
            finish_foreground_run(db, run_id, "completed", attempt_id=state.attempt_id,
                                  writer_token=state.writer_token)
            # QA row and the delivery checkpoint commit in the same DB transaction.
            saver.save()
            if not save_checkpoint:
                db.commit()
            from ..task_events import notify_tasks_changed
            notify_tasks_changed()

        if state.phase in ("deliver", "completed"):
            if state.delivery_only and state.phase == "deliver":
                from ..foreground_run_store import finish as finish_foreground_run
                finish_foreground_run(db, run_id, "completed", attempt_id=state.attempt_id,
                                      writer_token=state.writer_token)
                saver.save()
            result = state.result or {}
            outcome_event = next((event for event in reversed(state.trace)
                                  if event.get("kind") == "task_outcome"), None)
            if outcome_event is not None:
                yield "task_outcome", deepcopy(outcome_event)
            progress_text = ("计划已生成，等待审阅" if result.get("outcome") == "planned"
                             else "本轮未形成可执行计划" if state.execution_mode == "plan"
                             else "全部任务完成" if result.get("completed")
                             else "本轮已结束，保留未完成的任务进度")
            if public_plan():
                progress_event = next((event for event in reversed(state.trace)
                                       if event.get("kind") == "todo_update"
                                       and event.get("text") == progress_text), None)
                if progress_event is None:
                    _, progress_event = log("todo_update", {"text": progress_text, "steps": public_plan()})
                yield "todo_update", deepcopy(progress_event)
            yield log("answer", state.result or {})
            if state.phase == "deliver":
                sh = run_hook(f"attempt/{state.attempt_id}/hook/stop" if state.attempt_id else "hook/stop", "Stop", {"answer": state.answer,
                    "conversation_id": conversation_id, "evidence_ids": [e["id"] for e in state.evidence],
                    "refusal": (state.result or {}).get("refusal", 0)})
                if sh["ran"]:
                    yield log("hook", {"text": f"Stop 钩子执行 {sh['ran']} 条", **sh})
                state.phase = "completed"
                saver.save()
            yield log("done", {"text": "完成"})
    except MemoryStageRebuilt as exc:
        memory_change = exc.reason == "memory"
        yield log("answer_reset", {"text": "记忆已更新，正在重新核对当前任务" if memory_change else "已整理原生上下文，继续当前任务"})
        yield log("memory" if memory_change else "status", {
            "text": "会话记忆已更新，已保留任务目标与操作回执，正在使用新记忆继续" if memory_change else
                    "已保留目标、纠偏、证据位置和操作回执，正在自动继续处理后续步骤",
            "generation": state.memory_generation, "stage_reason": exc.reason})
        return state.snapshot()
    except (ResourceBudgetExceeded, ContextCapacityExceeded, SkillsChanged, MemoryScopeChanged) as exc:
        if isinstance(exc, SkillsChanged) and getattr(exc, 'skill_request_not_dispatched', False):
            state.pending_operation = None
            state.resolution = None
        # Preserve the exact phase and all receipts. Resource admission is not
        # the old step-limit pause, and must never imply a completed goal.
        if (getattr(exc, "service_request_not_dispatched", False) is True and state.pending_operation
                and state.pending_operation.get("name") in {"deepseek_search", "deepseek_fetch", "deepseek_ocr", "deepseek_research", "deepseek_file_read"}):
            # The service receipt proves admission failed before dispatch. It
            # remains prepared and is safe to authorize again after adjustment.
            state.pending_operation = None
            state.resolution = None
        state.resource_pause = {"kind": "budget" if isinstance(exc, ResourceBudgetExceeded) else 'memory_changed' if isinstance(exc, MemoryScopeChanged) else 'skill_changed' if isinstance(exc, SkillsChanged) else "context",
                                "message": str(exc), "phase": state.phase}
        event = log("paused", {"text": str(exc), "reason": "resource_budget" if isinstance(exc, ResourceBudgetExceeded) else 'memory_changed' if isinstance(exc, MemoryScopeChanged) else 'skill_changed' if isinstance(exc, SkillsChanged) else "context_capacity",
                                "resource_pause": deepcopy(state.resource_pause), "resume_available": True})
        saver.save()
        from ..foreground_run_store import finish as finish_foreground_run
        finish_foreground_run(db, run_id, "paused", str(exc), attempt_id=state.attempt_id, writer_token=state.writer_token)
        db.commit()
        yield event
    except (AgentCancelled, MemoryCancelled, ResourceRequestCancelled):
        if git_prepared:
            for operation in git_operations.cancel_agent_pending(git_owner):
                yield log('git_operation', {'operation_id': operation['id'], 'project_id': operation['project_id'],
                    'action': operation['action'], 'status': operation['status'], 'text': 'Git 操作已取消，未执行写入'})
        saver.save()
        yield log("cancelled", {"text": "已停止，已完成的步骤保留在检查点中"})
    finally:
        if git_prepared:
            git_operations.cancel_agent_pending(git_owner)


def _skipped_tool():
    return {"result": json.dumps({"execution_unknown": True, "skipped_by_user": True,
        "error": "此步骤中断后由用户选择跳过，是否已完成未知，不得假设操作成功。"}, ensure_ascii=False),
        "error": "", "executed": False}


def _external_papers(name, result):
    if name not in ("mcp__builtin__arxiv_search", "mcp__builtin__semantic_scholar_search"):
        return {}
    try:
        payload = json.loads(result)
        return {"external_papers": [{"title": p.get("title", ""), "authors": p.get("authors", ""),
            "year": p.get("year", p.get("published", "")), "url": p.get("url", ""),
            "pdf": p.get("pdf", ""), "source": payload.get("source", "")}
            for p in payload.get("papers", []) if p.get("pdf")]}
    except (ValueError, AttributeError):
        return {"external_papers": []}

