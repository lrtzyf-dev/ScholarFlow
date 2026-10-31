"""Bundled real ripgrep, scope verification, paging and model dispatch."""
import json
from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import project_search as search, workspace
from app.agent.tools import execute_tool, tool_risk
from app.execution_mode import PLAN_READ_TOOLS
from app.main import app


@pytest.fixture
def project(db, tmp_path):
    root = tmp_path / 'search-project'; root.mkdir(); (root / 'src').mkdir()
    (root / 'src/a.py').write_text('alpha = 1\n中文 😀 alpha ALPHA\nmalpha\n', encoding='utf-8')
    (root / 'readme.txt').write_text('alpha.* literal\n', encoding='utf-8')
    item = workspace.register(db, str(root)); db.commit()
    return root, workspace.project_scope(item)


def run(project, query='alpha', **kwargs):
    return search.search(project[1], search.SearchRequest(query=query, **kwargs))


def test_recursive_literal_unicode_case_and_parts(project):
    result = run(project)
    assert result['total'] == 4 and result['files'] == 2 and not result['limited']
    hit = next(m for m in result['matches'] if m['path'] == 'src/a.py' and m['line'] == 2)
    assert [p['text'] for p in hit['parts'] if p['match']] == ['alpha', 'ALPHA']
    assert hit['column'] == 6 and hit['offset'] == len('alpha = 1\r\n中文 😀 ')
    assert run(project, 'alpha.*')['total'] == 1
    assert run(project, 'ALPHA', case_sensitive=True)['total'] == 1
    assert run(project, whole_word=True)['total'] == 3


def test_regex_globs_and_directory_range(project):
    assert run(project, r'alpha\s*=\s*\d+', regex=True)['total'] == 1
    assert run(project, include='**/*.py')['files'] == 1
    assert run(project, exclude='**/*.py')['files'] == 1
    assert {m['path'] for m in run(project, path='src')['matches']} == {'src/a.py'}
    with pytest.raises(HTTPException):
        run(project, path='src/a.py')
    for query in ('[', r'(?<=a)b'):
        with pytest.raises(HTTPException) as error:
            run(project, query, regex=True)
        assert error.value.status_code == 400


def test_ignore_hidden_and_internal_directories(project):
    root, _ = project
    (root / '.gitignore').write_text('ignored.txt\n')
    (root / 'ignored.txt').write_text('alpha')
    (root / '.hidden.txt').write_text('alpha')
    for name in ('.git', 'node_modules', '.venv', '__pycache__'):
        (root / name).mkdir(); (root / name / 'secret.txt').write_text('alpha')
    assert run(project)['total'] == 4
    assert run(project, ignored=True)['total'] == 5
    assert run(project, hidden=True)['total'] == 5
    result = run(project, hidden=True, ignored=True)
    assert result['total'] == 6 and not any('secret' in m['path'] for m in result['matches'])
    assert not run(project, include='.hidden.txt')['matches']


def test_binary_non_utf8_and_oversize_are_not_returned(project):
    root, _ = project
    (root / 'binary.bin').write_bytes(b'alpha\n\x00alpha')
    (root / 'utf16.txt').write_bytes('alpha'.encode('utf-16'))
    (root / 'invalid.txt').write_bytes(b'alpha\n\xff')
    (root / 'large.txt').write_bytes(b'alpha\n' + b'a' * workspace.MAX_FILE_BYTES)
    assert run(project)['total'] == 4


def test_bom_crlf_empty_regex_and_long_line_snippets(project):
    root, _ = project
    (root / 'bom.txt').write_bytes(b'\xef\xbb\xbfalpha\r\nsecond alpha\r\n')
    result = run(project, include='bom.txt', case_sensitive=True)
    first, second = result['matches']
    assert first['column'] == 1 and first['offset'] == 0
    assert second['offset'] == len('alpha\r\nsecond ') and second['line'] == 2
    assert run(project, '^', regex=True, include='bom.txt')['total'] == 2
    (root / 'long.txt').write_text('x' * 12000 + 'alpha' + 'y' * 5000)
    hit = run(project, include='long.txt')['matches'][0]
    assert hit['column'] == 12001 and hit['offset'] == 12000 and hit['snippet_limited']
    assert sum(len(p['text']) for p in hit['parts']) <= 486


def test_snapshot_paging_and_changed_source(project):
    root, _ = project
    (root / 'many.txt').write_text('alpha\n' * 85)
    first = run(project, include='many.txt')
    assert len(first['matches']) == 40 and first['next_offset'] == 40
    second = run(project, include='many.txt', offset=40, snapshot=first['snapshot'])
    assert len(second['matches']) == 40 and second['matches'][0]['line'] == 41
    with pytest.raises(HTTPException):
        run(project, include='many.txt', offset=40)
    (root / 'many.txt').write_text('alpha changed\n' * 85)
    with pytest.raises(HTTPException) as error:
        run(project, include='many.txt', offset=40, snapshot=first['snapshot'])
    assert error.value.status_code == 409


