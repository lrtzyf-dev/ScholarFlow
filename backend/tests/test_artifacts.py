"""Real database/API contracts for durable documents and reproducible packages."""
import io
import json
import subprocess
import sys
import zipfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import artifact_store as store
from app.artifact_models import ArtifactManifest, ArtifactRevision, ArtifactChange
from app.artifact_compute import bundle, recompute, SCRIPT
from app.main import app
from app.models import Conversation
from app.source_store import SourceScope, SourceAuthorization, capture_source, add_text_locator


def document(db, **overrides):
    return store.create(db, **({"request_id": "create-123456", "title": "研究草稿", "kind": "document",
        "content": "# 方法\n原方法\n\n# 结果\n人工内容，不要改变\n", "evidence": [], "analysis": {}} | overrides))


def proposal(db, artifact, **overrides):
    row = store.get(db, artifact["id"], artifact.get("conversation_id"))
    return row, store.propose(db, row, **({"request_id": "modify-123456", "base_revision_id": artifact["current_revision_id"],
        "content": "# 方法\n新方法\n\n# 结果\n人工内容，不要改变\n"} | overrides))


def test_idempotent_create_and_payload_conflict(db):
    first = document(db)
    assert document(db)["id"] == first["id"]
    assert db.query(ArtifactRevision).count() == 1
    with pytest.raises(HTTPException) as error:
        document(db, content="different")
    assert error.value.status_code == 409


def test_section_edit_preserves_unselected_bytes_and_review(db):
    first = document(db)
    selected = store.sections(first["revision"]["content"])[0]
    row, change = proposal(db, first, content=None, section={"index": 0, "expected_sha256": selected["sha256"], "content": "# 方法\n已校正\n\n"})
    assert change["content"].endswith("# 结果\n人工内容，不要改变\n")
    assert store.revision(db, row).content == first["revision"]["content"]
    accepted = store.decide(db, row, db.get(ArtifactChange, change["id"]), accept=True, expected_revision_id=first["current_revision_id"])
    assert accepted["status"] == "accepted"
    assert store.revision(db, row).version == 2
    assert store.revision(db, row).content == change["content"]
    # An acknowledgement loss never produces a third revision.
    store.decide(db, row, db.get(ArtifactChange, change["id"]), accept=True, expected_revision_id=first["current_revision_id"])
    assert db.query(ArtifactRevision).count() == 2


def test_conflict_preserves_both_drafts_and_restore_appends(db):
    first = document(db)
    row, left = proposal(db, first)
    _, right = proposal(db, first, request_id="modify-right123", content="另一窗口的编辑")
    store.decide(db, row, db.get(ArtifactChange, left["id"]), accept=True, expected_revision_id=first["current_revision_id"])
    with pytest.raises(HTTPException) as error:
        store.decide(db, row, db.get(ArtifactChange, right["id"]), accept=True, expected_revision_id=first["current_revision_id"])
    assert error.value.status_code == 409
    assert db.get(ArtifactChange, right["id"]).content == "另一窗口的编辑"
    assert store.change_public(db, row, db.get(ArtifactChange, right["id"]))["conflict"]
    restore = store.propose(db, row, request_id="restore-123456", base_revision_id=row.current_revision_id, restored_from=first["current_revision_id"])
    store.decide(db, row, db.get(ArtifactChange, restore["id"]), accept=True, expected_revision_id=row.current_revision_id)
    assert store.revision(db, row).content == first["revision"]["content"]
    assert store.revision(db, row).version == 3
    assert db.query(ArtifactRevision).count() == 3


def test_reject_keeps_head_and_persists_draft(db):
    first = document(db)
    row, change = proposal(db, first)
    result = store.decide(db, row, db.get(ArtifactChange, change["id"]), accept=False, expected_revision_id=row.current_revision_id)
    assert result["status"] == "rejected"
    assert row.current_revision_id == first["current_revision_id"]
    assert db.get(ArtifactChange, change["id"]).content == change["content"]


