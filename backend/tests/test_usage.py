"""token 用量统计测试：估算函数、端到端累计、会话聚合接口。"""
import json

from fastapi.testclient import TestClient

from app import llm
from app.main import app

client = TestClient(app)


def test_estimate_tokens_counts_cjk_and_english():
    assert llm.estimate_tokens("注意力机制") == 5  # 5 个中文字
    assert llm.estimate_tokens("") == 0
    # 纯英文 8 个字符约 2 token
    assert llm.estimate_tokens("abcdefgh") >= 1


def test_estimate_usage_shape():
    messages = [{"role": "user", "content": "解释注意力"}]
    resp = llm.LLMResponse(content="这是答案")
    usage = llm.estimate_usage(messages, resp)
    assert set(usage) == {"prompt_tokens", "completion_tokens", "total_tokens"}
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_chat_stream_returns_usage(seeded_paper):
    local_client = TestClient(app)
    answer_payload = None
    with local_client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "模型有多少注意力头", "paper_id": seeded_paper.id},
    ) as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                if payload.get("kind") == "answer":
                    answer_payload = payload
    assert answer_payload is not None
    usage = answer_payload["usage"]
    assert usage["total_tokens"] > 0
    assert usage["prompt_tokens"] >= 0
    assert usage["completion_tokens"] > 0


def test_usage_summary_endpoint(seeded_paper):
    local_client = TestClient(app)
    # 跑一轮产生记录，显式指定会话 ID；必须读完整个流再退出，否则生成器会被提前取消
    conv_id = "c_usage_test_001"
    with local_client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "模型 BLEU 多少", "paper_id": seeded_paper.id,
              "conversation_id": conv_id},
    ) as resp:
        assert resp.status_code == 200
        for line in resp.iter_lines():
            if line.startswith("data: "):
                json.loads(line[6:])  # 排空，确保 run_agent 跑到提交
    r = local_client.get(f"/api/conversations/{conv_id}/usage")
    data = r.json()
    assert data["turns"] >= 1
    assert data["usage"]["total_tokens"] > 0


def test_global_usage_empty():
    result = client.get("/api/conversations/usage/summary")
    assert result.status_code == 200
    payload = result.json()
    compaction = payload.pop('compaction')
    assert compaction == {'calls': 0, 'unknown_calls': 0, 'provider_calls': 0, 'estimated_calls': 0,
                          'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}
    assert payload == {"turns": 0, "conversations": 0, "usage": {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
    }}


def test_global_usage_deduplicates_nested_forks_and_counts_new_work(db):
    from app.models import Conversation, QARecord

    db.add(Conversation(id="original", title="Original"))
    db.add(QARecord(id="turn_one", conversation_id="original", question="Q", answer="A",
                    usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}))
    db.add(QARecord(id="no_usage", conversation_id="original", question="Legacy", answer="A"))
    db.commit()
    branch = client.post("/api/conversations/original/fork", json={}).json()["id"]
    nested = client.post(f"/api/conversations/{branch}/fork", json={}).json()["id"]
    db.add(QARecord(id="new_work", conversation_id=nested, question="New", answer="A",
                    usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28}))
    db.commit()
    result = client.get("/api/conversations/usage/summary").json()
    assert result.pop('compaction')['calls'] == 0
    assert result == {"turns": 2, "conversations": 3, "usage": {
        "prompt_tokens": 30, "completion_tokens": 13, "total_tokens": 43,
    }}
    assert client.get(f"/api/conversations/{nested}/usage").json()["usage"]["total_tokens"] == 43
    assert client.delete("/api/conversations/original").status_code == 200
    result = client.get("/api/conversations/usage/summary").json()
    assert result["turns"] == 2
    assert result["usage"]["total_tokens"] == 43


def test_usage_origin_migration_for_existing_database(monkeypatch):
    from sqlalchemy import create_engine, text
    from app import db as db_module

    legacy = create_engine("sqlite://")
    try:
        with legacy.begin() as connection:
            connection.execute(text("CREATE TABLE qa_records (id VARCHAR(32) PRIMARY KEY, usage JSON)"))
        monkeypatch.setattr(db_module, "engine", legacy)
        db_module._lightweight_migrations()
        db_module._lightweight_migrations()
        with legacy.connect() as connection:
            columns = {row[1] for row in connection.execute(text("PRAGMA table_info(qa_records)"))}
            assert "usage_origin_id" in columns
    finally:
        legacy.dispose()
