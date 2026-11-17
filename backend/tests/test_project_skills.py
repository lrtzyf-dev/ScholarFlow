import json
import pytest
from app import skill_store as store
from app.agent.checkpoint import AgentState, prepare_resume


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(store, 'SETTINGS_PATH', tmp_path / 'settings.json')


def create(name='same', project='a', content='project a'):
    return store.create_user_skill({'name': name, 'content': content}, project_id=project)


def test_same_name_isolated_and_identity_ignores_frontmatter():
    a, b = create(), create(project='b', content='project b')
    assert store.load_skill('same').get('error')
    assert store.load_skill('same', project_id='a')['content'] == 'project a'
    assert store.load_skill('same', project_id='b')['content'] == 'project b'
    assert a['identity'] != b['identity']
    path = store.DATA_DIR / 'project_skills/projects/a/same/SKILL.md'
    path.write_text('---\nname: malicious-identity\n---\nnew', encoding='utf-8')
    assert store.list_skills(project_id='a')[-1]['name'] != 'malicious-identity'
    assert store.load_skill('same', project_id='a').get('untrusted')


@pytest.mark.parametrize('name', ['', '..', '../x', '/tmp', 'a/b', 'a\\b', 'CON', 'aux', ' x', 'x.'])
def test_invalid_name_never_deletes_root(name):
    create(project=None)
    with pytest.raises(ValueError):
        store.delete_user_skill(name)
    assert store.load_skill('same')['content'] == 'project a'
    assert store.load_skill(name).get('error')


def test_project_revoke_does_not_revoke_other_project_and_explicit_cannot_bypass():
    a, b = create(), create(project='b')
    store.set_disabled('same', True, project_id='a')
    assert store.load_skill('same', explicit=True, project_id='a').get('disabled')
    store.validate_manifest([store.manifest(b)])
    with pytest.raises(store.SkillsChanged):
        store.validate_manifest([store.manifest(a)])


def test_update_requires_exact_review_and_invalidates_even_restored_bytes():
    a = create()
    path = store.DATA_DIR / 'project_skills/projects/a/same/SKILL.md'
    path.write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError):
        store.trust_skill('same', a['revision'], project_id='a')
    assert not store.list_skills(False, project_id='a') or not any(i['name'] == 'same' for i in store.list_skills(False, project_id='a'))
    changed = store.inspect_skill('same', project_id='a')
    store.trust_skill('same', changed['revision'], project_id='a')
    with pytest.raises(store.SkillsChanged):
        store.validate_manifest([store.manifest(a)])


def test_resume_reloads_only_reviewed_versions_and_cancels_remaining_calls():
    a = create()
    state = AgentState(question='x', phase='tools', skills=['same'], skill_manifest=[store.manifest(a)],
        calls=[{'id': 'c1', 'name': 'write', 'arguments': {}}], messages=[
            {'role': 'system', 'content': 'BASE<!-- scholarflow-skills:start -->old<!-- scholarflow-skills:end -->'},
            {'role': 'tool', 'tool_call_id': 'old', 'content': json.dumps(a)}],
        resource_pause={'kind': 'skill_changed'})
    store.set_disabled('same', True, project_id='a')
    resumed, work = prepare_resume(state.snapshot())
    assert work and resumed.phase == 'model' and not resumed.calls and not resumed.skill_manifest
    assert resumed.step == 1
    assert resumed.messages[0]['content'] == 'BASE'
    assert 'project a' not in str(resumed.messages)
    assert 'cancelled' in resumed.messages[-2]['content']


def test_resume_unknown_operation_never_clears_pending():
    a = create()
    state = AgentState(question='x', phase='model', skill_manifest=[store.manifest(a)], resource_pause={'kind': 'skill_changed'},
        pending_operation={'key': 'k', 'kind': 'tool', 'name': 'mcp__x__write', 'arguments': {}})
    with pytest.raises(store.SkillsChanged):
        prepare_resume(state.snapshot())
    assert state.pending_operation


def test_settings_corruption_fails_closed():
    create()
    store.SETTINGS_PATH.write_text('{bad', encoding='utf-8')
    with pytest.raises(ValueError):
        store.list_skills(project_id='a')


def test_frontmatter_newline_not_interpreted_as_commands():
    with pytest.raises(ValueError):
        store.create_user_skill({'name': 'x', 'title': 'x\n---\nevil'})


def test_edit_uses_version_compare_and_does_not_undo_disabled_state():
    original = create()
    store.set_disabled('same', True, project_id='a')
    changed = store.update_skill('same', {'expected_revision': original['revision'], 'content': 'revision 2'}, project_id='a')
    assert changed['trusted'] and changed['disabled'] and changed['revision'] != original['revision']
    with pytest.raises(ValueError):
        store.update_skill('same', {'expected_revision': original['revision'], 'content': 'stale'}, project_id='a')
    assert store.inspect_skill('same', project_id='a')['content'] == 'revision 2'


def test_executor_pauses_before_followup_tool_after_loaded_skill_is_revoked(db, monkeypatch):
    from app.agent import loop
    from app.llm import LLMResponse
    from app.models import TaskRecord
    from copy import deepcopy
    create(project=None)
    original = loop.execute_tool
    executed, snapshots = [], []
    def tool(db, name, arguments, **kwargs):
        executed.append(name)
        result = original(db, name, arguments, **kwargs)
        if name == 'load_skill':
            store.set_disabled('same', True)
        return result
    class Model:
        def stream_chat(self, messages, tools=None):
            yield 'final', LLMResponse('', [
                {'id': 'skill', 'name': 'load_skill', 'arguments': {'name': 'same'}},
                {'id': 'next', 'name': 'list_papers', 'arguments': {}}])
    monkeypatch.setattr(loop, 'get_llm', lambda: Model())
    monkeypatch.setattr(loop, 'execute_tool', tool)
    monkeypatch.setattr(loop.hooks_store, 'run_hooks', lambda *a, **k: {'ran': 0, 'blocked': False, 'feedback': ''})
    row = TaskRecord(type='agent', title='skill revocation', conversation_id='skills-executor',
        payload={'question': 'read skill'}, status='running', events=[])
    db.add(row); db.commit()
    events = list(loop.run_agent(db, 'read skill', conversation_id=row.conversation_id, run_id=row.id,
        save_checkpoint=lambda value: snapshots.append(deepcopy(value))))
    assert executed == ['load_skill']
    assert events[-1][0] == 'paused'
    assert snapshots[-1]['resource_pause']['kind'] == 'skill_changed'
    assert not snapshots[-1]['pending_operation']
    assert snapshots[-1]['skill_manifest'][0]['name'] == 'same'
