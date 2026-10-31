"""协作式取消回归：取消标志存储、run_agent 检查点、cancel_task 状态处理。"""
from app.agent import loop as loop_mod
from app.models import TaskRecord


class _SilentLLM:
    """取消应在首个检查点生效，任何流式 LLM 调用都不应发生。"""

    def chat(self, *a, **k):
        raise RuntimeError("force deterministic default plan")

    def stream_chat(self, messages, tools=None):
        raise AssertionError("已请求取消，不应再发起 LLM 流式调用")


def test_cancel_store_basic():
    from app import cancel_store

    rid = "run_unit_basic"
    cancel_store.clear(rid)
    assert cancel_store.is_cancelled(rid) is False
    # 前台 chat（run_id=None）永远不取消
    assert cancel_store.is_cancelled(None) is False

    cancel_store.request_cancel(rid)
    assert cancel_store.is_cancelled(rid) is True
    # 幂等：重复请求不报错，状态保持
    cancel_store.request_cancel(rid)
    assert cancel_store.is_cancelled(rid) is True

    cancel_store.clear(rid)
    assert cancel_store.is_cancelled(rid) is False


def test_run_agent_cancelled_before_first_step(db, monkeypatch):
    """启动前已请求取消：初始化走完后，在第一轮思考检查点停止，产出 cancelled 事件。"""
    from app import cancel_store

    monkeypatch.setattr(loop_mod, "get_llm", lambda: _SilentLLM())

    run_id = "run_pre_cancel"
    cancel_store.clear(run_id)
    cancel_store.request_cancel(run_id)

    events = list(loop_mod.run_agent(db, "测试一个会被取消的问题", run_id=run_id))
    kinds = [k for k, _ in events]

    assert "cancelled" in kinds
    # 取消后不再产出正常答案、收尾事件或落库答案
    assert "answer" not in kinds
    assert "done" not in kinds
    cancel_store.clear(run_id)


def test_cancel_task_dangling_without_thread(db):
    """重启后线程已不在内存的悬挂 running 任务：直接落为 cancelled。"""
    from app import task_runner

    task = TaskRecord(type="agent", title="悬挂任务", status="running",
                      progress=0.3, events=[], payload={})
    db.add(task)
    db.commit()

    out = task_runner.cancel_task(db, task.id)
    assert out["status"] == "cancelled"
    db.refresh(task)
    assert task.status == "cancelled"


def test_cancel_task_finished_is_noop(db):
    """已结束任务请求取消：状态不变，直接返回当前记录。"""
    from app import task_runner

    task = TaskRecord(type="agent", title="已完成", status="done",
                      progress=1.0, events=[], payload={})
    db.add(task)
    db.commit()

    out = task_runner.cancel_task(db, task.id)
    assert out["status"] == "done"


def test_cancel_running_task_sets_flag(db, monkeypatch):
    """运行中任务：cancel_task 设置取消标志并唤醒审批，状态稍后由线程改为 cancelled。"""
    from app import task_runner

    task = TaskRecord(type="agent", title="运行中", status="running",
                      progress=0.4, events=[], payload={})
    db.add(task)
    db.commit()

    # 模拟任务线程仍在内存
    monkeypatch.setattr(task_runner, "_running", {task.id: object()})
    flagged: list[str] = []
    awakened: list[str] = []
    monkeypatch.setattr(task_runner.cancel_store, "request_cancel",
                        lambda rid: flagged.append(rid))
    monkeypatch.setattr(task_runner.approval_store, "resolve_for_run",
                        lambda rid, decision="deny": awakened.append(rid) or 1)

    out = task_runner.cancel_task(db, task.id)
    assert flagged == [task.id]
    assert awakened == [task.id]
    # 标志刚设置，线程尚未到达检查点，数据库状态此刻仍为 running
    assert out["status"] == "running"


def test_cancel_task_missing_returns_error(db):
    from app import task_runner

    out = task_runner.cancel_task(db, "nonexistent_id")
    assert "error" in out
