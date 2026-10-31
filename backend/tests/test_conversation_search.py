import asyncio
import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import task_events
from app.api import tasks
from app.main import app
from app.models import Conversation, DeletedConversation, DesktopState, ForegroundRun, QARecord

client = TestClient(app)


def page(**params):
    response = client.get('/api/conversations/page', params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_literal_search_chinese_body_excerpt_and_scopes(db):
    db.add_all([Conversation(id='c_body', title='Review'),
                Conversation(id='c_archived', title='Review', archived_at=datetime.utcnow()),
                Conversation(id='c_deleted', title='Deleted'), DeletedConversation(id='c_deleted')])
    for cid in ('c_body', 'c_archived', 'c_deleted'):
        db.add(QARecord(id='qa_' + cid, conversation_id=cid, question='Question',
                        answer='x' * 3000 + '中文短语 <script>alert(1)</script> 100%_ Accuracy' + 'z' * 3000,
                        evidence=[{'content': 'secret evidence'}], trace=[{'secret': 'trace'}]))
    db.commit()
    for keyword in ('中文短语', '100%_', 'accuracy', '<script>'):
        result = page(q=keyword)
        assert result['total'] == 1
        item = result['conversations'][0]
        assert item['id'] == 'c_body' and item['match_field'] == 'answer'
        assert item['match_record_id'] == 'qa_c_body'
        assert len(item['match_text']) <= 160 and keyword.lower() in item['match_text'].lower()
        assert 'secret' not in json.dumps(item) and 'answer' not in item
    assert page(q='中文短语', scope='all')['total'] == 2
    assert page(q='中文短语', scope='archived')['conversations'][0]['id'] == 'c_archived'
    assert page(q="' OR 1=1 --")['total'] == 0
    assert page(q='   ')['total'] == 1
    long_phrase = '中文' * 100
    db.add(QARecord(id='qa_long', conversation_id='c_body', question='Long', answer='prefix ' + long_phrase + ' suffix'))
    db.commit()
    assert long_phrase in page(q=long_phrase)['conversations'][0]['match_text']


def test_keyset_ties_sort_scope_and_query_bound_cursor(db):
    at = datetime(2026, 1, 1)
    db.add_all(Conversation(id=f'c_{i:03}', title=f'Entry {i}', created_at=at + timedelta(days=i), updated_at=at) for i in range(105))
    db.commit()
    first = page(limit=40)
    assert len(first['conversations']) == 40 and first['total'] == 105
    cursor = first['next_cursor']
    ids = [i['id'] for i in first['conversations']]
    while cursor:
        next_page = page(limit=40, cursor=cursor)
        ids += [i['id'] for i in next_page['conversations']]
        cursor = next_page['next_cursor']
    assert ids == [f'c_{i:03}' for i in reversed(range(105))]
    for params in ({'q': 'Entry'}, {'scope': 'archived'}, {'sort': 'created_at'}):
        assert client.get('/api/conversations/page', params={'cursor': first['next_cursor'], **params}).status_code == 422
    for cursor in ('invalid', 'W10=', 'bnVsbA=='):
        assert client.get('/api/conversations/page', params={'cursor': cursor}).status_code == 422
    assert page(sort='created_at', limit=1)['conversations'][0]['id'] == 'c_104'
    assert page(q='Entry 1', limit=1)['conversations'][0]['id'] == 'c_001', 'exact title remains resolvable beyond the first page of substrings'
    assert client.get('/api/conversations/page', params={'limit': 101}).status_code == 422
    assert client.get('/api/conversations/page', params={'q': 'x' * 201}).status_code == 422


def test_fallbacks_archive_precedence_and_lookups(db):
    db.add_all([QARecord(id='qa_legacy', conversation_id='c_legacy', question='Legacy 中文', answer='Body'),
                DesktopState(key='conversation:c_draft', value={'text': 'Draft 中文', 'images': ['DO NOT RETURN']}),
                ForegroundRun(id='chat_partial', conversation_id='c_run', question='Partial', partial_answer='Interrupted 中文', status='interrupted'),
                Conversation(id='c_archive', title='Archive', archived_at=datetime.utcnow()),
                DesktopState(key='conversation:c_archive', value={'text': 'Archived 中文'}),
                DeletedConversation(id='c_deleted'),
                DesktopState(key='conversation:c_deleted', value={'text': 'Deleted 中文'})])
    db.commit()
    result = page(q='中文')
    by_id = {c['id']: c for c in result['conversations']}
    assert set(by_id) == {'c_legacy', 'c_draft', 'c_run'}
    assert by_id['c_run']['match_run_id'] == 'chat_partial'
    assert by_id['c_draft']['draft_only']
    assert 'DO NOT RETURN' not in json.dumps(result)
    assert page(scope='archived', q='中文')['conversations'][0]['id'] == 'c_archive'
    lookup = client.post('/api/conversations/lookup', json={'ids': ['c_run', 'c_deleted', 'missing'], 'q': '中文'}).json()
    assert [c['id'] for c in lookup['conversations']] == ['c_run']
    assert client.get('/api/conversations/c_run/activity').status_code == 200
    assert client.get('/api/conversations/c_deleted/activity').status_code == 404
    assert client.post('/api/conversations/lookup', json={'ids': ['x'] * 101}).status_code == 422
    assert client.post('/api/conversations/lookup', json={'ids': []}).json()['conversations'] == []


def test_sidebar_pins_paginate_before_recent_and_delete_boundary(db):
    at = datetime(2026, 1, 1)
    db.add_all(Conversation(id=f'c_{i:03}', title='Pinned', pinned_at=at, pin_position=i,
                            created_at=at, updated_at=at) for i in range(42))
    db.add(Conversation(id='recent', title='New', created_at=at + timedelta(days=1), updated_at=at + timedelta(days=1)))
    db.commit()
    first = page(sort='sidebar_updated_at')
    assert first['pinned_total'] == 42
    assert first['conversations'][0]['id'] == 'c_000'
    assert client.delete('/api/conversations/c_039').status_code == 200
    second = page(sort='sidebar_updated_at', cursor=first['next_cursor'])
    assert [c['id'] for c in second['conversations']] == ['c_040', 'c_041', 'recent']


@pytest.mark.parametrize('action', ['pin', 'rename', 'delete', 'fork', 'archive', 'detail'])
def test_paged_legacy_history_actions_materialize_only_the_selected_conversation(db, action):
    db.add_all(QARecord(id=f'qa_{i}', conversation_id=f'c_{i}', question=f'Legacy {i}', answer='Answer') for i in range(3))
    db.commit()
    assert page()['total'] == 3
    assert db.query(Conversation).count() == 0, 'reading a page is read-only'
    if action == 'pin':
        response = client.put('/api/conversations/c_1/pin', json={'pinned': True})
    elif action == 'rename':
        response = client.put('/api/conversations/c_1', json={'title': 'Renamed'})
    elif action == 'archive':
        response = client.put('/api/conversations/c_1/archive', json={'archived': True, 'expected_revision': 0})
    elif action == 'fork':
        response = client.post('/api/conversations/c_1/fork', json={})
    elif action == 'delete':
        response = client.delete('/api/conversations/c_1')
    else:
        response = client.get('/api/conversations/c_1')
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.get(Conversation, 'c_0') is None and db.get(Conversation, 'c_2') is None
    assert (db.get(Conversation, 'c_1') is not None) == (action != 'delete')
    assert page(scope='all')['total'] == (2 if action == 'delete' else 4 if action == 'fork' else 3)


def test_signal_sse_does_not_serialize_conversation_history(db):
    db.add_all(Conversation(id=f'c_{i}', title='A' * 200) for i in range(150))
    db.commit()
    result = client.get('/api/tasks', params={'conversation_mode': 'signal'}).json()
    assert 'conversations' not in result and len(json.dumps(result)) < 1000

    async def check():
        response = await tasks.events(conversation_mode='signal')
        stream = response.body_iterator
        try:
            first = await asyncio.wait_for(anext(stream), 3)
            task_events.notify_tasks_changed()
            second = await asyncio.wait_for(anext(stream), 3)
            assert first != second and 'conversations"' not in first + second
        finally:
            await stream.aclose()
    asyncio.run(check())
