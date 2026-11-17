"""Local account quota policy. No key input or external capability probes."""
from contextlib import contextmanager
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from ..db import get_db
from .. import account_quotas as store
from .model_profiles import _ProtectedInputRoute

router = APIRouter(prefix="/api/account-quotas", tags=["account-quotas"], route_class=_ProtectedInputRoute)


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0, strict=True)


class CreateInput(RevisionInput):
    name: str = Field(min_length=1, max_length=120)
    origin: str = Field(min_length=1, max_length=400)
    concurrency: int = Field(default=2, ge=1, le=64, strict=True)


class UpdateInput(RevisionInput):
    name: str = Field(min_length=1, max_length=120)
    concurrency: int = Field(ge=1, le=64, strict=True)
    state: Literal["active", "retired"] = "active"


class BindInput(RevisionInput):
    account_id: str = Field(min_length=1, max_length=96)
    config_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


@contextmanager
def errors(db):
    try:
        yield
    except store.QuotaPolicyError as exc:
        db.rollback()
        raise HTTPException(exc.status_code, str(exc)) from None


@router.get("")
def policy(db: Session = Depends(get_db)):
    with errors(db):
        return store.public_snapshot(db)


@router.post("/groups")
def create(req: CreateInput, db: Session = Depends(get_db)):
    with errors(db):
        return store.change(db, expected_revision=req.expected_revision, operation="create",
            fields=req.model_dump(exclude={"expected_revision"}))


@router.put("/groups/{group_id}")
def update(group_id: str, req: UpdateInput, db: Session = Depends(get_db)):
    with errors(db):
        return store.change(db, expected_revision=req.expected_revision, operation="update", group_id=group_id,
            fields=req.model_dump(exclude={"expected_revision"}))


@router.post("/groups/{group_id}/bind")
def bind(group_id: str, req: BindInput, db: Session = Depends(get_db)):
    with errors(db):
        return store.change(db, expected_revision=req.expected_revision, operation="bind", group_id=group_id,
            fields=req.model_dump(exclude={"expected_revision"}))
