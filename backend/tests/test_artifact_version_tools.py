"""Saved artifact history and pending restores through API/actual agent branch.

Fabricated local revisions only; no model transport, user files or credentials.
"""
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import update

from app import artifact_store as store
from app.artifact_models import ArtifactChange, ArtifactManifest, ArtifactRevision
from app.artifact_tools import ARTIFACT_TOOL_SPECS, ARTIFACT_TOOLS, ARTIFACT_READ_TOOLS
from app.agent.tools import execute_tool, SAFE_TOOLS, tool_risk
from app.execution_mode import PLAN_READ_TOOLS
from app.history_revisions import ANSWER_READ_TOOLS
from app.main import app
from app.models import Conversation, DeletedConversation
from app.runtime_context import bind_runtime_config
from app.timeutil import utcnow


NAME = 'list_research_artifact_versions'
CID = 'artifact-version-tool-tests'
client = TestClient(app)


def versions(db, count=3, *, cid=CID, request_prefix='versions'):
    if db.get(Conversation, cid) is None:
        db.add(Conversation(id=cid)); db.commit()
    first = store.create(db, conversation_id=cid, request_id=request_prefix + '-create123',
        title='固定研究成果', kind='document', content='# 方法\nversion 1\n', evidence=[], analysis={})
    row = store.get(db, first['id'], cid)
    for version in range(2, count + 1):
        change = store.propose(db, row, request_id=f'{request_prefix}-edit{version:08d}',
            base_revision_id=row.current_revision_id, content=f'# 方法\nversion {version}\n')
        store.decide(db, row, db.get(ArtifactChange, change['id']), accept=True,
            expected_revision_id=row.current_revision_id)
    db.commit()
    return row, first


def agent(db, name, args, *, cid=CID, binding=None):
    with bind_runtime_config(binding or {'conversation_id': cid}):
        return json.loads(execute_tool(db, name, args, conversation_id=cid))


def api_history(row, **params):
    return client.get(f'/api/artifacts/{row.id}/revisions', params={'conversation_id': CID, **params})


def restore_args(row, target, **extra):
    return {'artifact_id': row.id, 'base_revision_id': row.current_revision_id,
        'request_id': 'restore-pending123', 'restored_from': target, **extra}


def test_api_and_agent_share_bounded_saved_history_and_current_marker(db):
    row, first = versions(db, 5)
    store.propose(db, row, request_id='pending-not-history123',
        base_revision_id=row.current_revision_id, content='UNACCEPTED_DRAFT')
    response = api_history(row, limit=2)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page == agent(db, NAME, {'artifact_id': row.id, 'limit': 2})
    assert [x['version'] for x in page['items']] == [5, 4]
    assert [x['is_current'] for x in page['items']] == [True, False]
    assert page['next_before_version'] == 4 and page['current_version'] == 5
    assert page['saved_fixed_revisions_only'] and not page['includes_pending_drafts']
    assert page['trust'] == 'research_draft_history_not_external_evidence'
    assert 'UNACCEPTED_DRAFT' not in json.dumps(page)
    second = agent(db, NAME, {'artifact_id': row.id, 'before_version': 4, 'limit': 2})
    last = api_history(row, before_version=second['next_before_version'], limit=2).json()
    assert [x['version'] for x in second['items']] == [3, 2]
    assert [x['id'] for x in last['items']] == [first['current_revision_id']]
    assert last['next_before_version'] is None
    empty = api_history(row, before_version=1).json()
    assert empty['items'] == [] and empty['next_before_version'] is None
    assert db.query(ArtifactRevision).count() == 5


@pytest.mark.parametrize('args', [
    {'limit': 0}, {'limit': 101}, {'limit': True}, {'limit': 2.5}, {'limit': '2'},
    {'before_version': 0}, {'before_version': True}, {'before_version': -1},
    {'before_version': 2.5}, {'before_version': '2'}, {'before_version': 10 ** 100},
])
def test_actual_agent_rejects_invalid_pagination_instead_of_unbounded_query(db, args):
    row, _ = versions(db, 1)
    with pytest.raises(HTTPException) as error:
        agent(db, NAME, {'artifact_id': row.id, **args})
    assert error.value.status_code == 422


