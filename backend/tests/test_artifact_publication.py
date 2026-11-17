from fastapi.testclient import TestClient
from app.main import app
from app.models import ProjectChange

client = TestClient(app)


def setup(tmp_path):
    project = client.post('/api/projects', json={'path':str(tmp_path)}).json()
    conv = client.post('/api/conversations', json={'title':'成果发布', 'project_id':project['id']}).json()
    created = client.post('/api/artifacts', json={'request_id':'publication-create123', 'conversation_id':conv['id'],
        'title':'科研结论', 'content':'# 方法\n固定版本正文 [^e7]\n',
        'evidence':[{'id':'e7', 'paper_title':'旧参考', 'quote':'原句'}]})
    assert created.status_code == 200, created.text
    artifact = created.json()
    return artifact, {'conversation_id':conv['id'], 'request_id':'publish-123456789', 'revision_id':artifact['current_revision_id'],
        'project_id':project['id'], 'workspace_revision':0, 'path':'reports/report.md', 'expected_file_sha256':None}


def test_fixed_publication_review_apply_retry_and_undo(tmp_path, db):
    artifact, payload = setup(tmp_path)
    endpoint = f"/api/artifacts/{artifact['id']}/publish-proposal"
    response = client.post(endpoint, json=payload)
    assert response.status_code == 200, response.text
    change = response.json()
    assert not (tmp_path/'reports/report.md').exists()
    assert client.post(endpoint,json=payload).json()['id'] == change['id']
    assert db.query(ProjectChange).count() == 1
    detail = client.get(f"/api/project-changes/{change['id']}/content?version=after").json()['content']
    assert '[^e7]: ' in detail and '原句' in detail
    decision = client.post(f"/api/project-changes/{change['id']}/apply",json={'revision':change['revision']})
    assert decision.status_code == 200, decision.text
    assert (tmp_path/'reports/report.md').read_text(encoding='utf-8') == detail
    assert client.post(endpoint,json=payload).json()['id'] == change['id']
    undo = client.post(f"/api/project-changes/{change['id']}/undo", json={'conversation_id':payload['conversation_id']})
    assert undo.status_code == 200, undo.text
    reverse = undo.json()
    assert client.post(f"/api/project-changes/{reverse['id']}/apply",json={'revision':reverse['revision']}).status_code == 200
    assert not (tmp_path/'reports/report.md').exists()


def test_changed_disk_or_scope_never_overwritten(tmp_path):
    artifact, payload = setup(tmp_path)
    endpoint = f"/api/artifacts/{artifact['id']}/publish-proposal"
    (tmp_path/'reports').mkdir()
    (tmp_path/'reports/report.md').write_text('人工写入',encoding='utf-8')
    assert client.post(endpoint,json=payload).status_code == 409
    payload['expected_file_sha256'] = client.get(f"/api/projects/{payload['project_id']}/editable-file",params={'path':payload['path']}).json()['sha256']
    proposed = client.post(endpoint,json=payload)
    assert proposed.status_code == 200, proposed.text
    (tmp_path/'reports/report.md').write_text('后来输入',encoding='utf-8')
    change = proposed.json()
    assert client.post(f"/api/project-changes/{change['id']}/apply",json={'revision':change['revision']}).status_code == 409
    assert (tmp_path/'reports/report.md').read_text(encoding='utf-8') == '后来输入'
    payload['workspace_revision'] = 99
    assert client.post(endpoint,json=payload).status_code == 409
    payload['workspace_revision'] = 0; payload['path'] = '../outside.md'
    assert client.post(endpoint,json=payload).status_code == 400


def test_reject_changes_no_files_and_other_conversation_scope_denied(tmp_path):
    artifact, payload = setup(tmp_path)
    endpoint = f"/api/artifacts/{artifact['id']}/publish-proposal"
    other = client.post('/api/conversations',json={'title':'other'}).json()['id']
    assert client.post(endpoint,json={**payload,'conversation_id':other}).status_code == 404
    change = client.post(endpoint,json=payload).json()
    assert client.post(f"/api/project-changes/{change['id']}/reject",json={'revision':change['revision']}).status_code == 200
    assert not (tmp_path/'reports').exists()
