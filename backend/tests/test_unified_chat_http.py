"""Unified model-selected actions over real local HTTP, never paid providers.

The scripted provider tests transport, decision ownership, citations and saved
history. It does not measure a real model's intent or answer quality.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading

import httpx
import pytest

from app import approval_store, cancel_store, config, llm, run_store
from app.agent import loop
from app.models import Conversation, QARecord
from app.rag.citation import split_sentences
from tests.test_conversation_controls_http import serving_app


MODEL_ID = "Synthetic/Case-Sensitive-Model-V4"
SYNTHETIC_KEY = "Unified-HTTP-Generated-Credential"
MIXED_QUESTION = "先根据这篇论文概括注意力头的设置，再解释 Python 列表的 append 方法。"
SOURCE_SENTENCE = ("Scaled dot-product attention computes the attention function as a weighted sum of values, "
                   "and multi-head attention runs h projections in parallel.")
INDEPENDENT_PYTHON = "Python 的列表是可变序列，调用 append 会在列表末尾增加一个元素。"
UNCITED_PAPER_FACT = "该论文实验组的识别准确率达到了百分之九十九。"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for name in ("_active", "_finished", "_early_cancels"):
        monkeypatch.setattr(run_store, name, {})
    monkeypatch.setattr(cancel_store, "_flags", {})
    monkeypatch.setattr(approval_store, "_pending", {})
    monkeypatch.setattr(llm, "_llm", None)
    monkeypatch.setattr(config.settings, "llm_api_key", "")
    monkeypatch.setattr(config.settings, "memory_recall", True)
    monkeypatch.setattr(config.settings, "memory_capture", True)
    monkeypatch.setattr(loop.hooks_store, "run_hooks", lambda *a, **k: {
        "ran": 0, "blocked": False, "feedback": ""})


@contextmanager
def local_provider(*, citation_scope=None):
    assert citation_scope in (None, "independent", "paper_fact")
    calls = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            entry = {"path": self.path, "body": body,
                     "auth": self.headers.get("Authorization")}
            calls.append(entry)
            messages = body["messages"]
            system = str(messages[0].get("content", ""))
            user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            tool_messages = [m for m in messages if m["role"] == "tool"]
            if "引用适用范围审查员" in system:
                # Scripted scope decisions verify the live protocol and runtime
                # binding, not a real model's ability to judge these sentences.
                scope = json.loads(user)
                message = {"role": "assistant", "content": json.dumps({"results": [
                    {"index": candidate["index"],
                     "requires_citation": candidate["text_prefix"] != INDEPENDENT_PYTHON,
                     "reason": "Independent Python explanation" if candidate["text_prefix"] == INDEPENDENT_PYTHON
                     else "Claim about the paper still requires source evidence"}
                    for candidate in scope["uncited_candidates"]]})}
            elif "引用审计员" in system:
                indexes = [int(index) for index in re.findall(r"\[论断 (\d+)\]", user)]
                message = {"role": "assistant", "content": json.dumps({"results": [
                    {"index": index, "supported": True, "reason": "Synthetic provider contract"}
                    for index in indexes]})}
            elif tool_messages:
                evidence = []
                for item in tool_messages:
                    evidence.extend(json.loads(item["content"]).get("evidence", []))
                if evidence:
                    first = evidence[0]
                    if citation_scope:
                        assert first["id"] == "e1"
                        assert SOURCE_SENTENCE in " ".join(first["quote"].split())
                        suffix = INDEPENDENT_PYTHON if citation_scope == "independent" else UNCITED_PAPER_FACT
                        text = f"{SOURCE_SENTENCE}[^{first['id']}]\n\n{suffix}"
                    else:
                        text = "\n\n".join(f"{sentence}[^{first['id']}]"
                            for sentence in split_sentences(first["quote"]))
                else:
                    payload = json.loads(tool_messages[-1]["content"])
                    papers = payload.get("papers", [])
                    text = f"文献清单已读取，共 {len(papers)} 条。"
                message = {"role": "assistant", "content": text}
            elif citation_scope and user == MIXED_QUESTION:
                message = {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "mixed_scope_retrieval", "type": "function", "function": {
                        "name": "retrieve_evidence", "arguments": json.dumps({"query": "attention heads", "k": 1})}}]}
            elif user == "继续" and any("CONTINUE_SOURCE" in str(m.get("content", ""))
                                         for m in messages if m["role"] == "user"):
                message = {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "context_retrieval", "type": "function", "function": {
                        "name": "retrieve_evidence", "arguments": json.dumps({"query": "attention heads", "k": 1})}}]}
            elif user == "你好，请列出当前文献":
                message = {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "selected_inventory", "type": "function", "function": {
                        "name": "list_papers", "arguments": "{}"}}]}
            elif "什么模型" in user:
                model_id = re.search(r'"model_id":\s*"([^"]+)"', system)
                message = {"role": "assistant", "content": f"本轮模型 ID：{model_id[1] if model_id else 'missing'}"}
            else:
                message = {"role": "assistant", "content": {
                    "你好": "你好，有什么可以帮你？",
                    "用通俗的话解释为什么天空是蓝色的": "阳光经过大气时，较短波长的光更容易被散射，因此晴空常呈蓝色。",
                    "请写一句春天的祝福": "愿你在春日里遇见新的希望。",
                    "你好，研究方法该怎么选择": "先明确研究问题，再根据数据和约束选择合适的方法。",
                    "继续": "继续说明：先确定问题，再选择方法。",
                }.get(user, "请说明你希望我接下来处理的具体内容。")}
            entry["returned_content"] = message["content"]
            usage = {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9}
            if "引用适用范围审查员" in system:
                # Distinct review usage makes omission detectable even when
                # another small audit inference is also counted in the total.
                usage = {"prompt_tokens": 101, "completion_tokens": 202, "total_tokens": 303}
            entry["returned_usage"] = usage
            if body.get("stream"):
                delta = dict(message)
                if delta.get("tool_calls"):
                    delta["tool_calls"] = [dict(call, index=i) for i, call in enumerate(delta["tool_calls"])]
                wire = "data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n"
                wire += "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
                wire += "data: [DONE]\n\n"
                content_type = "text/event-stream"
            else:
                wire = json.dumps({"choices": [{"message": message}], "usage": usage})
                content_type = "application/json"
            payload = wire.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}/Unified/v1", calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(3)


def request(client, method, url, body=None):
    response = client.request(method, url, json=body)
    assert response.status_code == 200, (response.status_code, response.text)
    return response.json()


def create_profile(client, url):
    result = request(client, "POST", "/api/model-profiles", {
        "name": "Unified synthetic provider", "base_url": url,
        "model_id": MODEL_ID, "api_key": SYNTHETIC_KEY, "reasoning_effort": ""})
    return result.get("profile", result)


def select_profile(client, cid, profile):
    options = request(client, "GET", f"/api/conversation-options/{cid}")
    request(client, "PUT", f"/api/conversation-options/{cid}", {
        "expected_revision": options["revision"], "model_profile_id": profile["id"],
        "approval_mode": "full_access", "reasoning_effort": ""})


def chat(client, run_id, cid, question, **extra):
    request(client, "POST", "/api/foreground-runs", {
        "id": run_id, "conversation_id": cid, "question": question, **extra})
    response = client.post("/api/chat/stream", json={
        "run_id": run_id, "conversation_id": cid, "question": question, **extra})
    assert response.status_code == 200, response.text
    frames = []
    for block in response.text.split("\n\n"):
        lines = block.splitlines()
        kind = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        if kind and data:
            frames.append((kind, json.loads(data)))
    assert sum(kind == "answer" for kind, _ in frames) == 1, response.text
    assert sum(kind == "done" for kind, _ in frames) == 1
    assert SYNTHETIC_KEY not in response.text and "credential_ref" not in response.text
    return frames, next(data for kind, data in frames if kind == "answer")


def test_all_question_shapes_reach_model_without_upfront_planner_or_forced_retrieval():
    questions = ["你好", "用通俗的话解释为什么天空是蓝色的", "请写一句春天的祝福",
                 "你好呀，你用的是什么模型", "你好，研究方法该怎么选择"]
    with local_provider() as (url, calls), serving_app() as base:
        with httpx.Client(base_url=base, timeout=25) as client:
            profile = create_profile(client, url)
            for index, question in enumerate(questions):
                cid = f"c_unified_direct_{index}"
                select_profile(client, cid, profile)
                before = len(calls)
                frames, answer = chat(client, f"chat_unified_direct_{index:04}", cid, question)
                assert len(calls) == before + 1, "only the model's actual response inference may run"
                call = calls[-1]
                assert call["body"]["messages"][-1]["content"] == question
                assert call["body"]["model"] == MODEL_ID and call["body"]["stream"] is True
                assert call["auth"] == "Bearer " + SYNTHETIC_KEY
                system = call["body"]["messages"][0]["content"]
                assert MODEL_ID in system and "credential_ref" not in system and SYNTHETIC_KEY not in system
                assert not any(kind in {"plan", "tool_call", "critic", "audit", "refuse"} for kind, _ in frames)
                assert answer["plan_steps"] == [] and answer["refusal"] == 0
                assert answer["gate"]["status"] == answer["audit"]["status"] == "not_applicable"
                if "什么模型" in question:
                    assert MODEL_ID in answer["answer"]
                saved = request(client, "GET", f"/api/conversations/{cid}")["messages"]
                assert len(saved) == 1 and saved[0]["answer"] == answer["answer"]
                assert saved[0]["runtime_config"]["model_id"] == MODEL_ID


def test_same_input_uses_context_and_model_chosen_real_tools(db, seeded_paper):
    contexts = [("c_unified_context_direct", "CONTINUE_EXPLANATION: 下次继续解释方法选择"),
                ("c_unified_context_source", "CONTINUE_SOURCE: 下次继续查找论文的注意力头数")]
    for index, (cid, question) in enumerate(contexts):
        db.add(Conversation(id=cid, title=question))
        db.add(QARecord(id=f"qa_unified_seed_{index}", conversation_id=cid,
                        question=question, answer="好的，请继续。", in_memory=1))
    db.commit()
    with local_provider() as (url, calls), serving_app() as base:
        with httpx.Client(base_url=base, timeout=25) as client:
            profile = create_profile(client, url)
            results = []
            for index, (cid, _) in enumerate(contexts):
                select_profile(client, cid, profile)
                results.append(chat(client, f"chat_unified_context_{index:04}", cid, "继续"))
                history = request(client, "GET", f"/api/conversations/{cid}")["messages"]
                assert len(history) == 2 and history[-1]["question"] == "继续"
            direct_frames, direct_answer = results[0]
            source_frames, source_answer = results[1]
            assert not any(kind == "tool_call" for kind, _ in direct_frames)
            assert direct_answer["evidence"] == []
            assert [data["name"] for kind, data in source_frames if kind == "tool_call"] == ["retrieve_evidence"]
            assert source_answer["refusal"] == 0 and source_answer["evidence"]
            assert source_answer["evidence"][0]["paper_id"] == seeded_paper.id
            assert source_answer["gate"]["passed"] is True
            assert any(kind == "critic" for kind, _ in source_frames)
            assert any(kind == "audit" for kind, _ in source_frames)
            assert source_answer["audit"]["status"] == "passed"
            first_calls = [call for call in calls if call["body"].get("stream")
                           and not any(m["role"] == "tool" for m in call["body"]["messages"])]
            assert len(first_calls) == 2
            assert all(call["body"]["messages"][-1]["content"] == "继续" for call in first_calls)
            assert all("科研任务规划器" not in call["body"]["messages"][0]["content"] for call in calls)


def test_mixed_greeting_keeps_model_selected_inventory_and_persists(db, seeded_paper):
    with local_provider() as (url, calls), serving_app() as base:
        with httpx.Client(base_url=base, timeout=25) as client:
            profile = create_profile(client, url)
            cid = "c_unified_inventory"
            select_profile(client, cid, profile)
            frames, answer = chat(client, "chat_unified_inventory_0001", cid, "你好，请列出当前文献")
            assert len(calls) == 2
            assert [data["name"] for kind, data in frames if kind == "tool_call"] == ["list_papers"]
            assert "共 1 条" in answer["answer"] and answer["refusal"] == 0
            assert answer["evidence"] == [] and answer["gate"]["status"] == "not_applicable"
            assert answer["completed"] is True
            history = request(client, "GET", f"/api/conversations/{cid}")["messages"]
            assert len(history) == 1 and history[0]["answer"] == answer["answer"]


@pytest.mark.parametrize("citation_scope", ["independent", "paper_fact"])
def test_mixed_citation_scope_uses_bound_api_and_keeps_paper_claims_checked(db, seeded_paper, citation_scope):
    with local_provider(citation_scope=citation_scope) as (url, calls), serving_app() as base:
        with httpx.Client(base_url=base, timeout=25) as client:
            profile = create_profile(client, url)
            cid = f"c_unified_scope_{citation_scope}"
            select_profile(client, cid, profile)
            frames, answer = chat(client, f"chat_unified_scope_{citation_scope}", cid, MIXED_QUESTION)
            assert [data["name"] for kind, data in frames if kind == "tool_call"] == ["retrieve_evidence"]
            assert len(answer["evidence"]) == 1
            evidence = answer["evidence"][0]
            assert evidence["id"] == "e1" and evidence["paper_id"] == seeded_paper.id
            assert SOURCE_SENTENCE in " ".join(evidence["quote"].split())
            assert all(call["path"] == "/Unified/v1/chat/completions"
                       and call["auth"] == "Bearer " + SYNTHETIC_KEY
                       and call["body"]["model"] == MODEL_ID for call in calls)
            scope_calls = [call for call in calls
                           if "引用适用范围审查员" in call["body"]["messages"][0]["content"]]
            audit_calls = [call for call in calls
                           if "引用审计员" in call["body"]["messages"][0]["content"]]
            response_calls = [call for call in calls if call["body"].get("stream")]
            assert len(response_calls) == 2
            original_answer = response_calls[-1]["returned_content"]
            suffix = INDEPENDENT_PYTHON if citation_scope == "independent" else UNCITED_PAPER_FACT
            assert original_answer == f"{SOURCE_SENTENCE}[^e1]\n\n{suffix}"
            for call in scope_calls:
                assert not call["body"].get("stream")
                assert call["body"]["temperature"] == 0
                payload = json.loads(call["body"]["messages"][-1]["content"])
                assert payload["question"] == MIXED_QUESTION and payload["full_answer"] == original_answer
                assert payload["uncited_candidates"] == [{"index": 0, "text_prefix": suffix}]
                assert payload["evidence"] == [{key: evidence.get(key)
                                               for key in ("id", "paper_title", "quote", "page")}]
            scope = answer["gate"]["scoped_review"]
            assert scope["verified"] is True and scope["mode"] == "llm"
            assert scope["scope"] == "citation_requirement_only"
            assert scope["original_total_claims"] == 2 and scope["original_marked_claims"] == 1
            assert scope["original_unsupported_count"] == 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                expected = sum(call["returned_usage"][key] for call in calls)
                assert answer["usage"][key] == expected, "every answer, scope review and audit must be billed"
            if citation_scope == "independent":
                assert len(calls) == 4 and len(scope_calls) == len(audit_calls) == 1
                assert not any(kind in {"retry", "refuse"} for kind, _ in frames)
                assert answer["answer"] == original_answer
                assert answer["refusal"] == 0 and answer["completed"] is True
                assert answer["gate"]["passed"] is True and answer["gate"]["unsupported"] == []
                assert answer["gate"]["total_claims"] == answer["gate"]["marked_claims"] == 1
                assert scope["exemptions"][0]["text"] == INDEPENDENT_PYTHON
                assert answer["audit"]["status"] == "passed" and answer["audit"]["verified"] is True
                assert answer["audit"]["mode"] == "llm"
                audit_events = [data["audit"] for kind, data in frames if kind == "audit"]
                assert len(audit_events) == 1 and audit_events[0]["factual_claims"] == 1
                assert audit_events[0]["unsupported"] == audit_events[0]["unverified"] == []
                audit_prompt = audit_calls[0]["body"]["messages"][-1]["content"]
                assert SOURCE_SENTENCE in audit_prompt and "引用证据 e1" in audit_prompt
                assert INDEPENDENT_PYTHON not in audit_prompt
            else:
                # The synthetic rewrite deliberately repeats the bad paper
                # claim: both reviews must retain it and final delivery refuse.
                assert len(calls) == 5 and len(scope_calls) == 2 and audit_calls == []
                assert any(kind == "retry" for kind, _ in frames)
                assert any(kind == "refuse" for kind, _ in frames)
                assert scope["exemptions"] == []
                assert answer["gate"]["passed"] is False
                assert answer["gate"]["unsupported"] == [UNCITED_PAPER_FACT]
                assert answer["gate"]["grounded_ratio"] == 0.5
                assert answer["refusal"] == 1 and answer["completed"] is False
                assert UNCITED_PAPER_FACT not in answer["answer"]
            history = request(client, "GET", f"/api/conversations/{cid}")["messages"]
            assert len(history) == 1 and history[0]["answer"] == answer["answer"]
            assert history[0]["runtime_config"]["mode"] == "api"
            assert history[0]["runtime_config"]["model_id"] == MODEL_ID
            assert history[0]["usage"] == answer["usage"]
