"""无幻觉框架 API 端点测试。"""
import pytest

from app.api.writing import FrameworkRequest, framework_api


def test_framework_endpoint_ok(db):
    out = framework_api(FrameworkRequest(idea="research idea"), db)
    assert out["idea"] == "research idea"
    assert len(out["sections"]) == 3
    assert out["mode"] in ("offline", "llm")


def test_empty_idea_400(db):
    with pytest.raises(Exception) as exc:
        framework_api(FrameworkRequest(idea="   "), db)
    assert exc.value.status_code == 400


def test_bad_max_sources_400(db):
    with pytest.raises(Exception) as exc:
        framework_api(FrameworkRequest(idea="x", max_sources=99), db)
    assert exc.value.status_code == 400
