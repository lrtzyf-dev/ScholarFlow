"""Server-owned result checks. Exit code and structural tests are not semantics."""
from copy import deepcopy
import hashlib

from . import goal_ledger as ledger
from .goal_contracts import normalize
from .models import ProjectCommand
from .artifact_models import ArtifactManifest
from . import artifact_store
from .timeutil import utcnow


def _text_checks(text, spec):
    return {"required_present": all(term in text for term in spec.get("contains", [])),
            "forbidden_absent": all(term not in text for term in spec.get("excludes", []))}


def command_receipt(db, goal, command_id, spec):
    row = db.get(ProjectCommand, command_id, populate_existing=True) if isinstance(command_id, str) else None
    if row is None or row.conversation_id != goal.conversation_id or row.run_id != goal.run_id:
        return None
    checks = {"requested_command": row.command == spec["command"], "requested_directory": row.path == spec["path"],
        "execution_succeeded": row.status == "succeeded" and type(row.exit_code) is int and row.exit_code == 0,
        "complete_output": not row.output_limited, **_text_checks(row.output, spec)}
    return {"kind": "command_acceptance", "command_id": row.id, "command_revision": row.revision,
        "command_sha256": hashlib.sha256(row.command.encode()).hexdigest(),
        "output_sha256": hashlib.sha256(row.output.encode()).hexdigest(), "checks": checks,
        "passed": all(checks.values()), "verification": "execution_and_explicit_text_checks",
        "semantic_verification": "not_assessed"}


def artifact_receipt(db, goal, artifact_id, revision_id, spec):
    row = db.get(ArtifactManifest, artifact_id, populate_existing=True) if isinstance(artifact_id, str) else None
    if row is None or row.conversation_id != goal.conversation_id:
        return None
    from fastapi import HTTPException
    try:
        rev = artifact_store.revision(db, row, revision_id)
    except HTTPException:
        return None
    fixed = {(item.get("revision_id"), item.get("locator_id")) for item in rev.evidence
        if item.get("revision_id") and item.get("locator_id") and item.get("verification") == "source_frozen_semantics_unverified"}
    checks = {**_text_checks(rev.content, spec), "fixed_source_count": len(fixed) >= spec["min_fixed_sources"]}
    return {"kind": "artifact_acceptance", "artifact_id": row.id, "revision_id": rev.id,
        "manifest_sha256": rev.manifest_sha256, "fixed_source_count": len(fixed), "checks": checks,
        "count_scope": "distinct_revision_locator_pairs",
        "passed": all(checks.values()), "verification": "fixed_artifact_structure_and_explicit_text_checks",
        "semantic_verification": "not_assessed"}


def export_receipt(db, goal, export_id, spec):
    """Re-read the server-owned file; never accept a model's hash or verdict."""
    from fastapi import HTTPException
    from .artifact_models import ArtifactExport
    from . import artifact_exports
    exported = db.get(ArtifactExport, export_id, populate_existing=True) if isinstance(export_id, str) else None
    if (exported is None or exported.conversation_id != goal.conversation_id
            or exported.origin_goal_id != goal.id or not goal.run_id
            or exported.origin_run_id != goal.run_id):
        return None
    if (exported.format != spec["format"]
            or spec.get("artifact_id", exported.artifact_id) != exported.artifact_id
            or spec.get("revision_id", exported.revision_id) != exported.revision_id):
        return None
    try:
        validation = artifact_exports.validate(db, exported)
        artifact = artifact_receipt(db, goal, exported.artifact_id, exported.revision_id, spec)
    except (HTTPException, ValueError, TypeError):
        return None
    if artifact is None:
        return None
    checks = {**artifact["checks"], "container_readable": validation.get("container_readable") is True,
        "export_content_checked": validation.get("content_checked") is True,
        "source_binding_checked": validation.get("source_binding_checked") is True,
        "required_computation_recorded": not spec["requires_recorded_execution"] or validation.get("computation_recorded") is True}
    return {"kind": "artifact_export_acceptance", "export_id": exported.id,
        "artifact_id": exported.artifact_id, "revision_id": exported.revision_id,
        "format": exported.format, "payload_sha256": exported.payload_sha256,
        "payload_size": exported.payload_size, "manifest_sha256": exported.manifest_sha256,
        "fixed_source_count": artifact["fixed_source_count"], "count_scope": artifact["count_scope"],
        "checks": checks, "passed": all(checks.values()), "validation": deepcopy(validation),
        "download_url": artifact_exports.public(exported)["download_url"],
        "verification": "fixed_export_bytes_and_explicit_source_text_checks", "semantic_verification": "not_assessed"}


def _record(db, goal, row, proof, *, apply_status=True):
    if proof is None:
        return
    desired = "completed" if proof["passed"] else "open"
    if proof in row.evidence and (not apply_status or row.status == desired):
        return
    ledger._revision(db, goal, "result_contract_check", "execution", row.source_message_id,
        {"requirement_id": row.id, "proof": deepcopy(proof)})
    row.evidence = [*row.evidence, proof]
    row.updated_at = utcnow()
    if apply_status:
        row.status = desired


