"""Real directory scope, context snapshots, chat association and recovery."""
import json
import subprocess
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app import workspace, task_runner
from app.agent import loop
from app.agent.checkpoint import AgentState
from app.agent.tools import execute_tool
from app.db import _lightweight_migrations
from app.llm import LLMResponse
from app.main import app
from app.models import Conversation, ForegroundRun, QARecord, TaskRecord

client = TestClient(app)


@pytest.fixture
def project(db, tmp_path):
    root = tmp_path / "local-project"
    root.mkdir()
    (root / "README.md").write_text("Unique project data. <img src=x onerror=alert(1)>\n" + "A" * 18000, encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "note.txt").write_text("Nested file data", encoding="utf-8")
    (root / "AGENTS.md").write_text("Root instructions", encoding="utf-8")
    result = workspace.register(db, str(root))
    db.add(Conversation(id="c_workspace", project_id=result.id))
    db.commit()
    return result


def scope(project):
    return workspace.project_scope(project)


def test_project_registration_and_pagination(project, db):
    assert workspace.register(db, str(Path(project.root_path) / ".")).id == project.id
    with pytest.raises(HTTPException):
        workspace.register(db, "relative/path")
    root = Path(project.root_path)
    for n in range(205):
        (root / f"f{n:03}.txt").write_text(str(n))
    first = workspace.list_files(scope(project))
    second = workspace.list_files(scope(project), offset=first["next_offset"])
    assert first["entries"][0]["kind"] == "directory"
    assert len(first["entries"]) == 200 and second["next_offset"] is None
    assert len({f["path"] for f in first["entries"] + second["entries"]}) == 208
    assert workspace.list_files(scope(project), query="README")["entries"][0]["path"] == "README.md"


@pytest.mark.parametrize("path", ["../secret.txt", "src/../../secret.txt", "C:\\secret.txt", "\\\\server\\share", "/etc/passwd", "README.md:secret", "src/../README.md", "src//note.txt", "src/./note.txt", "src./note.txt", "src/\x00file", ".git/config"])
def test_path_escape_rejected(project, path):
    with pytest.raises(HTTPException):
        workspace.read_text(scope(project), path)


def test_file_pages_binary_size_and_missing(project):
    first = workspace.read_text(scope(project), "README.md")
    second = workspace.read_text(scope(project), "README.md", offset=first["next_offset"])
    assert len(first["text"]) == 8000 and first["sha256"] == second["sha256"]
    root = Path(project.root_path)
    (root / "binary.bin").write_bytes(b"\x00hello")
    (root / "huge.txt").write_bytes(b"a" * (workspace.MAX_FILE_BYTES + 1))
    (root / "encoding.txt").write_bytes(b"\xff\xfe\x00")
    for path in ["binary.bin", "huge.txt", "encoding.txt", "missing.txt", "src"]:
        with pytest.raises(HTTPException):
            workspace.read_text(scope(project), path)


