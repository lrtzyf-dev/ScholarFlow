"""Fresh parent-goal authorization at model/tool/transport boundaries.

Interactive calls without a run binding never borrow a process-global goal.
Checks constrain new dispatches; they are not an OS sandbox or remote rollback.
"""
from copy import deepcopy
from .runtime_context import current_runtime_config


class GoalPolicyChanged(ValueError):
    def __init__(self, denial):
        self.denial = denial
        super().__init__(denial["error"])


def _unavailable(reason):
    return {"blocked_by_goal_constraint": True, "not_executed": True, "error": reason}


def parent_snapshot(binding=None):
    binding = current_runtime_config() if binding is None else binding
    binding = binding or {}
    if binding.get("research_parent"):
        from .research_children import policy_binding
        binding = policy_binding(binding)
    rid = binding.get("parent_run_id") or binding.get("run_id")
    gid, cid = binding.get("goal_id"), binding.get("conversation_id")
    if not rid and not gid:
        return None
    from . import cancel_store
    if rid and cancel_store.is_cancelled(rid):
        raise GoalPolicyChanged(_unavailable("父任务已停止，新调用尚未执行"))
    from .db import SessionLocal
    from .models import Conversation
    from .runtime_models import RuntimeRun
    from .goal_ledger import snapshot_goal
    from .goal_models import Goal
    with SessionLocal() as reader:
        conversation = reader.get(Conversation, cid) if cid else None
        if conversation is None or conversation.archived_at is not None:
            raise GoalPolicyChanged(_unavailable("父任务的会话已删除、归档或失效，新调用尚未执行"))
        run = reader.get(RuntimeRun, rid) if rid else None
        if rid and (run is None or run.conversation_id != cid or gid and gid != run.goal_id):
            raise GoalPolicyChanged(_unavailable("父任务绑定已失效，新调用尚未执行"))
        if run and run.current_attempt_id and not binding.get("attempt_id"):
            raise GoalPolicyChanged(_unavailable("父任务缺少已绑定的执行身份，新调用尚未执行"))
        if run and binding.get("attempt_id") and run.current_attempt_id != binding["attempt_id"]:
            raise GoalPolicyChanged(_unavailable("父任务已由新尝试接管，旧调用尚未执行"))
        gid = gid or (run.goal_id if run else None)
        goal = reader.get(Goal, gid) if gid else None
        if goal is None or goal.conversation_id != cid or rid and goal.run_id != rid:
            raise GoalPolicyChanged(_unavailable("父目标已删除或不属于当前任务，新调用尚未执行"))
        snapshot = snapshot_goal(reader, gid)
    if snapshot["control_state"] != "active" or snapshot["outcome"] == "cancelled":
        raise GoalPolicyChanged({**_unavailable("父目标已暂停或取消，新调用尚未执行"),
            "goal_id": gid, "goal_revision": snapshot["revision"]})
    return snapshot


def tool_denial(name, arguments=None, *, binding=None, snapshot=None):
    try:
        snapshot = parent_snapshot(binding) if snapshot is None else snapshot
    except GoalPolicyChanged as exc:
        return deepcopy(exc.denial)
    if snapshot is None:
        return None
    from .goal_candidates import tool_constraint
    denial = tool_constraint(snapshot, name, arguments)
    return {**denial, "not_executed": True} if denial else None


def assert_tool_allowed(name, arguments=None, *, binding=None):
    denial = tool_denial(name, arguments, binding=binding)
    if denial:
        raise GoalPolicyChanged(denial)