def test_fenced_headings_not_sections_and_unicode_offsets():
    text = "序言\r\n# 方法\r\n中文\r\n```md\r\n# 示例不是章节\r\n```\r\n## 子章\r\n内容\r\n# 结果\r\n人工正文"
    items = store.sections(text)
    assert [s["title"] for s in items] == ["方法", "子章", "结果"]
    assert text[items[0]["end"]:] == "# 结果\r\n人工正文"
    assert items[0]["end"] == items[1]["end"]


def test_fixed_source_quote_not_model_quote_and_scope(db):
    db.add_all([Conversation(id="conv-a"), Conversation(id="conv-b")])
    db.commit()
    source = capture_source(db, scope=SourceScope("conversation", "conv-a"),
        authorization=SourceAuthorization(SourceScope("conversation", "conv-a"), "user-upload", "upload"),
        kind="upload", title="notes.md", text="真实原句\n", raw_bytes="真实原句\n".encode(), media_type="text/markdown")
    locator = add_text_locator(db, source["revision_id"], scope=SourceScope("conversation", "conv-a"), line_start=1, line_end=1)
    db.commit()
    item = document(db, conversation_id="conv-a", evidence=[{"revision_id": source["revision_id"], "locator_id": locator["locator_id"], "quote": "模型编造"}])
    assert item["revision"]["evidence"][0]["quote"] == "真实原句\n"
    assert item["revision"]["evidence"][0]["verification"] == "source_frozen_semantics_unverified"
    with pytest.raises(HTTPException):
        store.get(db, item["id"], "conv-b")
    with pytest.raises(HTTPException):
        document(db, request_id="create-other123", conversation_id="conv-b", evidence=[{"revision_id": source["revision_id"]}])


def test_sparse_ids_export_and_restored_revision_preserve_source_mapping(db):
    from app.api.artifacts import download
    db.add(Conversation(id="conv-export")); db.commit()
    scoped = SourceScope("conversation", "conv-export")
    source = capture_source(db, scope=scoped, authorization=SourceAuthorization(scoped, "user-upload", "upload"),
        kind="upload", title="same.md", text="第一句\n第二句\n", raw_bytes="第一句\n第二句\n".encode(), media_type="text/markdown")
    positions = [add_text_locator(db, source["revision_id"], scope=scoped, line_start=n, line_end=n) for n in (1, 2)]
    db.commit()
    evidence = [{"id": identifier, "revision_id": source["revision_id"], "locator_id": locator["locator_id"]}
                for identifier, locator in zip(("e7", "e12"), positions)]
    first = document(db, conversation_id="conv-export", content="第一句 [^e7]，第二句 [^e12]。", evidence=evidence)
    row, change = proposal(db, first, content="新文")
    store.decide(db, row, db.get(ArtifactChange, change["id"]), accept=True, expected_revision_id=first["current_revision_id"])
    restore = store.propose(db, row, request_id="restore-export123", base_revision_id=row.current_revision_id, restored_from=first["current_revision_id"])
    store.decide(db, row, db.get(ArtifactChange, restore["id"]), accept=True, expected_revision_id=row.current_revision_id)
    raw = download(row.id, format="md", conversation_id="conv-export", revision_id=row.current_revision_id, db=db).body.decode()
    for identifier, locator in zip(("e7", "e12"), positions):
        assert f'[^{identifier}]: ' in raw and locator['locator_id'] in raw
    assert '第一句' in raw and '第二句' in raw
    with pytest.raises(HTTPException) as error:
        document(db, conversation_id="conv-export", request_id="duplicate-id123", evidence=[evidence[0], evidence[0]])
    assert error.value.status_code == 422


