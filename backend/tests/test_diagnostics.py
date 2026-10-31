"""配置层诊断（codex doctor）结构与隔离测试。"""
from fastapi.testclient import TestClient

from app.diagnostics import run_diagnostics
from app.main import app

client = TestClient(app)
VALID = {"ok", "warn", "error"}


def test_report_structure():
    report = run_diagnostics()
    assert report["overall"] in VALID
    assert set(report["summary"]) == VALID
    assert report["checks"]
    for c in report["checks"]:
        assert c["status"] in VALID
        assert c["id"] and c["label"] and c["detail"]
    # 汇总计数与实际条目一致
    actual = {s: sum(1 for c in report["checks"] if c["status"] == s) for s in VALID}
    assert actual == report["summary"]
    # overall 与最高严重级别一致
    if report["summary"]["error"]:
        assert report["overall"] == "error"
    elif report["summary"]["warn"]:
        assert report["overall"] == "warn"
    else:
        assert report["overall"] == "ok"


def test_individual_checks_dont_raise():
    from app import diagnostics as d

    assert d._check_mode()["status"] in VALID
    assert d._check_config_sources()["status"] in VALID
    assert d._check_database()["status"] in VALID
    assert d._check_data_dir()["status"] in VALID
    assert d._check_disk()["status"] in VALID
    assert d._check_mcp()["status"] in VALID
    assert isinstance(d._check_params(), list)


def test_endpoint_returns_report():
    r = client.get("/api/debug/config")
    assert r.status_code == 200
    body = r.json()
    assert "overall" in body and "checks" in body and "summary" in body
