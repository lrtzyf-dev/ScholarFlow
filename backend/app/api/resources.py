"""Scoped composer budget defaults, active reservations and queue visibility."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from ..db import get_db
from .. import resource_budget, resource_policy
from ..resource_models import ResourceBudget
from ..runtime_models import RuntimeRun
from ..model_profiles import require_conversation, ModelConfigurationError
from ..request_resources import scheduler

router = APIRouter(prefix="/api/conversation-resources", tags=["resources"])


class PolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)
    currency: str = "USD"
    amount_limit: str | None = None
    request_estimate: str | None = None
    active_seconds_limit: float | None = None


class IncreaseInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)
    amount_limit: str | None = None
    active_seconds_limit: float | None = None


def require(db, cid, write=False):
    try:
        require_conversation(db, cid, write=write)
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


def own_budget(db, cid, budget_id):
    budget = db.get(ResourceBudget, budget_id)
    run = db.get(RuntimeRun, budget.owner_run_id) if budget else None
    if not run or run.conversation_id != cid:
        raise HTTPException(404, "当前会话中没有此任务预算")
    return budget


@router.get("/{cid}")
def get_resources(cid: str, db: Session = Depends(get_db)):
    require(db, cid)
    runs = list(db.scalars(select(RuntimeRun).where(RuntimeRun.conversation_id == cid)
                          .order_by(RuntimeRun.updated_at.desc()).limit(20)))
    budgets = list(db.scalars(select(ResourceBudget).where(ResourceBudget.owner_run_id.in_([run.id for run in runs])))) if runs else []
    # Independent resource snapshots perform no writes. Do not hold this read
    # Session through a future mutation of the budget's short transaction.
    ids = [row.id for row in budgets]
    queue = [entry for run in runs for entry in scheduler.snapshot(run.id)["items"]]
    from ..run_timing import snapshot as timing_snapshot
    return {**resource_policy.snapshot_policy(db, cid),
        "budgets": [resource_budget.budget_snapshot(identity) for identity in ids],
        "queue": queue, "history_limit": 20,
        "timings": [timing_snapshot(db, run) for run in runs],
        "time_scope": "sum_of_provider_request_seconds", "remote_hard_cap": False}


@router.put("/{cid}")
def set_resources(cid: str, body: PolicyInput, db: Session = Depends(get_db)):
    require(db, cid, write=True)
    try:
        saved = resource_policy.update_policy(db, cid, body.model_dump(exclude={"expected_revision"}), body.expected_revision)
        db.commit()
        return saved
    except (resource_budget.ResourceBudgetError, IntegrityError) as exc:
        db.rollback()
        raise HTTPException(409 if isinstance(exc, IntegrityError) or "已变化" in str(exc) else 422,
                            "预算设置已变化，请重新载入" if isinstance(exc, IntegrityError) else str(exc)) from None


@router.put("/{cid}/budgets/{budget_id}")
def increase_resources(cid: str, budget_id: str, body: IncreaseInput, db: Session = Depends(get_db)):
    require(db, cid, write=True)
    own_budget(db, cid, budget_id)
    db.rollback()
    try:
        return resource_budget.increase_budget(budget_id, amount_limit=body.amount_limit,
            active_seconds_limit=body.active_seconds_limit, expected_revision=body.expected_revision)
    except resource_budget.ResourceBudgetError as exc:
        raise HTTPException(409 if "已变化" in str(exc) else 422, str(exc)) from None
