"""记忆开关回归：使用记忆（recall）与生成记忆（capture）。"""
from app.agent import loop as loop_mod
from app.config import settings
from app.llm import LLMResponse, _chunk
from app.models import QARecord


def _add_turn(db, cid, q, a, in_memory=1):
    rec = QARecord(conversation_id=cid, question=q, answer=a, in_memory=in_memory)
    db.add(rec)
    db.commit()
    return rec


def test_load_history_returns_prior_turns(db):
    _add_turn(db, "c1", "问题一", "答案一")
    _add_turn(db, "c1", "问题二", "答案二")
    msgs, info = loop_mod._load_history(db, "c1")
    assert info["total"] == 2
    # 每轮 user + assistant 两条消息
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == "问题一"


def test_recall_off_returns_empty(db, monkeypatch):
    monkeypatch.setattr(settings, "memory_recall", False)
    _add_turn(db, "c1", "问题一", "答案一")
    msgs, info = loop_mod._load_history(db, "c1")
    assert msgs == []
    assert info.get("recall_off") is True


def test_excludes_in_memory_zero(db):
    _add_turn(db, "c1", "可见轮次", "答案", in_memory=1)
    _add_turn(db, "c1", "静默轮次", "答案", in_memory=0)
    msgs, info = loop_mod._load_history(db, "c1")
    assert info["total"] == 1
    assert msgs[0]["content"] == "可见轮次"


class _CaptureFakeLLM:
    """一轮检索 + 一轮作答，用于跑通完整 run_agent。"""

    def __init__(self):
        self.calls = 0

    def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield ("final", LLMResponse(content="", tool_calls=[
                {"id": "c1", "name": "retrieve_evidence",
                 "arguments": {"query": "heads", "k": 2}}]))
            return
        text = "已根据证据作答 e1[^e1]"
        for piece in _chunk(text):
            yield ("delta", "answer", piece)
        yield ("final", LLMResponse(content=text))


def test_capture_off_saves_record_in_memory_zero(db, seeded_paper, monkeypatch):
    monkeypatch.setattr(settings, "memory_capture", False)
    monkeypatch.setattr(loop_mod, "get_llm", lambda: _CaptureFakeLLM())
    cid = "conv_capture_off"

    kinds = [k for k, _ in loop_mod.run_agent(
        db, "x", paper_id=seeded_paper.id, conversation_id=cid)]
    assert "answer" in kinds

    rec = db.query(QARecord).filter(QARecord.conversation_id == cid).first()
    assert rec is not None
    # 记录仍保存，但不进入未来记忆
    assert rec.in_memory == 0


def test_capture_on_default_saves_in_memory_one(db, seeded_paper, monkeypatch):
    monkeypatch.setattr(settings, "memory_capture", True)
    monkeypatch.setattr(loop_mod, "get_llm", lambda: _CaptureFakeLLM())
    cid = "conv_capture_on"

    list(loop_mod.run_agent(db, "x", paper_id=seeded_paper.id, conversation_id=cid))
    rec = db.query(QARecord).filter(QARecord.conversation_id == cid).first()
    assert rec.in_memory == 1
