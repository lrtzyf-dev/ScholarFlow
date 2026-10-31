"""Database transitions shared by foreground SSE and final-answer delivery."""
from datetime import datetime

from sqlalchemy.orm import Session

from .models import ForegroundRun


def finish(db: Session, run_id: str | None, status: str, error: str = "") -> None:
    if not run_id:
        return
    run = db.get(ForegroundRun, run_id)
    if not run or run.status != "running":
        return
    run.status = status
    run.error = error[:2000]
    run.updated_at = datetime.utcnow()
    run.heartbeat_at = run.updated_at
    run.revision += 1
