"""Independent byte-integrity contract against the actual download API.

The fixture creates fixed synthetic document/analysis artifacts through real
TestClient requests and the test database. No live HTTP, model, credentials,
packaging, or publication is involved. Receipts contain identifiers and hashes,
never document bodies, environment/configuration, or private provider data.
"""
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.main import app


OUTPUT = Path(__file__).resolve().parents[2] / "verification" / "artifact-export-receipts-current" / "receipts"
DOCUMENT = "# 方法\n固定合成数据示例，结果只适用于当前输入。\n\n# 结果\n样本量为3，数值依次为1、2、3。\n"
DATA = "category,score\r\nA,1\r\nB,2\r\nC,3\r\n"


@pytest.fixture
def actual_api_artifacts():
    # Deliberately use the production ASGI application and router. A TestClient
    # context would start unrelated schedulers; ordinary requests use the real
    # API stack without starting those lifecycle services.
    client = TestClient(app)
    specifications = {
        "document": {"request_id": "d03-independent-document-0001", "kind": "document",
            "title": "D03固定合成文档", "content": DOCUMENT, "evidence": [], "analysis": {}},
        "analysis": {"request_id": "d03-independent-analysis-0001", "kind": "analysis",
            "title": "D03固定合成数据", "content": DATA, "evidence": [],
            "analysis": {"category_column": "category", "numeric_columns": ["score"],
                "chart_type": "bar", "units": {"score": "count"}, "missing_policy": "gap",
                "title": "Fixed synthetic counts"}},
    }
    artifacts = {}
    for kind, specification in specifications.items():
        response = client.post("/api/artifacts", json=specification)
        assert response.status_code == 200
        created = response.json()
        assert created["kind"] == kind
        assert created["revision"]["content_sha256"] == hashlib.sha256(specification["content"].encode("utf-8")).hexdigest()
        artifacts[kind] = created
    try:
        yield client, artifacts
    finally:
        client.close()


@pytest.mark.parametrize("kind,format", [
    ("document", "md"),
    ("document", "docx"),
    ("analysis", "bundle"),
    ("analysis", "svg"),
    ("document", "manifest"),
])
def test_download_sha256_identifies_actual_response_bytes(actual_api_artifacts, kind, format):
    client, artifacts = actual_api_artifacts
    item = artifacts[kind]
    revision_id = item["current_revision_id"]
    response = client.get(f"/api/artifacts/{item['id']}/download", params={
        "format": format, "revision_id": revision_id})
    assert response.status_code == 200
    actual_sha256 = hashlib.sha256(response.content).hexdigest()
    receipt = {
        "artifact_id": item["id"],
        "artifact_kind": kind,
        "format": format,
        "requested_revision_id": revision_id,
        "returned_revision_id": response.headers.get("X-Artifact-Revision"),
        "source_content_sha256": item["revision"]["content_sha256"],
        "claimed_download_sha256": response.headers.get("X-Content-SHA256"),
        "actual_download_sha256": actual_sha256,
        "response_byte_count": len(response.content),
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # Fresh artifact/revision identifiers make evidence additive on a future
    # post-fix run. Exclusive creation prevents replacing any prior receipt.
    target = OUTPUT / f"receipt-{format}-{item['id']}-{revision_id}.json"
    with target.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(receipt, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.write("\n")
    assert response.headers.get("X-Artifact-Revision") == revision_id
    assert response.headers.get("X-Content-SHA256") == actual_sha256, (
        f"{format}: X-Content-SHA256 must identify actual download bytes; "
        f"claimed={receipt['claimed_download_sha256']}, actual={actual_sha256}, "
        f"source_content={receipt['source_content_sha256']}")
