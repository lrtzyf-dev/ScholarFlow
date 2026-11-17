"""Read-only, conversation-scoped request facts and usage totals."""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..call_ledger import list_calls, summarize_calls
from ..db import get_db
from ..models import Conversation, DeletedConversation, ForegroundRun, TaskRecord
from ..runtime_models import RunAttempt

router = APIRouter(prefix="/api/chat/runs", tags=["call-ledger"])


@router.get("/{run_id}/calls")
def run_calls(run_id: str,
              conversation_id: str = Query(min_length=1, max_length=64),
              attempt_id: str | None = Query(default=None, min_length=1, max_length=96),
              limit: int = Query(default=50, ge=1, le=200),
              offset: int = Query(default=0, ge=0, le=1_000_000),
              include_synthetic: bool = False, db: Session = Depends(get_db)):
    if db.get(DeletedConversation, conversation_id):
        raise HTTPException(410, "该会话已删除")
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(404, "未找到该会话")
    run = db.get(ForegroundRun, run_id)
    if run is None:
        run = db.get(TaskRecord, run_id)
        if run is not None and run.type != "agent":
            run = None
    if run is None or run.conversation_id != conversation_id:
        raise HTTPException(404, "未找到该会话中的运行")
    if attempt_id:
        attempt = db.get(RunAttempt, attempt_id)
        if attempt is None or attempt.run_id != run_id:
            raise HTTPException(404, "未找到该运行中的执行尝试")
    filters = {"run_id": run_id, "attempt_id": attempt_id,
               "include_synthetic": include_synthetic, "db": db}
    return {"run_id": run_id, "conversation_id": conversation_id, "attempt_id": attempt_id,
            **list_calls(limit=limit, offset=offset, **filters),
            "summary": summarize_calls(**filters)}