@pytest.mark.parametrize('query', [{'limit': 0}, {'limit': 101}, {'before_version': 0},
    {'before_version': 10 ** 100}])
def test_api_rejects_invalid_pagination(db, query):
    row, _ = versions(db, 1)
    assert api_history(row, **query).status_code == 422


def test_cross_conversation_and_runtime_binding_are_enforced(db):
    row, first = versions(db)
    db.add(Conversation(id='artifact-other-conversation')); db.commit()
    wrong = 'artifact-other-conversation'
    assert client.get(f'/api/artifacts/{row.id}/revisions', params={'conversation_id': wrong}).status_code == 404
    for name, args in [(NAME, {'artifact_id': row.id}),
            ('propose_research_edit', restore_args(row, first['current_revision_id']))]:
        with pytest.raises(HTTPException) as error:
            agent(db, name, args, cid=wrong)
        assert error.value.status_code == 404
        mismatch = agent(db, name, args, cid=wrong, binding={'conversation_id': CID})
        assert '范围' in mismatch['error']
    assert db.query(ArtifactChange).filter_by(status='proposed').count() == 0


def test_archived_read_allowed_restore_denied_and_deleted_read_denied(db):
    row, first = versions(db)
    conversation = db.get(Conversation, CID); conversation.archived_at = utcnow(); db.commit()
    assert api_history(row).status_code == 200
    assert agent(db, NAME, {'artifact_id': row.id})['current_version'] == 3
    with pytest.raises(HTTPException) as error:
        agent(db, 'propose_research_edit', restore_args(row, first['current_revision_id']))
    assert error.value.status_code == 409
    db.add(DeletedConversation(id=CID)); db.commit()
    assert api_history(row).status_code == 410
    with pytest.raises(HTTPException) as error:
        agent(db, NAME, {'artifact_id': row.id})
    assert error.value.status_code == 410


@pytest.mark.parametrize('target', ['current', 'old'])
def test_api_and_actual_agent_detect_corrupt_returned_saved_revision(db, target):
    row, first = versions(db)
    revision_id = row.current_revision_id if target == 'current' else first['current_revision_id']
    db.execute(update(ArtifactRevision).where(ArtifactRevision.id == revision_id).values(content='DAMAGED'))
    db.commit(); db.expire_all()
    assert api_history(row).status_code == 409
    with pytest.raises(HTTPException) as error:
        agent(db, NAME, {'artifact_id': row.id})
    assert error.value.status_code == 409


def test_current_integrity_is_checked_even_when_filtered_out_of_page(db):
    row, _ = versions(db)
    db.execute(update(ArtifactRevision).where(ArtifactRevision.id == row.current_revision_id)
        .values(evidence=[{'untrusted': 'damage'}])); db.commit(); db.expire_all()
    assert api_history(row, before_version=2).status_code == 409


def test_manifest_current_version_disagreement_fails_closed(db):
    row, _ = versions(db)
    db.execute(update(ArtifactManifest).where(ArtifactManifest.id == row.id).values(version=999))
    db.commit(); db.expire_all()
    assert api_history(row).status_code == 409


