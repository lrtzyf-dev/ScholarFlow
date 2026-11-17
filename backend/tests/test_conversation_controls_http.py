"""Two real local providers verify per-run routing across concurrent HTTP chats.

Only generated credentials and the pytest-isolated database are used. This is
protocol/lifecycle validation, not a real-model answer-quality measurement.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from app import approval_store, cancel_store, config, llm, run_store
from app.main import app


def wait_until(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    pytest.fail("local integration condition did not become true")


@contextmanager
def serving_app():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        wait_until(lambda: server.started)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(8)
        sock.close()
        assert not thread.is_alive()


@contextmanager
def provider(label):
    calls = []
    first_call = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            entry = {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
            calls.append(entry)
            first_call.set()
            assert release.wait(12), "synthetic provider was not released"
            messages = body["messages"]
            for item in messages:
                for call in item.get("tool_calls", []):
                    assert call["type"] == "function"
                    assert call["function"]["name"] == "list_papers"
                    assert isinstance(call["function"]["arguments"], str)
            tool_rounds = sum(item["role"] == "tool" for item in messages)
            if "规划器" in str(messages[0].get("content", "")):
                message = {"role": "assistant", "content": json.dumps({"steps": [
                    {"title": "Inspect local papers", "phase": "read"},
                    {"title": "Explain available material", "phase": "final"},
                ]})}
            elif body.get("tools") and tool_rounds < 3:
                message = {"role": "assistant", "content": "", "tool_calls": [{
                    "id": f"call_{label}_{tool_rounds}", "type": "function", "function": {
                        "name": "list_papers", "arguments": "{}",
                    },
                }]}
            else:
                message = {"role": "assistant", "content": f"{label}: no source papers are available."}
            usage = {"prompt_tokens": 8, "completion_tokens": 3, "total_tokens": 11}
            if body.get("stream"):
                wire = "data: " + json.dumps({"choices": [{"delta": message}]}) + "\n\n"
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
        yield {
            "url": f"http://127.0.0.1:{httpd.server_port}/{label}/v1",
            "calls": calls, "first": first_call, "release": release,
        }
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(3)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(run_store, "_active", {})
    monkeypatch.setattr(run_store, "_finished", {})
    monkeypatch.setattr(run_store, "_early_cancels", {})
    monkeypatch.setattr(cancel_store, "_flags", {})
    monkeypatch.setattr(approval_store, "_pending", {})
    monkeypatch.setattr(llm, "_llm", None)
    for name in ("llm_base_url", "llm_api_key", "llm_model", "reasoning_effort", "approval_mode"):
        monkeypatch.setattr(config.settings, name, getattr(config.settings, name))
    # Legacy/global changes must never influence a profile-bound invocation.
    config.settings.llm_api_key = ""
    config.settings.reasoning_effort = ""


def request(client, method, url, body=None):
    response = client.request(method, url, json=body)
    assert response.status_code == 200, (response.status_code, response.text)
    return response.json()


def test_concurrent_real_http_runs_keep_profile_version_after_other_conversation_changes():
    with provider("ProviderA") as pa, provider("ProviderB") as pb, serving_app() as base:
        with httpx.Client(base_url=base, timeout=20) as client:
            profiles = []
            for name, p in (("A", pa), ("B", pb)):
                profiles.append(request(client, "POST", "/api/model-profiles", {
                    "name": f"Synthetic {name}", "base_url": p["url"], "model_id": "Org/CaseSensitive-X",
                    "api_key": f"Synthetic-{name}-Credential", "reasoning_effort": "",
                }))
            # API may wrap a mutation's result for future extensions.
            a, b = [item.get("profile", item) for item in profiles]
            for cid, profile in (("c_binding_http_a", a), ("c_binding_http_b", b)):
                options = request(client, "GET", f"/api/conversation-options/{cid}")
                request(client, "PUT", f"/api/conversation-options/{cid}", {
                    "expected_revision": options["revision"], "model_profile_id": profile["id"],
                    "approval_mode": "full_access", "reasoning_effort": "",
                })
            work = [
                ("chat_binding_http_000a", "c_binding_http_a", "Inspect BINDING_A local papers"),
                ("chat_binding_http_000b", "c_binding_http_b", "Inspect BINDING_B local papers"),
            ]
            for run_id, cid, question in work:
                request(client, "POST", "/api/foreground-runs", {
                    "id": run_id, "conversation_id": cid, "question": question,
                })

            def chat(item):
                run_id, cid, question = item
                with httpx.Client(base_url=base, timeout=25) as stream_client:
                    response = stream_client.post("/api/chat/stream", json={
                        "run_id": run_id, "conversation_id": cid, "question": question,
                    })
                    assert response.status_code == 200
                    return response.text

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(chat, item) for item in work]
                try:
                    assert pa["first"].wait(10) and pb["first"].wait(10)
                    # Change both the saved profile and this conversation's NEXT
                    # selection while their already-admitted runs are in flight.
                    request(client, "PUT", f"/api/model-profiles/{a['id']}", {
                        "expected_version": a["version"], "base_url": pb["url"],
                        "api_key": "Synthetic-Changed-Credential", "model_id": "Changed/Model",
                    })
                    options = request(client, "GET", "/api/conversation-options/c_binding_http_a")
                    request(client, "PUT", "/api/conversation-options/c_binding_http_a", {
                        "expected_revision": options["revision"], "model_profile_id": b["id"],
                    })
                    config.settings.llm_base_url = "http://127.0.0.1:1/never-used"
                    config.settings.llm_api_key = "Synthetic-Global-Changed"
                    config.settings.llm_model = "Global/Changed"
                finally:
                    pa["release"].set()
                    pb["release"].set()
                outputs = [future.result(timeout=25) for future in futures]
            assert all("event: answer" in output for output in outputs)
            for label, p in (("A", pa), ("B", pb)):
                assert len(p["calls"]) >= 4, "three tool round-trips and the final answer must reach real HTTP"
                assert not any("规划器" in str(call["body"]["messages"][0].get("content", ""))
                               for call in p["calls"]), "no mandatory planner request before model decisions"
                for call in p["calls"]:
                    assert call["auth"] == f"Bearer Synthetic-{label}-Credential"
                    assert call["body"]["model"] == "Org/CaseSensitive-X"
                    assert call["path"] == f"/Provider{label}/v1/chat/completions"
                    text = json.dumps(call["body"]["messages"])
                    assert f"BINDING_{'B' if label == 'A' else 'A'}" not in text
            for _, cid, _ in work:
                history = request(client, "GET", f"/api/conversations/{cid}")["messages"]
                assert len(history) == 1
                actual = history[0]["runtime_config"]
                assert actual["model_id"] == "Org/CaseSensitive-X"
                assert "Synthetic-" not in json.dumps(actual), "no plaintext credentials in history"
                assert "credential_ref" not in json.dumps(history)
            assert "Synthetic-Global-Changed" not in "".join(outputs)
            assert "credential_ref" not in "".join(outputs)
            listed = request(client, "GET", "/api/model-profiles")
            assert "Credential" not in json.dumps(listed)
