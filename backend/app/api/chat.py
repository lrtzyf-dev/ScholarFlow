"""Stream foreground work independently of the browser's connection lifetime."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from contextlib import closing
from datetime import datetime

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import StreamingResponse

from .. import cancel_store, run_store
from ..agent.loop import run_agent
from ..conversation_store import ConversationArchived, ConversationDeleted, check_conversation_available
from sqlalchemy import text
from ..db import SessionLocal
from ..foreground_run_store import finish as finish_foreground_run
from ..models import ForegroundRun
from ..schemas import ChatRequest
from ..execution_mode import PlanConflict, claim_reviewed_plan
from .. import workspace

router = APIRouter(prefix="/api/chat", tags=["chat"])


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: str = Path(pattern=r"^chat_[A-Za-z0-9_-]{8,64}$")):
    return {"run_id": run_id, "status": run_store.cancel(run_id)}


@router.post("/stream")
def chat_stream(req: ChatRequest):
    run_id = req.run_id or f"chat_{uuid.uuid4().hex}"
    with run_store.coordinated():
        with SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            try:
                check_conversation_available(db, req.conversation_id)
            except ConversationDeleted as exc:
                raise HTTPException(410, str(exc)) from exc
            except ConversationArchived as exc:
                raise HTTPException(409, str(exc)) from exc
            checkpoint = db.get(ForegroundRun, run_id)
            workspace.check_revision(db, req.conversation_id, req.workspace_revision)
            if checkpoint:
                scope = checkpoint.workspace
                workspace.validate_scope(db, req.conversation_id, scope)
                if (scope or {}).get("project_id") != req.project_id or (
                    not req.reviewed_plan_id and [f["path"] for f in (scope or {}).get("files", [])] != list(dict.fromkeys(req.file_paths))):
                    raise HTTPException(409, "项目或引用文件与前台检查点不匹配")
            else:
                scope = workspace.capture(db, req.conversation_id, req.project_id, req.file_paths, req.reviewed_plan_id, req.workspace_revision)
            if req.reviewed_plan_id and checkpoint is None:
                raise HTTPException(409, "执行已审阅计划前必须创建前台检查点")
            if checkpoint and (checkpoint.execution_mode, checkpoint.reviewed_plan_id, checkpoint.conversation_id) != (
                    req.execution_mode, req.reviewed_plan_id, req.conversation_id):
                raise HTTPException(409, "执行模式、计划来源或会话与前台检查点不匹配")
            if checkpoint:
                if checkpoint.status != "running" or checkpoint.started_at is not None:
                    raise HTTPException(409, "此轮次已启动或结束，请明确选择重新提问")
                checkpoint.started_at = datetime.utcnow()
            try:
                claim_reviewed_plan(db, req.reviewed_plan_id, req.conversation_id, run_id, req.retry_plan_run_id)
            except PlanConflict as exc:
                raise HTTPException(409, str(exc)) from exc
            if not run_store.begin(run_id, req.conversation_id):
                raise HTTPException(409, "执行 ID 已使用，请新建一轮请求")
            try:
                db.commit()
            except Exception:
                run_store.finish(run_id, "failed")
                raise

    frames: queue.Queue[str | None] = queue.Queue(maxsize=64)
    disconnected = threading.Event()

    def publish(frame: str | None) -> bool:
        while not disconnected.is_set():
            try:
                frames.put(frame, timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def worker():
        status = "done"
        heartbeat_stop = threading.Event()

        def heartbeat():
            while not heartbeat_stop.wait(5):
                try:
                    with SessionLocal() as heartbeat_db:
                        run = heartbeat_db.get(ForegroundRun, run_id)
                        if run is None or run.status != "running":
                            return
                        run.heartbeat_at = datetime.utcnow()
                        heartbeat_db.commit()
                except Exception:
                    continue

        heartbeat_thread = threading.Thread(target=heartbeat, name=f"{run_id}-heartbeat", daemon=True)
        heartbeat_thread.start()
        try:
            with SessionLocal() as db:
                with closing(run_agent(
                    db, req.question, req.paper_id, selected_skills=req.skills,
                    conversation_id=req.conversation_id, images=req.images, run_id=run_id,
                    execution_mode=req.execution_mode, reviewed_plan_id=req.reviewed_plan_id,
                    workspace=scope,
                )) as source:
                    for kind, event in source:
                        if kind == "cancelled":
                            status = "cancelled"
                        elif kind == "error":
                            status = "failed"
                        if not publish(f"event: {kind}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"):
                            break
            if cancel_store.is_cancelled(run_id):
                status = "cancelled"
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            event = {"kind": "error", "text": f"{type(exc).__name__}: {exc}"}
            publish(f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n")
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=1)
            terminal = status if status in ("cancelled", "failed") else "failed"
            try:
                with SessionLocal() as db:
                    finish_foreground_run(db, run_id, terminal,
                                          "服务端流在提交回答前结束。" if terminal == "failed" else "")
                    db.commit()
            except Exception:
                pass
            run_store.finish(run_id, status)
            publish(None)

    async def stream():
        started = False
        try:
            threading.Thread(target=worker, name=run_id, daemon=True).start()
            started = True
            yield f"event: run\ndata: {json.dumps({'kind': 'run', 'run_id': run_id})}\n\n"
            while True:
                try:
                    frame = await asyncio.to_thread(frames.get, True, 0.25)
                except queue.Empty:
                    continue
                if frame is None:
                    break
                yield frame
        finally:
            disconnected.set()
            if started:
                run_store.cancel(run_id)
            else:
                run_store.finish(run_id, "failed")

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-ID": run_id,
    })
