from copy import deepcopy
import json

import pytest

from app.agent.active_context import compact_messages, read_archive, ContextCapacityExceeded, progress_fingerprint
from app.agent.checkpoint import AgentState
from app.agent.readonly_cache import attached_read


def history(provider=None):
    result = [{"role": "system", "content": "preserve instructions"},
              {"role": "user", "content": "比较五篇论文并导出表格；不要写入文件。"}]
    for index in range(6):
        assistant = {"role": "assistant", "content": "reading", "tool_calls": [{"id": str(index), "type": "function",
            "function": {"name": "read_project_file", "arguments": json.dumps({"path": f"p{index}.txt"})}}]}
        if provider:
            assistant["_provider"] = deepcopy(provider)
        result.extend([assistant, {"role": "tool", "tool_call_id": str(index), "content": "原文证据αβγ\n" * 1000}])
    result.append({"role": "user", "content": "更正：只比较后三篇，导出要求保留。"})
    return result


def test_active_compaction_keeps_originals_requirements_and_retrievable_bytes():
    messages, archive = history(), {}
    original = deepcopy(messages)
    outgoing, manifest = compact_messages(messages, archive=archive, window_tokens=50000, output_reserve=10000)
    assert messages == original
    assert manifest["estimated_input_tokens_after"] <= 40000
    assert len(archive) > 0
    assert [m for m in outgoing if m["role"] in ("system", "user")] == [m for m in messages if m["role"] in ("system", "user")]
    reference = manifest["entries"][0]["reference"]
    piece = read_archive(archive, {"reference": reference, "offset": 3, "length": 42}, authorize=lambda *a: True)
    assert piece["content"] == archive[reference]["content"][3:45]
    state = AgentState(question="q", context_archive=archive, context_manifest=manifest)
    restored = AgentState.model_validate(state.snapshot())
    assert restored.context_archive == archive


def test_private_envelope_remains_byte_identical_while_plain_function_results_compact():
    payload = {"version": 1, "assistant": {"reasoning_content": "keep this exactly", "tool_calls": []}, "scope": "opaque"}
    messages = history(payload)
    outgoing, manifest = compact_messages(messages, window_tokens=50000, output_reserve=10000)
    assert manifest["entries"]
    assert [m for m in outgoing if m["role"] == "assistant"] == [m for m in messages if m["role"] == "assistant"]


def test_signed_native_blocks_require_new_phase_instead_of_destructive_compaction():
    messages = history({"assistant": {"server_tool_calls": [{"signature": "opaque", "payload": "raw"}]}})
    original = deepcopy(messages)
    with pytest.raises(ContextCapacityExceeded):
        compact_messages(messages, window_tokens=50000, output_reserve=10000)
    assert messages == original


def test_scope_change_purges_archive_and_cannot_refill_removed_source():
    archive = {}
    binding = {"conversation_id": "A", "source_revision_ids": ["source-a"]}
    _, manifest = compact_messages(history(), binding=binding, archive=archive, window_tokens=50000, output_reserve=10000)
    reference = manifest["entries"][0]["reference"]
    with pytest.raises(ValueError, match="范围已变化"):
        read_archive(archive, {"reference": reference}, binding={"conversation_id": "B"}, authorize=lambda *a: True)
    messages = [{"role": "assistant", "tool_calls": [{"id": "read", "function": {"name": "read_attached_source",
        "arguments": json.dumps({"source_revision_id": "source-a"})}}]},
        {"role": "tool", "tool_call_id": "read", "content": "secret source content"}]
    outgoing, _ = compact_messages(messages, binding={"conversation_id": "B"}, archive=archive)
    assert not archive and "secret source content" not in json.dumps(outgoing)


def test_archive_rechecks_permission_and_hash_before_returning():
    archive = {}
    _, manifest = compact_messages(history(), archive=archive, window_tokens=50000, output_reserve=10000)
    reference = manifest["entries"][0]["reference"]
    with pytest.raises(ValueError, match="授权"):
        read_archive(archive, {"reference": reference}, authorize=lambda *a: False)
    archive[reference]["content"] += "changed"
    with pytest.raises(ValueError, match="校验失败"):
        read_archive(archive, {"reference": reference}, authorize=lambda *a: True)


def test_reordered_search_and_timestamps_cannot_fake_progress_but_pagination_can():
    one = {"evidence": [{"id": "e1", "quote": "A"}, {"id": "e2", "quote": "B"}], "timestamp": "old"}
    two = {"evidence": [{"id": "e55", "quote": "B"}, {"id": "e77", "quote": "A"}], "timestamp": "new"}
    assert progress_fingerprint("retrieve_evidence", {"query": "query1"}, one) == progress_fingerprint("retrieve_evidence", {"query": "query2"}, two)
    assert progress_fingerprint("read_project_file", {"path": "p", "offset": 0}, "text") != progress_fingerprint("read_project_file", {"path": "p", "offset": 100}, "text")
    assert progress_fingerprint("update_plan", {}, {"new": "title"}) is None
    assert progress_fingerprint("read_project_file", {}, {"error": "failed at a new time"}) is None


def test_immutable_cache_checks_authorization_before_hit_and_parser_change_invalidates(db,monkeypatch):
    from app import source_runtime
    from app.agent import tools
    from app.models import Conversation
    db.add(Conversation(id="c",title="Source cache"));db.commit()
    checks, executed, cache = [], [], {}
    permission = [True]
    def validate(*args, **kwargs):
        checks.append(True)
        if not permission[0]:
            raise ValueError("revoked")
        return {"revision_id": "r1"}
    monkeypatch.setattr(source_runtime, "require_attached_revision", validate)
    monkeypatch.setattr(tools, "tool_policy_error", lambda name: None)
    binding = {"run_id": "run", "source_bindings": [{"revision_id": "r1", "raw_sha256": "same", "extraction_fingerprint": "parser1"}]}
    args = {"source_revision_id": "r1", "line_start": 1, "line_end": 20}
    action = lambda: executed.append(True) or '{"evidence": [{"quote": "original"}]}'
    for _ in range(2):
        attached_read(db, args, binding=binding, conversation_id="c", cache=cache, action=action)
    assert len(checks) == 4 and len(executed) == 1
    binding["source_bindings"][0]["extraction_fingerprint"] = "parser2"
    attached_read(db, args, binding=binding, conversation_id="c", cache=cache, action=action)
    assert len(executed) == 2
    permission[0] = False
    with pytest.raises(ValueError, match="revoked"):
        attached_read(db, args, binding=binding, conversation_id="c", cache=cache, action=action)
    assert len(executed) == 2