def test_junction_cannot_escape_or_be_listed(project, tmp_path):
    root = Path(project.root_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not read")
    link = root / "escape"
    if workspace.os.name == "nt":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(HTTPException):
            workspace.read_text(scope(project), "escape/secret.txt")
        assert "escape" not in [e["name"] for e in workspace.list_files(scope(project))["entries"]]
    finally:
        if workspace.os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_replaced_root_and_open_handle_substitution_rejected(project, tmp_path, monkeypatch):
    root = Path(project.root_path)
    moved = root.with_name("old-root")
    root.rename(moved)
    root.mkdir()
    with pytest.raises(HTTPException, match="409"):
        workspace.root_path(scope(project))
    root.rmdir()
    moved.rename(root)
    outside = tmp_path / "secret.txt"
    outside.write_text("not in project")
    original = Path.open
    def substitute(self, *a, **kw):
        return original(outside if self == root / "README.md" else self, *a, **kw)
    monkeypatch.setattr(Path, "open", substitute)
    with pytest.raises(HTTPException, match="409"):
        workspace.read_text(scope(project), "README.md")


def test_rules_override_hierarchy_scope_and_limits(project):
    root = Path(project.root_path)
    (root / "src" / "AGENTS.md").write_text("Nested instructions")
    (root / "src" / "AGENTS.override.md").write_text("Nested override")
    rules = workspace.directory_rules(scope(project), ["src/note.txt"])
    assert [r["path"] for r in rules] == ["AGENTS.md", "src/AGENTS.override.md"]
    assert rules[-1]["directory"] == "src"
    assert workspace.directory_rules(scope(project), ["README.md"]) == rules[:1]
    (root / "AGENTS.md").write_text("x" * 32769)
    with pytest.raises(HTTPException):
        workspace.directory_rules(scope(project), [])


def test_project_binding_immutable_and_deleted_archive_protected(project, db):
    pid = project.id
    body = {"project_id": pid}
    assert client.put('/api/conversations/c_new/project', json=body).status_code == 200
    assert client.put('/api/conversations/c_new/project', json=body).status_code == 200
    other_root = Path(project.root_path).with_name("another")
    other_root.mkdir()
    other = workspace.register(db, str(other_root)); db.commit()
    assert client.put('/api/conversations/c_new/project', json={"project_id": other.id}).status_code == 409
    db.add(Conversation(id="c_started")); db.add(QARecord(conversation_id="c_started", question="before", answer="A")); db.commit()
    assert client.put('/api/conversations/c_started/project', json=body).status_code == 409
    conv = db.get(Conversation, "c_workspace"); conv.archived_at = datetime.utcnow(); db.commit()
    assert client.put('/api/conversations/c_workspace/project', json=body).status_code == 409
    assert client.delete('/api/conversations/c_new').status_code == 200
    assert client.put('/api/conversations/c_new/project', json=body).status_code == 410


def test_project_init_never_overwrites(project):
    root = Path(project.root_path)
    before = (root / "AGENTS.md").read_bytes()
    assert client.post(f'/api/projects/{project.id}/init').json()["created"] is False
    assert (root / "AGENTS.md").read_bytes() == before
    (root / "AGENTS.md").unlink()
    assert client.post(f'/api/projects/{project.id}/init').json()["created"] is True


def test_snapshot_and_model_tool_scope(project, db):
    saved = workspace.capture(db, "c_workspace", project.id, ["README.md", "src/note.txt"])
    assert len(saved["files"]) == 2 and saved["files"][0]["next_offset"] == 8000
    (Path(project.root_path) / "README.md").write_text("new data")
    assert "Unique project data" in saved["files"][0]["text"]
    data = json.loads(execute_tool(db, "read_project_file", {"path": "README.md"}, project_scope=saved))
    assert data["text"] == "new data" and data["rules"][0]["path"] == "AGENTS.md"
    for arguments in [{"path": "../secret.txt"}, {"path": "README.md", "root": "C:/"}, {"path": "README.md", "project_id": "other"}]:
        assert "error" in json.loads(execute_tool(db, "read_project_file", arguments, project_scope=saved))
    assert "error" in json.loads(execute_tool(db, "list_project_files", {}))
    with pytest.raises(HTTPException):
        workspace.capture(db, "c_workspace", None, [])


class CaptureModel:
    def __init__(self): self.messages = None
    def stream_chat(self, messages, tools=None):
        self.messages = deepcopy(messages)
        yield "final", LLMResponse("Plan: inspect the local files before producing changes.")


def test_foreground_snapshot_injection_plan_handoff_and_fork(project, db, monkeypatch):
    model = CaptureModel()
    monkeypatch.setattr(loop, "get_llm", lambda: model)
    monkeypatch.setattr(loop, "make_plan", lambda *a: [])
    monkeypatch.setattr(loop.settings, "memory_recall", False)
    body = dict(id="chat_workspace_plan", conversation_id="c_workspace", question="Review the project",
                project_id=project.id, file_paths=["src/note.txt"], execution_mode="plan")
    assert client.post('/api/foreground-runs', json=body).status_code == 200
    (Path(project.root_path) / "src/note.txt").write_text("changed after admission")
    response = client.post('/api/chat/stream', json={**body, "run_id": body["id"]})
    assert response.status_code == 200 and 'event: answer' in response.text
    assert "Nested file data" in model.messages[-1]["content"]
    assert "changed after admission" not in model.messages[-1]["content"]
    assert "Root instructions" in model.messages[0]["content"]
    db.expire_all()
    plan = db.query(QARecord).filter_by(conversation_id="c_workspace").one()
    assert plan.workspace["files"][0]["text"] == "Nested file data"
    # Reject plan execution against files changed since planning.
    execution = {**body, "id": "chat_workspace_execute", "execution_mode": "execute", "reviewed_plan_id": plan.id}
    assert client.post('/api/foreground-runs', json=execution).status_code == 409
    (Path(project.root_path) / "src/note.txt").write_text("Nested file data")
    assert client.post('/api/foreground-runs', json=execution).status_code == 200
    fork = client.post('/api/conversations/c_workspace/fork', json={}).json()
    assert client.get(f'/api/conversations/{fork["id"]}/project').json()["project"]["id"] == project.id
    db.expire_all()
    assert db.query(QARecord).filter_by(conversation_id=fork["id"]).one().workspace == plan.workspace


def test_background_snapshot_and_checkpoint_scope(project, db, monkeypatch):
    monkeypatch.setattr(task_runner, "_launch", lambda db, task: {"id": task.id})
    task = client.post('/api/tasks/start', json=dict(question="Inspect files", conversation_id="c_workspace", project_id=project.id, file_paths=["src/note.txt"], execution_mode="plan"))
    assert task.status_code == 200
    db.expire_all(); row = db.get(TaskRecord, task.json()["id"])
    saved = row.payload["workspace"]
    assert saved["project_id"] == project.id
    row.status = "interrupted"
    row.checkpoint = AgentState(question="Inspect files", conversation_id="c_workspace", execution_mode="plan", workspace=saved).snapshot()
    db.commit()
    assert client.post(f'/api/tasks/{row.id}/resume', json={}).status_code == 200
    db.expire_all(); row = db.get(TaskRecord, row.id)
    invalid = deepcopy(row.checkpoint); invalid["workspace"]["root_path"] = "C:/"
    row.checkpoint = invalid; row.status = "interrupted"; db.commit()
    assert client.post(f'/api/tasks/{row.id}/resume', json={}).status_code == 409


def test_resumed_execution_uses_saved_context_and_scoped_file_tool(project, db, monkeypatch):
    saved = workspace.capture(db, 'c_workspace', project.id, ['src/note.txt'])
    seen = []
    class Reader:
        def stream_chat(self, messages, tools=None):
            seen.append(deepcopy(messages))
            if not any(m['role'] == 'tool' for m in messages):
                yield 'final', LLMResponse('', [{'id': 'file1', 'name': 'read_project_file', 'arguments': {'path': 'src/note.txt'}}])
            else:
                yield 'final', LLMResponse('Plan: inspect src/notes.txt and then implement the changes.')
    monkeypatch.setattr(loop, 'get_llm', Reader)
    monkeypatch.setattr(loop, 'make_plan', lambda *a: [])
    monkeypatch.setattr(loop.settings, 'memory_recall', False)
    class PowerLoss(BaseException): pass
    snapshots = []
    def persist(value):
        snapshots.append(deepcopy(value))
        if value['phase'] == 'tools': raise PowerLoss()
    with pytest.raises(PowerLoss):
        list(loop.run_agent(db, 'Inspect', conversation_id='c_workspace', execution_mode='plan', workspace=saved, save_checkpoint=persist))
    checkpoint = snapshots[-1]
    (Path(project.root_path) / 'src/note.txt').write_text('New on disk, original remains in checkpoint')
    result = list(loop.run_agent(db, 'Inspect', conversation_id='c_workspace', execution_mode='plan', workspace=saved, checkpoint=checkpoint))
    assert any(kind == 'answer' for kind, _ in result)
    assert 'Nested file data' in next(m['content'] for m in seen[-1] if m['role'] == 'user')
    tool = json.loads(next(m['content'] for m in seen[-1] if m['role'] == 'tool'))
    assert tool['text'].startswith('New on disk') and tool['path'] == 'src/note.txt'
    assert db.query(QARecord).filter_by(conversation_id='c_workspace').count() == 1


def test_project_chat_pagination_archives_and_metadata(project, db):
    pid = project.id
    for i in range(45):
        db.add(Conversation(id=f"c_page_{i}", title=f"Project chat {i}", project_id=pid, updated_at=datetime(2020, 1, 1) + timedelta(minutes=i)))
    db.add(Conversation(id="c_archive", project_id=pid, archived_at=datetime.utcnow()))
    db.add(Conversation(id="c_unscoped")); db.commit()
    first = client.get(f'/api/projects/{pid}/conversations').json()
    second = client.get(f'/api/projects/{pid}/conversations', params={"cursor": first["next_cursor"]}).json()
    rows = first["conversations"] + second["conversations"]
    assert len(rows) == len({c["id"] for c in rows}) == 46
    assert all(c["project_id"] == pid for c in rows)
    assert second["next_cursor"] is None
    assert client.get(f'/api/projects/{pid}/conversations?cursor=invalid').status_code == 400


def test_old_database_migration(tmp_path, monkeypatch):
    import app.db as module
    engine = create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE conversations (id TEXT PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE qa_records (id TEXT PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE foreground_runs (id TEXT PRIMARY KEY, updated_at DATETIME)"))
    monkeypatch.setattr(module, "engine", engine)
    _lightweight_migrations()
    with engine.connect() as conn:
        assert "project_id" in [r[1] for r in conn.execute(text("PRAGMA table_info(conversations)"))]
        for table in ["qa_records", "foreground_runs"]:
            assert "workspace" in [r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))]
    engine.dispose()
