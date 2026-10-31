"""研序 ScholarFlow 后端入口。"""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .api import (
    agents_md as agents_md_api,
    approvals,
    conversations as conversations_api,
    desktop_state,
    diagnostics as diagnostics_api,
    foreground_runs as foreground_runs_api,
    hooks as hooks_api,
    review,
    runtime,
    final_check,
    export as export_api,
    goal as goal_api,
    tasks as tasks_api,
    chat,
    graph,
    mcp as mcp_api,
    papers,
    samples,
    settings as settings_api,
    skills as skills_api,
)
from .config import BACKEND_DIR, settings
from .db import SessionLocal, init_db
from .models import Paper
from .schemas import HealthOut
from .runtime_owner import owner


def _frontend_dir() -> Path | None:
    """打包后前端位于 _internal/frontend；开发时位于 ../frontend/dist。"""
    if getattr(sys, "frozen", False):
        candidate = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "frontend"
    else:
        candidate = BACKEND_DIR.parent / "frontend" / "dist"
    return candidate if candidate.exists() else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import run_store, task_runner, git_operations, git_process, terminal_sessions, project_commands, worktrees, workspace_transfers
    owner.begin_service()
    try:
        init_db()
        workspace_transfers.recover_interrupted()
        with SessionLocal() as db:
            task_runner.recover_interrupted_tasks(db)
        from .project_changes import recover_interrupted
        recover_interrupted()
        git_operations.recover_interrupted()
        worktrees.recover_interrupted()
        project_commands.recover_interrupted()
        git_process.start()
        terminal_sessions.start()
        owner.ready()
        yield
    finally:
        owner.stopping()
        try:
            run_store.cancel_all()
        finally:
            try:
                await run_in_threadpool(task_runner.shutdown_background_tasks)
            finally:
                await run_in_threadpool(git_process.shutdown)
                await run_in_threadpool(terminal_sessions.shutdown)
                owner.finish_shutdown(idle=not (task_runner.running_task_ids() or run_store.active_runs() or git_operations.active() or terminal_sessions.active() or worktrees.active() or workspace_transfers.active()))


app = FastAPI(title="研序 ScholarFlow API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(papers.router)
app.include_router(agents_md_api.router)
app.include_router(chat.router)
app.include_router(graph.router)
app.include_router(settings_api.router)
app.include_router(mcp_api.router)
app.include_router(skills_api.router)
app.include_router(samples.router)
app.include_router(approvals.router)
app.include_router(hooks_api.router)
app.include_router(review.router)
app.include_router(runtime.router)
app.include_router(final_check.router)
app.include_router(export_api.router)
app.include_router(tasks_api.router)
app.include_router(conversations_api.router)
app.include_router(foreground_runs_api.router)
app.include_router(desktop_state.router)
app.include_router(diagnostics_api.router)
app.include_router(goal_api.router)
from .api import projects
app.include_router(projects.router)
from .api import file_changes
app.include_router(file_changes.router)
from .api import project_git
app.include_router(project_git.router)
from .api import terminals
app.include_router(terminals.router)
from .api import project_commands
app.include_router(project_commands.router)
from .api import worktrees
app.include_router(worktrees.router)


@app.get("/api/health", response_model=HealthOut)
def health():
    db = SessionLocal()
    try:
        count = db.query(Paper).count()
    finally:
        db.close()
    return {
        "status": "ok",
        "llm_mode": settings.llm_mode,
        "embedding_mode": settings.embedding_mode,
        "llm_model": settings.llm_model,
        "paper_count": count,
    }


# ---- 前端静态托管（单进程，同源）----
FRONTEND_DIR = _frontend_dir()
if FRONTEND_DIR is not None:
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.middleware("http")
    async def spa_fallback(request, call_next):
        if request.url.path.startswith("/api"):
            return await call_next(request)
        response = await call_next(request)
        if response.status_code == 404 and request.method == "GET":
            return FileResponse(FRONTEND_DIR / "index.html")
        return response

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
