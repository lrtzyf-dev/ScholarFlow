"""Independent saved-history/restore review through the production executor."""
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException

from app import approval_store, artifact_store as store
from app.agent import loop
from app.artifact_models import ArtifactChange, ArtifactManifest, ArtifactRevision
from app.artifact_tools import execute
from app.llm import LLMResponse
from app.models import Conversation


def saved(db):
    cid = "independent-saved-versions"
    db.add(Conversation(id=cid)); db.commit()
    item = store.create(db, conversation_id=cid, request_id="independent-initial", title="研究备忘录",
        kind="document", content="# 方法\n原始方法\n\n# 结果\n未修改内容\n", evidence=[], analysis={})
    row = store.get(db, item["id"], cid)
    change = store.propose(db, row, request_id="independent-second", base_revision_id=row.current_revision_id,
        content="# 方法\n第二版方法\n\n# 结果\n未修改内容\n")
    store.decide(db, row, db.get(ArtifactChange, change["id"]), accept=True, expected_revision_id=row.current_revision_id)
    return cid, row, item["current_revision_id"]


@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_real_executor_lists_then_reads_and_proposes_only_after_approval(db, monkeypatch, decision):
    cid, row, old = saved(db)
    head = row.current_revision_id
    monkeypatch.setattr(loop.settings, "approval_mode", "auto")
    monkeypatch.setattr(loop.settings, "memory_recall", False)
    monkeypatch.setattr(loop.goal_store, "get_current_goal", lambda: None)
    offered = []
    calls = [
        ("list_research_artifact_versions", {"artifact_id": row.id, "limit": 2}),
        ("read_research_artifact", {"artifact_id": row.id, "revision_id": head}),
        ("read_research_artifact", {"artifact_id": row.id, "revision_id": old}),
        ("propose_research_edit", {"artifact_id": row.id, "base_revision_id": head,
            "restored_from": old, "request_id": "independent-restore"}),
    ]
    class Model:
        def stream_chat(self, messages, tools=None):
            index = len(offered); offered.append(deepcopy(tools))
            names = {s["name"] for s in tools}
            assert "list_research_artifact_versions" in names and "git_project_status" not in names
            if index < len(calls):
                name, args = calls[index]
                yield "final", LLMResponse("", tool_calls=[{"id": f"history-{index}", "name": name, "arguments": args}])
            else:
                yield "final", LLMResponse("恢复修改保留为待审草稿。" if decision == "approve" else "本轮没有保存恢复草稿。")
    monkeypatch.setattr(loop, "get_llm", lambda: Model())
    events, requests = [], []
    source = loop.run_agent(db, "为备忘录准备恢复最初版的待审草稿。", conversation_id=cid)
    for kind, value in source:
        events.append((kind, value))
        if kind == "approval_request":
            requests.append(value)
            assert value["tool_name"] == "propose_research_edit"
            approval_store.resolve(value["approval_id"], decision)
    assert len(requests) == 1
    assert [e["name"] for kind, e in events if kind == "tool_call"] == [x[0] for x in calls]
    assert any(kind == "answer" for kind, _ in events)
    db.expire_all(); after = db.get(ArtifactManifest, row.id)
    assert after.current_revision_id == head and after.version == 2
    assert db.query(ArtifactRevision).filter_by(artifact_id=row.id).count() == 2
    changes = db.query(ArtifactChange).filter_by(artifact_id=row.id, status="proposed").all()
    if decision == "approve":
        assert len(changes) == 1 and changes[0].restored_from == old
        assert changes[0].result_revision_id is None and "原始方法" in changes[0].content
    else:
        assert changes == []


def test_pending_restore_survives_new_head_but_cannot_be_applied_over_it(db):
    cid, row, old = saved(db)
    head = row.current_revision_id
    args = {"artifact_id": row.id, "base_revision_id": head,
        "restored_from": old, "request_id": "independent-cas-restore"}
    pending = execute(db, "propose_research_edit", args, conversation_id=cid)
    concurrent = store.propose(db, row, request_id="independent-concurrent", base_revision_id=head, content="外部已接受的新修改")
    store.decide(db, row, db.get(ArtifactChange, concurrent["id"]), accept=True, expected_revision_id=head)
    with pytest.raises(HTTPException) as error:
        store.decide(db, row, db.get(ArtifactChange, pending["change_id"]), accept=True, expected_revision_id=head)
    assert error.value.status_code == 409
    retry = execute(db, "propose_research_edit", args, conversation_id=cid)
    assert retry["change_id"] == pending["change_id"] and retry["conflict"]
    assert row.version == 3 and store.revision(db, row).content == "外部已接受的新修改"
    assert db.get(ArtifactChange, pending["change_id"]).status == "proposed"


@pytest.mark.parametrize("arguments", [
    {"restored_from": None}, {"content": None}, {"section": None},
    {"restored_from": "0" * 32, "content": ""},
])
def test_null_or_ambiguous_restore_never_saves_a_draft(db, arguments):
    cid, row, _ = saved(db)
    before = db.query(ArtifactChange).count()
    with pytest.raises(HTTPException) as error:
        execute(db, "propose_research_edit", {"artifact_id": row.id,
            "request_id": "independent-invalid", "base_revision_id": row.current_revision_id, **arguments}, conversation_id=cid)
    assert error.value.status_code == 422
    assert db.query(ArtifactChange).count() == before
    assert db.query(ArtifactRevision).filter_by(artifact_id=row.id).count() == 2
