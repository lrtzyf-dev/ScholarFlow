"""A real probe's unknown result must explain public shape without leaking replay."""
import json
import httpx

from tests.test_model_capability_matrix_routing import target, official, probe, stream


def test_successful_native_diagnostic_retains_only_public_enum_observations(db, target):
    official(db, target)
    target[2][0] = lambda request: stream(json.loads(request.content)["tools"][0]["type"])
    value = probe(target, "native_tools")
    assert value["status"] == "supported"
    details = value["observations"]["native_activity_diagnostics"]
    assert len(details) == 2 and all(item["activity_count"] == 1 for item in details)
    assert all(item["wire_event_count"] == 1 and item["wire_event_shapes"][0]["call_has_status"] for item in details)


def test_unknown_native_response_does_not_claim_billed_search_as_activity_proof(db, target):
    official(db, target)
    chunks = [
        {"choices": [{"index": 0, "delta": {"reasoning_content": "PRIVATE_THOUGHT"},
            "server_tool_event": {"type": "step_reasoning_done", "step": 0}}]},
        {"choices": [{"index": 0, "delta": {"content": "Public answer", "server_tool_calls": [
            {"reasoning_content": "PRIVATE_STEP", "calls": [{"type": "deepseek_web_search",
                "status": "completed", "signature": "PRIVATE_SIGNATURE"}]}]}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11, "tool_search": 1}},
    ]
    target[2][0] = lambda request: httpx.Response(200,
        text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")
    value = probe(target, "native_tools")
    assert value["status"] == "unknown" and value["request_count"] == 1
    detail = value["observations"]["native_activity_diagnostics"][0]
    assert detail["wire_event_count"] == 1 and detail["wire_event_shapes"][0]["type"] == "step_reasoning_done"
    assert detail["final_native_steps"] == 1 and detail["activity_count"] == 0
    assert detail["final_has_content"] and not detail["final_has_local_calls"]
    public = json.dumps(value["observations"])
    assert "PRIVATE" not in public and "signature" not in public
