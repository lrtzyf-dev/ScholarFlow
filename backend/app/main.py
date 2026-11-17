"""研序 ScholarFlow 后端入口。"""
from __future__ import annotations

import sys
import os
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
    run_inputs as run_inputs_api,
    call_ledger as call_ledger_api,
    sources as sources_api,
    backups as backups_api,
    service_tools as service_tools_api,
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
    model_profiles as model_profiles_api,
    skills as skills_api,
    translate,
    tracking as tracking_api,
    visualization,
    writing as writing_api,
)
from .config import BACKEND_DIR, settings
from .db import SessionLocal, init_db
from .models import Paper
from .schemas import HealthOut
from .runtime_owner import owner
from .local_api_guard import LocalAPIGuard
from .local_session import LocalInstanceMiddleware
from .maintenance import MaintenanceMiddleware
from .release_info import VERSION


def _frontend_dir() -> Path | None:
    """打包后前端位于 _internal/frontend；开发时位于 ../frontend/dist。"""
    if getattr(sys, "frozen", False):
        candidate = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "frontend"
    else:
        candidate = BACKEND_DIR.parent / "frontend" / "dist"
    return candidate if candidate.exists() else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    from . import run_store, task_runner, git_operations, git_process, terminal_sessions, project_commands, worktrees, workspace_transfers, next_turn_dispatch
    owner.begin_service()
    tracking_scheduler = None
    timing_started = False
    try:
        init_db()
        from .model_profiles import initialize_installation_profile
        initialize_installation_profile()
        from .resource_budget import recover_startup_reservations
        recover_startup_reservations()
        from . import run_timing
        run_timing.recover_startup()
        from .library_index_service import recover_startup as recover_library_indexes
        recover_library_indexes()
        from .research_runs import recover_startup as recover_research_runs
        recover_research_runs()
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
        from .tracking.scheduler import TrackingScheduler
        tracking_scheduler = TrackingScheduler()
        tracking_scheduler.start()
        app.state.tracking_scheduler = tracking_scheduler
        owner.ready()
        next_turn_dispatch.start()
        run_timing.start()
        timing_started = True
        yield
    finally:
        owner.stopping()
        next_turn_dispatch.stop()
        from . import backup_service
        await run_in_threadpool(backup_service.wait_shutdown)
        from . import library_index_service
        await run_in_threadpool(library_index_service.wait_shutdown)
        from . import research_runs
        await run_in_threadpool(research_runs.wait_shutdown)
        if tracking_scheduler is not None:
            tracking_scheduler.stop()
        try:
            run_store.cancel_all()
        finally:
            try:
                await run_in_threadpool(task_runner.shutdown_background_tasks)
            finally:
                await run_in_threadpool(git_process.shutdown)
                await run_in_threadpool(terminal_sessions.shutdown)
                from . import run_timing
                if timing_started:
                    await run_in_threadpool(run_timing.stop)
                owner.finish_shutdown(idle=not (task_runner.running_task_ids() or run_store.active_runs() or git_operations.active() or terminal_sessions.active() or worktrees.active() or workspace_transfers.active() or backup_service.active() or library_index_service.active() or research_runs.active() or run_timing.active()))


app = FastAPI(title="研序 ScholarFlow API", version=VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(papers.router)
app.include_router(agents_md_api.router)
app.include_router(chat.router)
app.include_router(run_inputs_api.router)
app.include_router(call_ledger_api.router)
from .api import resources as resources_api
app.include_router(resources_api.router)
from .api import pricing as pricing_api
app.include_router(pricing_api.router)
from .api import subtasks as subtasks_api
app.include_router(subtasks_api.router)
app.include_router(sources_api.router)
from .api import source_versions as source_versions_api
app.include_router(source_versions_api.router)
from .api import library_index as library_index_api
app.include_router(library_index_api.router)
from .api import tables as tables_api
app.include_router(tables_api.router)
app.include_router(backups_api.router)
from .api import storage as storage_api
app.include_router(storage_api.router)
app.include_router(service_tools_api.router)
from .api import provider_files as provider_files_api
app.include_router(provider_files_api.router)
from .api import artifacts as artifacts_api
app.include_router(artifacts_api.router)
app.include_router(graph.router)
from .api import research
app.include_router(research.router)
from .api import bibliography
app.include_router(bibliography.router)
from .api import zotero
app.include_router(zotero.router)
from .api import release as release_api
app.include_router(release_api.router)
app.include_router(translate.router)
app.include_router(tracking_api.router)
app.include_router(visualization.router)
app.include_router(writing_api.router)
app.include_router(settings_api.router)
app.include_router(model_profiles_api.router)
from .api import account_quotas as account_quotas_api
app.include_router(account_quotas_api.router)
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
from .api import memory_governance as memory_governance_api
app.include_router(memory_governance_api.router)
from .api import history_revisions as history_revisions_api
app.include_router(history_revisions_api.router)
from .api import run_center as run_center_api
app.include_router(run_center_api.router)
from .api import onboarding as onboarding_api
app.include_router(onboarding_api.router)
from .api import diagnostic_history as diagnostic_history_api
app.include_router(diagnostic_history_api.router)
app.include_router(foreground_runs_api.router)
app.include_router(desktop_state.router)
app.include_router(diagnostics_api.router)
app.include_router(goal_api.router)
from .api import projects
app.include_router(projects.router)
from .api import file_changes
app.include_router(file_changes.router)
from .api import execution_environments
app.include_router(execution_environments.router)
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
    from .model_profiles import default_profile_id, public_profile, ModelConfigurationError
    db = SessionLocal()
    try:
        count = db.query(Paper).count()
        try:
            selected = public_profile(db, default_profile_id(db), usable=True)
            mode, model = selected["mode"], selected["model_id"]
        except ModelConfigurationError:
            mode, model = "unconfigured", ""
    finally:
        db.close()
    return {
        "status": "ok",
        "llm_mode": mode,
        "embedding_mode": settings.embedding_mode,
        "llm_model": model,
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


# Keep authentication outside the SPA fallback as well as all API routes. A
# fallback response must not discard the document/bridge protection headers.
app.add_middleware(MaintenanceMiddleware)
app.add_middleware(LocalInstanceMiddleware)
app.add_middleware(LocalAPIGuard,
                   allow_development_origins=not getattr(sys, "frozen", False),
                   test_transport=not getattr(sys, "frozen", False)
                   and os.environ.get("SCHOLARFLOW_TEST_TRANSPORT") == "1")
