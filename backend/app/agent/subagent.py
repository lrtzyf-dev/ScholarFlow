"""Read-only child workers backed by durable nodes, not executor futures."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import json

from sqlalchemy.orm import Session
from ..llm import BaseLLM
from .prompts import SUBAGENT_PROMPT
from .tools import execute_tool, get_tool_specs, tool_risk, tool_policy_error
from .active_context import compact_messages, progress_fingerprint, read_archive, CONTEXT_TOOL, CONTEXT_TOOL_SPEC
from ..mcp_policy import approval_for
from .. import cancel_store, subtask_store
from ..runtime_context import current_runtime_config, bind_runtime_config
from ..skill_store import SkillsChanged, validate_manifest as validate_skills, manifest as skill_manifest
from ..memory_governance import MemoryScopeChanged, assert_request_memory, inherit_loaded_memory
from ..goal_policy import parent_snapshot, tool_denial, GoalPolicyChanged


def _safe_tool_specs() -> list[dict]:
    return [t for t in get_tool_specs() if tool_risk(t["name"]) == "safe"
            and approval_for(t["name"]) != "always" and not tool_policy_error(t["name"])]


def _payload(result):
    try:
        parsed = json.loads(result)
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def collected_evidence(results):
    """Return only tool-produced evidence, never a child's summary as proof."""
    evidence, seen = [], set()
    for child in results:
        for receipt in child.get("evidence", []):
            payload = _payload(receipt.get("result"))
            if payload.get("error"):
                continue
            for item in payload.get("evidence", []):
                if not isinstance(item, dict) or not isinstance(item.get("quote"), str) or not item["quote"].strip():
                    continue
                identity = (item.get("revision_id"), item.get("locator_id"), item.get("paper_id"), item.get("chunk_id"), item["quote"])
                if identity not in seen:
                    seen.add(identity)
                    evidence.append(deepcopy(item))
    return evidence