def test_reproduction_bundle_runs_exported_script_and_hashes(db, tmp_path):
    analysis = document(db, kind="analysis", content="method,score\r\nA,0.81\r\nB,0.87\r\nC,\r\n", analysis={
        "category_column": "method", "numeric_columns": ["score"], "chart_type": "bar", "units": {"score": "fraction"}})
    row = store.get(db, analysis["id"])
    rev = store.revision(db, row)
    with zipfile.ZipFile(io.BytesIO(bundle(db, row, rev))) as package:
        assert not json.loads(package.read("manifest.json"))["recorded_execution"]
    receipt = recompute(db, row, rev)
    assert receipt["status"] == "succeeded"
    assert receipt["stats"]["score"]["missing"] == 1
    assert receipt["stats"]["score"]["mean"] == pytest.approx(0.84)
    with zipfile.ZipFile(io.BytesIO(bundle(db, row, rev))) as package:
        manifest = json.loads(package.read("manifest.json"))
        assert manifest["recorded_execution"]
        assert package.read("reproduce.py").decode() == SCRIPT
        for name, expected in manifest["files"].items():
            assert store.digest(package.read(name)) == expected
        package.extractall(tmp_path)
    previous = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    run = subprocess.run([sys.executable, "-I", "-S", str(tmp_path / "reproduce.py")], cwd=tmp_path, capture_output=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8")) == previous
    assert (tmp_path / "chart.svg").read_text(encoding="utf-8") == previous["svg"]
    assert store.digest((tmp_path / "data.csv").read_bytes()) == previous["data_sha256"]


@pytest.mark.parametrize("chart_type", ["line", "bar", "radar"])
def test_empty_preview_retains_full_dataset_statistics(db, chart_type):
    content = "category,a,b,c\n" + "row,,,\n" * 200 + "valid,1,2,3\n"
    item = document(db, kind="analysis", content=content, analysis={"category_column": "category",
        "numeric_columns": ["a", "b", "c"], "chart_type": chart_type})
    row = store.get(db, item["id"])
    result = recompute(db, row, store.revision(db, row))
    assert result["row_count"] == 201 and result["stats"]["a"]["mean"] == 1
    assert any("preview is empty" in value for value in result["warnings"])


@pytest.mark.parametrize("bad", [{"quote": {}}, {"paper_title": []}, {"url": 3}, {"page": True}, {"page": 0}])
def test_malformed_legacy_evidence_rejected_before_persistence(db, bad):
    with pytest.raises(HTTPException) as error:
        document(db, evidence=[bad])
    assert error.value.status_code == 422
    assert db.query(ArtifactManifest).count() == 0


@pytest.mark.parametrize("content,config", [
    ("x,y,y\nA,1,2\n", {}), ("x,y\nA,NaN\n", {}), ("x,y\nA,Infinity\n", {}),
    ("x,y\nA,text\n", {}), ("x,y\nA,1,2\n", {}),
    ("x,y\nA,1\n", {"chart_type": "radar"}),
    ("x,y\nA,1\n", {"missing_policy": "zero"}),
])
def test_invalid_analysis_never_saves(db, content, config):
    with pytest.raises(HTTPException) as error:
        document(db, kind="analysis", content=content, analysis={"category_column": "x", "numeric_columns": ["y"], "chart_type": "line"} | config)
    assert error.value.status_code == 422
    assert db.query(ArtifactManifest).count() == 0


def test_api_history_download_and_pending_reload():
    client = TestClient(app)
    created = client.post("/api/artifacts", json={"request_id": "api-create123", "title": "API草稿", "content": "# 结果\n初始\n"})
    assert created.status_code == 200, created.text
    item = created.json()
    assert client.get("/api/artifacts").json()["total"] == 1
    changed = client.post(f"/api/artifacts/{item['id']}/changes", json={"request_id": "api-edit12345", "base_revision_id": item["current_revision_id"], "content": "# 结果\n新正文\n"})
    assert changed.status_code == 200, changed.text
    assert client.get(f"/api/artifacts/{item['id']}/changes").json()["total"] == 1
    assert client.get(f"/api/artifacts/{item['id']}/download?format=md").text.endswith("初始\n\n")
    assert client.get(f"/api/artifacts/{item['id']}/revisions").json()["items"][0]["version"] == 1
