"""端到端 API 冒烟测试（Mock LLM + 本地向量，全程无外部 API）。"""
import textwrap
from datetime import datetime

from fastapi.testclient import TestClient
from reportlab.pdfgen import canvas

from app.main import app
from app.models import Conversation, QARecord

client = TestClient(app)


def test_conversation_fork_preserves_usage_and_memory_flags(db):
    """分叉会话时保留原消息的记忆开关与 token 快照。"""
    source_id = "c_fork_source"
    db.add(Conversation(id=source_id, title="源会话", created_at=datetime.utcnow(), updated_at=datetime.utcnow()))
    db.add(QARecord(
        id="qa_fork_source", conversation_id=source_id, question="问题", answer="回答",
        evidence=[], claims=[], trace=[], skills=[], refusal=0, latency_ms=12,
        in_memory=0, usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
        created_at=datetime.utcnow(),
    ))
    db.commit()

    response = client.post(f"/api/conversations/{source_id}/fork", json={})
    assert response.status_code == 200
    fork_id = response.json()["id"]
    forked = db.query(QARecord).filter(QARecord.conversation_id == fork_id).one()
    assert forked.in_memory == 0
    assert forked.usage == {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}


def test_health_is_mock():
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["llm_mode"] == "mock"
    assert data["embedding_mode"] == "local"


def test_upload_and_parse_flow(tmp_path):
    path = tmp_path / "tiny.pdf"
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica", 14)
    c.drawString(50, 780, "Mini Paper")
    c.setFont("Helvetica", 10)
    y = 750
    for piece in textwrap.wrap("The Transformer uses multi-head attention with 8 heads and reaches 28.4 BLEU.", 90):
        c.drawString(50, y, piece)
        y -= 16
    c.save()

    with open(path, "rb") as f:
        r = client.post("/api/papers/upload", files={"file": ("tiny.pdf", f, "application/pdf")})
    assert r.status_code == 200, r.text
    paper = r.json()
    assert paper["status"] == "parsed"
    assert paper["page_count"] == 1

    # 页面数据可读
    r2 = client.get(f"/api/papers/{paper['id']}/pages/1")
    assert r2.status_code == 200
    assert "multi-head" in r2.json()["text"]

    # 论文列表
    r3 = client.get("/api/papers")
    assert any(p["id"] == paper["id"] for p in r3.json())


def test_upload_duplicate_is_deduped(tmp_path):
    path = tmp_path / "dup.pdf"
    c = canvas.Canvas(str(path), pagesize=(595, 842))
    c.setFont("Helvetica", 14)
    c.drawString(50, 780, "Duplicate Paper")
    c.setFont("Helvetica", 10)
    c.drawString(50, 750, "The model uses 8 attention heads and 6 encoder layers.")
    c.save()

    with open(path, "rb") as f:
        first = client.post("/api/papers/upload",
                            files={"file": ("dup.pdf", f, "application/pdf")}).json()
    assert first["duplicate"] is False

    # 再次上传同一文件：应命中内容哈希，返回已有论文
    with open(path, "rb") as f:
        second = client.post("/api/papers/upload",
                             files={"file": ("dup.pdf", f, "application/pdf")}).json()
    assert second["duplicate"] is True
    assert second["id"] == first["id"]

    # 论文总数没有增加
    papers = client.get("/api/papers").json()
    assert sum(1 for p in papers if p["id"] == first["id"]) == 1


def test_chat_stream_has_evidence(seeded_paper):
    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"question": "how many attention heads does the model use", "paper_id": seeded_paper.id},
    ) as resp:
        assert resp.status_code == 200
        event_kinds = []
        answer_payload = None
        for line in resp.iter_lines():
            if line.startswith("event: "):
                event_kinds.append(line.split("event: ")[1])
            elif line.startswith("data: ") and "answer" in line:
                import json
                answer_payload = json.loads(line[6:])
        assert "tool_call" in event_kinds
        assert "answer" in event_kinds
        assert answer_payload is not None
        assert "[^e1]" in answer_payload["answer"]
        assert answer_payload["gate"]["passed"] is True
