"""Local tariff management and conversation-scoped charge reconciliation."""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..pricing_models import PricingVersion, CallPriceAssessment, CallChargeConfirmation
from ..ledger_models import CallRecord
from ..models import Conversation, DeletedConversation, ForegroundRun, TaskRecord
from ..runtime_models import RuntimeRun
from .. import pricing, call_ledger
from ..resource_budget import _write, ResourceBudgetError

router = APIRouter(prefix='/api/pricing', tags=['pricing'])


class Component(BaseModel):
    model_config = ConfigDict(extra='forbid')
    unit: str
    subtract_units: list[str] = Field(default_factory=list)
    rate: str
    per: str = '1'


class VersionIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str
    endpoint: str
    service: str
    model_id: str | None = None
    profile_id: str | None = None
    currency: str
    components: list[Component]
    informational_units: list[str] = Field(default_factory=list)
    complete_contract: bool = False
    source_reference: str


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=0, strict=True)
    amount: str
    currency: str
    reference: str


def own_call(db, cid, call_id, *, write=False):
    if write:
        from ..model_profiles import require_conversation, ModelConfigurationError
        try:
            require_conversation(db, cid, write=True)
        except ModelConfigurationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
    if db.get(DeletedConversation, cid):
        raise HTTPException(410, '会话已删除')
    if not db.get(Conversation, cid):
        raise HTTPException(404, '会话不存在')
    call = db.get(CallRecord, call_id)
    run = db.get(RuntimeRun, call.run_id) if call and call.run_id else None
    if run is None and call and call.run_id:
        run = db.get(ForegroundRun, call.run_id) or db.get(TaskRecord, call.run_id)
    if not run or run.conversation_id != cid:
        raise HTTPException(404, '此会话中没有该调用')
    return call


@router.get('/versions')
def versions(db: Session = Depends(get_db)):
    from ..call_ledger import UNITS
    return {'items': [pricing.public_version(row) for row in db.scalars(select(PricingVersion).order_by(PricingVersion.created_at.desc()))],
            'units': sorted(UNITS - {'total_tokens'}), 'automatic_prices': False,
            'notice': '用户填写价目用于估算，供应商账单需另行核对；旧调用保留请求开始时的版本'}


@router.post('/versions')
def create(body: VersionIn):
    try:
        with _write() as db:
            return pricing.create_version(db, body.model_dump())
    except (pricing.PricingError, call_ledger.CallLedgerError) as exc:
        raise HTTPException(422, str(exc)) from None


@router.post('/versions/{id}/disable')
def disable(id: str):
    with _write() as db:
        row = db.get(PricingVersion, id)
        if not row:
            raise HTTPException(404, '价目版本不存在')
        row.active = 0
        return pricing.public_version(row)


@router.get('/conversations/{cid}/calls')
def calls(cid: str, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=1000000), db: Session = Depends(get_db)):
    if db.get(DeletedConversation, cid):
        raise HTTPException(410, '会话已删除')
    if not db.get(Conversation, cid):
        raise HTTPException(404, '会话不存在')
    ids = set(db.scalars(select(RuntimeRun.id).where(RuntimeRun.conversation_id == cid)))
    ids.update(db.scalars(select(ForegroundRun.id).where(ForegroundRun.conversation_id == cid)))
    ids.update(db.scalars(select(TaskRecord.id).where(TaskRecord.conversation_id == cid, TaskRecord.type == 'agent')))
    rows = list(db.scalars(select(CallRecord).where(CallRecord.run_id.in_(ids), CallRecord.synthetic == 0)
        .order_by(CallRecord.created_at.desc(), CallRecord.id.desc()).offset(offset).limit(limit + 1))) if ids else []
    return {'items': [call_ledger._snapshot(row, db) for row in rows[:limit]], 'has_more': len(rows) > limit, 'offset': offset, 'limit': limit}


@router.get('/conversations/{cid}/calls/{call_id}')
def details(cid: str, call_id: str, db: Session = Depends(get_db)):
    call = own_call(db, cid, call_id)
    assessment = db.get(CallPriceAssessment, call_id)
    confirmations = list(db.scalars(select(CallChargeConfirmation).where(CallChargeConfirmation.call_id == call_id).order_by(CallChargeConfirmation.revision)))
    return {**call_ledger._snapshot(call, db), 'tariff': assessment.tariff_snapshot if assessment else None,
        'confirmations': [{'revision': row.revision, 'amount': row.amount, 'currency': row.currency,
                           'reference': row.reference, 'created_at': row.created_at.isoformat()} for row in confirmations]}


@router.post('/conversations/{cid}/calls/{call_id}/confirm')
def confirm(cid: str, call_id: str, body: ConfirmIn):
    try:
        with _write() as db:
            call = own_call(db, cid, call_id, write=True)
            return pricing.confirm_call(db, call, **body.model_dump())
    except (pricing.PricingError, ResourceBudgetError) as exc:
        raise HTTPException(409 if '已变化' in str(exc) else 422, str(exc)) from None
