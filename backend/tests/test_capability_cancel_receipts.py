"""Cancellation preserves received usage and never invents remote rollback."""
import json

import httpx

from app import model_capabilities as probes
from tests.test_model_capability_matrix_routing import target  # noqa: F401; shared isolated fixture


def test_stream_cancel_receipt_is_cancelled_and_retains_usage_already_received(db, target):
    pid, calls, handler = target
    did = "capability-" + "a" * 32
    usage = {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}
    class Content(httpx.SyncByteStream):
        def __iter__(self):
            yield ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": "partial"}}], "usage": usage}) + "\n\n").encode()
            probes.cancel(pid, did)
            yield b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    handler[0] = lambda request: httpx.Response(200, stream=Content())
    result = probes.run(pid, "streaming", probes.preview(pid)["expected_config"], did)
    assert result["failure_kind"] == "cancelled" and len(calls) == 1
    receipt = result["calls"][0]
    assert receipt["state"] == "cancelled" and receipt["estimated_usage"]["total_tokens"] == 5
    assert receipt["synthetic"] is True and result["status"] == "unknown"


def test_cancel_after_complete_http_keeps_completed_receipt_and_actual_response_counts(db, target):
    pid, calls, handler = target
    did = "capability-" + "b" * 32
    def handle(request):
        probes.cancel(pid, did)
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}})
    handler[0] = handle
    result = probes.run(pid, "text", probes.preview(pid)["expected_config"], did)
    assert result["failure_kind"] == "cancelled" and len(calls) == 1
    assert result["calls"][0]["state"] == "completed"
    assert result["calls"][0]["estimated_usage"]["total_tokens"] == 5
