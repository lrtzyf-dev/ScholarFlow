"""持久目标 /goal：状态机、系统提示注入与 REST 端点测试。"""
import pytest
from fastapi.testclient import TestClient

from app import goal_store
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_goals():
    goal_store._save([])
    yield
    goal_store._save([])


# ---- 存储状态机 ----

def test_set_goal_creates_active():
    g = goal_store.set_goal("综述覆盖近三年顶会")
    assert g["status"] == goal_store.ACTIVE
    cur = goal_store.get_current_goal()
    assert cur["id"] == g["id"]
    assert cur["description"] == "综述覆盖近三年顶会"


def test_set_replaces_previous_open_goal():
    first = goal_store.set_goal("旧目标")
    second = goal_store.set_goal("新目标")
    # 旧目标被取消并标注
    goals = {g["id"]: g for g in goal_store.load_goals()}
    assert goals[first["id"]]["status"] == goal_store.CANCELLED
    assert "取代" in goals[first["id"]]["note"]
    # 当前目标是新目标
    assert goal_store.get_current_goal()["id"] == second["id"]


def test_empty_description_rejected():
    with pytest.raises(goal_store.GoalError):
        goal_store.set_goal("   ")


def test_pause_resume_complete_flow():
    goal_store.set_goal("完成实验对比")
    paused = goal_store.pause_goal()
    assert paused["status"] == goal_store.PAUSED
    # 暂停后不注入系统提示
    assert goal_store.active_goal_block() == ""
    resumed = goal_store.resume_goal()
    assert resumed["status"] == goal_store.ACTIVE
    assert "完成实验对比" in goal_store.active_goal_block()
    done = goal_store.complete_goal()
    assert done["status"] == goal_store.DONE
    assert done["completed_at"]
    assert goal_store.get_current_goal() is None


def test_pause_when_no_goal_errors():
    with pytest.raises(goal_store.GoalError):
        goal_store.pause_goal()


def test_double_pause_errors():
    goal_store.set_goal("x")
    goal_store.pause_goal()
    with pytest.raises(goal_store.GoalError):
        goal_store.pause_goal()


def test_cancel_goal():
    goal_store.set_goal("临时目标")
    out = goal_store.cancel_goal()
    assert out["status"] == goal_store.CANCELLED
    assert goal_store.get_current_goal() is None


# ---- REST 端点 ----

def test_goal_api_lifecycle():
    r = client.post("/api/goal", json={"description": "写满五页 related work"})
    assert r.status_code == 200
    assert r.json()["current"]["status"] == "active"

    r = client.post("/api/goal/pause")
    assert r.json()["current"]["status"] == "paused"

    r = client.post("/api/goal/resume")
    assert r.json()["current"]["status"] == "active"

    r = client.post("/api/goal/complete")
    assert r.json()["current"] is None
    assert len(r.json()["goals"]) == 1


def test_goal_api_empty_returns_400():
    r = client.post("/api/goal", json={"description": ""})
    assert r.status_code == 400


def test_goal_api_pause_none_returns_400():
    r = client.post("/api/goal/pause")
    assert r.status_code == 400
