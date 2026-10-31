"""人工审批端点：用户在前端批准/拒绝高风险工具调用。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import approval_store
from ..schemas import ApprovalDecision

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


@router.post("/decide")
def decide(req: ApprovalDecision):
    if req.decision not in ("approve", "deny"):
        raise HTTPException(400, "decision 必须是 approve 或 deny")
    if req.scope not in ("once", "session"):
        raise HTTPException(400, "scope 必须是 once 或 session")
    ok = approval_store.resolve(req.approval_id, req.decision, req.scope)
    if not ok:
        raise HTTPException(404, "审批不存在或已被处理")
    return {"approval_id": req.approval_id, "decision": req.decision, "scope": req.scope}
