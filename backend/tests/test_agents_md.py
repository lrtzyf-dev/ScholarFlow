"""分层 AGENTS.md 测试：合并、大小校验、读写、/init 脚手架、API。"""
import pytest
from fastapi.testclient import TestClient

from app import agents_md
from app.main import app


@pytest.fixture(autouse=True)
def clean_global():
    """每个用例前移除全局文件，保证隔离。"""
    if agents_md.GLOBAL_AGENTS_PATH.exists():
        agents_md.GLOBAL_AGENTS_PATH.unlink()
    yield
    if agents_md.GLOBAL_AGENTS_PATH.exists():
        agents_md.GLOBAL_AGENTS_PATH.unlink()


def test_merge_empty_returns_blank():
    assert agents_md.merge_rules("", "") == ""
    assert agents_md.merge_rules("   ", None) == ""


def test_merge_layers_order_and_headers():
    merged = agents_md.merge_rules("用中文回答", "引用须真实")
    assert merged.startswith("# 工作规范")
    assert "全局规范" in merged and "项目规范" in merged
    # 全局层在项目层之前
    assert merged.index("全局规范") < merged.index("项目规范")
    assert "用中文回答" in merged and "引用须真实" in merged


def test_validate_size_limit():
    agents_md.validate_size("x" * 100)  # 不抛异常
    oversized = "字" * (agents_md.MAX_AGENTS_BYTES + 1)
    with pytest.raises(agents_md.AgentsMdError):
        agents_md.validate_size(oversized)


def test_global_write_and_load_roundtrip():
    agents_md.write_global_rules("# 全局\n始终标注来源")
    assert agents_md.load_global_rules() == "# 全局\n始终标注来源"


def test_write_oversized_rejected():
    with pytest.raises(agents_md.AgentsMdError):
        agents_md.write_global_rules("a" * (agents_md.MAX_AGENTS_BYTES + 1))
    assert not agents_md.GLOBAL_AGENTS_PATH.exists()


def test_init_creates_scaffold_and_no_overwrite():
    content, created = agents_md.init_agents_md()
    assert created is True and "AGENTS.md" in content
    # 已存在且不允许覆盖：返回旧内容，created=False
    agents_md.write_global_rules("自定义内容")
    content2, created2 = agents_md.init_agents_md()
    assert created2 is False and content2 == "自定义内容"
    # 允许覆盖：写回脚手架
    content3, created3 = agents_md.init_agents_md(overwrite=True)
    assert created3 is True and "AGENTS.md" in content3


def test_api_get_put_and_init():
    client = TestClient(app)
    r = client.get("/api/agents-md")
    assert r.status_code == 200
    assert r.json()["global_exists"] is False
    # 写入全局规范
    r = client.put("/api/agents-md", json={"global_rules": "始终用中文"})
    assert r.status_code == 200 and r.json()["global_rules"] == "始终用中文"
    # 一键生成（已存在，不覆盖）
    r = client.post("/api/agents-md/init", json={"overwrite": False})
    assert r.status_code == 200 and r.json()["created"] is False
    # 覆盖生成
    r = client.post("/api/agents-md/init", json={"overwrite": True})
    assert r.status_code == 200 and r.json()["created"] is True


def test_api_put_oversized_returns_400():
    client = TestClient(app)
    bad = "a" * (agents_md.MAX_AGENTS_BYTES + 1)
    r = client.put("/api/agents-md", json={"global_rules": bad})
    assert r.status_code == 400
