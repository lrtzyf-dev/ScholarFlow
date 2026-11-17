"""Only the UI test imports this server. All provider HTTP is synthetic."""
import json
import re
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import uvicorn
from app.main import app
from app import model_capabilities

_capability_init = model_capabilities._Client.__init__
def synthetic_capability_init(self, *args, **kwargs):
    _capability_init(self, *args, **kwargs)
    self._synthetic = True
model_capabilities._Client.__init__ = synthetic_capability_init

calls, uploaded, file_tokens = [], {}, {}
delay_next_chat = False
original_client = httpx.Client


def handle(request):
    global delay_next_chat
    if request.url.host != "api.deepseek.com":
        raise AssertionError("The UI fixture refuses every non-synthetic host")
    calls.append({"method": request.method, "path": request.url.path})
    if request.url.path == "/files" and request.method == "POST":
        body = request.content.decode()
        filename = re.search(r'filename="([^"]+)"', body).group(1)
        assert body.index('name="purpose"') < body.index('name="file"')
        raw = body.split('Content-Type: application/octet-stream\r\n\r\n', 1)[1].rsplit('\r\n--', 1)[0].encode()
        # Real service can report a processed size (e.g. dropping final LF).
        file_id = "file-api-ui-synthetic-" + str(len(file_tokens) + 1)
        token = re.search(rb"Token: (synthetic-[a-f0-9]+)", raw)
        file_tokens[file_id] = token.group(1).decode() if token else None
        uploaded.update(id=file_id, object="file", filename=filename, bytes=len(raw) - 1,
            created_at=int(time.time()), expires_at=int(time.time()) + 86400, purpose="user_data")
        return httpx.Response(200, json=uploaded)
    if request.method == "DELETE":
        return httpx.Response(200, json={"id": uploaded["id"], "deleted": True})
    if request.method == "GET" and request.url.path.startswith("/files/"):
        return httpx.Response(200, json=uploaded)
    body = json.loads(request.content)
    if delay_next_chat:
        delay_next_chat = False
        time.sleep(2)
    message = {"role": "assistant", "content": "OK", "reasoning_content": "Synthetic protocol field"}
    finish = "stop"
    if body.get("tools") and body["tools"][0].get("function", {}).get("name") == "capability_echo":
        stage = sum(v["role"] == "tool" for v in body["messages"]) + 1
        if stage <= 3:
            message.update(content="", tool_calls=[{"id": f"synthetic-call-{stage}", "type": "function",
                "function": {"name": "capability_echo", "arguments": json.dumps({"stage": stage})}}])
            finish = "tool_calls"
    elif body.get("response_format"):
        assert body["response_format"] == {"type": "json_object"}
        message["content"] = '{"answer":17,"ok":true}'
    elif isinstance(body["messages"][0].get("content"), list):
        parts = body["messages"][0]["content"]
        file = next((part for part in parts if part.get("type") == "file"), None)
        message["content"] = file_tokens[file["file_id"]] if file else "red"
    elif "17 * 23" in str(body["messages"]):
        message["content"] = "391"
    usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    if body.get("stream"):
        chunks = []
        if body.get("tools") and body["tools"][0].get("type", "").startswith("deepseek_"):
            tool = body["tools"][0]["type"]
            chunks.append({"choices": [{"index": 0, "delta": {}, "server_tool_event": {"type": "call_completed", "call": {
                "id": "synthetic-native-ui", "type": tool, "status": "completed", "action": {"queries": ["synthetic native UI"]}}}}]})
        chunks.extend([{"choices": [{"index": 0, "delta": message}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage}])
        return httpx.Response(200, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"})
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": finish}], "usage": usage})


httpx.Client = lambda *a, **kw: original_client(*a, **{**kw, "transport": httpx.MockTransport(handle)})


@app.get("/__test__/provider-calls")
def provider_calls():
    return calls

app.router.routes.insert(0, app.router.routes.pop())


@app.get("/__test__/delay-next-chat")
def delay_chat():
    global delay_next_chat
    delay_next_chat = True
    return {"ok": True}


app.router.routes.insert(0, app.router.routes.pop())


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]))