def test_result_limit_is_explicit(project, monkeypatch):
    root, _ = project
    (root / 'many.txt').write_text('alpha\n' * 70)
    monkeypatch.setattr(search, 'MAX_MATCHES', 50)
    result = run(project, include='many.txt')
    assert result['total'] == 50 and result['limited'] and '匹配行数达到上限' in result['warnings']


def test_external_rg_config_cannot_inject_commands(project, tmp_path, monkeypatch):
    config = tmp_path / 'rg-config'
    config.write_text('--pre=nonexistent-command\n--follow\n--text\n')
    monkeypatch.setenv('RIPGREP_CONFIG_PATH', str(config))
    assert run(project)['total'] == 4


def test_junction_outside_project_is_not_searched(project, tmp_path):
    root, _ = project
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'secret.txt').write_text('alpha classified')
    link = root / 'escape'
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)], check=True, capture_output=True)
    try:
        assert not any(m['path'].startswith('escape') for m in run(project)['matches'])
        with pytest.raises(HTTPException):
            run(project, path='escape')
    finally:
        link.rmdir()


def test_replaced_root_and_file_handle_substitution(project, tmp_path, monkeypatch):
    root, _ = project
    moved = root.with_name('moved'); root.rename(moved); root.mkdir()
    try:
        with pytest.raises(HTTPException):
            run(project)
    finally:
        root.rmdir(); moved.rename(root)
    outside = tmp_path / 'secret.txt'; outside.write_text('alpha secret')
    original = Path.open
    def substitute(self, *args, **kwargs):
        return original(outside if self == root / 'src/a.py' else self, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', substitute)
    result = run(project)
    assert all(m['path'] != 'src/a.py' for m in result['matches'])
    assert 'secret' not in json.dumps(result)


def test_changed_hit_is_omitted_and_incomplete(project, monkeypatch):
    original = search._run
    def changing(root, req):
        result = original(root, req)
        (root / 'src/a.py').write_text('changed while searching')
        return result
    monkeypatch.setattr(search, '_run', changing)
    result = run(project)
    assert result['limited'] and result['total'] == 1


def test_output_and_time_limits_are_reported(project, monkeypatch):
    monkeypatch.setattr(search, 'MAX_OUTPUT', 200)
    assert run(project)['limited']
    monkeypatch.setattr(search, 'SCAN_SECONDS', -1)
    assert run(project)['limited']


def test_api_scope_preview_hash_draft_cas_and_tool_parameters(project):
    root, scope = project; client = TestClient(app)
    base = f"/api/projects/{scope['project_id']}"
    response = client.post(base + '/search', json={'query': 'alpha'})
    assert response.status_code == 200
    hit = response.json()['matches'][0]
    assert client.get(base + '/file', params={'path': hit['path'], 'sha256': hit['sha256']}).status_code == 200
    (root / hit['path']).write_text('new alpha')
    assert client.get(base + '/file', params={'path': hit['path'], 'sha256': hit['sha256']}).status_code == 409
    assert client.post(base + '/search', json={'query': 'alpha', 'path': '../outside'}).status_code == 400
    assert client.post(base + '/search', json={'query': 'alpha', 'project_id': 'other'}).status_code == 422
    path = f"/api/desktop-state/projects/{scope['project_id']}/search"
    assert client.put(path, json={'revision': 0, 'value': {'query': '中文', 'regex': True}}).status_code == 200
    assert client.put(path, json={'revision': 0, 'value': {'query': 'other'}}).status_code == 409
    assert client.get(path).json()['value']['query'] == '中文'
    assert tool_risk('search_project_files') == 'safe' and 'search_project_files' in PLAN_READ_TOOLS
    for arguments in ({'query': 'alpha', 'project_id': 'other'}, {'query': 'alpha', 'path': '../outside'}, {'query': ''}):
        assert 'error' in json.loads(execute_tool(None, 'search_project_files', arguments, project_scope=scope))


@pytest.mark.parametrize('mode', ['execute', 'plan'])
def test_real_agent_search_dispatch_uses_only_server_scope(project, db, monkeypatch, mode):
    from app.agent import loop
    from app.llm import LLMResponse
    from app.models import Conversation
    _, scope = project
    db.add(Conversation(id='search-agent', project_id=scope['project_id'])); db.commit()
    results = []
    class Model:
        def stream_chat(self, messages, tools=None):
            if not any(m['role'] == 'tool' for m in messages):
                assert 'search_project_files' in {t['name'] for t in tools}
                yield 'final', LLMResponse('', [{'id': 'search1', 'name': 'search_project_files', 'arguments': {'query': '中文'}}])
            else:
                results.extend(json.loads(m['content']) for m in messages if m['role'] == 'tool')
                yield 'final', LLMResponse('Found the Chinese text in src/a.py line 2.')
    monkeypatch.setattr(loop, 'get_llm', Model)
    monkeypatch.setattr(loop, 'make_plan', lambda *a: [])
    monkeypatch.setattr(loop.settings, 'memory_recall', False)
    events = list(loop.run_agent(db, 'Find source text', conversation_id='search-agent', execution_mode=mode,
                               workspace={**scope, 'files': [], 'rules': []}))
    assert results[0].get('matches', [{}])[0].get('path') == 'src/a.py', results
    assert not any(kind == 'approval_request' for kind, _ in events)
