"""Public child history and explicit reconciliation, never independent dispatch."""
from copy import deepcopy
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import select, func
from fastapi import HTTPException
from .models import ForegroundRun, TaskRecord
from .runtime_models import RuntimeRun
from .resource_models import PersistentSubtask, PersistentSubtaskAttempt, SubtaskOperationResolution, SubtaskCallLink
from .model_profiles import require_conversation, ModelConfigurationError
from . import subtask_store, run_store, task_runner
from .timeutil import utcnow


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _text(value, limit=2000):
    value = str(value or '')[:limit]
    value = re.sub(r'(?i)\b(?:sk-[\w-]+|bearer\s+[^\s,;]+)', '[已隐藏凭证]', value)
    return re.sub(r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[=:]\s*)[^\s,;]+', r'\1[已隐藏凭证]', value)


def _arguments(value, depth=0):
    if depth > 5:
        return '[内容过深]'
    if isinstance(value, dict):
        return {str(k)[:80]: '[已隐藏凭证]' if re.search(r'(?i)key|token|password|secret|authorization|cookie', str(k))
                else _arguments(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        return [_arguments(v, depth + 1) for v in value[:40]]
    return value if value is None or type(value) in (int, float, bool) else _text(value)


def parent(db, run_id, *, write=False):
    logical = db.get(RuntimeRun, run_id)
    if logical is None:
        raise HTTPException(404, '所属任务不存在')
    if logical.conversation_id:
        try:
            require_conversation(db, logical.conversation_id, write=write)
        except ModelConfigurationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
    row = db.get(ForegroundRun if logical.kind == 'foreground' else TaskRecord, run_id)
    if row is None or row.conversation_id != logical.conversation_id:
        raise HTTPException(404, '所属任务记录不可用')
    checkpoint = row.agent_checkpoint if logical.kind == 'foreground' else row.checkpoint
    return logical, row, checkpoint or {}


def _active(logical, row):
    return (logical.status in ('running', 'queued') or row.status in ('running', 'pending')
            or logical.id in run_store.active_runs() or logical.id in task_runner.running_task_ids())


def _progress(checkpoint):
    value = checkpoint if isinstance(checkpoint, dict) else {}
    collected = value.get('collected', [])
    pending = value.get('pending_operation')
    return {'completed_reads': len(collected) if isinstance(collected, list) else 0,
        'tool_index': value.get('tool_index', 0), 'queued_tools': len(value.get('calls', [])),
        'tools': [_text(c.get('tool'), 160) for c in collected[-30:] if isinstance(c, dict)],
        'pending': {'name': _text(pending.get('name'), 160), 'arguments': _arguments(pending.get('arguments', {})),
                    'fingerprint': _hash(pending)} if isinstance(pending, dict) else None,
        'archived_results': len(value.get('context_archive') or {}),
        'loaded_skills': [{'name': _text(v.get('name') or v.get('identity'), 160), 'revision': _text(v.get('revision'), 80)}
                          for v in value.get('skill_manifest', []) if isinstance(v, dict)]}


def _result(value):
    value = value if isinstance(value, dict) else {}
    return {'findings': _text(value.get('findings'), 16000), 'outcome': value.get('outcome'),
        'incomplete': [_text(item) for item in value.get('incomplete', [])[:40]],
        'semantic_verified': False, 'summary_is_completion_proof': False}


def _resolution(row):
    return {'id': row.id, 'attempt': row.attempt, 'action': row.action, 'reference': _text(row.reference),
        'created_at': row.created_at.isoformat(), 'source': 'user_reconciliation', 'provider_verified': False}


def _node(row):
    return {'id': row.id, 'parent_run_id': row.parent_run_id, 'state': row.state, 'attempt': row.attempt,
        'topic': _text((row.specification or {}).get('topic') or '子任务', 500),
        'goal': _text((row.specification or {}).get('goal'), 4000),
        'budget_id': row.budget_id, 'progress': _progress(row.checkpoint), 'result': _result(row.result),
        'updated_at': row.updated_at.isoformat(), 'cancel_requested': bool(row.cancel_requested)}


def tree(db, run_id, *, offset=0, limit=40):
    logical, row, checkpoint = parent(db, run_id)
    query = select(PersistentSubtask).where(PersistentSubtask.parent_run_id == run_id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    nodes = list(db.scalars(query.order_by(PersistentSubtask.created_at, PersistentSubtask.id).offset(offset).limit(limit)))
    active = _active(logical, row)
    review = subtask_store.resume_safety(db, parent_run_id=run_id, checkpoint=checkpoint)
    return {'parent': {'id': run_id, 'kind': logical.kind, 'conversation_id': logical.conversation_id,
        'revision': logical.revision, 'status': logical.status, 'active': active,
        'title': _text(row.question if logical.kind == 'foreground' else row.title, 500)},
        'recovery': {'safe_children': bool(review.get('safe')), 'reason': review.get('reason'),
            'requires_parent_admission': True, 'can_reconcile': not active,
            'message': '所属任务仍在运行，请先停止并等待执行结束。' if active else
                '子任务结果可由所属任务恢复流程接管；恢复时仍检查模型、权限、来源与预算。' if review.get('safe') else
                '请检查未确认操作，再回到所属任务处理恢复；不会单独启动子任务。'},
        'items': [_node(node) for node in nodes], 'total': total,
        'next_offset': offset + limit if offset + limit < total else None}


def attempts(db, node_id, *, offset=0, limit=20):
    node = db.get(PersistentSubtask, node_id)
    if node is None:
        raise HTTPException(404, '子任务不存在')
    parent(db, node.parent_run_id)
    query = select(PersistentSubtaskAttempt).where(PersistentSubtaskAttempt.subtask_id == node.id)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    rows = db.scalars(query.order_by(PersistentSubtaskAttempt.number.desc()).offset(offset).limit(limit)).all()
    receipts = db.scalars(select(SubtaskOperationResolution).where(SubtaskOperationResolution.subtask_id == node.id)
        .order_by(SubtaskOperationResolution.created_at.desc()).limit(100)).all()
    return {'node': _node(node), 'items': [{'number': row.number, 'state': row.state,
        'progress': _progress(row.checkpoint), 'result': _result(row.result), 'created_at': row.created_at.isoformat(),
        'updated_at': row.updated_at.isoformat()} for row in rows], 'total': total,
        'next_offset': offset + limit if offset + limit < total else None,
        'resolutions': [_resolution(receipt) for receipt in receipts]}


def calls(db, node_id, *, attempt=None, offset=0, limit=20):
    from .ledger_models import CallRecord
    from .call_ledger import _snapshot
    node = db.get(PersistentSubtask, node_id)
    if node is None:
        raise HTTPException(404, '子任务不存在')
    parent(db, node.parent_run_id)
    query = select(CallRecord).join(SubtaskCallLink, SubtaskCallLink.call_id == CallRecord.id).where(SubtaskCallLink.subtask_id == node_id)
    if attempt is not None:
        query = query.where(SubtaskCallLink.attempt == attempt)
    rows = list(db.scalars(query.order_by(CallRecord.created_at.desc(), CallRecord.id.desc()).offset(offset).limit(limit + 1)))
    return {'items': [_snapshot(row, db) for row in rows[:limit]], 'next_offset': offset + limit if len(rows) > limit else None,
        'notice': '仅列出已记录子任务归属的调用；较早历史无法可靠补认归属。金额仍区分估算、未知与用户账单核对。'}


def reconcile(db, node_id, body):
    node = db.get(PersistentSubtask, node_id)
    if node is None:
        raise HTTPException(404, '子任务不存在')
    logical, row, parent_checkpoint = parent(db, node.parent_run_id, write=True)
    prior = db.scalar(select(SubtaskOperationResolution).where(SubtaskOperationResolution.subtask_id == node.id,
        SubtaskOperationResolution.request_id == body['request_id']))
    if prior:
        if (prior.attempt, prior.operation_fingerprint, prior.action, prior.reference) != (
                body['expected_attempt'], body['operation_fingerprint'], body['action'], body['reference']):
            raise HTTPException(409, '此核对请求编号已用于其他操作')
        return _resolution(prior)
    if _active(logical, row):
        raise HTTPException(409, '所属任务仍在运行，请先停止并等待执行结束')
    if logical.revision != body['expected_parent_revision'] or node.attempt != body['expected_attempt']:
        raise HTTPException(409, '任务已变化，请重新读取后核对；保留当前草稿')
    cp = deepcopy(node.checkpoint or {})
    pending, index, calls = cp.get('pending_operation'), cp.get('tool_index', 0), cp.get('calls', [])
    if (not isinstance(pending, dict) or _hash(pending) != body['operation_fingerprint']
            or not str(pending.get('name', '')).startswith('mcp__')
            or type(index) is not int or not 0 <= index < len(calls) or calls[index] != pending
            or node.state == 'completed'):
        raise HTTPException(409, '未确认的外部操作已变化或不可核对')
    # A read-only review cannot accidentally resolve a node from another scope.
    parent_pending = parent_checkpoint.get('pending_operation') or {}
    specifications = (parent_pending.get('arguments') or {}).get('tasks', [])
    if parent_pending.get('name') != 'spawn_subagent' or node.specification not in specifications:
        raise HTTPException(409, '父检查点未指向此子任务派发，请在所属任务中处理')
    if body['reference'] != _text(body['reference']) or not body['reference'].strip():
        raise HTTPException(422, '核对依据不能为空或包含凭证')
    receipt = SubtaskOperationResolution(id=uuid4().hex, subtask_id=node.id, parent_run_id=logical.id,
        attempt=node.attempt, request_id=body['request_id'], operation_fingerprint=body['operation_fingerprint'],
        action=body['action'], reference=body['reference'])
    db.add(receipt)
    if body['action'] == 'skip':
        result = json.dumps({'error': '用户核对后跳过未确认的外部调用，结果未知，不能作为完成证据。',
            'user_skipped': True, 'reconciliation_id': receipt.id}, ensure_ascii=False)
        cp.setdefault('messages', []).append({'role': 'tool', 'tool_call_id': pending.get('id'), 'content': result})
        cp.setdefault('collected', []).append({'tool': pending['name'], 'arguments': deepcopy(pending.get('arguments', {})), 'result': result})
        cp['tool_index'] = index + 1
    # Retry is an explicit user decision, not an invented provider receipt.
    cp['pending_operation'] = None
    cp.setdefault('reconciliations', []).append({'id': receipt.id, 'action': body['action'], 'operation': _hash(pending)})
    node.checkpoint, node.writer_token, node.state, node.updated_at = cp, None, 'blocked', utcnow()
    logical.revision += 1
    logical.updated_at = utcnow()
    db.flush()
    return _resolution(receipt)
