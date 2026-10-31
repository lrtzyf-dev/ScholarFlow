"""Agent 主循环回归：并行工具调用、证据 id 全局唯一、工具异常隔离。"""
import json

from app.agent import loop as loop_mod
from app.llm import LLMResponse, _chunk


class FakeLLM:
    """按脚本返回：第一轮两个并行检索，第二轮基于证据作答（流式）。"""

    def __init__(self):
        self.calls: list[list[dict]] = []

    def stream_chat(self, messages, tools=None):
        self.calls.append([dict(m) for m in messages])
        if len(self.calls) == 1:
            yield ("final", LLMResponse(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "retrieve_evidence",
                     "arguments": {"query": "attention heads", "k": 3}},
                    {"id": "c2", "name": "retrieve_evidence",
                     "arguments": {"query": "BLEU", "k": 3}},
                ],
            ))
            return
        # 第二轮：逐句引用全部出现过的证据 id（标记格式 [^eN]），并逐字流式
        tool_ev_ids = []
        for m in messages:
            if m["role"] != "tool":
                continue
            try:
                payload = json.loads(m["content"])
            except json.JSONDecodeError:
                continue
            tool_ev_ids.extend(e["id"] for e in payload.get("evidence", []))
        text = "\n".join(f"模型相关结论见证据 {eid}[^{eid}]" for eid in tool_ev_ids)
        for piece in _chunk(text):
            yield ("delta", "answer", piece)
        yield ("final", LLMResponse(content=text))


def test_parallel_calls_and_unique_evidence(db, seeded_paper, monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(loop_mod, "get_llm", lambda: fake)

    events = []
    for kind, event in loop_mod.run_agent(db, "模型有多少注意力头，BLEU 多少？",
                                          paper_id=seeded_paper.id):
        events.append((kind, event))

    answer_ev = next(e for k, e in events if k == "answer")
    ids = answer_ev["evidence_ids"]

    # 证据 id 全局唯一、连续，无两次检索各自从 e1 开始的冲突
    assert len(ids) == len(set(ids)), ids
    assert ids == [f"e{i}" for i in range(1, len(ids) + 1)], ids

    # 第二轮模型看到的消息：恰好一条 assistant 承载 2 个 tool_calls，其后跟 2 条 tool
    second = fake.calls[1]
    assistant_tool = [m for m in second if m["role"] == "assistant" and m.get("tool_calls")]
    assert len(assistant_tool) == 1
    assert len(assistant_tool[0]["tool_calls"]) == 2
    idx = second.index(assistant_tool[0])
    assert second[idx + 1]["role"] == "tool"
    assert second[idx + 2]["role"] == "tool"


def test_guarded_tool_approval_flow(db, seeded_paper, monkeypatch):
    """高风险工具（web_fetch）：先收到 approval_request，批准后才执行；拒绝则跳过。"""
    from app import approval_store

    class WebFetchLLM:
        def stream_chat(self, messages, tools=None):
            if not any(m["role"] == "tool" for m in messages):
                yield ("final", LLMResponse(content="", tool_calls=[
                    {"id": "c1", "name": "mcp__builtin__web_fetch",
                     "arguments": {"url": "https://example.com"}}]))
                return
            text = "已读取网页内容 e1[^e1]"
            for piece in _chunk(text):
                yield ("delta", "answer", piece)
            yield ("final", LLMResponse(content=text))

    monkeypatch.setattr(loop_mod, "get_llm", lambda: WebFetchLLM())
    loop_mod.settings.approval_mode = "auto"

    gen = loop_mod.run_agent(db, "抓取该网页", paper_id=seeded_paper.id)
    events, apv_id = [], None
    while True:
        try:
            kind, ev = next(gen)
        except StopIteration:
            break
        events.append((kind, ev))
        if kind == "approval_request":
            apv_id = ev["approval_id"]
            assert ev["tool_name"] == "mcp__builtin__web_fetch"
            # 批准一次（生成器正暂停在 yield，事件已被 set，恢复后 wait 立即返回）
            assert approval_store.resolve(apv_id, "approve", "once")

    kinds = [k for k, _ in events]
    assert "approval_request" in kinds
    assert "approval_result" in kinds
    # 批准后工具确实执行：最终答案到达（answer 之后才是 done）
    assert any(k == "answer" for k in kinds)


def test_guarded_tool_denied_skips_execution(db, seeded_paper, monkeypatch):
    """拒绝高风险工具：不执行，返回 denied_by_user，agent 仍能收尾。"""
    from app import approval_store

    class WebFetchLLM:
        def stream_chat(self, messages, tools=None):
            if not any(m["role"] == "tool" for m in messages):
                yield ("final", LLMResponse(content="", tool_calls=[
                    {"id": "c1", "name": "mcp__builtin__web_fetch",
                     "arguments": {"url": "https://example.com"}}]))
                return
            text = "无法读取该网页，我换用本地证据 e1[^e1]"
            for piece in _chunk(text):
                yield ("delta", "answer", piece)
            yield ("final", LLMResponse(content=text))

    monkeypatch.setattr(loop_mod, "get_llm", lambda: WebFetchLLM())
    loop_mod.settings.approval_mode = "read_only"

    gen = loop_mod.run_agent(db, "抓取该网页", paper_id=seeded_paper.id)
    executed = {"web_fetch": False}
    events = []
    while True:
        try:
            kind, ev = next(gen)
        except StopIteration:
            break
        events.append((kind, ev))
        if kind == "approval_request":
            approval_store.resolve(ev["approval_id"], "deny", "once")

    # 拒绝结果事件出现，且最终仍给出答案（不依赖该工具）
    deny = next(e for k, e in events if k == "approval_result" and e["decision"] == "deny")
    assert deny
    assert any(k == "answer" for k, _ in events)


def test_tool_risk_classification():
    from app.agent.tools import tool_risk

    assert tool_risk("retrieve_evidence") == "safe"
    assert tool_risk("list_papers") == "safe"
    assert tool_risk("mcp__builtin__arxiv_search") == "safe"
    assert tool_risk("mcp__builtin__semantic_scholar_search") == "safe"
    assert tool_risk("mcp__builtin__crossref_search") == "safe"
    assert tool_risk("mcp__builtin__web_fetch") == "guarded"
    assert tool_risk("import_paper_from_url") == "guarded"
    assert tool_risk("mcp__custom__anything") == "guarded"


def test_tool_exception_is_isolated(db, seeded_paper, monkeypatch):
    class OneCallLLM:
        def stream_chat(self, messages, tools=None):
            if not any(m["role"] == "tool" for m in messages):
                yield ("final", LLMResponse(content="", tool_calls=[
                    {"id": "c1", "name": "retrieve_evidence",
                     "arguments": {"query": "heads", "k": 2}}]))
                return
            text = "已根据证据作答 e1[^e1]"
            for piece in _chunk(text):
                yield ("delta", "answer", piece)
            yield ("final", LLMResponse(content=text))

    monkeypatch.setattr(loop_mod, "get_llm", lambda: OneCallLLM())

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(loop_mod, "execute_tool", boom)

    kinds = [k for k, _ in loop_mod.run_agent(db, "x", paper_id=seeded_paper.id)]
    # 工具异常被捕获并以 error 事件下发，循环未崩溃
    assert "error" in kinds
    assert "answer" in kinds
