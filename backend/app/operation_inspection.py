"""Inspect returned unknown operations in their original foreground/background run."""
from uuid import uuid4

from fastapi import HTTPException

from .agent.checkpoint import AgentState
from .operation_receipts import public_receipt, signature
from .subtask_inspection import parent, _active, _arguments, _text
from .timeutil import utcnow


def listing(db, run_id):
    logical, row, checkpoint = parent(db, run_id)
    try:
        state = AgentState.model_validate(checkpoint)
    except ValueError:
        raise HTTPException(409, "执行检查点格式不可核对") from None
    active = _active(logical, row)
    items = []
    for failure in state.tool_failures:
        if not failure.get("execution_unknown"):
            continue
        receipts = failure.get("reconciliations") or []
        consumed = failure.get("retry_consumptions") or []
        last = receipts[-1] if receipts else None
        items.append({"operation_key": failure["operation_key"], "signature": failure["signature"],
            "tool_name": _text(failure["tool_name"], 160), "arguments": _arguments(failure.get("arguments", {})),
            "error": _text(failure.get("error"), 2000), "status": failure.get("status"),
            "provider_verified": False, "reported_state": last["action"] if last else "unknown",
            "retry_consumed": bool(last and any(c.get("reconciliation_id") == last["id"] for c in consumed)),
            "reconciliations": [public_receipt(receipt) for receipt in receipts]})
    return {"parent": {"id": run_id, "revision": logical.revision,
        "attempt_id": logical.current_attempt_id, "active": active, "kind": logical.kind},
        "items": items, "can_reconcile": not active,
        "notice": "核对由用户提供，供应商未验证。已执行声明不生成成功证据；未执行只允许原操作重试一次，仍须恢复原任务并通过当前权限检查。"}


def reconcile(db, run_id, body):
    logical, row, checkpoint = parent(db, run_id, write=True)
    state = AgentState.model_validate(checkpoint)
    failure = next((item for item in state.tool_failures if item.get("operation_key") == body["operation_key"]
        and item.get("signature") == body["signature"] and item.get("execution_unknown")), None)
    if failure is None or signature(failure["tool_name"], failure["arguments"]) != body["signature"]:
        raise HTTPException(409, "未确认的操作已变化，请重新读取后核对")
    receipts = failure.setdefault("reconciliations", [])
    # A lost response returns the same immutable user declaration, even if
    # its CAS revision is now old. Another body cannot reuse its identity.
    for candidate in state.tool_failures:
        for receipt in candidate.get("reconciliations", []):
            if receipt["request_id"] == body["request_id"]:
                expected = {key: body[key] for key in ("operation_key", "signature", "action", "reference")}
                if (any(receipt.get(key) != value for key, value in expected.items())
                        or receipt["attempt_id"] != body["expected_attempt_id"]
                        or receipt.get("expected_parent_revision") != body["expected_parent_revision"]):
                    raise HTTPException(409, "核对请求编号已用于其他声明，历史不会被覆盖")
                return public_receipt(receipt)
    if failure.get("status") != "open":
        raise HTTPException(409, "此操作已有后续实际结果，不再接受重试声明")
    guard = state.memory_side_effects.get(body["signature"], {})
    if guard.get("operation_key") != failure["operation_key"] or not guard.get("execution_unknown"):
        raise HTTPException(409, "该操作之后已有新的执行记录，请核对最新操作")
    if _active(logical, row):
        raise HTTPException(409, "任务仍在运行，请先停止并等待结束后核对")
    if (logical.revision != body["expected_parent_revision"] or logical.current_attempt_id != body["expected_attempt_id"]
            or state.attempt_id != logical.current_attempt_id):
        raise HTTPException(409, "任务已被更新或接管，请重新读取后核对")
    if state.pending_operation:
        raise HTTPException(409, "另有中断步骤待确认，请先在原恢复入口处理")
    reference = body["reference"]
    if not reference.strip() or reference != _text(reference):
        raise HTTPException(422, "请填写不含凭证的核对依据")
    receipt = {"id": uuid4().hex, "request_id": body["request_id"], "attempt_id": logical.current_attempt_id,
        "expected_parent_revision": body["expected_parent_revision"],
        "operation_key": failure["operation_key"], "signature": failure["signature"], "action": body["action"],
        "reference": reference, "created_at": utcnow().isoformat(), "provider_verified": False}
    receipts.append(receipt)
    # Never clear the original unknown fact, alter operations, mark a failure
    # resolved, or complete a goal based only on this declaration.
    if logical.kind == "foreground":
        row.agent_checkpoint = state.snapshot()
        row.revision += 1
    else:
        row.checkpoint = state.snapshot()
    row.updated_at = utcnow()
    logical.revision += 1
    logical.updated_at = utcnow()
    db.flush()
    return public_receipt(receipt)
