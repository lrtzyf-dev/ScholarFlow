"""Resumable planner/executor/critic, preserving every completed tool result."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime

from . import loop as ops
from .checkpoint import AgentState, Checkpointer, CheckpointNeedsReview
from ..execution_mode import PLAN_READ_TOOLS, reviewed_plan_text
from ..conversation_memory import MemoryCancelled
from .. import project_changes, project_commands, git_operations
from fastapi import HTTPException


class AgentCancelled(Exception):
    pass


def execute(db, question, paper_id, skills, conversation_id, images, run_id,
            checkpoint, save_checkpoint, execution_mode="execute", reviewed_plan_id=None, workspace=None):
    state = (AgentState.model_validate(checkpoint) if checkpoint else AgentState(
        question=question, paper_id=paper_id, conversation_id=conversation_id,
        skills=skills or [], max_steps=max(1, ops.settings.max_agent_steps),
        execution_mode=execution_mode, reviewed_plan_id=reviewed_plan_id,
        workspace=workspace,
    ))
    if (state.question, state.paper_id, state.conversation_id) != (question, paper_id, conversation_id):
        raise ValueError("检查点与任务不匹配")
    if (state.execution_mode, state.reviewed_plan_id) != (execution_mode, reviewed_plan_id):
        raise ValueError("检查点的执行模式或计划来源不匹配")
    from ..workspace import validate_scope
    validate_scope(db, conversation_id, state.workspace)
    if state.workspace != workspace:
        raise ValueError("检查点的项目文件快照不匹配")
    if state.reviewed_plan_id:
        source_text = reviewed_plan_text(db, state.reviewed_plan_id, conversation_id, run_id)
        if not state.reviewed_plan:
            state.reviewed_plan = source_text
    if state.pending_operation and not state.resolution:
        raise CheckpointNeedsReview("上一步执行结果未确认，请检查后选择重试或跳过")
    saver = Checkpointer(state, save_checkpoint)
    git_owner = {'conversation_id': conversation_id, 'run_id': run_id or state.record_id}
    git_prepared = False

    def log(kind, payload):
        event = {"kind": kind, "time": datetime.now().strftime("%H:%M:%S"), **payload}
        state.trace.append(event)
        if kind == "answer" and state.phase == "setup":
            state.answer = payload.get("answer", "")
            state.result = dict(payload)
        return kind, event

    def check_cancel():
        if ops.cancel_store.is_cancelled(run_id):
            raise AgentCancelled()

    def run_hook(key, name, payload, **kwargs):
        check_cancel()
        if state.execution_mode == "plan":
            return {"ran": 0, "blocked": False, "feedback": "计划模式停用命令钩子"}
        return saver.perform(
            key, "hook", name, payload,
            lambda: ops.hooks_store.run_hooks(name, payload, **kwargs),
            {"ran": 0, "blocked": False, "feedback": "用户跳过了中断的钩子"},
        )

    def todo(phase):
        ops.plan_advance(state.todo, phase)
        return log("todo_update", {"text": "更新任务进度", "steps": state.todo})

    try:
        check_cancel()
        yield log("execution_mode", {"execution_mode": state.execution_mode, "reviewed_plan_id": state.reviewed_plan_id,
                                     "text": "计划模式：仅分析与规划" if state.execution_mode == "plan" else "执行模式"})
        if state.phase == "setup":
            saver.save()
            prepared = yield from ops._prepare_agent(
                db, question, paper_id, skills, conversation_id, images, run_id,
                log=log, run_hook=run_hook,
                execution_mode=state.execution_mode, reviewed_plan=state.reviewed_plan,
                workspace=state.workspace,
            )
            if prepared is None:
                if state.result:
                    state.phase = "completed"
                    saver.save()
                return
            state.messages, state.todo = prepared
            state.phase = "model"
            saver.save()
        elif checkpoint:
            yield log("status", {"text": "已恢复执行上下文，保留已完成的工具结果"})
            yield log("plan", {"text": "恢复已保存计划", "steps": state.todo})

        llm = ops.get_llm()
        tool_specs = ops.get_tool_specs()
        if state.execution_mode == "plan":
            tool_specs = [spec for spec in tool_specs if spec["name"] in PLAN_READ_TOOLS]
        # Permissions granted in a previous process/run do not authorize new work.
        session_allowed = set()

        def needs_approval(name):
            from ..mcp_policy import approval_for
            policy = approval_for(name)
            if policy == "always":
                return True
            if policy == "never" or ops.tool_risk(name) == "safe":
                return False
            return ops.settings.approval_mode != "full_access" and name not in session_allowed

        while state.phase in ("model", "tools"):
            check_cancel()
            if state.phase == "model":
                if state.step >= state.max_steps:
                    state.answer = "达到最大推理步数，未能形成可靠回答。"
                    state.phase = "finalize"
                    saver.save()
                    break
                yield log("step", {"text": f"第 {state.step + 1} 轮思考"})
                response = None
                try:
                    with closing(ops._stream_chat_with_retry(llm, state.messages, tools=tool_specs)) as source:
                        for item in source:
                            check_cancel()
                            if item[0] == "delta":
                                yield log(f"{item[1]}_delta", {"text": item[2]})
                            else:
                                response = item[1]
                except AgentCancelled:
                    raise
                except Exception as exc:
                    yield log("error", {"text": f"大模型调用失败：{type(exc).__name__}: {exc}"})
                    return  # Retain the model boundary so a later resume can retry it.
                check_cancel()
                if response is None:
                    yield log("error", {"text": "模型流结束但未返回完整结果，可从检查点重试"})
                    return
                for key in state.usage:
                    state.usage[key] += int((response.usage or {}).get(key) or 0)
                if response.reasoning_content:
                    yield log("thinking", {"text": response.reasoning_content})
                elif response.tool_calls and response.content:
                    yield log("thinking", {"text": response.content, "preamble": True})
                if not response.tool_calls:
                    state.answer = response.content
                    state.phase = "finalize"
                    yield todo("write")
                else:
                    state.calls = [dict(call, id=call.get("id") or f"call_{state.step}_{i}")
                                   for i, call in enumerate(response.tool_calls)]
                    state.tool_index = 0
                    state.messages.append({"role": "assistant", "content": response.content or None,
                                           "tool_calls": [
                        {"id": call["id"], "type": "function", "function": {
                            "name": call["name"], "arguments": json.dumps(call.get("arguments", {}), ensure_ascii=False),
                        }} for call in state.calls
                    ]})
                    state.phase = "tools"
                saver.save()

            while state.phase == "tools" and state.tool_index < len(state.calls):
                check_cancel()
                call = state.calls[state.tool_index]
                name, arguments = call["name"], call.get("arguments", {})
                key = f"{state.step}/{state.tool_index}"
                tool_key = f"tool/{key}"
                yield todo(ops._tool_phase(name))
                # A completed tool may still need its post hook/result bookkeeping.
                cached = tool_key in state.operations
                if cached:
                    yield log("status", {"text": f"复用已保存的「{name}」结果，继续处理剩余步骤"})
                else:
                    yield log("thinking", {"text": ops._narrate(name, arguments)})
                    yield log("tool_call", {"name": name, "arguments": arguments})
                skip_pending = bool(state.pending_operation and
                                    state.pending_operation.get("key") == tool_key and state.resolution == "skip")
                if state.execution_mode == "plan" and name not in PLAN_READ_TOOLS:
                    if state.pending_operation and state.pending_operation.get("key") == tool_key:
                        state.pending_operation = state.resolution = None
                    outcome = {"result": json.dumps({"error": "计划模式禁止此操作，请仅提交计划等待用户审阅", "blocked_by_plan_mode": True}),
                               "error": "", "executed": False}
                    state.operations[tool_key] = outcome
                    saver.save()
                    yield log("policy_block", {"name": name, "text": f"计划模式已阻止「{name}」"})
                elif cached or skip_pending:
                    outcome = saver.perform(tool_key, "tool", name, arguments,
                                            lambda: None, _skipped_tool())
                else:
                    hk = run_hook(f"hook/{key}/pre", "PreToolUse",
                                  {"tool_name": name, "arguments": arguments}, matcher_value=name)
                    if hk["ran"]:
                        yield log("hook", {"text": f"PreToolUse 钩子执行 {hk['ran']} 条", **hk})
                    approved = not hk["blocked"]
                    denial = ""
                    change = None
                    command = None
                    git_operation = None
                    approval_arguments = arguments
                    if hk["blocked"]:
                        denial = json.dumps({"error": hk.get("feedback", "钩子阻断"), "blocked_by_hook": True}, ensure_ascii=False)
                        yield log("hook_block", {"text": "PreToolUse 钩子阻断了该工具", "tool_name": name,
                                                 "feedback": hk.get("feedback", "")})
                    if approved and name == "edit_project_file":
                        try:
                            change = project_changes.prepare(state.workspace, conversation_id, arguments,
                                run_id=run_id, operation_key=f"agent:{run_id or state.record_id}:{tool_key}")
                            approval_arguments = {k: change[k] for k in ('path', 'operation', 'summary')}
                            approval_arguments['change_id'] = change['id']
                            yield log('project_change', {**approval_arguments, 'text': f"文件变更：{change['path']}"})
                            saver.save()
                            if change['status'] not in ('proposed', 'conflict', 'applied'):
                                approved = False
                                denial = json.dumps({'error': '此文件修改已拒绝或中断，请重新读取后生成新的修改。'}, ensure_ascii=False)
                        except HTTPException as exc:
                            approved = False
                            denial = json.dumps({'error': exc.detail}, ensure_ascii=False)
                    if approved and name == 'run_project_command':
                        try:
                            command = project_commands.prepare(state.workspace, conversation_id, arguments,
                                run_id=run_id or state.record_id, operation_key=f'agent:{run_id or state.record_id}:{tool_key}',
                                retry=bool(state.pending_operation and state.pending_operation.get('key') == tool_key and state.resolution == 'retry'))
                            approval_arguments = {key: command[key] for key in ('command', 'cwd', 'summary', 'timeout_seconds', 'attempt')}
                            approval_arguments['command_id'] = command['id']
                            yield log('project_command', {**approval_arguments, 'status': command['status'], 'text': f"项目命令：{command['summary']}"})
                            saver.save()
                        except HTTPException as exc:
                            approved = False
                            denial = json.dumps({'error': exc.detail}, ensure_ascii=False)
                    if approved and name == 'git_project_write':
                        try:
                            git_operation = git_operations.prepare_agent(state.workspace, conversation_id, arguments,
                                owner=git_owner, operation_key=tool_key)
                            git_prepared = True
                            approval_arguments = {field: git_operation[field] for field in
                                ('project_id', 'action', 'paths', 'message', 'revision', 'branch', 'head')}
                            approval_arguments.update(operation_id=git_operation['id'],
                                patch_preview=git_operation['patch'][:2000], patch_truncated=len(git_operation['patch']) > 2000)
                            yield log('git_operation', {'operation_id': git_operation['id'], 'project_id': git_operation['project_id'],
                                'action': git_operation['action'], 'status': git_operation['status'], 'text': f"Git 操作：{git_operation['action']}"})
                            saver.save()
                        except HTTPException as exc:
                            approved = False
                            denial = json.dumps({'error': exc.detail}, ensure_ascii=False)
                    if (approved and needs_approval(name) and not (change and change['status'] == 'applied')
                            and not (command and command['status'] in project_commands.TERMINAL)
                            and not (git_operation and git_operation['status'] != 'prepared')):
                        ph = run_hook(f"hook/{key}/permission", "PermissionRequest", {
                            "tool_name": name, "arguments": approval_arguments,
                            "reason": ops.approval_reason(name, arguments),
                        }, matcher_value=name)
                        if ph["ran"]:
                            yield log("hook", {"text": f"PermissionRequest 钩子执行 {ph['ran']} 条", **ph})
                        pending = ops.approval_store.create(name, approval_arguments, ops.tool_risk(name),
                                                           ops.approval_reason(name, arguments), run_id=run_id)
                        try:
                            yield log("approval_request", {"text": "需要你的批准", **pending.to_request()})
                            check_cancel()
                            ops.approval_store.wait(pending.id)
                            check_cancel()
                            approved = pending.decision == "approve"
                            if approved and pending.scope == "session":
                                session_allowed.add(name)
                            yield log("approval_result", {"approval_id": pending.id,
                                "decision": "approve" if approved else "deny", "scope": pending.scope,
                                "text": "已批准，继续执行" if approved else "你已拒绝该操作，跳过此工具"})
                        finally:
                            ops.approval_store.discard(pending.id)
                        if not approved:
                            if change:
                                project_changes.reject(change['id'])
                            if command:
                                project_commands.reject(command['id'])
                            if git_operation:
                                git_operation = git_operations.decide(git_operation['project_id'], git_operation['id'],
                                    git_operation['revision'], cancel=True, agent_owner=git_owner)
                            denial = json.dumps({"error": "用户拒绝了该工具的执行，请换一种方式。",
                                                 "denied_by_user": True}, ensure_ascii=False)
                            if git_operation:
                                denial = json.dumps({**git_operations.agent_result(git_operation),
                                    'error': '用户拒绝了该 Git 操作，未执行写入。', 'denied_by_user': True}, ensure_ascii=False)
                    if not approved:
                        outcome = {"result": denial, "error": "", "executed": False}
                        state.operations[tool_key] = outcome
                        saver.save()
                    else:
                        if name == "spawn_subagent":
                            start_hk = run_hook(f"hook/{key}/sub_start", "SubagentStart", {"task": arguments})
                            if start_hk["ran"]:
                                yield log("hook", {"text": f"SubagentStart 钩子执行 {start_hk['ran']} 条", **start_hk})
                        check_cancel()

                        def invoke():
                            try:
                                if change:
                                    result = project_changes.apply(change['id'], run_id=run_id)
                                    return {"result": json.dumps(result, ensure_ascii=False), "error": "", "executed": True}
                                if command:
                                    result = project_commands.run(command['id'], run_id=run_id or state.record_id)
                                    return {'result': json.dumps(result, ensure_ascii=False), 'error': '', 'executed': True}
                                if git_operation:
                                    result = git_operations.decide(git_operation['project_id'], git_operation['id'],
                                        git_operation['revision'], agent_owner=git_owner)
                                    return {'result': json.dumps(git_operations.agent_result(result), ensure_ascii=False), 'error': '', 'executed': True}
                                scope = {"conversation_id": conversation_id} if name == "read_conversation_history" else {}
                                if name == 'read_project_command_output':
                                    scope = {'project_scope': state.workspace, 'conversation_id': conversation_id}
                                if name in ("list_project_files", "read_project_file", "search_project_files", "git_project_status", "git_project_diff"):
                                    scope = {"project_scope": state.workspace}
                                result = ops.execute_tool(db, name, arguments, scope_paper_id=paper_id, **scope)
                                return {"result": result, "error": "", "executed": True}
                            except Exception as exc:
                                error = f"工具「{name}」执行失败：{type(exc).__name__}: {exc}"
                                return {"result": json.dumps({"error": error, "hint": "请换一种方式或跳过该工具。"}, ensure_ascii=False),
                                        "error": error, "executed": True}

                        outcome = saver.perform(tool_key, "tool", name, arguments, invoke, _skipped_tool())
                if name == 'git_project_write':
                    try:
                        git_result = json.loads(outcome['result'])
                        if git_result.get('operation_id'):
                            yield log('git_operation', {'text': f"Git 操作结果：{git_result['action']} · {git_result['status']}",
                                **{field: git_result[field] for field in ('operation_id', 'project_id', 'action', 'status')}})
                    except (ValueError, KeyError, TypeError):
                        pass
                if name == 'run_project_command' and outcome.get('executed'):
                    try:
                        command_result = json.loads(outcome['result'])
                        if command_result.get('command_id'):
                            yield log('project_command', {'text': f"项目命令结束：{command_result['summary']} · {command_result['status']}",
                                **{key: command_result[key] for key in
                                ('command_id', 'command', 'cwd', 'summary', 'timeout_seconds', 'attempt', 'status', 'exit_code')}})
                    except (ValueError, KeyError, TypeError):
                        pass
                if outcome.get("error"):
                    yield log("error", {"text": outcome["error"]})
                result = outcome["result"]
                if outcome.get("executed"):
                    ah = run_hook(f"hook/{key}/post", "PostToolUse", {
                        "tool_name": name, "arguments": arguments, "result_preview": result[:600],
                    }, matcher_value=name)
                    if ah["ran"]:
                        yield log("hook", {"text": f"PostToolUse 钩子执行 {ah['ran']} 条", **ah})
                if name == "spawn_subagent" and outcome.get("executed"):
                    sh = run_hook(f"hook/{key}/sub_stop", "SubagentStop", {
                        "task": arguments, "result_preview": result[:600],
                    })
                    if sh["ran"]:
                        yield log("hook", {"text": f"SubagentStop 钩子执行 {sh['ran']} 条", **sh})
                    try:
                        yield log("subagents", {"text": "子 agent 已完成", "subagents": json.loads(result).get("subagents", [])})
                    except (ValueError, AttributeError):
                        pass
                result, state.evidence_counter, evidence = ops._renumber_evidence(result, state.evidence_counter)
                state.evidence.extend(evidence)
                state.messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
                state.tool_index += 1
                saver.save()
                yield log("tool_result", {"name": name, "preview": result[:600], **_external_papers(name, result)})
            if state.phase == "tools":
                state.calls = []
                state.tool_index = 0
                state.step += 1
                state.phase = "model"
                saver.save()

        check_cancel()
        if state.phase == "finalize":
            ids = [e["id"] for e in state.evidence]
            gate = {"passed": False, "unsupported": [], "invalid_markers": [], "total_claims": 0, "marked_claims": 0}
            audit = {"passed": True, "unsupported": [], "mode": "none"}
            online = ops.settings.llm_mode in ("api", "litellm")
            if ids:
                yield todo("audit")
                gate = ops.citation_gate(state.answer, ids)
                yield log("critic", {"text": "引用闸门校验", "gate": gate})
                if gate["passed"]:
                    audit = ops.audit_answer(db, state.answer, state.evidence, llm=llm)
                    yield log("audit", {"text": f"引用语义审计（{audit['mode']}）", "audit": audit})
                if online and (not gate["passed"] or not audit.get("passed", True)):
                    check_cancel()
                    yield log("retry", {"text": "存在无引用或证据不支持的论断，要求重写"})
                    extra = ""
                    if audit.get("unsupported"):
                        extra = "\n以下论断必须删除或改写：" + "；".join(u["text"] for u in audit["unsupported"])
                    retry_messages = [*state.messages, {"role": "user", "content": ops.RETRY_PROMPT.format(
                        evidence="\n".join(f"- [{eid}]" for eid in ids)) + extra}]
                    try:
                        rewritten = ops._chat_with_retry(llm, retry_messages, tools=None)
                        state.answer = rewritten.content
                        for key in state.usage:
                            state.usage[key] += int((rewritten.usage or {}).get(key) or 0)
                    except Exception as exc:
                        yield log("error", {"text": f"重写失败：{type(exc).__name__}: {exc}"})
                    gate = ops.citation_gate(state.answer, ids)
                    if gate["passed"]:
                        audit = ops.audit_answer(db, state.answer, state.evidence, llm=llm)
                        yield log("audit", {"text": f"重写后语义审计（{audit['mode']}）", "audit": audit})
            refusal = 0
            if ids and (not gate["passed"] or (online and not audit.get("passed", True))):
                state.answer = "根据现有文献证据无法可靠回答该问题（存在未通过引用校验的论断），已拒答。"
                refusal = 1
                yield log("refuse", {"text": "证据不足，触发拒答"})
            elif not state.answer.strip():
                state.answer = "未检索到任何相关论文或模型未返回有效内容，请先上传文献或检查模型配置后重试。"
                refusal = 1
                yield log("refuse", {"text": "无有效答案，触发兜底"})
            check_cancel()
            claims = ops.parse_claims(state.answer, ids) if ids else []
            saver.update_elapsed()
            from ..conversation_store import ensure_conversation
            record = db.get(ops.QARecord, state.record_id)
            if record is None:
                record = ops.QARecord(id=state.record_id, conversation_id=conversation_id, paper_id=paper_id,
                    execution_mode=state.execution_mode,
                    reviewed_plan_id=state.reviewed_plan_id,
                    plan_steps=[dict(step, status="pending") for step in state.todo] if state.execution_mode == "plan" else None,
                    plan_images=images if state.execution_mode == "plan" else None,
                    workspace=state.workspace,
                    question=question, answer=state.answer, claims=claims, evidence=state.evidence,
                    trace=state.trace, skills=state.skills, refusal=refusal, latency_ms=state.elapsed_ms,
                    in_memory=1 if getattr(ops.settings, "memory_capture", True) else 0, usage=state.usage)
                db.add(record)
                ensure_conversation(db, conversation_id)
            if state.execution_mode == "execute":
                ops.plan_finish(state.todo)
            else:
                state.todo = [dict(step, status="pending") for step in state.todo]
            state.result = {"answer": state.answer, "claims": claims, "evidence_ids": ids,
                "evidence": state.evidence, "gate": gate, "refusal": refusal,
                "latency_ms": state.elapsed_ms, "usage": state.usage, "qa_id": state.record_id}
            state.result.update(execution_mode=state.execution_mode, reviewed_plan_id=state.reviewed_plan_id,
                                plan_steps=state.todo if state.execution_mode == "plan" else None)
            state.phase = "deliver"
            from ..foreground_run_store import finish as finish_foreground_run
            finish_foreground_run(db, run_id, "completed")
            # QA row and the delivery checkpoint commit in the same DB transaction.
            saver.save()
            if not save_checkpoint:
                db.commit()
            from ..task_events import notify_tasks_changed
            notify_tasks_changed()

        if state.phase in ("deliver", "completed"):
            yield log("todo_update", {"text": "计划已生成，等待审阅" if state.execution_mode == "plan" else "全部任务完成", "steps": state.todo})
            yield log("answer", state.result or {})
            if state.phase == "deliver":
                sh = run_hook("hook/stop", "Stop", {"answer": state.answer,
                    "conversation_id": conversation_id, "evidence_ids": [e["id"] for e in state.evidence],
                    "refusal": (state.result or {}).get("refusal", 0)})
                if sh["ran"]:
                    yield log("hook", {"text": f"Stop 钩子执行 {sh['ran']} 条", **sh})
                state.phase = "completed"
                saver.save()
            yield log("done", {"text": "完成"})
    except (AgentCancelled, MemoryCancelled):
        if git_prepared:
            for operation in git_operations.cancel_agent_pending(git_owner):
                yield log('git_operation', {'operation_id': operation['id'], 'project_id': operation['project_id'],
                    'action': operation['action'], 'status': operation['status'], 'text': 'Git 操作已取消，未执行写入'})
        saver.save()
        yield log("cancelled", {"text": "已停止，已完成的步骤保留在检查点中"})
    finally:
        if git_prepared:
            git_operations.cancel_agent_pending(git_owner)


def _skipped_tool():
    return {"result": json.dumps({"execution_unknown": True, "skipped_by_user": True,
        "error": "此步骤中断后由用户选择跳过，是否已完成未知，不得假设操作成功。"}, ensure_ascii=False),
        "error": "", "executed": False}


def _external_papers(name, result):
    if name not in ("mcp__builtin__arxiv_search", "mcp__builtin__semantic_scholar_search"):
        return {}
    try:
        payload = json.loads(result)
        return {"external_papers": [{"title": p.get("title", ""), "authors": p.get("authors", ""),
            "year": p.get("year", p.get("published", "")), "url": p.get("url", ""),
            "pdf": p.get("pdf", ""), "source": payload.get("source", "")}
            for p in payload.get("papers", []) if p.get("pdf")]}
    except (ValueError, AttributeError):
        return {"external_papers": []}
