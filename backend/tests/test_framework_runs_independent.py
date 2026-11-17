"""Root review: corrupted paid receipt cannot be presented or bought again."""
from copy import deepcopy
import json
from uuid import uuid4
import pytest
from sqlalchemy import text
from app import research_runs, run_store, model_profiles, source_store
from app.db import SessionLocal
from app.models import Conversation
from app.runtime_models import RuntimeRun, RunAttempt
from app.llm import LLMResponse
from app.writing import framework_run
from tests.test_framework_evidence import seed, response, CID


def test_corrupted_known_generation_never_replays_a_paid_request(db, monkeypatch):
    seed(db, b"attention is measured\nFixed passage for independent review.", "text/plain")
    selected = model_profiles.save_profile(db, {"name": "Independent framework", "base_url": "https://independent-model.example.test/v1",
        "model_id": "Exact-Independent-Model", "api_key": "synthetic-independent-secret"})
    model_profiles.set_default(db, selected.id); db.commit()
    calls = []
    class Model:
        def chat(self, messages, **kwargs):
            calls.append(deepcopy(messages))
            if "独立的论点支持审查者" in messages[0]["content"]:
                return LLMResponse(json.dumps({"checks": []}))
            return LLMResponse(json.dumps(response()))
    monkeypatch.setattr(framework_run, "get_llm", Model)
    rid = "research_" + uuid4().hex
    db.rollback(); db.execute(text("BEGIN IMMEDIATE"))
    _, attempt, _ = research_runs.create(db, kind="framework", run_id=rid, conversation_id=CID,
        inputs={"idea": "attention measured", "max_sources": 8}, request_id="independent-create")
    db.commit(); research_runs.execute(rid, attempt.id)
    db.expire_all()
    first = research_runs.public(db, rid, CID)
    assert first["outcome"] == "partial" and len(calls) == 2
    checkpoint = deepcopy(db.get(RunAttempt, attempt.id).checkpoint)
    key = next(k for k in checkpoint["operations"] if k.startswith("research:llm:0:"))
    checkpoint["operations"][key]["payload"]["content"] = "CORRUPTED SUCCESS RECEIPT"
    db.get(RunAttempt, attempt.id).checkpoint = checkpoint; db.commit()
    revision = db.get(RuntimeRun, rid).revision; db.rollback()
    db.execute(text("BEGIN IMMEDIATE"))
    _, replacement, _ = research_runs.resume(db, rid, conversation_id=CID, request_id="independent-resume",
        expected_revision=revision)
    db.commit()
    with pytest.raises(source_store.SourceCorruption, match="回执校验失败"):
        research_runs.execute(rid, replacement.id)
    assert len(calls) == 2
    with SessionLocal() as reader:
        result = research_runs.public(reader, rid, CID)
        assert result["result"] == first["result"]
