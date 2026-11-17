"""Conversation defaults and one immutable budget binding per logical run.

These functions use the caller's Session exclusively. The run admission commits
before network work, so no nested SQLite writer can deadlock configuration save.
"""
from copy import deepcopy
from decimal import Decimal
import math
import re
from uuid import uuid4

from sqlalchemy import select, update
from .resource_budget import _money, ResourceBudgetError
from .resource_models import ConversationResourcePolicy, ResourceBudget
from .timeutil import utcnow


DEFAULT_POLICY = {"currency": "USD", "amount_limit": None, "request_estimate": None,
    "price_version": None, "active_seconds_limit": None}


def normalize_policy(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_POLICY):
        raise ResourceBudgetError("资源预算配置格式无效")
    policy = {**DEFAULT_POLICY, **value}
    if not re.fullmatch(r"[A-Z]{3}", policy["currency"] or ""):
        raise ResourceBudgetError("预算币种无效")
    for key in ("amount_limit", "request_estimate"):
        if policy[key] is not None:
            policy[key] = str(Decimal(_money(policy[key])) / 10**6)
    if policy["amount_limit"] is not None:
        if policy["request_estimate"] is None or Decimal(policy["request_estimate"]) <= 0:
            raise ResourceBudgetError("费用预算需要正数的单次请求预留估值")
        if Decimal(policy["request_estimate"]) > Decimal(policy["amount_limit"]):
            raise ResourceBudgetError("单次预留估值不能高于任务费用预算")
    if policy["request_estimate"] is not None:
        policy["price_version"] = "user-request-estimate-v1"
    else:
        policy["price_version"] = None
    seconds = policy["active_seconds_limit"]
    if seconds is not None and (type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 10**9):
        raise ResourceBudgetError("请求执行时间预算须为有限正数")
    return policy


def snapshot_policy(db, conversation_id):
    row = db.get(ConversationResourcePolicy, conversation_id) if conversation_id else None
    return {"revision": row.revision if row else 0, "policy": deepcopy(row.policy if row else DEFAULT_POLICY)}


def update_policy(db, conversation_id, value, expected_revision):
    policy = normalize_policy(value)
    row = db.get(ConversationResourcePolicy, conversation_id)
    if row is None:
        if expected_revision != 0:
            raise ResourceBudgetError("预算设置已变化，请重新载入")
        row = ConversationResourcePolicy(conversation_id=conversation_id, revision=1, policy=policy)
        db.add(row)
        db.flush()
    else:
        changed = db.execute(update(ConversationResourcePolicy).where(
            ConversationResourcePolicy.conversation_id == conversation_id,
            ConversationResourcePolicy.revision == expected_revision).values(
                policy=policy, revision=expected_revision + 1, updated_at=utcnow())
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise ResourceBudgetError("预算设置已变化，请重新载入")
        db.expire(row)
    return snapshot_policy(db, conversation_id)


def freeze_budget(db, *, run_id, binding):
    result = deepcopy(binding or {})
    budget_id = result.get("resource_budget_id")
    if budget_id:
        budget = db.get(ResourceBudget, budget_id)
        if budget is None or budget.owner_run_id not in (run_id, result.get("parent_run_id")):
            raise ResourceBudgetError("已保存任务的预算绑定不存在或不属于当前任务")
        return result
    policy = normalize_policy((result.get("resource_policy") or {}).get("policy", {}))
    if policy["amount_limit"] is None and policy["active_seconds_limit"] is None:
        return result
    if not run_id:
        raise ResourceBudgetError("有预算的执行必须具有可恢复任务编号")
    budget = db.scalar(select(ResourceBudget).where(ResourceBudget.owner_run_id == run_id))
    if budget is None:
        budget = ResourceBudget(id=uuid4().hex, owner_run_id=run_id, currency=policy["currency"],
            limit_micros=_money(policy["amount_limit"]) if policy["amount_limit"] is not None else None,
            request_estimate_micros=_money(policy["request_estimate"]) if policy["request_estimate"] is not None else None,
            request_price_version=policy["price_version"],
            seconds_limit_ms=math.ceil(policy["active_seconds_limit"] * 1000) if policy["active_seconds_limit"] is not None else None)
        db.add(budget)
        db.flush()
    result["resource_budget_id"] = budget.id
    return result