def run_subagent(db: Session, llm: BaseLLM, task: dict,
                 max_rounds: int = 2, scope_paper_id: str | None = None, *, node=None) -> dict:
    """Run until a final response or stalled/blocked state.

    max_rounds remains source compatible; it is no longer a hidden stop budget.
    Returning a child summary never proves that the parent's goal is complete.
    """
    topic = str(task.get("topic", "")).strip()
    goal = str(task.get("goal", topic)).strip()
    binding = current_runtime_config() or {}
    checkpoint = deepcopy((node or {}).get("checkpoint") or {})
    checkpoint.setdefault("messages", [
        {"role": "system", "content": SUBAGENT_PROMPT + (
            f"\n\n本轮已锁定用户选择的论文 ID：{scope_paper_id}。不得改查其他论文。" if scope_paper_id else "")},
        {"role": "user", "content": f"子任务：{topic}\n目标：{goal}"}])
    checkpoint.setdefault("collected", [])
    checkpoint.setdefault("calls", [])
    checkpoint.setdefault("tool_index", 0)
    checkpoint.setdefault("seen", [])
    checkpoint.setdefault("no_progress", 0)
    checkpoint.setdefault("context_archive", {})
    checkpoint.setdefault("readonly_cache", {})
    checkpoint.setdefault('skill_manifest', [])
    messages, collected = checkpoint["messages"], checkpoint["collected"]
    specs = _safe_tool_specs()
    specs.append(deepcopy(CONTEXT_TOOL_SPEC))
    allowed_tools = {spec["name"] for spec in specs}

    def save(*, result=None, state="running"):
        if node:
            subtask_store.checkpoint(node["id"], node["writer_token"], checkpoint, result=result, state=state)

    def cancelled():
        return cancel_store.is_cancelled(binding.get("run_id")) or (node and subtask_store.cancelled(node["id"]))

    def result(findings, outcome, missing, state):
        output = {"topic": topic, "findings": findings, "evidence": deepcopy(collected),
            "outcome": outcome, "incomplete": missing, "scope": deepcopy((node or {}).get("scope") or {
                "paper_id": scope_paper_id, "source_revision_ids": binding.get("source_revision_ids", [])}),
            "semantic_verified": False, "summary_is_completion_proof": False,
            "checkpoint_ref": node["id"] if node else None,
            "subtask_id": node["id"] if node else None, "attempt": node["attempt"] if node else 1}
        save(result=output, state=state)
        return output

    def request_context(request_messages, tools):
        validate_skills(checkpoint['skill_manifest'])
        assert_request_memory(binding)
        parent = parent_snapshot(binding)
        if parent is not None:
            restrictions = [{"title": row["title"], "status": row["status"],
                "result_contract": (row.get("tool_arguments") or {}).get("result_contract")}
                for row in parent["requirements"] if row["status"] not in ("cancelled", "superseded")]
            request_messages = [*request_messages[:1], {"role": "system", "content":
                "父目标的最新要求与持续限制如下；当前模型连接仍可用于回答，工具须逐次核对。\n" +
                json.dumps({"goal_revision": parent["revision"], "requirements": restrictions}, ensure_ascii=False)},
                *request_messages[1:]]
        outgoing, manifest = compact_messages(request_messages, tools=tools, binding=binding,
            workspace=binding.get("workspace"), archive=checkpoint["context_archive"])
        checkpoint["context_manifest"] = manifest
        save()
        return outgoing

    def model_request(request_messages, *, with_tools=True):
        # Rebuild the advertised set for every model request. Already-returned
        # calls are checked again below, so a mid-response edit takes effect.
        parent = parent_snapshot(binding)
        tools = ([spec for spec in _safe_tool_specs() if spec["name"] in allowed_tools
                  and not tool_denial(spec["name"], binding=binding, snapshot=parent)]
                 + [deepcopy(CONTEXT_TOOL_SPEC)]) if with_tools else None
        from ..model_capability_policy import capability, text_only_messages
        if capability(binding, "tools") == "unsupported":
            tools = None
            request_messages = text_only_messages(request_messages)
        outgoing = request_context(request_messages, tools)
        parent_snapshot(binding)  # Last model invocation boundary, no long Session.
        return llm.chat(outgoing, tools=tools, temperature=0.2)

    if not topic:
        return result("（子任务为空，已跳过）", "blocked", ["缺少子任务目标"], "failed")
    try:
        while True:
            if cancelled():
                return result("父任务已停止，子任务已取消。", "cancelled", ["任务尚未交付"], "cancelled")
            if not checkpoint["calls"]:
                response = model_request(messages)
                validate_skills(checkpoint['skill_manifest'])
                if not response.tool_calls:
                    findings = response.content.strip()
                    if not findings:
                        return result("子任务模型没有返回有效结果。", "partial", ["缺少有效归纳"], "failed")
                    checkpoint["provider_context"] = response.persisted_context() if hasattr(response, "persisted_context") else {}
                    errors = [c["tool"] for c in collected if _payload(c["result"]).get("error")]
                    missing = ["父任务须核对结论与目标、证据的一致性"]
                    if errors:
                        missing.append("存在未完成读取：" + "、".join(sorted(set(errors))))
                    return result(findings, "partial", missing, "completed" if not errors else "partial")
                checkpoint["calls"] = deepcopy(response.tool_calls)
                checkpoint["tool_index"] = 0
                if hasattr(response, "assistant_message"):
                    messages.append(response.assistant_message())
                else:
                    messages.append({"role": "assistant", "content": response.content,
                        "tool_calls": [{"id": c["id"], "type": "function", "function": {"name": c["name"],
                            "arguments": json.dumps(c.get("arguments", {}), ensure_ascii=False)}} for c in response.tool_calls]})
                save()
            novel = False
            while checkpoint["tool_index"] < len(checkpoint["calls"]):
                validate_skills(checkpoint['skill_manifest'])
                if cancelled():
                    return result("父任务已停止，子任务已取消。", "cancelled", ["任务尚未交付"], "cancelled")
                call = checkpoint["calls"][checkpoint["tool_index"]]
                name, arguments = call["name"], call.get("arguments", {})
                current_allowed = {spec["name"] for spec in _safe_tool_specs()}
                current_denial = tool_denial(name, arguments, binding=binding) if name != CONTEXT_TOOL else None
                if name == CONTEXT_TOOL:
                    def authorize(original_name, original_arguments):
                        if original_name not in current_allowed or tool_policy_error(original_name):
                            return False
                        if tool_denial(original_name, original_arguments, binding=binding):
                            return False
                        if original_name == "read_attached_source":
                            from ..source_runtime import require_attached_revision
                            require_attached_revision(db, original_arguments.get("source_revision_id"),
                                conversation_id=binding.get("conversation_id"), binding=binding)
                            return True
                        if original_name == "read_project_file":
                            from ..workspace import validate_scope, scoped_path
                            validate_scope(db, binding.get("conversation_id"), binding.get("workspace"))
                            scoped_path(binding.get("workspace"), original_arguments.get("path", ""))
                            return True
                        return False
                    try:
                        tool_result = json.dumps(read_archive(checkpoint["context_archive"], arguments,
                            binding=binding, workspace=binding.get("workspace"), authorize=authorize), ensure_ascii=False)
                    except Exception as exc:
                        tool_result = json.dumps({"error": str(exc), "context_access_denied": True}, ensure_ascii=False)
                elif current_denial:
                    tool_result = json.dumps(current_denial, ensure_ascii=False)
                elif (name not in allowed_tools or name not in current_allowed
                        or approval_for(name) == "always" or tool_policy_error(name)):
                    tool_result = json.dumps({"error": "子任务仅允许当前已开放的只读工具，已阻止此调用。",
                        "blocked_by_subagent_policy": True}, ensure_ascii=False)
                else:
                    pending = checkpoint.get("pending_operation")
                    if pending and name.startswith("mcp__"):
                        return result("外部子任务调用中断，需核对远端结果后继续。", "blocked",
                                      ["中断的外部操作尚未对账"], "blocked")
                    checkpoint["pending_operation"] = deepcopy(call)
                    save()
                    scope = {}
                    if scope_paper_id:
                        scope["scope_paper_id"] = scope_paper_id
                    if binding.get("conversation_id"):
                        scope["conversation_id"] = binding["conversation_id"]
                    if binding.get("workspace"):
                        scope["project_scope"] = binding["workspace"]
                    try:
                        from .readonly_cache import attached_read, verified_read
                        def action():
                            denial = tool_denial(name, arguments, binding=binding)
                            if denial:
                                return json.dumps(denial, ensure_ascii=False)
                            return execute_tool(db, name, arguments, **scope)
                        if name == "read_attached_source":
                            tool_result = attached_read(db, arguments, binding=binding,
                                conversation_id=binding.get("conversation_id"), cache=checkpoint["readonly_cache"], action=action)
                        else:
                            tool_result = verified_read(db, name, arguments, binding=binding,
                                conversation_id=binding.get("conversation_id"), project_scope=binding.get("workspace"),
                                scope_paper_id=scope_paper_id, cache=checkpoint["readonly_cache"], action=action)
                        if node:
                            db.commit()
                    except Exception as exc:
                        from ..request_resources import raise_if_resource_boundary
                        raise_if_resource_boundary(exc)
                        if isinstance(exc, SkillsChanged):
                            raise
                        db.rollback()
                        tool_result = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
                fingerprint = progress_fingerprint(name, arguments, tool_result)
                if name == 'load_skill':
                    info = _payload(tool_result)
                    if info.get('identity') and 'content' in info:
                        checkpoint['skill_manifest'] = [item for item in checkpoint['skill_manifest'] if item['identity'] != info['identity']]
                        checkpoint['skill_manifest'].append(skill_manifest(info))
                if fingerprint and fingerprint not in checkpoint["seen"]:
                    checkpoint["seen"].append(fingerprint)
                    novel = True
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": tool_result})
                collected.append({"tool": name, "arguments": deepcopy(arguments), "result": tool_result})
                checkpoint["tool_index"] += 1
                checkpoint["pending_operation"] = None
                save()
            checkpoint["calls"] = []
            checkpoint["tool_index"] = 0
            checkpoint["no_progress"] = 0 if novel else checkpoint["no_progress"] + 1
            if checkpoint["no_progress"] == 1:
                messages.append({"role": "user", "content": "上一阶段未得到新证据。请调整读取范围或检索方法；已有资料足够则交付结论，否则具体说明缺项。"})
            save()
            if checkpoint["no_progress"] >= 2:
                summary_messages = [*messages, {"role": "user", "content":
                    "连续调整仍无新进展。请仅归纳已获得材料，明确未完成部分及所需补充。"}]
                summary = model_request(summary_messages, with_tools=False)
                return result(summary.content.strip(), "partial", ["两次调整未取得新证据", "父任务仍需核验结论"], "partial")
    except Exception as exc:
        from ..resource_budget import ResourceBudgetExceeded
        from ..request_resources import ResourceRequestCancelled
        from .active_context import ContextCapacityExceeded
        if isinstance(exc, ResourceRequestCancelled):
            return result("父任务已停止，子任务已取消。", "cancelled", ["请求未完成"], "cancelled")
        if isinstance(exc, GoalPolicyChanged):
            return result(str(exc), "blocked", ["父目标或约束已变化，请核对后继续"], "blocked")
        if isinstance(exc, (ResourceBudgetExceeded, ContextCapacityExceeded, SkillsChanged, MemoryScopeChanged)):
            result(str(exc), "blocked", ["资源条件需要调整后继续"], "blocked")
            raise
        return result(f"子 agent 调用失败：{type(exc).__name__}", "partial", ["模型或工具调用未完成"], "failed")


