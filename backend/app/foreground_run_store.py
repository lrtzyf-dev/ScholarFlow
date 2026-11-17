"""Database transitions shared by foreground SSE and final-answer delivery."""
from datetime import datetime
from copy import deepcopy

from sqlalchemy.orm import Session

from .models import ForegroundRun
from .timeutil import utcnow


def finish(db: Session, run_id: str | None, status: str, error: str = "", *,
           attempt_id: str | None = None, writer_token: str | None = None) -> None:
    if not run_id:
        return
    if attempt_id:
        from .run_lifecycle import assert_writer
        assert_writer(db, run_id, attempt_id, writer_token)
    run = db.get(ForegroundRun, run_id)
    if not run or run.status != "running":
        return
    run.status = status
    run.error = error[:2000]
    run.updated_at = utcnow()
    run.heartbeat_at = run.updated_at
    run.revision += 1


def save_agent_checkpoint(db: Session, run_id: str | None, snapshot: dict) -> None:
    """Persist the private executor state at each durable boundary.

    The UI checkpoint intentionally exposes only partial answer/progress fields;
    this full snapshot stays in the database and is never serialized by _snapshot.
    """
    if not run_id:
        return
    from .run_lifecycle import save_checkpoint
    save_checkpoint(db, run_id, snapshot)
    run = db.get(ForegroundRun, run_id)
    if run is None:
        # Direct API callers may omit the UI-owned foreground row.
        db.commit()
        return
    if run.status == "dismissed":
        return
    now = utcnow()
    run.agent_checkpoint = snapshot
    if snapshot.get("runtime_config"):
        run.runtime_config = deepcopy(snapshot["runtime_config"])
    run.phase = snapshot.get("phase", run.phase)
    run.updated_at = now
    run.heartbeat_at = now
    run.revision += 1
    db.commit()