def test_restore_agent_creates_one_pending_draft_never_applies_it(db):
    row, first = versions(db)
    head_id = row.current_revision_id
    target = first['current_revision_id']
    args = restore_args(row, target)
    result = agent(db, 'propose_research_edit', args)
    assert result['status'] == 'draft_pending_review' and not result['saved_document_changed']
    assert result['restored_from'] == target and result['base_revision_id'] == head_id
    assert 'version 1' in result['diff'] and 'version 3' in result['diff']
    assert agent(db, 'propose_research_edit', args) == result
    db.expire_all(); current = db.get(ArtifactManifest, row.id)
    assert current.current_revision_id == head_id and current.version == 3
    assert db.query(ArtifactRevision).count() == 3
    pending = db.query(ArtifactChange).filter_by(status='proposed').all()
    assert len(pending) == 1 and pending[0].restored_from == target and pending[0].result_revision_id is None
    reload = client.get(f'/api/artifacts/{row.id}/changes/{result["change_id"]}',
        params={'conversation_id': CID}).json()
    assert reload['restored_from'] == target and reload['content'] == first['revision']['content']
    assert reload['status'] == 'proposed'
    listed = agent(db, NAME, {'artifact_id': row.id})
    assert [x['version'] for x in listed['items']] == [3, 2, 1]
    assert not any(x['restored_from'] for x in listed['items'])
    with pytest.raises(HTTPException) as error:
        agent(db, 'propose_research_edit', {**args, 'restored_from': head_id})
    assert error.value.status_code == 409


@pytest.mark.parametrize('extra', [
    {}, {'content': 'one', 'section': {}}, {'content': 'one', 'restored_from': '0' * 32},
    {'section': {}, 'restored_from': '0' * 32}, {'content': 'one', 'section': {}, 'restored_from': '0' * 32},
])
def test_edit_choices_exactly_one_and_no_draft_saved_on_ambiguity(db, extra):
    row, _ = versions(db, 1)
    args = {'artifact_id': row.id, 'base_revision_id': row.current_revision_id, 'request_id': 'exclusive-edit123', **extra}
    with pytest.raises(HTTPException) as error:
        agent(db, 'propose_research_edit', args)
    assert error.value.status_code == 422
    assert db.query(ArtifactChange).filter_by(status='proposed').count() == 0


def test_cross_artifact_restore_is_denied_and_stale_base_stays_pending(db):
    row, first = versions(db)
    other, _ = versions(db, 1, request_prefix='other-artifact')
    with pytest.raises(HTTPException) as error:
        agent(db, 'propose_research_edit', restore_args(row, other.current_revision_id))
    assert error.value.status_code == 404
    result = agent(db, 'propose_research_edit', restore_args(row, first['current_revision_id'],
        base_revision_id=first['current_revision_id']))
    assert result['conflict'] and result['status'] == 'draft_pending_review'
    db.expire_all()
    assert db.get(ArtifactManifest, row.id).version == 3


def test_history_mode_allows_listing_but_cannot_propose_restore(db):
    row, first = versions(db)
    binding = {'conversation_id': CID, 'history_revision': {'mode': 'answer_only'}}
    assert agent(db, NAME, {'artifact_id': row.id}, binding=binding)['current_version'] == 3
    result = agent(db, 'propose_research_edit', restore_args(row, first['current_revision_id']), binding=binding)
    assert result['blocked_by_history_revision']
    assert db.query(ArtifactChange).filter_by(status='proposed').count() == 0


def test_registry_risk_plan_history_source_attribution_and_goal_policy_lists():
    from app.agent.source_attribution import PROCESS_TOOLS, SOURCE_TOOLS
    from app.goal_contracts import policy_denial
    assert NAME in ARTIFACT_TOOLS & ARTIFACT_READ_TOOLS & SAFE_TOOLS & PLAN_READ_TOOLS & ANSWER_READ_TOOLS
    assert tool_risk(NAME) == 'safe'
    assert NAME in PROCESS_TOOLS and NAME not in SOURCE_TOOLS
    assert not any('accept' in n or 'decide' in n or 'restore' in n for n in ARTIFACT_TOOLS)
    spec = next(x for x in ARTIFACT_TOOL_SPECS if x['name'] == 'propose_research_edit')
    assert {x['required'][0] for x in spec['parameters']['oneOf']} == {'content', 'section', 'restored_from'}
    for rule in ('no_commands', 'no_file_write', 'no_external_tools'):
        snapshot = {'id': 'goal', 'revision': 1, 'requirements': [{'id': 'req', 'source_message_id': 'user',
            'title': 'constraint', 'tool_arguments': {'result_contract': {'kind': 'policy', 'rule': rule}}}]}
        assert policy_denial(snapshot, NAME) is None
