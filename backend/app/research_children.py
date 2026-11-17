"""Fenced agent-to-research bridge using the existing run/attempt state machine.

Links and grants record input relationships and human permissions only. No
thread, future, execution state, or copy of a supplier call is stored here.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import select, text

from . import research_runs as runs, run_lifecycle, run_store, source_store as sources
from .agent.checkpoint import AgentState, recovery_info
from .db import SessionLocal
from .research_models import (ResearchChildLink, ResearchChildAttemptLink,
    ResearchChildRetryGrant, ResearchChildRetryConsumption, ResearchChildCommand)
from .runtime_models import RuntimeRun, RunAttempt
from .source_runtime import conversation_scope

START = "start_research_child"
RESUME = "resume_research_child"
READ = "read_research_child"
WRITE_TOOLS = {START, RESUME}
READ_TOOLS = {READ}


def _identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", value):
        raise sources.SourceError(label + "须为8至96位字母、数字、下划线或连字符")
    return value


def _denied(reason):
    from .goal_policy import GoalPolicyChanged
    return GoalPolicyChanged({"blocked_by_goal_constraint": True, "not_executed": True, "error": reason})


def assert_parent(db, binding, *, allow_terminal=False, check_steering=True):
    """Only executor-supplied exact identities can exclude a parent writer."""
    if not isinstance(binding, dict) or any(not binding.get(k) for k in
            ("run_id", "attempt_id", "writer_token", "conversation_id", "goal_id")):
        raise _denied("研究子任务缺少完整父执行身份，尚未执行")
    rid, cid = binding["run_id"], binding["conversation_id"]
    conversation_scope(db, cid)
    run = db.get(RuntimeRun, rid, populate_existing=True)
    if run is None or run.conversation_id != cid or run.goal_id != binding["goal_id"]:
        raise _denied("研究子任务父会话或目标不匹配，尚未执行")
    try:
        attempt = run_lifecycle.assert_writer(db, rid, binding["attempt_id"], binding["writer_token"],
            allow_terminal=allow_terminal)
    except run_lifecycle.RunConflict:
        raise _denied("父执行已结束或由新尝试接管，子任务未继续执行") from None
    from . import cancel_store
    from .goal_models import Goal
    goal = db.get(Goal, run.goal_id, populate_existing=True)
    if cancel_store.is_cancelled(rid) or goal is None or goal.conversation_id != cid or goal.run_id != rid:
        raise _denied("父任务已停止或目标已失效，子任务未继续执行")
    if goal.control_state != "active" or goal.outcome == "cancelled":
        raise _denied("父目标已暂停或取消，子任务未继续执行")
    if not allow_terminal and run.status not in ("queued", "running"):
        raise _denied("父任务未在执行，子任务未发起新动作")
    if check_steering:
        from .run_inputs import RunInput
        steer = db.scalar(select(RunInput.input_id).where(RunInput.run_id == rid,
            RunInput.kind == "steer", RunInput.status == "queued").limit(1))
        if steer:
            raise _denied("父任务收到新的补充要求，先应用纠偏再继续研究子任务")
    return run, attempt


def parent_record(binding, link_id):
    return {"link_id": link_id, **{k: binding[k] for k in
        ("run_id", "attempt_id", "writer_token", "conversation_id", "goal_id")}}


def selected_parent_paper(db, binding):
    _, attempt = assert_parent(db, binding)
    selected = (attempt.checkpoint or {}).get("paper_id")
    if selected is not None and (not isinstance(selected, str) or not re.fullmatch(r"[a-f0-9]{32}", selected)):
        raise _denied("父任务论文范围损坏，未扩大研究范围")
    return selected


def assert_input_scope(db, binding, kind, inputs):
    selected = selected_parent_paper(db, binding)
    if not selected:
        return
    if kind == "quality" and inputs["paper_id"] != selected:
        raise _denied("本轮已选择单篇论文，引用分析不能改查其他文献")
    if kind == "matrix":
        from .research_store import matrix
        item = matrix(db, inputs["matrix_id"], binding["conversation_id"])
        ids = inputs["paper_ids"] or [paper["id"] for paper in item.document["papers"]]
        if set(ids) - {selected}:
            raise _denied("本轮已选择单篇论文，矩阵提取不能扩大到其他文献；请先由用户调整范围")
    if kind == "translation" and inputs.get("source_ref"):
        from .source_models import SourceLibraryBinding, SourceRevision, SourceSnapshot, Source
        fixed = db.get(SourceLibraryBinding, selected)
        def original_hash(revision_id, scope_type, scope_id):
            return db.scalar(select(SourceSnapshot.raw_sha256).join(SourceRevision, SourceRevision.snapshot_id == SourceSnapshot.id)
                .join(Source, Source.id == SourceRevision.source_id).where(SourceRevision.id == revision_id,
                    Source.scope_type == scope_type, Source.scope_id == scope_id))
        source = original_hash(inputs["source_ref"]["revision_id"], "conversation", binding["conversation_id"])
        original = original_hash(fixed.revision_id, "library", selected) if fixed else None
        if original is None or original != source:
            raise _denied("固定翻译来源不属于本轮选定论文，未读取其他文献")


def checked_link(db, child_run_id, conversation_id, *, parent_run_id=None):
    conversation_scope(db, conversation_id, writing=False)
    link = db.scalar(select(ResearchChildLink).where(ResearchChildLink.child_run_id == child_run_id))
    if link is None or link.conversation_id != conversation_id or parent_run_id and link.parent_run_id != parent_run_id:
        raise sources.SourceNotFound("当前父任务没有此研究子任务")
    return link


def attach_attempt(db, link, attempt, binding):
    """Bind a new attempt; never modify an earlier terminal attempt."""
    assert_parent(db, binding)
    if (link.parent_run_id, link.parent_goal_id, link.conversation_id) != (
            binding["run_id"], binding["goal_id"], binding["conversation_id"]):
        raise _denied("研究子任务关系与父执行不匹配")
    if attempt.run_id != link.child_run_id:
        raise sources.SourceCorruption("研究子任务尝试不属于固定关系")
    row = db.get(ResearchChildAttemptLink, attempt.id)
    token_sha = hashlib.sha256(binding["writer_token"].encode()).hexdigest()
    if row is not None:
        if row.link_id != link.id or row.parent_attempt_id != binding["attempt_id"] or row.parent_writer_sha256 != token_sha:
            raise sources.SourceConflict("已绑定的研究尝试不能改归其他父执行")
        return
    state = AgentState.model_validate(attempt.checkpoint)
    state.runtime_config = {**state.runtime_config, "parent_run_id": link.parent_run_id,
        "research_parent": parent_record(binding, link.id)}
    attempt.checkpoint = state.snapshot()
    db.add(ResearchChildAttemptLink(child_attempt_id=attempt.id, link_id=link.id,
        parent_attempt_id=binding["attempt_id"], parent_writer_sha256=token_sha))
    db.flush()


def assert_linked_control(db, child_run_id, parent_binding=None):
    link = db.scalar(select(ResearchChildLink).where(ResearchChildLink.child_run_id == child_run_id))
    if link is None:
        if parent_binding is not None:
            raise sources.SourceConflict("研究子任务缺少固定父关系")
        return None
    if parent_binding is None:
        raise sources.SourceConflict("此研究任务由对话管理，请从父任务继续或合并；未发起新调用")
    assert_parent(db, parent_binding)
    if (link.parent_run_id, link.parent_goal_id, link.conversation_id) != (
            parent_binding["run_id"], parent_binding["goal_id"], parent_binding["conversation_id"]):
        raise sources.SourceConflict("不能使用其他父任务恢复此研究任务")
    return link


def authorize_state(db, state):
    """Fresh parent, goal, queued steering and exact attempt checks."""
    binding = state.runtime_config or {}
    link = db.scalar(select(ResearchChildLink).where(ResearchChildLink.child_run_id == binding.get("run_id")))
    parent = binding.get("research_parent")
    if link is None:
        if parent:
            raise _denied("研究任务父关系已删除，未继续执行")
        return None
    attribution = db.get(ResearchChildAttemptLink, state.attempt_id)
    if not isinstance(parent, dict) or attribution is None or attribution.link_id != link.id:
        raise _denied("研究任务缺少可核对的父尝试关系")
    if (parent.get("link_id"), parent.get("run_id"), parent.get("goal_id"), parent.get("conversation_id"),
            parent.get("attempt_id"), hashlib.sha256(str(parent.get("writer_token", "")).encode()).hexdigest()) != (
            link.id, link.parent_run_id, link.parent_goal_id, link.conversation_id,
            attribution.parent_attempt_id, attribution.parent_writer_sha256):
        raise _denied("研究任务父身份校验失败，未继续执行")
    if binding.get("resource_budget_id") != link.resource_budget_id:
        raise _denied("研究任务预算与父任务不一致，未继续执行")
    parent_run, parent_attempt = assert_parent(db, parent)
    parent_config = (parent_attempt.checkpoint or {}).get("runtime_config") or {}
    if parent_config.get("resource_budget_id") != link.resource_budget_id:
        raise _denied("父预算绑定已变化，未继续执行")
    job = runs.descriptor(state)
    assert_input_scope(db, parent, link.kind, job["inputs"])
    selected = selected_parent_paper(db, parent)
    if selected and link.kind == "framework" and any(row.get("id", row.get("paper_id")) != selected
            for row in job.get("candidate_snapshot", []) + job.get("source_snapshot", [])):
        raise _denied("论文框架固定来源不属于当前选定论文，未扩大研究范围")
    from .goal_ledger import snapshot_goal
    from .goal_candidates import tool_constraint
    snapshot = snapshot_goal(db, parent_run.goal_id)
    denial = tool_constraint(snapshot, START, {"kind": link.kind})
    if denial:
        raise _denied(denial["error"])
    return deepcopy(parent)


def policy_binding(binding):
    """Normalize only a verified child; arbitrary parent_run_id is not proof."""
    if not (binding or {}).get("research_parent"):
        return binding
    rid, aid = binding.get("run_id"), binding.get("attempt_id")
    with SessionLocal() as db:
        attempt = db.get(RunAttempt, aid) if aid else None
        if attempt is None or attempt.run_id != rid:
            raise _denied("研究任务执行身份失效")
        state = AgentState.model_validate(attempt.checkpoint)
        if state.runtime_config.get("research_parent") != binding["research_parent"]:
            raise _denied("研究任务父绑定与持久检查点不一致")
        run_lifecycle.assert_writer(db, rid, aid, state.writer_token)
        return authorize_state(db, state)


def _same_parent_authority(db, current, granted_attempt_id):
    if current.id == granted_attempt_id:
        return True
    # Recovery creates one new parent attempt. A grant does not carry across
    # arbitrary later turns/attempts, or through another interrupted replay.
    return (current.checkpoint or {}).get("source_attempt_id") == granted_attempt_id


def _grant(db, link, state, parent_attempt, grant_id=None):
    pending_sha = runs.digest(state.pending_operation) if state.pending_operation else None
    if pending_sha is None:
        return None
    query = select(ResearchChildRetryGrant).where(ResearchChildRetryGrant.link_id == link.id,
        ResearchChildRetryGrant.child_attempt_id == state.attempt_id,
        ResearchChildRetryGrant.pending_sha256 == pending_sha)
    if grant_id is not None:
        query = query.where(ResearchChildRetryGrant.id == grant_id)
    for grant in db.scalars(query.order_by(ResearchChildRetryGrant.created_at.desc())):
        if db.get(ResearchChildRetryConsumption, grant.id) is None and _same_parent_authority(db, parent_attempt, grant.parent_attempt_id):
            return grant
    return None


def _approved_unstarted_retry(db, link, state):
    # The retry was admitted, but the operation boundary has not started it:
    # Checkpointer clears resolution before it saves a new dispatch intent.
    # A post-dispatch unknown therefore cannot reuse this permission.
    proof = state.context_archive.get("research_retry_permission") or {}
    if state.resolution != "retry" or not state.pending_operation or proof.get("pending_sha256") != runs.digest(state.pending_operation):
        return None
    grant = db.get(ResearchChildRetryGrant, proof.get("grant_id")) if proof.get("grant_id") else None
    used = db.get(ResearchChildRetryConsumption, grant.id) if grant else None
    if grant is None or used is None or grant.link_id != link.id or grant.pending_sha256 != proof["pending_sha256"]:
        return None
    attempt, seen = db.get(RunAttempt, state.attempt_id), set()
    while attempt and attempt.id not in seen and len(seen) < 10000:
        seen.add(attempt.id)
        if attempt.id == used.child_attempt_id:
            return grant
        if attempt.run_id != link.child_run_id:
            break
        attempt = db.get(RunAttempt, (attempt.checkpoint or {}).get("source_attempt_id")) if (attempt.checkpoint or {}).get("source_attempt_id") else None
    return None


def create_retry_grant(db, *, child_run_id, conversation_id, parent_run_id,
        expected_parent_revision, expected_parent_attempt_id, expected_child_attempt_id,
        pending_sha256, request_id):
    """Human HTTP boundary only; model tools never call this function."""
    _identifier(request_id, "许可请求编号")
    link = checked_link(db, child_run_id, conversation_id, parent_run_id=parent_run_id)
    parent = db.get(RuntimeRun, parent_run_id, populate_existing=True)
    attempt = run_lifecycle.current_attempt(db, parent_run_id)
    if parent is None or attempt is None:
        raise sources.SourceNotFound("父任务已不存在")
    facts = (link.id, expected_parent_attempt_id, expected_parent_revision, expected_child_attempt_id, pending_sha256)
    prior = db.scalar(select(ResearchChildRetryGrant).where(
        ResearchChildRetryGrant.conversation_id == conversation_id, ResearchChildRetryGrant.request_id == request_id))
    if prior:
        if (prior.link_id, prior.parent_attempt_id, prior.parent_revision, prior.child_attempt_id, prior.pending_sha256) != facts:
            raise sources.SourceConflict("许可请求编号已用于其他未确认调用")
        return prior, False
    if (parent.revision, attempt.id) != (expected_parent_revision, expected_parent_attempt_id):
        raise sources.SourceConflict("父执行已变化，请重新核对再批准")
    _, child, state = runs.checked(db, child_run_id, conversation_id)
    child_run = db.get(RuntimeRun, child_run_id)
    if child_run.status in ("queued", "running") or child_run_id in run_store.active_runs():
        raise sources.SourceConflict("研究调用仍在运行，请等结果确认后再批准重试")
    if child.id != expected_child_attempt_id or not state.pending_operation or runs.digest(state.pending_operation) != pending_sha256:
        raise sources.SourceConflict("未确认调用已变化，旧许可未授权新请求")
    assert_parent(db, {"run_id": parent.id, "attempt_id": attempt.id, "writer_token": attempt.writer_token,
        "goal_id": parent.goal_id, "conversation_id": conversation_id}, allow_terminal=True, check_steering=False)
    grant = ResearchChildRetryGrant(id=uuid4().hex, conversation_id=conversation_id, link_id=link.id,
        parent_attempt_id=attempt.id, parent_revision=parent.revision, child_attempt_id=child.id,
        pending_sha256=pending_sha256, request_id=request_id)
    db.add(grant); db.flush()
    return grant, True


def public(db, child_run_id, conversation_id, *, parent_run_id=None):
    link = checked_link(db, child_run_id, conversation_id, parent_run_id=parent_run_id)
    value = runs.public(db, child_run_id, conversation_id)
    _, _, state = runs.checked(db, child_run_id, conversation_id)
    parent = db.get(RuntimeRun, link.parent_run_id, populate_existing=True)
    parent_attempt = run_lifecycle.current_attempt(db, link.parent_run_id)
    approved = _grant(db, link, state, parent_attempt) if parent_attempt and state.pending_operation else None
    return {**value, "parent_run_id": link.parent_run_id, "parent_goal_id": link.parent_goal_id,
        "parent_revision": parent.revision if parent else None,
        "parent_attempt_id": parent_attempt.id if parent_attempt else None,
        "parent_status": parent.status if parent else None,
        "link_id": link.id, "input_sha256": link.input_sha256,
        "pending_sha256": runs.digest(state.pending_operation) if state.pending_operation else None,
        "unknown_retry_approved": bool(approved or _approved_unstarted_retry(db, link, state)),
        "retry_grant_id": approved.id if approved else None,
        "managed_by_parent": True, "can_resume": False,
        "resume_from": "parent_run", "delivery_verified": value["outcome"] == "completed" and
            value["result_integrity"] == "checked" and value["status"] == "ended"}


def _fixed_evidence(db, conversation_id, payload, *, validate=True):
    from .source_runtime import validate_source_evidence
    found, seen, stack = [], set(), [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if all(isinstance(node.get(k), str) and node[k] for k in ("source_id", "revision_id", "locator_id", "quote")):
                key = tuple(node[k] for k in ("source_id", "revision_id", "locator_id", "quote"))
                if key not in seen:
                    # Trust neither child summaries nor an asserted verified flag.
                    if validate:
                        validate_source_evidence(db, conversation_id, [node])
                    seen.add(key); found.append(node)
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return found


def verified_delivery(db, *, parent_run_id, conversation_id, child_run_id, payload=None):
    """Server proof for parent goal/plan completion; never trust tool prose."""
    try:
        link = checked_link(db, child_run_id, conversation_id, parent_run_id=parent_run_id)
        parent = db.get(RuntimeRun, parent_run_id, populate_existing=True)
        if parent is None or parent.goal_id != link.parent_goal_id or parent.conversation_id != conversation_id:
            return None
        run, attempt, state = runs.checked(db, child_run_id, conversation_id)
        value = public(db, child_run_id, conversation_id, parent_run_id=parent_run_id)
        if not value["delivery_verified"] or attempt.status != "ended" or (state.result or {}).get("completed") is not True:
            return None
        job = runs.descriptor(state)
        if link.input_sha256 != runs.digest({"kind": run.kind, "inputs": job["inputs"]}):
            return None
        attribution = db.get(ResearchChildAttemptLink, attempt.id)
        source_parent = db.get(RunAttempt, attribution.parent_attempt_id) if attribution else None
        if attribution is None or attribution.link_id != link.id or source_parent is None or source_parent.run_id != parent_run_id:
            return None
        result_sha = runs.digest(value["result"])
        if payload is not None and (not isinstance(payload, dict) or
                (payload.get("run_id"), payload.get("attempt_id"), payload.get("revision"), payload.get("result_sha256")) !=
                (run.id, attempt.id, run.revision, result_sha)):
            return None
        all_evidence = _fixed_evidence(db, conversation_id, value["result"], validate=False)
        allowed = {tuple(e[k] for k in ("source_id", "revision_id", "locator_id", "quote")): e for e in all_evidence}
        # Import only source entries actually exposed by this tool page. The
        # model must request the next evidence page to obtain further eN IDs.
        selected = []
        for candidate in (payload.get("evidence", []) if payload is not None else all_evidence):
            key = tuple(candidate.get(k) for k in ("source_id", "revision_id", "locator_id", "quote")) if isinstance(candidate, dict) else None
            if key not in allowed:
                return None
            selected.append(deepcopy(allowed[key]))
        from .source_runtime import validate_source_evidence
        validate_source_evidence(db, conversation_id, selected)
        return {"kind": "durable_research_delivery", "parent_run_id": parent_run_id, "parent_goal_id": link.parent_goal_id,
            "child_run_id": run.id, "child_goal_id": run.goal_id, "child_attempt_id": attempt.id,
            "child_revision": run.revision, "research_kind": run.kind, "result_sha256": result_sha,
            "input_sha256": link.input_sha256, "source_count": value["source_count"],
            "completed": True, "evidence": selected, "semantic_verification": "not_implied_by_execution_completion"}
    except (sources.SourceError, run_lifecycle.RunConflict, ValueError):
        return None


def comparison_delivery(db, *, parent_run_id, conversation_id, child_run_id, payload=None):
    """Exact saved matrix coverage, not a count asserted by the child model."""
    delivery = verified_delivery(db, parent_run_id=parent_run_id, conversation_id=conversation_id,
        child_run_id=child_run_id, payload=payload)
    if not delivery or delivery["research_kind"] != "matrix":
        return None
    try:
        from . import matrix_run
        from .research_models import MatrixRevision
        _, _, state = runs.checked(db, child_run_id, conversation_id)
        fixed = matrix_run._snapshot(db, state, "matrix")
        corpus = matrix_run._snapshot(db, state, "corpus")
        job = runs.descriptor(state)
        records = matrix_run._results(job)
        ids, fields = fixed.document["paper_ids"], fixed.document["fields"]
        if len(set(ids)) != len(ids) or len(set(fields)) != len(fields) or not ids or not fields:
            return None
        # A partial field selection cannot stand in for the original matrix's
        # comparison. It remains a valid research result without closing this
        # broader comparison contract.
        if set(fields) != {field["key"] for field in fixed.document["document"]["dimensions"]}:
            return None
        expected = {(field, pid) for field in fields for pid in ids}
        identities = [(cell.get("field_key"), cell.get("paper_id")) for cell in records]
        if len(identities) != len(expected) or set(identities) != expected:
            return None
        if any(cell.get("cell", {}).get("extraction_status") not in ("retrieved", "not_found") for cell in records):
            return None
        if {paper["paper_id"] for paper in corpus.document["papers"]} != set(ids):
            return None
        value, _ = runs.checked_payload(state, job)
        revision = db.scalar(select(MatrixRevision).where(MatrixRevision.matrix_id == job["inputs"]["matrix_id"],
            MatrixRevision.version == value.get("applied_version")))
        receipt = revision.document.get("matrix_run", {}) if revision else {}
        if (receipt.get("run_id"), receipt.get("matrix_snapshot_fingerprint"), receipt.get("document_sha256")) != (
                child_run_id, fixed.fingerprint, runs.digest(value.get("document"))):
            return None
        if runs.digest(revision.document.get("data")) != receipt["document_sha256"]:
            return None
        papers = {paper["id"]: paper for paper in fixed.document["document"]["papers"] if paper["id"] in ids}
        if set(papers) != set(ids):
            return None
        scope = sources.SourceScope("conversation", conversation_id)
        source_versions = [sources.read_revision(db, papers[pid]["revision_id"], scope=scope) for pid in ids]
        hashes = [source["raw_sha256"] for source in source_versions]
        if len(set(hashes)) != len(ids):
            return None
        parent_attempt = run_lifecycle.current_attempt(db, parent_run_id)
        parent_state = AgentState.model_validate(parent_attempt.checkpoint)
        if parent_state.paper_id and set(ids) != {parent_state.paper_id}:
            return None
        selected = parent_state.source_revision_ids or (parent_state.runtime_config or {}).get("source_revision_ids", [])
        if selected:
            parent_sources = [sources.read_revision(db, rid, scope=scope) for rid in selected]
            if {source["raw_sha256"] for source in parent_sources} != set(hashes):
                return None
        return {"kind": "compared_source_set", "source_ids": list(ids), "field_keys": list(fields),
            "source_versions": [{"paper_id": pid, "revision_id": papers[pid]["revision_id"], "raw_sha256": hashes[index]}
                for index, pid in enumerate(ids)], "cell_count": len(expected),
            "matrix_snapshot_fingerprint": fixed.fingerprint,
            "research_delivery": {key: value for key, value in delivery.items() if key != "evidence"},
            "verification": "fixed_selection_extraction_coverage_only", "semantic_support": "unverified"}
    except (sources.SourceError, run_lifecycle.RunConflict, ValueError, KeyError, TypeError, AttributeError):
        return None


def matches_original_start(db, *, parent_run_id, conversation_id, child_run_id, arguments, payload=None):
    """A resume/read can close only the exact original admitted start step."""
    if not isinstance(arguments, dict) or set(arguments) != {"kind", "inputs", "request_id"}:
        return False
    try:
        link = checked_link(db, child_run_id, conversation_id, parent_run_id=parent_run_id)
        kind = arguments["kind"]
        inputs = runs.adapter(kind).validate_inputs(arguments["inputs"])
        if (link.kind, link.request_id, link.input_sha256) != (kind, arguments["request_id"],
                runs.digest({"kind": kind, "inputs": inputs})):
            return False
        return verified_delivery(db, parent_run_id=parent_run_id, conversation_id=conversation_id,
            child_run_id=child_run_id, payload=payload) is not None
    except (sources.SourceError, run_lifecycle.RunConflict, ValueError):
        return False


def tool_public(db, child_run_id, conversation_id, *, parent_run_id, offset=0, page_size=16000,
        evidence_offset=0, evidence_limit=8):
    """Bound model-context output; UI/HTTP retain the complete durable result."""
    if type(offset) is not int or offset < 0 or type(page_size) is not int or not 1000 <= page_size <= 24000:
        raise sources.SourceError("研究结果分页范围无效")
    if type(evidence_offset) is not int or evidence_offset < 0 or type(evidence_limit) is not int or not 1 <= evidence_limit <= 20:
        raise sources.SourceError("研究证据分页范围无效")
    value = public(db, child_run_id, conversation_id, parent_run_id=parent_run_id)
    evidence = _fixed_evidence(db, conversation_id, value["result"], validate=False)
    from .source_runtime import validate_source_evidence
    selected = deepcopy(evidence[evidence_offset:evidence_offset + evidence_limit])
    validate_source_evidence(db, conversation_id, selected)
    from collections import Counter
    ids = Counter(str(item.get("id", "")) for item in evidence)
    for entry in selected:
        local_id = str(entry.get("id", ""))
        entry.update(child_citation_id=local_id, child_citation_id_unique=bool(local_id and ids[local_id] == 1),
            child_citation_key=child_run_id + ":" + runs.digest([local_id, *[entry[k] for k in
                ("source_id", "revision_id", "locator_id", "quote")]]))
    value.update(evidence=selected, evidence_total=len(evidence),
        evidence_next_offset=evidence_offset + evidence_limit if evidence_offset + evidence_limit < len(evidence) else None)
    value["citation_scope"] = {"kind": "child_run", "child_run_id": child_run_id,
        "instruction": "result中的引用编号属于子任务，不是父回答eN。只按本页顶层evidence的固定来源重写父引用；仅child_citation_id_unique为true时可按局部编号映射，否则必须使用child_citation_key或revision_id+locator_id+quote，不能猜测。固定result与摘要未改写。"}
    inputs = value["inputs"]
    if "text" in inputs:
        text_value = inputs.pop("text")
        inputs.update(text_characters=len(text_value), text_sha256=runs.digest(text_value))
    serialized = json.dumps(value["result"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    value["result_sha256"] = runs.digest(value["result"])
    if len(serialized) > page_size or offset:
        value["result"] = {"serialized_json_page": serialized[offset:offset + page_size],
            "offset": offset, "total_characters": len(serialized), "page_sha256": runs.digest(serialized[offset:offset + page_size]),
            "next_offset": offset + page_size if offset + page_size < len(serialized) else None,
            "instruction": "这是持久结果JSON的精确分页；请用read_research_child按next_offset续读后解析，不得将片段视为完整结果。"}
    return value


def _run(child_run_id, attempt_id):
    with SessionLocal() as db:
        run = db.get(RuntimeRun, child_run_id)
        cid = run.conversation_id
    with run_store.coordinated():
        if not run_store.begin(child_run_id, cid, attempt_id=attempt_id):
            raise sources.SourceConflict("研究子任务已有执行器，请读取现有结果")
    try:
        from . import maintenance
        with maintenance.activity():
            runs.execute(child_run_id, attempt_id)
    except Exception as error:
        runs._mark_failure(child_run_id, attempt_id, error)
    finally:
        with SessionLocal() as db:
            run = db.get(RuntimeRun, child_run_id)
            status = run.status if run else "interrupted"
        run_store.finish(child_run_id, status, attempt_id=attempt_id)


def _resume(db, link, binding, request_id, *, expected_revision=None, grant_id=None):
    parent, parent_attempt = assert_parent(db, binding)
    run, attempt, state = runs.checked(db, link.child_run_id, link.conversation_id, writing=True)
    previous = run_lifecycle.admitted_request(db, run.id, request_id)
    if previous:
        attribution = db.get(ResearchChildAttemptLink, previous.id)
        if attribution is None or attribution.link_id != link.id or attribution.parent_attempt_id != parent_attempt.id:
            raise sources.SourceConflict("恢复请求编号属于其他父执行尝试")
        return previous, False
    if expected_revision is not None and expected_revision != run.revision:
        raise sources.SourceConflict("研究子任务已更新，请先读取当前结果")
    grant = _grant(db, link, state, parent_attempt, grant_id)
    approved = _approved_unstarted_retry(db, link, state)
    if state.pending_operation and grant is None and approved is None:
        raise sources.SourceConflict("研究子任务有未确认的付费调用，需用户在待办入口批准本次重复费用风险")
    _, new, fresh = runs.resume(db, run.id, conversation_id=link.conversation_id,
        request_id=request_id, expected_revision=run.revision, retry_unknown=bool(grant or approved), parent_binding=binding)
    if grant:
        db.add(ResearchChildRetryConsumption(grant_id=grant.id,
            parent_attempt_id=parent_attempt.id, child_attempt_id=new.id))
        frozen = AgentState.model_validate(new.checkpoint)
        frozen.context_archive["research_retry_permission"] = {"grant_id": grant.id, "pending_sha256": grant.pending_sha256}
        new.checkpoint = frozen.snapshot()
        db.flush()
    return new, fresh


def execute(name, arguments, *, binding):
    """Synchronous durable call from the parent checkpoint's tool boundary."""
    permitted = {START: {"kind", "inputs", "request_id"},
        RESUME: {"child_run_id", "request_id", "expected_revision", "grant_id", "expected_matrix_version"},
        READ: {"child_run_id", "offset", "page_size", "evidence_offset", "evidence_limit"}}
    if name not in permitted or not isinstance(arguments, dict) or set(arguments) - permitted[name]:
        raise sources.SourceError("研究子任务参数包含未知或未授权字段")
    if not binding or not binding.get("conversation_id"):
        raise sources.SourceError("研究子任务需要绑定真实父会话")
    required = {START: {"kind", "inputs", "request_id"}, RESUME: {"child_run_id", "request_id", "expected_revision"}, READ: {"child_run_id"}}
    if not required[name] <= set(arguments):
        raise sources.SourceError("研究子任务缺少必填参数")
    if name != START and (not isinstance(arguments["child_run_id"], str) or not re.fullmatch(r"research_[a-f0-9]{32}", arguments["child_run_id"])):
        raise sources.SourceError("研究子任务编号无效")
    if name == RESUME and (type(arguments["expected_revision"]) is not int or arguments["expected_revision"] < 1):
        raise sources.SourceError("请提供当前研究子任务版本")
    if "expected_matrix_version" in arguments and (type(arguments["expected_matrix_version"]) is not int or arguments["expected_matrix_version"] < 1):
        raise sources.SourceError("请提供当前矩阵版本")
    cid, parent_id = binding["conversation_id"], binding.get("run_id")
    if name == READ:
        with SessionLocal() as db:
            assert_parent(db, binding, allow_terminal=True, check_steering=False)
            return tool_public(db, arguments.get("child_run_id"), cid, parent_run_id=parent_id,
                offset=arguments.get("offset", 0), page_size=arguments.get("page_size", 16000),
                evidence_offset=arguments.get("evidence_offset", 0), evidence_limit=arguments.get("evidence_limit", 8))
    request_id = _identifier(arguments.get("request_id"), "研究请求编号")
    fresh, attempt = False, None
    with run_store.coordinated(), SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        assert_parent(db, binding)
        from .goal_ledger import snapshot_goal
        from .goal_candidates import tool_constraint
        denial = tool_constraint(snapshot_goal(db, binding["goal_id"]), name, arguments)
        if denial:
            raise _denied(denial["error"])
        if name == START:
            kind = arguments.get("kind")
            inputs = runs.adapter(kind).validate_inputs(arguments.get("inputs"))
            assert_input_scope(db, binding, kind, inputs)
            expected = runs.digest({"kind": kind, "inputs": inputs})
            link = db.scalar(select(ResearchChildLink).where(ResearchChildLink.parent_run_id == parent_id,
                ResearchChildLink.request_id == request_id))
            if link:
                if link.input_sha256 != expected or link.conversation_id != cid or link.parent_goal_id != binding["goal_id"]:
                    raise sources.SourceConflict("研究请求编号已固定另一组输入，不得重试为新任务")
                run, old, state = runs.checked(db, link.child_run_id, cid)
                # A terminal partial is a real deliverable requiring a domain
                # decision; repeating start must not buy it again.
                if run.status in ("queued", "running") and run.id in run_store.active_runs():
                    raise sources.SourceConflict("研究子任务仍在运行，请读取当前结果")
                if run.outcome == "completed" or run.status == "ended":
                    db.commit()
                    return tool_public(db, run.id, cid, parent_run_id=parent_id)
                attempt, fresh = _resume(db, link, binding,
                    "bridge-" + runs.digest([request_id, binding["attempt_id"], old.id])[:56])
            else:
                child_id = "research_" + runs.digest([parent_id, request_id])[:32]
                _, attempt, fresh = runs.create(db, kind=kind, run_id=child_id, conversation_id=cid,
                    inputs=inputs, request_id=request_id, parent_binding=binding)
                link = ResearchChildLink(parent_run_id=parent_id, child_run_id=child_id,
                    parent_goal_id=binding["goal_id"], conversation_id=cid, request_id=request_id,
                    kind=kind, input_sha256=expected, resource_budget_id=binding.get("resource_budget_id"))
                db.add(link); db.flush()
                attach_attempt(db, link, attempt, binding)
        else:
            link = checked_link(db, arguments.get("child_run_id"), cid, parent_run_id=parent_id)
            command = db.scalar(select(ResearchChildCommand).where(ResearchChildCommand.link_id == link.id,
                ResearchChildCommand.request_id == request_id))
            intent = runs.digest(arguments)
            if command and command.input_sha256 != intent:
                raise sources.SourceConflict("恢复请求编号已固定其他输入，请保留原请求或使用新编号")
            if not command:
                db.add(ResearchChildCommand(link_id=link.id, request_id=request_id, input_sha256=intent))
                db.flush()
            if "expected_matrix_version" in arguments:
                if link.kind != "matrix" or "grant_id" in arguments:
                    raise sources.SourceError("仅矩阵已知候选允许无付费合并，不能夹带重试许可")
                from .matrix_run import apply_saved
                run = db.get(RuntimeRun, link.child_run_id)
                if not command and arguments.get("expected_revision") != run.revision:
                    raise sources.SourceConflict("研究子任务已更新，请重新读取")
                apply_saved(db, link.child_run_id, cid, arguments["expected_matrix_version"], request_id,
                    parent_binding=binding)
            else:
                run = db.get(RuntimeRun, link.child_run_id)
                if command and run.status == "ended":
                    db.commit()
                    return tool_public(db, run.id, cid, parent_run_id=parent_id)
                admission_id = "bridge-" + runs.digest([link.id, request_id, binding["attempt_id"]])[:56]
                attempt, fresh = _resume(db, link, binding, admission_id,
                    expected_revision=None if command else arguments["expected_revision"], grant_id=arguments.get("grant_id"))
        child_id = link.child_run_id
        attempt_id = attempt.id if attempt is not None else None
        db.commit()
    if fresh:
        _run(child_id, attempt_id)
    with SessionLocal() as db:
        return tool_public(db, child_id, cid, parent_run_id=parent_id)


