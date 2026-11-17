"""Actual observed partial completion shape, paired without guessing success."""
from copy import deepcopy
import json

import httpx
import pytest

from app.llm import ModelResponseError
from app.providers.deepseek_research import ActivityProjector
from tests.test_model_capability_matrix_routing import target, official, probe


def started(identity="call-a", tool="deepseek_web_search"):
    return {"type": "call_started", "step": 0, "call": {"id": identity,
        "type": tool, "action": {"queries": ["a synthetic query"], "url": "https://example.com/"}}}


def finished(identity="call-a", status="completed"):
    return {"type": "call_completed", "step": 0, "call": {"id": identity, "status": status,
        "signature": "PRIVATE_SIGNATURE", "unknown": {"reasoning": "PRIVATE_THOUGHT"}}}


def test_live_partial_completion_joins_only_exact_id_and_preserves_raw_private_data():
    projector = ActivityProjector()
    start, done = started(), finished()
    original = deepcopy(done)
    assert projector.feed(start)["status"] == "started"
    result = projector.feed(done)
    assert result["tool"] == "deepseek_web_search" and result["status"] == "completed"
    assert result["queries"] == ["a synthetic query"] and result["activity_id"] == "call-a"
    assert done == original and "PRIVATE" not in json.dumps(result)


def test_interleaved_calls_cannot_inherit_another_tools_type():
    projector = ActivityProjector()
    projector.feed(started("search")); projector.feed(started("read", "deepseek_read_url"))
    assert projector.feed(finished("absent")) is None
    assert projector.feed(finished("read"))["tool"] == "deepseek_read_url"
    assert projector.feed(finished("search"))["tool"] == "deepseek_web_search"


@pytest.mark.parametrize("status,expected", [("failed", "failed"), (None, "unknown"), ("other", "unknown")])
def test_started_receipt_never_invents_completed_status(status, expected):
    projector = ActivityProjector(); projector.feed(started())
    assert projector.feed(finished(status=status))["status"] == expected


def test_missing_identity_cannot_confirm_completed_activity():
    projector = ActivityProjector(); event = started()
    event["type"] = "call_completed"; event["call"].pop("id"); event["call"]["status"] = "completed"
    assert projector.feed(event)["status"] == "unknown"


def test_reused_identity_or_conflicting_tool_and_status_fail_closed():
    projector = ActivityProjector(); projector.feed(started())
    with pytest.raises(ModelResponseError): projector.feed(started(tool="deepseek_read_url"))
    done = finished(); done["call"]["type"] = "deepseek_read_url"
    with pytest.raises(ModelResponseError): projector.feed(done)
    assert projector.feed(finished())["status"] == "completed"
    with pytest.raises(ModelResponseError): projector.feed(finished(status="failed"))


def test_completion_action_cannot_replace_the_started_request():
    projector = ActivityProjector(); projector.feed(started())
    event = finished(); event["call"]["action"] = {"queries": ["another target"]}
    with pytest.raises(ModelResponseError): projector.feed(event)


def test_standalone_completion_cannot_reuse_id_for_another_tool_or_action():
    projector = ActivityProjector()
    event = finished(); event["call"].update(type="deepseek_web_search", action={"queries": ["original"]})
    assert projector.feed(event)["status"] == "completed"
    assert projector.feed(deepcopy(event))["status"] == "completed"
    for delta in ({"type": "deepseek_read_url"}, {"action": {"queries": ["changed"]}}):
        changed = deepcopy(event); changed["call"].update(delta)
        with pytest.raises(ModelResponseError): projector.feed(changed)


def test_production_probe_recognizes_real_partial_completion_wire_shape(db, target):
    official(db, target)
    def handler(request):
        tool = json.loads(request.content)["tools"][0]["type"]
        chunks = [{"choices": [{"index": 0, "delta": {}, "server_tool_event": e}]}
            for e in (started(tool=tool), finished())]
        chunks.append({"choices": [{"index": 0, "delta": {"content": "Public result"}, "finish_reason": "stop"}]})
        return httpx.Response(200, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")
    target[2][0] = handler
    value = probe(target, "native_tools")
    assert value["status"] == "supported" and value["request_count"] == 2
    assert value["observations"]["confirmed_native_tools"] == ["deepseek_web_search", "deepseek_read_url"]
    assert "PRIVATE" not in json.dumps(value)