def _refresh_references(db, goal, row, spec):
    """Independent receipts do not invalidate one another. Recheck every ID."""
    references = {}
    for proof in row.evidence:
        if spec["kind"] == "command_result" and proof.get("kind") == "command_acceptance":
            references[(proof.get("command_id"),)] = proof
        elif spec["kind"] == "artifact_result" and proof.get("kind") == "artifact_acceptance":
            references[(proof.get("artifact_id"), proof.get("revision_id"))] = proof
        elif spec["kind"] == "artifact_export_result" and proof.get("kind") == "artifact_export_acceptance":
            references[(proof.get("export_id"),)] = proof
    results = []
    for old in references.values():
        if spec["kind"] == "command_result":
            proof = command_receipt(db, goal, old.get("command_id"), spec)
        elif spec["kind"] == "artifact_export_result":
            proof = export_receipt(db, goal, old.get("export_id"), spec)
        else:
            proof = artifact_receipt(db, goal, old.get("artifact_id"), old.get("revision_id"), spec)
        if proof is None:
            proof = {"kind": old["kind"], "passed": False, "reference_unavailable": True,
                **{key: old[key] for key in ("command_id", "artifact_id", "revision_id", "export_id") if key in old},
                "semantic_verification": "not_assessed"}
        results.append(proof)
        _record(db, goal, row, proof, apply_status=False)
    desired = "completed" if any(p["passed"] for p in results) else "open"
    if row.status != desired:
        ledger._revision(db, goal, "result_contract_state", "execution", row.source_message_id,
            {"requirement_id": row.id, "status": desired, "aggregation": "any_current_valid_matching_receipt"})
        row.status, row.updated_at = desired, utcnow()


def record_tool(db, goal_id, name, result):
    if not isinstance(result, dict) or result.get("error") or result.get("execution_unknown"):
        return
    goal = ledger._goal(db, goal_id)
    for row in ledger._requirements(db, goal_id):
        raw = (row.tool_arguments or {}).get("result_contract")
        if not row.required or row.status in ("cancelled", "superseded", "blocked") or not raw:
            continue
        if row.evidence and row.evidence[-1].get("kind") == "user_confirmation":
            continue
        spec, proof = normalize(raw), None
        if spec["kind"] == "command_result" and name == "run_project_command":
            proof = command_receipt(db, goal, result.get("command_id"), spec)
            if proof and not (proof["checks"]["requested_command"] and proof["checks"]["requested_directory"]):
                proof = None
        elif spec["kind"] == "artifact_result" and name == "save_research_artifact":
            # The real tool returns IDs, while a proposed edit has not been
            # accepted and therefore cannot count as a saved result.
            proof = artifact_receipt(db, goal, result.get("artifact_id"), result.get("revision_id"), spec)
        elif spec["kind"] == "artifact_export_result" and name == "export_research_artifact":
            proof = export_receipt(db, goal, result.get("export_id"), spec)
        if proof:
            _record(db, goal, row, proof, apply_status=False)
            _refresh_references(db, goal, row, spec)
    db.flush()


def evaluate(db, goal_id, answer):
    goal = ledger._goal(db, goal_id)
    for row in ledger._requirements(db, goal_id):
        raw = (row.tool_arguments or {}).get("result_contract")
        if not row.required or row.status in ("cancelled", "superseded", "blocked") or not raw:
            continue
        if row.evidence and row.evidence[-1].get("kind") == "user_confirmation":
            continue
        spec = normalize(raw)
        if spec["kind"] == "answer_terms":
            checks = _text_checks(answer, spec)
            proof = {"kind": "answer_acceptance", "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                "checks": checks, "passed": all(checks.values()), "verification": "explicit_text_checks",
                "semantic_verification": "not_assessed"}
            _record(db, goal, row, proof)
        elif spec["kind"] in ("command_result", "artifact_result", "artifact_export_result"):
            # Current DB facts, not a copied model/tool payload, decide whether
            # an earlier receipt remains valid. Keep history if invalidated.
            _refresh_references(db, goal, row, spec)
    db.flush()


def refresh_export_references(db, goal_id):
    """Finalization also rechecks already completed byte receipts."""
    from sqlalchemy import select
    from .goal_models import GoalRevision
    goal = ledger._goal(db, goal_id)
    latest_user_action = db.scalar(select(GoalRevision.action).where(GoalRevision.goal_id == goal_id,
        GoalRevision.source == "user").order_by(GoalRevision.revision.desc()).limit(1))
    if goal.outcome == "completed" and latest_user_action == "confirm_complete":
        return False  # Preserve an explicit whole-goal human confirmation.
    invalid = False
    for row in ledger._requirements(db, goal_id):
        raw = (row.tool_arguments or {}).get("result_contract") or {}
        if (not row.required or row.status in ("cancelled", "superseded", "blocked")
                or not isinstance(raw, dict) or raw.get("kind") != "artifact_export_result"
                or row.evidence and row.evidence[-1].get("kind") == "user_confirmation"):
            continue
        _refresh_references(db, goal, row, normalize(raw))
        invalid = invalid or row.status != "completed"
    # Ledger readers use populate_existing with autoflush disabled. Persist the
    # refreshed requirement state before finalization reads those rows again.
    db.flush()
    return invalid