def resume_safety(db, *, parent_run_id, checkpoint):
    """Prove pending tool replay from durable child receipts, not model text."""
    pending = checkpoint.get("pending_operation") if isinstance(checkpoint, dict) else None
    if not pending or pending.get("name") not in WRITE_TOOLS:
        return {"safe": False, "reason": "not_research_child"}
    args = pending.get("arguments") or {}
    parent = db.get(RuntimeRun, parent_run_id)
    parent_attempt = run_lifecycle.current_attempt(db, parent_run_id)
    if parent is None or parent_attempt is None or parent_attempt.id != checkpoint.get("attempt_id"):
        return {"safe": False, "reason": "stale_parent"}
    try:
        if pending["name"] == START:
            _identifier(args.get("request_id"), "研究请求编号")
            kind = args.get("kind")
            inputs = runs.adapter(kind).validate_inputs(args.get("inputs"))
            link = db.scalar(select(ResearchChildLink).where(ResearchChildLink.parent_run_id == parent_run_id,
                ResearchChildLink.request_id == args["request_id"]))
            if link is None:
                return {"safe": True, "reason": "not_admitted_no_dispatch"}
            if link.input_sha256 != runs.digest({"kind": kind, "inputs": inputs}):
                return {"safe": False, "reason": "input_mismatch"}
        else:
            link = checked_link(db, args.get("child_run_id"), parent.conversation_id, parent_run_id=parent_run_id)
        _, child, state = runs.checked(db, link.child_run_id, parent.conversation_id)
        facts = {"child_run_id": link.child_run_id, "child_attempt_id": child.id,
            "pending_sha256": runs.digest(state.pending_operation) if state.pending_operation else None}
        if link.child_run_id in run_store.active_runs():
            return {**facts, "safe": False, "reason": "child_still_running"}
        if state.pending_operation and _grant(db, link, state, parent_attempt, args.get("grant_id")) is None and _approved_unstarted_retry(db, link, state) is None:
            return {**facts, "safe": False, "reason": "unknown_paid_child_requires_human_grant"}
        if runs.descriptor(state).get("resume_blocked_reason") and child.status != "ended":
            return {**facts, "safe": False, "reason": "child_requires_domain_review"}
        return {**facts, "safe": True, "reason": "human_grant" if state.pending_operation else "known_durable_child"}
    except (sources.SourceError, run_lifecycle.RunConflict, ValueError):
        return {"safe": False, "reason": "invalid_child_receipt"}


def descendant_call_scope(db, run_id, attempt_id=None):
    """Deduplicated run/attempt scopes, with bounded iterative cycle handling."""
    scopes, frontier = {(run_id, attempt_id)}, [(run_id, attempt_id)]
    while frontier:
        rid, aid = frontier.pop()
        links = list(db.scalars(select(ResearchChildLink).where(ResearchChildLink.parent_run_id == rid)))
        for link in links:
            if aid is None:
                additions = [(link.child_run_id, None)]
            else:
                additions = [(link.child_run_id, child) for child in db.scalars(select(ResearchChildAttemptLink.child_attempt_id)
                    .where(ResearchChildAttemptLink.link_id == link.id, ResearchChildAttemptLink.parent_attempt_id == aid))]
            for scope in additions:
                if scope not in scopes:
                    scopes.add(scope); frontier.append(scope)
                    if len(scopes) > 10000:
                        raise sources.SourceCorruption("研究子任务关系超过可核对范围，未汇总不完整费用")
    return scopes