def run_subagents(db: Session, llm: BaseLLM, tasks: list[dict],
                  max_workers: int = 4, scope_paper_id: str | None = None, *, conversation_id=None, project_scope=None) -> list[dict]:
    if not tasks:
        return []
    from ..db import SessionLocal
    binding = current_runtime_config() or {}
    if conversation_id is not None:
        binding["conversation_id"] = conversation_id
    if project_scope is not None:
        binding["workspace"] = deepcopy(project_scope)
    binding = inherit_loaded_memory(db, binding)
    parent_run_id = binding.get("run_id")
    scope = subtask_store.child_scope(db, binding, paper_id=scope_paper_id, tool_specs=_safe_tool_specs())
    scope["memory_fingerprint"] = binding.get("memory_fingerprint")

    def reusable(node):
        try:
            parent_snapshot(binding)
        except GoalPolicyChanged:
            return False
        for entry in (node.get("result") or {}).get("evidence", []):
            name = entry.get("tool")
            if tool_denial(name, entry.get("arguments", {}), binding=binding):
                return False
            if name == "read_project_file":
                from ..workspace import read_text
                original = _payload(entry.get("result"))
                try:
                    current = read_text(binding.get("workspace"), entry.get("arguments", {}).get("path", ""), limit=1)
                except Exception:
                    return False
                if not original.get("sha256") or current["sha256"] != original["sha256"]:
                    return False
            elif name in ("list_project_files", "search_project_files", "git_project_status", "git_project_diff",
                          "load_skill", "list_skills", "compare_papers"):
                return False  # No stable input revision contract yet.
        return True

    def work(task):
        node = None
        if parent_run_id:
            node, reused = subtask_store.claim(parent_run_id=parent_run_id,
                parent_attempt_id=binding.get("attempt_id"), specification=task, scope=scope,
                conversation_id=binding.get("conversation_id"), budget_id=binding.get("resource_budget_id"))
            if reused:
                if reusable(node):
                    return {**node["result"], "reused": True, "reuse_scope": "saved_observation_not_a_new_external_search"}
                subtask_store.invalidate_result(node["id"], node["attempt"])
                node, _ = subtask_store.claim(parent_run_id=parent_run_id,
                    parent_attempt_id=binding.get("attempt_id"), specification=task, scope=scope,
                    conversation_id=binding.get("conversation_id"), budget_id=binding.get("resource_budget_id"))
        child_binding = {**binding, "parent_run_id": parent_run_id, "subtask_id": node["id"] if node else None,
            "subtask_attempt": node["attempt"] if node else None, "subtask_writer_token": node["writer_token"] if node else None}
        with SessionLocal() as sub_db, bind_runtime_config(child_binding):
            return run_subagent(sub_db, llm, task, scope_paper_id=scope_paper_id, node=node)

    with ThreadPoolExecutor(max_workers=min(max(1, max_workers), len(tasks))) as pool:
        unique = {}
        futures = []
        for task in tasks:
            identity = json.dumps(task, sort_keys=True, ensure_ascii=False)
            if identity not in unique:
                unique[identity] = pool.submit(copy_context().run, work, task)
            futures.append(unique[identity])
        return [future.result() for future in futures]
