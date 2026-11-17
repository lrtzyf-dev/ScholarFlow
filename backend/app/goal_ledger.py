"""Transactional goals and requirements. Helpers flush; their caller commits.

Plans describe methods, not authority to remove obligations. The user's full
input is retained as goal provenance even when there are no structured checks.
No helper invokes a model or creates a visible plan for an ordinary chat.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .goal_models import Goal, GoalRequirement, GoalRevision
from .timeutil import utcnow


class GoalError(ValueError):
    pass


class GoalConflict(GoalError):
    pass


class GoalMigrationError(GoalError):
    pass


OPEN_OUTCOMES = ("open", "partial", "blocked")
USER_ACTIONS = {"append", "add_requirements", "cancel_requirements", "cancel", "pause", "resume", "confirm_complete", "replace"}


def _text(value, label, *, limit=None):
    if not isinstance(value, str) or not value.strip():
        raise GoalError(f"{label}不能为空")
    value = value.strip()
    if limit and len(value) > limit:
        raise GoalError(f"{label}过长")
    return value


def _goal(db: Session, goal_id: str) -> Goal:
    goal = db.get(Goal, goal_id, populate_existing=True)
    if goal is None:
        raise GoalError("目标不存在")
    return goal


def _requirements(db, goal_id):
    return list(db.scalars(select(GoalRequirement).where(GoalRequirement.goal_id == goal_id)
                           .order_by(GoalRequirement.created_at, GoalRequirement.id)
                           .execution_options(populate_existing=True)))


def _stamp(value):
    return value.isoformat() if value else None


def snapshot_goal(db: Session, goal_id: str) -> dict:
    db.flush()
    goal = _goal(db, goal_id)
    requirements = [{"id": r.id, "requirement_id": r.id, "identity": r.identity, "title": r.title,
                     "phase": r.phase, "origin": r.origin, "required": bool(r.required), "status": r.status,
                     "source_message_id": r.source_message_id, "source_revision": r.source_revision,
                     "acceptance": r.acceptance, "tool_name": r.tool_name,
                     "tool_arguments": deepcopy(r.tool_arguments), "evidence": deepcopy(r.evidence),
                     "cancelled_revision": r.cancelled_revision}
                    for r in _requirements(db, goal_id)]
    revisions = [{"revision": r.revision, "action": r.action, "source": r.source,
                  "source_message_id": r.source_message_id, "reason": r.reason,
                  "changes": deepcopy(r.changes), "created_at": _stamp(r.created_at)}
                 for r in db.scalars(select(GoalRevision).where(GoalRevision.goal_id == goal_id)
                                    .order_by(GoalRevision.revision))]
    outstanding = [r for r in requirements if r["required"] and r["status"] not in ("completed", "cancelled")]
    result = {"id": goal.id, "goal_id": goal.id, "scope": goal.scope, "run_id": goal.run_id,
            "conversation_id": goal.conversation_id, "description": goal.description, "outcome": goal.outcome,
            "control_state": goal.control_state, "revision": goal.revision,
            "source_message_id": goal.source_message_id, "note": goal.note,
            "created_at": _stamp(goal.created_at), "updated_at": _stamp(goal.updated_at),
            "completed_at": _stamp(goal.completed_at), "requirements": requirements,
            "incomplete_requirements": outstanding, "revisions": revisions,
            "verification": "structured" if any(r["required"] for r in requirements) else "not_required"}
    from .goal_candidates import projection
    result["input_coverage"] = projection(result)
    return result


def _revision(db, goal, action, source, message_id, changes, reason="", expected_revision=None):
    """CAS at the database boundary; no caller may overwrite a stale revision."""
    expected = goal.revision if expected_revision is None else expected_revision
    if type(expected) is not int or expected < 1:
        raise GoalError("目标修订号无效")
    result = db.execute(update(Goal).where(Goal.id == goal.id, Goal.revision == expected)
                        .values(revision=expected + 1, updated_at=utcnow())
                        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise GoalConflict("目标已更新，请刷新后重试")
    db.expire(goal, ["revision", "updated_at"])
    db.add(GoalRevision(goal_id=goal.id, revision=expected + 1, action=action, source=source,
                        source_message_id=message_id, reason=reason, changes=deepcopy(changes)))
    return expected + 1


def _identity(step):
    if step.get("acceptance"):
        payload = ["acceptance", step["acceptance"]]
    else:
        payload = [step.get("title"), step.get("phase", "analyze"), step.get("tool_name"), step.get("tool_arguments")]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def create_goal(db: Session, description: str, *, scope: str, source_message_id: str,
                run_id: str | None = None, conversation_id: str | None = None,
                goal_id: str | None = None, source="user") -> dict:
    _text(description, "目标描述")  # Preserve original Unicode character offsets.
    message_id = _text(source_message_id, "用户来源编号", limit=160)
    if scope not in ("global", "run", "conversation") or source not in ("user", "legacy_import"):
        raise GoalError("目标作用域或创建来源无效")
    goal = Goal(id=goal_id or uuid.uuid4().hex, scope=scope, description=description,
                run_id=run_id, conversation_id=conversation_id, source_message_id=message_id,
                outcome="open", control_state="active", revision=1)
    db.add(goal)
    db.flush()
    db.add(GoalRevision(goal_id=goal.id, revision=1, action="create", source=source,
                        source_message_id=message_id, changes={"description": description, "scope": scope}))
    db.flush()
    return snapshot_goal(db, goal.id)


def ensure_run_goal(db: Session, run_id: str, conversation_id: str | None, question: str,
                    *, source_message_id: str | None = None) -> dict:
    run_id = _text(run_id, "运行编号", limit=96)
    _text(question, "用户输入")
    existing = db.scalar(select(Goal).where(Goal.run_id == run_id))
    if existing:
        if existing.conversation_id != conversation_id or existing.description.strip() != question.strip():
            raise GoalConflict("同一运行编号不能绑定不同会话或用户输入")
        return snapshot_goal(db, existing.id)
    snapshot = create_goal(db, question, scope="run", run_id=run_id, conversation_id=conversation_id,
                           source_message_id=source_message_id or f"run:{run_id}:user")
    # Reuse existing explicit acceptance checks, but never invoke a planner.
    from .agent.planner import add_acceptance_requirements
    checks = add_acceptance_requirements([], question)
    if checks:
        snapshot = sync_plan_requirements(db, snapshot["id"], checks, trusted_user_acceptance=True)
    snapshot = register_input_contracts(db, snapshot["id"], question, snapshot["source_message_id"], 1)
    return snapshot


def record_user_input(db: Session, goal_id: str, *, expected_revision: int,
                      source_message_id: str, content: str, kind: str = "steer") -> dict:
    """Keep the full user correction without guessing a tool obligation.

    Language/style changes, prohibitions and additions are all authoritative
    context, but a raw sentence is not a deterministic acceptance predicate.
    Structured additions/cancellations still use explicit user revisions.
    """
    message_id = _text(source_message_id, "用户来源编号", limit=160)
    _text(content, "用户输入")  # Validate without trimming the provenance text.
    if kind not in ("steer", "append", "constraint", "clarification"):
        raise GoalError("用户输入类型无效")
    goal = _goal(db, goal_id)
    if goal.outcome not in OPEN_OUTCOMES:
        raise GoalError("已终结目标不能改写，请创建新的目标")
    previous = db.scalar(select(GoalRevision).where(GoalRevision.goal_id == goal_id,
                                                   GoalRevision.source_message_id == message_id,
                                                   GoalRevision.action == "user_input"))
    changes = {"content": content, "input_kind": kind}
    if previous:
        if previous.changes != changes:
            raise GoalConflict("同一用户输入编号不能绑定不同内容")
        return snapshot_goal(db, goal_id)
    _revision(db, goal, "user_input", "user", message_id, changes,
              expected_revision=expected_revision)
    db.flush()
    return register_input_contracts(db, goal_id, content, message_id, goal.revision)


def revise_goal(db: Session, goal_id: str, *, expected_revision: int, action: str,
                source: str = "user", source_message_id: str,
                requirements: list[dict] | None = None, requirement_ids: list[str] | None = None,
                reason: str = "") -> dict:
    if source != "user" or action not in USER_ACTIONS:
        raise GoalError("只有明确用户修订可以更改或取消目标要求")
    message_id = _text(source_message_id, "用户来源编号", limit=160)
    goal = _goal(db, goal_id)
    if goal.revision != expected_revision:
        raise GoalConflict("目标已更新，请刷新后重试")
    if goal.outcome not in OPEN_OUTCOMES:
        raise GoalError("已终结目标不能改写，请创建新的目标")
    rows = _requirements(db, goal_id)
    selected = [r for r in rows if r.id in (requirement_ids or [])]
    if action == "cancel_requirements" and (not requirement_ids or len(selected) != len(set(requirement_ids))):
        raise GoalError("必须指定当前目标的有效要求编号")
    additions = []
    if action in ("add_requirements", "append"):
        if not requirements:
            raise GoalError("必须提供新增要求")
        for item in requirements:
            if not isinstance(item, dict):
                raise GoalError("要求格式无效")
            title = _text(item.get("title"), "要求描述")
            phase = item.get("phase", "analyze")
            if phase not in ("search", "read", "analyze", "write", "audit", "final"):
                raise GoalError("要求阶段无效")
            if "required" in item and type(item["required"]) is not bool:
                raise GoalError("要求的必需标记必须为布尔值")
            if item.get("tool_name") is not None and (not isinstance(item["tool_name"], str)
                                                       or not item["tool_name"].strip()
                                                       or not isinstance(item.get("tool_arguments"), dict)):
                raise GoalError("工具要求必须提供工具名称和目标参数")
            additions.append({"title": title, "phase": phase,
                              "acceptance": item.get("acceptance"), "tool_name": item.get("tool_name"),
                              "tool_arguments": deepcopy(item.get("tool_arguments")),
                              "required": item.get("required", True) is not False})
    if action == "pause" and goal.control_state != "active":
        raise GoalError("仅进行中的目标可以暂停")
    if action == "resume" and goal.control_state != "paused":
        raise GoalError("仅已暂停的目标可以恢复")
    revision = _revision(db, goal, action, source, message_id,
                         {"requirement_ids": list(requirement_ids or []), "requirements": additions},
                         reason, expected_revision)
    now = utcnow()
    if action in ("cancel", "replace"):
        goal.outcome, goal.completed_at, goal.note = "cancelled", now, reason
        selected = [r for r in rows if r.status != "completed"]
    if action in ("cancel", "replace", "cancel_requirements"):
        for row in selected:
            row.status, row.cancelled_revision, row.updated_at = "cancelled", revision, now
    elif action in ("pause", "resume"):
        goal.control_state = "paused" if action == "pause" else "active"
    elif action == "confirm_complete":
        for row in rows:
            if row.status not in ("completed", "cancelled"):
                row.status = "completed"
                row.evidence = [*row.evidence, {"kind": "user_confirmation", "source_message_id": message_id,
                                               "revision": revision}]
        goal.outcome, goal.completed_at = "completed", now
    elif action in ("add_requirements", "append"):
        for item in additions:
            identity = _identity(item)
            existing = next((r for r in rows if r.identity == identity), None)
            if existing:
                if existing.origin == "model_plan":
                    # Only this explicit user action can promote a method to
                    # an obligation. Merely repeating it in a plan cannot.
                    existing.origin = "user"
                    existing.required = int(item["required"])
                    existing.source_message_id = message_id
                    existing.source_revision = revision
                    existing.updated_at = now
                    if existing.status == "superseded":
                        existing.status = "open"
                continue
            row = GoalRequirement(goal_id=goal_id, identity=identity, title=item["title"], phase=item["phase"],
                                  origin="user", source_message_id=message_id, source_revision=revision,
                                  required=int(item["required"]), acceptance=item["acceptance"],
                                  tool_name=item["tool_name"], tool_arguments=item["tool_arguments"])
            db.add(row)
            rows.append(row)
        goal.outcome, goal.completed_at = "open", None
    db.flush()
    return snapshot_goal(db, goal_id)


def _proof_matches(row, proof):
    """Re-use deterministic target validators; plain generated text is no proof."""
    if (not isinstance(proof, dict) or proof.get("kind") in (None, "delivery")
            or (proof.get("kind") == "answer" and row.origin != "model_plan")):
        return False
    from .agent import planner
    step = {"title": row.title, "phase": row.phase, "status": "pending", "evidence": [],
            "acceptance": row.acceptance, "tool_name": row.tool_name, "tool_arguments": row.tool_arguments}
    if row.acceptance:
        if row.acceptance.startswith("full_file:"):
            if not _valid_page_receipt(row, proof):
                return False
            pages = [p for p in row.evidence if p.get("kind") == "file_page"]
            step["evidence"] = deepcopy(pages)
            page = {"offset": proof.get("offset"), "next_offset": proof.get("end"),
                    "total_chars": proof.get("total_chars"), "sha256": proof.get("sha256")}
            if page["next_offset"] == page["total_chars"]:
                page["next_offset"] = None
            return planner.record_file_read([step], proof.get("path", ""), page)
        if row.acceptance == "project_file":
            return (proof.get("kind") in ("project_file", "tool_result")
                    and bool(proof.get("path")) and proof.get("status") == "applied")
        if row.acceptance == "successful_command":
            return (proof.get("kind") in ("command", "tool_result")
                    and proof.get("status") == "succeeded" and type(proof.get("exit_code")) is int
                    and proof["exit_code"] == 0)
        return False
    return planner.complete_one([step], row.phase, proof)


def register_input_contracts(db, goal_id, content, message_id, input_revision):
    """Register only server-recognized contracts from an existing user input.

    Model candidates never enter this function. The supplied text and revision
    must exactly match the goal's authoritative user provenance.
    """
    from .goal_candidates import input_candidates, contract_key, user_inputs
    snapshot = snapshot_goal(db, goal_id)
    if not any(item["source_message_id"] == message_id and item["source_revision"] == input_revision
               and item["content"] == content for item in user_inputs(snapshot)):
        raise GoalError("要求合同必须来自目标已保存的用户原话")
    goal = _goal(db, goal_id)
    rows = _requirements(db, goal_id)
    by_identity = {row.identity: row for row in rows}
    additions, cancellations = [], []
    import re
    correction = bool(re.search(r"改为|改成|改作|变为|instead|rather|change", content, re.I))
    for candidate in input_candidates(content, message_id, input_revision):
        contract = candidate.get("contract")
        if not contract:
            continue
        if contract["kind"] == "file_write_constraint":
            if not contract["allowed"]:
                cancellations.extend(row for row in rows if row.required and row.status not in ("completed", "cancelled")
                    and (row.acceptance == "project_file" or (row.tool_arguments or {}).get("contract", {}).get("kind") == "export_table_count"
                         or (row.tool_arguments or {}).get("result_contract", {}).get("kind") == "artifact_export_result"))
            continue
        if correction:
            cancellations.extend(row for row in rows if row.required and row.status not in ("completed", "cancelled")
                and (row.tool_arguments or {}).get("contract", {}).get("kind") == contract["kind"]
                and (row.tool_arguments or {}).get("contract") != contract)
        if any(row.status != "cancelled" and (row.tool_arguments or {}).get("contract") == contract for row in rows):
            continue
        step = {"acceptance": contract_key(contract) + ":" + hashlib.sha256(message_id.encode()).hexdigest()[:16]}
        identity = _identity(step)
        if identity in by_identity:
            continue
        row = GoalRequirement(goal_id=goal_id, identity=identity, title=candidate["quote"], phase="audit",
            origin="user_acceptance", required=1, source_message_id=message_id, source_revision=input_revision,
            acceptance=step["acceptance"], tool_arguments={"contract": contract,
                "source_span": {key: candidate[key] for key in ("start", "end", "quote")}}, evidence=[])
        additions.append(row)
        by_identity[identity] = row
    from .artifact_export_intents import export_clauses, PARSER_VERSION as export_parser
    from .goal_contracts import normalize
    for candidate in export_clauses(content, message_id, input_revision):
        spec = normalize(candidate["result_contract"])
        if correction:
            cancellations.extend(row for row in rows if row.required and row.status not in ("completed", "cancelled", "superseded")
                and (row.tool_arguments or {}).get("result_contract", {}).get("kind") == "artifact_export_result"
                and (row.tool_arguments or {})["result_contract"] != spec)
        if any(row.status not in ("cancelled", "superseded")
               and (row.tool_arguments or {}).get("result_contract") == spec for row in [*rows, *additions]):
            continue
        acceptance = "user_export_contract:" + hashlib.sha256(json.dumps([message_id, spec], sort_keys=True).encode()).hexdigest()
        identity = _identity({"acceptance": acceptance})
        if identity in by_identity:
            continue
        row = GoalRequirement(goal_id=goal_id, identity=identity, title=candidate["quote"], phase="write",
            origin="user_acceptance", required=1, source_message_id=message_id, source_revision=input_revision,
            acceptance=acceptance, tool_arguments={"result_contract": spec,
                "source_span": {key: candidate[key] for key in ("start", "end", "quote")}, "parser_version": export_parser}, evidence=[])
        additions.append(row)
        by_identity[identity] = row
    if additions or cancellations:
        revision = _revision(db, goal, "register_user_contracts", "user", message_id,
            {"candidate_parser": "user-clause-contracts/1", "source_revision": input_revision,
             "export_candidate_parser": export_parser,
             "added": [row.acceptance for row in additions], "cancelled": [row.id for row in cancellations]})
        for row in cancellations:
            row.status, row.cancelled_revision, row.updated_at = "cancelled", revision, utcnow()
        db.add_all(additions)
        db.flush()
    return snapshot_goal(db, goal_id)


def propose_requirement_candidates(db, goal_id, candidates):
    """Persist bounded model proposals with exact spans, never as requirements."""
    from .goal_candidates import user_inputs
    if not isinstance(candidates, list) or len(candidates) > 24:
        raise GoalError("每次最多登记24项要求候选")
    snapshot = snapshot_goal(db, goal_id)
    inputs = {item["source_message_id"]: item for item in user_inputs(snapshot)}
    proposed = []
    for item in candidates:
        allowed = {"source_message_id", "start", "end", "quote", "title"}
        if not isinstance(item, dict) or set(item) != allowed:
            raise GoalError("候选只能提供原话来源、区间、原句和候选标题，不能设置必需或完成状态")
        source = inputs.get(item["source_message_id"])
        start, end = item["start"], item["end"]
        if (source is None or type(start) is not int or type(end) is not int
                or not 0 <= start < end <= len(source["content"]) or end - start > 4000
                or not isinstance(item["quote"], str) or source["content"][start:end] != item["quote"]
                or not isinstance(item["title"], str) or not 1 <= len(item["title"].strip()) <= 160):
            raise GoalError("要求候选必须引用本目标用户输入的准确字符区间")
        proposed.append({**deepcopy(item), "source_revision": source["source_revision"],
                         "origin": "model_candidate", "required": False, "status": "unverified"})
    previous = snapshot["input_coverage"]["model_candidates"]
    additions = [item for item in proposed if item not in previous]
    if additions:
        _revision(db, _goal(db, goal_id), "propose_candidates", "model", snapshot["source_message_id"],
                  {"candidates": additions})
        db.flush()
    return snapshot_goal(db, goal_id)


def record_contract_tool_result(db, goal_id, name, arguments, result, operation_key):
    """Called only after the executor has verified an actual built-in receipt.

    These proofs deliberately cannot be supplied through plan/evidence APIs.
    Counting repeated calls does not prove source or output quantity.
    """
    if not isinstance(result, dict) or result.get("error"):
        return
    from .goal_receipts import record_tool
    record_tool(db, goal_id, name, result)
    for row in _requirements(db, goal_id):
        contract = (row.tool_arguments or {}).get("contract", {})
        if not row.required or row.status == "cancelled":
            continue
        proof = None
        if contract.get("kind") == "compare_source_count" and name in (
                "start_research_child", "resume_research_child", "read_research_child"):
            from .research_children import comparison_delivery
            goal = _goal(db, goal_id)
            proof = comparison_delivery(db, parent_run_id=goal.run_id, conversation_id=goal.conversation_id,
                child_run_id=result.get("run_id"), payload=result)
            if proof:
                proof["operation_key"] = operation_key
        elif contract.get("kind") == "compare_source_count" and name == "compare_papers":
            requested = arguments.get("paper_ids")
            papers = result.get("papers")
            if isinstance(requested, list) and isinstance(papers, list):
                ids = [paper.get("id") for paper in papers if isinstance(paper, dict)]
                if (len(ids) == len(papers) and all(isinstance(pid, str) and pid for pid in ids)
                        and all(isinstance(pid, str) for pid in requested)
                        and len(set(ids)) == len(ids) and set(ids) == set(requested)
                        and result.get("comparison_method") == "retrieved_passages"
                        and isinstance(result.get("rows"), list) and result["rows"]
                        and all(isinstance(item, dict) and set((item.get("cells") or {})) == set(ids) for item in result["rows"])):
                    proof = {"kind": "compared_source_set", "source_ids": ids, "operation_key": operation_key}
        elif (contract.get("kind") == "export_table_count" and name == "edit_project_file"
                and result.get("status") == "applied"):
            path = result.get("path")
            from .workspace import relative_path
            from fastapi import HTTPException
            try:
                requested_path = relative_path(arguments.get("path", ""))
            except HTTPException:
                requested_path = None
            if path == requested_path and isinstance(path, str) and path.lower().endswith((".csv", ".tsv")):
                import re
                after_hash = result.get("after_hash")
                valid = (arguments.get("operation") in ("create", "update") and isinstance(after_hash, str)
                         and re.fullmatch(r"[a-f0-9]{64}", after_hash) is not None)
                # The actual applied bytes may preserve a BOM/CRLF style. The
                # server receipt, not model-supplied content, identifies them.
                proof = {"kind": "exported_table_file" if valid else "export_invalidated",
                         "path": path.replace("\\", "/"), "operation_key": operation_key,
                         "sha256": after_hash if valid else None}
        if proof and proof not in row.evidence:
            _revision(db, _goal(db, goal_id), "record_contract_receipt", "execution", row.source_message_id,
                      {"requirement_id": row.id, "operation_key": operation_key})
            row.evidence = [*row.evidence, proof]
            row.updated_at = utcnow()
    db.flush()


def evaluate_input_contracts(db, goal_id, answer, *, workspace=None):
    """Only bounded structural coverage is hard-verified; meaning stays unverified."""
    from .goal_candidates import count_markdown_tables
    from .goal_receipts import evaluate
    evaluate(db, goal_id, answer)
    goal = _goal(db, goal_id)
    changed = []
    for row in _requirements(db, goal_id):
        contract = (row.tool_arguments or {}).get("contract", {})
        kind = contract.get("kind")
        if not row.required or row.status in ("cancelled", "superseded", "blocked") or kind not in (
                "answer_table_count", "export_table_count", "compare_source_count"):
            continue
        # Explicit user review is authoritative until the user reopens or
        # replaces that item. A later answer must not silently undo it.
        if row.evidence and row.evidence[-1].get("kind") == "user_confirmation":
            continue
        counts = []
        if kind == "answer_table_count":
            counts = [count_markdown_tables(answer)]
        elif kind == "export_table_count":
            latest = {proof["path"].casefold(): proof for proof in row.evidence
                      if proof.get("kind") in ("exported_table_file", "export_invalidated")}
            verified = []
            if workspace:
                from .project_changes import read_original
                from fastapi import HTTPException
                for proof in latest.values():
                    if proof["kind"] != "exported_table_file" or not proof.get("sha256"):
                        continue
                    try:
                        actual, actual_hash = read_original(workspace, proof["path"])
                    except (HTTPException, OSError):
                        continue
                    if actual_hash == proof["sha256"]:
                        import csv
                        import io
                        try:
                            cells = list(csv.reader(io.StringIO(actual or ""), delimiter="\t" if proof["path"].lower().endswith(".tsv") else ","))
                        except csv.Error:
                            cells = []
                        if len(cells) > 1 and len(cells[0]) > 1 and all(len(item) == len(cells[0]) for item in cells):
                            verified.append(proof["path"])
            counts = [len(verified)]
        else:
            for proof in row.evidence:
                if proof.get("kind") != "compared_source_set":
                    continue
                research = proof.get("research_delivery")
                if research:
                    from .research_children import comparison_delivery
                    current = comparison_delivery(db, parent_run_id=goal.run_id, conversation_id=goal.conversation_id,
                        child_run_id=research.get("child_run_id"))
                    if (not current or current["research_delivery"] != research or current["source_ids"] != proof.get("source_ids")
                            or current["field_keys"] != proof.get("field_keys") or current["source_versions"] != proof.get("source_versions")
                            or current["cell_count"] != proof.get("cell_count")
                            or current["matrix_snapshot_fingerprint"] != proof.get("matrix_snapshot_fingerprint")):
                        continue
                counts.append(len(proof["source_ids"]))
        passed = any(count >= contract["count"] if contract["operator"] == "at_least" else count == contract["count"] for count in counts)
        status = "completed" if passed else "open"
        if row.status != status:
            row.status, row.updated_at = status, utcnow()
            changed.append({"requirement_id": row.id, "status": status, "observed_counts": counts,
                            "verification": "structure_only", "semantic_support": "unverified"})
    if changed:
        _revision(db, goal, "evaluate_user_contracts", "execution", goal.source_message_id,
                  {"coverage": changed, "answer_sha256": hashlib.sha256(answer.encode()).hexdigest()})
    db.flush()
    return snapshot_goal(db, goal_id)


def _valid_page_receipt(row, proof):
    if not row.acceptance or not row.acceptance.startswith("full_file:") or proof.get("kind") != "file_page":
        return False
    path = str(proof.get("path") or "").replace("\\", "/").casefold()
    offset, end, total = proof.get("offset"), proof.get("end"), proof.get("total_chars")
    return (row.acceptance == f"full_file:{path}"
            and type(offset) is int and type(end) is int and type(total) is int
            and 0 <= offset <= end <= total and (end > offset or total == 0)
            and isinstance(proof.get("sha256"), str) and bool(proof["sha256"]))


def record_requirement_evidence(db: Session, goal_id: str, requirement_id: str, evidence: dict) -> dict:
    goal = _goal(db, goal_id)
    row = db.get(GoalRequirement, requirement_id)
    if row is None or row.goal_id != goal_id:
        raise GoalError("要求不属于当前目标")
    if goal.outcome == "cancelled" or row.status in ("cancelled", "superseded"):
        return snapshot_goal(db, goal_id)
    if not isinstance(evidence, dict):
        raise GoalError("完成依据格式无效")
    evidence = deepcopy(evidence)
    proved = _proof_matches(row, evidence)
    # Keep valid file-range receipts even if coverage is still incomplete.
    keep_page = _valid_page_receipt(row, evidence)
    if row.acceptance and row.acceptance.startswith("full_file:") and not keep_page:
        raise GoalError("文件范围凭据无效或不属于当前来源")
    if not proved and not keep_page:
        raise GoalError("该依据不能证明目标要求完成")
    if evidence in row.evidence:
        return snapshot_goal(db, goal_id)
    _revision(db, goal, "record_evidence", "execution", goal.source_message_id,
              {"requirement_id": row.id, "proof_kind": evidence.get("kind"), "completed": proved})
    row.evidence = [*row.evidence, evidence]
    row.updated_at = utcnow()
    if proved:
        row.status = "completed"
    db.flush()
    return snapshot_goal(db, goal_id)


def sync_plan_requirements(db: Session, goal_id: str, plan: list[dict], *, source_message_id=None,
                           trusted_user_acceptance=False) -> dict:
    """Track methods separately from authoritative user acceptance checks.

    Only the server's explicit-user acceptance extractor may set the trusted
    flag. A model plan (including its acceptance/required claims) is a method,
    not authorization to create or remove a user's obligation.
    """
    goal = _goal(db, goal_id)
    if goal.outcome in ("completed", "cancelled"):
        return snapshot_goal(db, goal_id)
    rows = _requirements(db, goal_id)
    by_identity = {r.identity: r for r in rows}
    additions = []
    active_identities = {_identity(s) for s in plan if isinstance(s, dict)
                         and s.get("title") and s.get("phase") != "final"}
    reclassified = []
    for row in rows:
        if row.origin != "model_plan":
            continue
        # Repair early ledger rows that incorrectly promoted every method.
        if row.required:
            row.required = 0
            reclassified.append(row.id)
        if row.identity not in active_identities and row.status == "open":
            row.status = "superseded"
            reclassified.append(row.id)
        elif row.identity in active_identities and row.status == "superseded":
            row.status = "open"
            reclassified.append(row.id)
    for step in plan:
        if not isinstance(step, dict) or not step.get("title") or step.get("phase") == "final":
            continue
        identity = _identity(step)
        if identity in by_identity:
            continue
        authoritative = bool(trusted_user_acceptance and step.get("acceptance"))
        row = GoalRequirement(goal_id=goal_id, identity=identity, title=step["title"],
                              phase=step.get("phase", "analyze"), origin="user_acceptance" if authoritative else "model_plan",
                              source_message_id=source_message_id or goal.source_message_id,
                              source_revision=goal.revision + 1, acceptance=step.get("acceptance"),
                              tool_name=step.get("tool_name"), tool_arguments=deepcopy(step.get("tool_arguments")),
                              status="open", evidence=[], required=int(authoritative))
        additions.append(row)
        by_identity[identity] = row
    if additions or reclassified:
        _revision(db, goal, "register_requirements", "model", source_message_id or goal.source_message_id,
                  {"identities": [r.identity for r in additions],
                   "reclassified_method_ids": list(dict.fromkeys(reclassified))})
        db.add_all(additions)
        db.flush()
    by_id = {r.id: r for r in [*rows, *additions]}
    for step in plan:
        if not isinstance(step, dict) or not step.get("title") or step.get("phase") == "final":
            continue
        row = by_identity.get(_identity(step))
        if row is None:
            continue
        # Multiple methods may bind the same same-goal user requirement, but
        # each proof must independently pass that requirement's validator.
        references = step.get("requirement_ids") or []
        if not isinstance(references, list):
            references = []
        references = [*references, step.get("requirement_id")]
        bindings = [by_id[ref] for ref in dict.fromkeys(ref for ref in references if isinstance(ref, str))
                    if ref in by_id and by_id[ref].required and by_id[ref].status != "cancelled"]
        step["requirement_id"] = row.id
        step["requirement_origin"] = row.origin
        step["requirement_required"] = bool(row.required)
        step["requirement_ids"] = [r.id for r in bindings if r.id != row.id]
        if row.status == "cancelled":
            continue
        for proof in step.get("evidence") or []:
            for target in [row, *bindings]:
                try:
                    record_requirement_evidence(db, goal_id, target.id, proof)
                except GoalConflict:
                    raise
                except GoalError:
                    # Model-returned status or prose is not execution evidence.
                    continue
    return snapshot_goal(db, goal_id)


def apply_goal_to_plan(snapshot: dict, plan: list[dict]) -> list[dict]:
    """Project canonical status onto existing steps without inventing a plan.

    Explicitly cancelled obligations leave the active plan; they remain in the
    immutable goal revision history. Omitted requirements remain in the ledger
    and final acceptance even if no visible step currently represents them.
    Caller assigns the returned list to state.todo and saves in its transaction.
    """
    by_id = {r["id"]: r for r in snapshot.get("requirements", [])}
    by_identity = {r["identity"]: r for r in snapshot.get("requirements", [])}
    projected = []
    for raw in plan:
        step = deepcopy(raw)
        identity = _identity(step)
        row = by_identity.get(identity)
        if row is None:
            # IDs alone cannot bind a different tool or target to old proof.
            candidate = by_id.get(step.get("requirement_id"))
            if candidate and candidate["identity"] == identity:
                row = candidate
        if row is None:
            projected.append(step)
            continue
        if row["status"] in ("cancelled", "superseded"):
            continue
        step["requirement_id"] = row["id"]
        step["requirement_origin"] = row["origin"]
        step["requirement_required"] = row["required"]
        if row["status"] == "completed":
            step["status"] = "done"
            step["evidence"] = deepcopy(row["evidence"])
        else:
            step["status"] = "in_progress" if step.get("status") == "in_progress" else "pending"
            step["evidence"] = deepcopy(row["evidence"])
        projected.append(step)
    return projected


def finalize_goal(db: Session, goal_id: str, *, evidence: dict | None = None) -> dict:
    """Summarize the durable ledger. An answer/delivery cannot complete it."""
    goal = _goal(db, goal_id)
    if goal.outcome == "cancelled":
        return snapshot_goal(db, goal_id)
    from .goal_receipts import refresh_export_references
    invalid_export = refresh_export_references(db, goal_id)
    if goal.outcome == "completed" and not invalid_export:
        return snapshot_goal(db, goal_id)
    rows = [r for r in _requirements(db, goal_id) if r.required]
    outstanding = [r for r in rows if r.status not in ("completed", "cancelled")]
    # Empty plans mean no structured verification, not proof of completion.
    outcome = "completed" if rows and not outstanding else "partial" if rows else "open"
    if goal.outcome != outcome:
        _revision(db, goal, "evaluate", "execution", goal.source_message_id,
                  {"outcome": outcome, "outstanding_ids": [r.id for r in outstanding]})
        goal.outcome = outcome
        goal.completed_at = utcnow() if outcome == "completed" else None
    db.flush()
    return snapshot_goal(db, goal_id)


def import_legacy_goals(db: Session, path: Path) -> int:
    """Validate all legacy data first. Never rewrite/remove the source file."""
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("目标历史必须为列表")
        prepared, ids = [], set()
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("历史目标格式无效")
            ident = _text(item.get("id"), "旧目标编号", limit=64)
            description = _text(item.get("description"), "旧目标描述")
            if ident in ids or item.get("status") not in ("active", "paused", "done", "cancelled"):
                raise ValueError("历史目标状态或编号无效")
            ids.add(ident)
            dates = {}
            for key in ("created_at", "updated_at", "completed_at"):
                value = item.get(key)
                dates[key] = (datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
                              .replace(tzinfo=None) if value else None)
            prepared.append((item, ident, description, dates))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise GoalMigrationError("旧目标历史无法安全迁移；原文件已保留，请先修复或恢复备份") from exc
    count = 0
    # Savepoint ensures a failed import does not leave a partially imported list.
    with db.begin_nested():
        for item, ident, description, dates in prepared:
            current = db.get(Goal, ident)
            if current:
                if current.scope != "global" or current.legacy_source != str(path.resolve()):
                    raise GoalMigrationError("旧目标编号与现有目标冲突；原文件已保留")
                continue
            snapshot = create_goal(db, description, scope="global", source_message_id=f"legacy:{ident}",
                                   goal_id=ident, source="legacy_import")
            goal = _goal(db, snapshot["id"])
            goal.legacy_source = str(path.resolve())
            goal.outcome = {"done": "completed", "cancelled": "cancelled"}.get(item["status"], "open")
            goal.control_state = "paused" if item["status"] == "paused" else "active"
            goal.note = str(item.get("note") or "")
            for key, value in dates.items():
                if value is not None:
                    setattr(goal, key, value)
            count += 1
        db.flush()
    return count
