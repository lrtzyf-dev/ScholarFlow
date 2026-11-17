import json
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.db import SessionLocal
from app import config, product_journey as journey, source_store
from app.models import Conversation, ConversationOptions, QARecord
from app.source_runtime import freeze_sources

client=TestClient(app)


def test_new_install_guide_is_local_skippable_and_resumable_without_changing_models(db,monkeypatch):
    import httpx
    monkeypatch.setattr(httpx,"Client",lambda *a,**kw:pytest.fail("onboarding attempted HTTP"))
    monkeypatch.setattr(config.settings,"llm_demo_mode",False)
    monkeypatch.setattr(config.settings,"llm_api_key","")
    snapshot=journey.snapshot(db)
    assert snapshot["show_automatically"] and snapshot["revision"]==0
    assert snapshot["model"]["mode"]=="unconfigured" and not snapshot["model"]["configured"]
    assert db.get(journey.OnboardingState,"first-research") is None
    original=journey.snapshot(db)["model"]
    journey.transition(db,"skip",0);db.commit()
    with SessionLocal() as other:
        state=journey.snapshot(other)
        assert state["status"]=="dismissed" and not state["show_automatically"]
        journey.transition(other,"reopen",state["revision"]);other.commit()
        assert journey.snapshot(other)["model"]==original
    db.expire_all()
    with pytest.raises(ValueError,match="已更新"):
        journey.transition(db,"skip",0)


def test_existing_history_never_forces_guide_or_implicitly_enables_demo(db):
    db.add(Conversation(id="experienced"));db.add(QARecord(id="old",conversation_id="experienced",question="existing",answer="answer"));db.commit()
    before=journey.snapshot(db)
    assert not before["show_automatically"]
    journey.transition(db,"demo_seen",0);db.commit()
    after=journey.snapshot(db)
    assert after["demo_seen"] and after["model"]==before["model"]
    assert db.query(QARecord).count()==1
    with pytest.raises(ValueError,match="尚有"):
        journey.transition(db,"complete",after["revision"])


def test_citation_milestone_requires_authorized_fixed_locator_and_real_answer(db):
    db.add(Conversation(id="journey"));db.flush()
    scope=source_store.SourceScope("conversation","journey")
    source=source_store.capture_source(db,scope=scope,authorization=source_store.SourceAuthorization(scope,"journey-source","upload"),
        kind="upload",title="private-study.md",raw_bytes=b"value 17",media_type="text/markdown",text="value 17",text_origin="provided_text")
    locator=source_store.add_text_locator(db,source["revision_id"],scope=scope,line_start=1,line_end=1)
    record=QARecord(id="journey-qa",conversation_id="journey",question="private question",answer="value 17",
        evidence=[locator],runtime_config=freeze_sources(db,"journey",[source["revision_id"]],{"mode":"mock"}))
    db.add(record);db.commit()
    assert not journey.citation_opened(db,"journey",source["revision_id"],locator["locator_id"])
    record.runtime_config={**record.runtime_config,"mode":"api"};db.commit()
    assert journey.citation_opened(db,"journey",source["revision_id"],locator["locator_id"])
    db.commit()
    state=journey.snapshot(db)
    assert next(step for step in state["milestones"] if step["id"]=="citation")["complete"]
    assert "private-study" not in json.dumps(db.get(journey.OnboardingState,"first-research").events)
    db.add(Conversation(id="other-journey"));db.commit()
    response=client.post('/api/onboarding/citation-visit',json={"conversation_id":"other-journey","revision_id":source["revision_id"],"locator_id":locator["locator_id"]})
    assert response.status_code==409
