"""后台任务端点：发起/列表/详情/恢复。"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..db import SessionLocal, get_db
from .. import task_events, task_runner
from ..conversation_store import ConversationArchived, ConversationDeleted
from ..execution_mode import ExecutionOptions, PlanConflict

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


class StartTaskRequest(ExecutionOptions):
    question: str
    paper_id: str | None = None
    skills: list[str] | None = None
    conversation_id: str | None = None
    title: str = ""


@router.post("/start")
def start(req: StartTaskRequest, db: Session = Depends(get_db)):
    if not req.question.strip():
        raise HTTPException(400, "问题不能为空")
    try:
        return task_runner.start_agent_task(
            db, req.question, req.paper_id, req.skills,
            req.conversation_id, req.title,
            execution_mode=req.execution_mode, reviewed_plan_id=req.reviewed_plan_id, retry_plan_run_id=req.retry_plan_run_id,
            project_id=req.project_id, file_paths=req.file_paths,
            workspace_revision=req.workspace_revision,
        )
    except ConversationDeleted as exc:
        raise HTTPException(410, str(exc)) from exc
    except ConversationArchived as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlanConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except task_runner.TaskConflict as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


@router.get("")
def list_all(conversation_mode: Literal["full", "signal"] = "full", db: Session = Depends(get_db)):
    return task_runner.task_snapshot(db, conversation_mode)


@router.get("/history")
def history(cursor: str | None = Query(default=None, max_length=512),
            limit: int = Query(default=30, ge=1, le=100), db: Session = Depends(get_db)):
    before = None
    if cursor:
        try:
            value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
            if not isinstance(value, list) or len(value) != 2 or not isinstance(value[1], str) or not 1 <= len(value[1]) <= 64:
                raise ValueError()
            at = datetime.fromisoformat(value[0])
            if at.tzinfo is not None:
                raise ValueError()
            before = (at, value[1])
        except (ValueError, TypeError, UnicodeError) as exc:
            raise HTTPException(422, "任务分页游标无效，请刷新列表") from exc
    return task_runner.task_history(db, limit, before)


@router.get("/events")
async def events(conversation_mode: Literal["full", "signal"] = "full"):
    """以 SSE 推送后台任务状态变化，前端断线后由 EventSource 自动重连。"""

    def snapshot():
        with SessionLocal() as db:
            return json.dumps(
                task_runner.task_snapshot(db, conversation_mode),
                ensure_ascii=False, separators=(",", ":"),
            )

    async def stream():
        last_payload = ""
        async with task_events.subscribe() as updates:
            while True:
                payload = await run_in_threadpool(snapshot)
                if payload != last_payload:
                    yield f"event: tasks\ndata: {payload}\n\n"
                    last_payload = payload
                try:
                    await asyncio.wait_for(updates.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{task_id}")
def detail(task_id: str, db: Session = Depends(get_db)):
    task = task_runner.get_task(db, task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


class ResumeRequest(BaseModel):
    resolution: Literal["retry", "skip"] | None = None


@router.post("/{task_id}/resume")
def resume(task_id: str, req: ResumeRequest | None = None, db: Session = Depends(get_db)):
    try:
        task = task_runner.resume_task(db, task_id, req.resolution if req else None)
    except ConversationDeleted as exc:
        raise HTTPException(410, str(exc)) from exc
    except ConversationArchived as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlanConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except task_runner.TaskConflict as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if "id" not in task:
        raise HTTPException(404, task["error"])
    return task


@router.post("/{task_id}/restart")
def restart(task_id: str, db: Session = Depends(get_db)):
    try:
        task = task_runner.restart_task(db, task_id)
    except ConversationDeleted as exc:
        raise HTTPException(410, str(exc)) from exc
    except ConversationArchived as exc:
        raise HTTPException(409, str(exc)) from exc
    except PlanConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except task_runner.TaskConflict as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    if "id" not in task:
        raise HTTPException(404, task["error"])
    return task


@router.post("/{task_id}/cancel")
def cancel(task_id: str, db: Session = Depends(get_db)):
    task = task_runner.cancel_task(db, task_id)
    if "id" not in task:
        raise HTTPException(404, task["error"])
    return task
