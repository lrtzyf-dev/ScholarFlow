"""Citation metadata must use the authorized raw Word table, never coordinates."""
from copy import deepcopy
from io import BytesIO
import json

import pytest
from docx import Document

from app import source_store
from app.agent.citation_scope import verified_source_metadata
from app.agent.audit import audit_answer
from app.llm import LLMResponse
from app.models import Conversation
from app.source_documents import add_document_locator, read_document
from app.source_runtime import freeze_sources


def fixed_word(db):
    cid = "word-shape-metadata"
    db.add_all([Conversation(id=cid), Conversation(id="word-shape-other")])
    db.commit()
    document = Document()
    document.add_paragraph("Synthetic fixture, not patient data.")
    for count, prefix in ((2, "FIRST"), (3, "SECOND")):
        table = document.add_table(rows=count, cols=2)
        for i, row in enumerate(table.rows):
            for j, cell in enumerate(row.cells):
                cell.text = f"{prefix}_{i}_{j}"
    stream = BytesIO()
    document.save(stream)
    raw = stream.getvalue()
    # Exercise the actual upload path so source access and frozen evidence
    # retain their production contract, not a directly inserted locator.
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        response = client.post("/api/sources/upload", files={"file": ("tables.docx", raw)},
            data={"conversation_id": cid, "request_id": "word-shape-contract"})
        assert response.status_code == 200, response.text
        revision = response.json()["revision_id"]
    freeze_sources(db, cid, [revision], {"conversation_id": cid})
    from app.source_runtime import conversation_scope
    scope = conversation_scope(db, cid, writing=False)
    doc = read_document(db, revision, scope=scope)
    blocks = [b for b in doc["blocks"] if b.get("table_path")]
    entries = []
    for text in ("FIRST_0_0", "SECOND_0_0"):
        block = next(b for b in blocks if b["text"] == text)
        loc = add_document_locator(db, revision, scope=scope, kind="word_block", block_id=block["block_id"])
        entries.append({"id": f"e{len(entries) + 1}", "source_id": doc["source_id"],
            "revision_id": revision, "locator_id": loc["locator_id"], "quote": loc["quote"],
            "source_location": deepcopy(loc["location"]), "page": 0, "bbox": None})
    db.commit()
    return cid, entries, revision, scope


def test_table_shape_is_from_complete_same_raw_table_not_the_single_cited_cell(db):
    cid, entries, revision, scope = fixed_word(db)
    entries[0]["table_shape"] = {"row_count": 999, "column_count": 999, "complete": True}
    entries[0]["source_location"] = {"table_path": "forged", "part_name": "fake.xml"}
    metadata = verified_source_metadata(db, entries, cid)
    first, second = [metadata[e]["table_shape"] for e in ("e1", "e2")]
    assert first["complete"] is second["complete"] is True
    assert first["row_count"] == 2 and second["row_count"] == 3
    assert first["column_count"] == second["column_count"] == 2
    assert first["revision_id"] == second["revision_id"] == revision
    assert first["part_name"] == second["part_name"] == "word/document.xml"
    assert first["table_path"] != second["table_path"]
    assert "FIRST_1_1" not in json.dumps(metadata)  # no uncited cell text
    assert first["raw_sha256"] == source_store.read_revision(db, revision, scope=scope)["raw_sha256"]
    with pytest.raises(Exception):
        verified_source_metadata(db, entries, "word-shape-other")


def test_citation_auditor_gets_actual_shape_with_narrow_structural_contract(db):
    cid, entries, revision, scope = fixed_word(db)
    class Model:
        def chat(self, messages, **kwargs):
            prompt = messages[-1]["content"]
            assert '"table_shape"' in prompt and '"row_count": 2' in prompt
            assert '"row_count": 3' not in prompt  # only cited table
            assert "四个单元格坐标或已读区域只给尺寸下界" in prompt
            assert "不证明已阅读全文" in prompt
            return LLMResponse(json.dumps({"results": [{"index": 0, "supported": True,
                "reason": "Synthetic verified shape test", "support_kind": "source",
                "process_scope": "none", "supports": []}]}))
    result = audit_answer(db, "此表总计2行2列。[^e1]", entries[:1], llm=Model(),
        messages=[], conversation_id=cid)
    assert result["passed"] and result["known_failed_count"] == result["unknown_count"] == 0


@pytest.mark.parametrize("field,value", [("revision_id", "other-revision"),
    ("raw_sha256", "0" * 64), ("part_name", "word/header1.xml"),
    ("table_path", "/document/body/tbl[2]")])
def test_cross_identity_structural_proof_is_not_projected(db, monkeypatch, field, value):
    cid, entries, revision, scope = fixed_word(db)
    from app import source_word_table_shapes
    original = source_word_table_shapes.verified_word_table_shape
    def swapped(*args, **kwargs):
        shape = original(*args, **kwargs)
        shape[field] = value
        return shape
    monkeypatch.setattr(source_word_table_shapes, "verified_word_table_shape", swapped)
    metadata = verified_source_metadata(db, entries[:1], cid)
    assert "table_shape" not in metadata["e1"]
    assert metadata["e1"]["location"]["table_path"] == "/document/body/tbl[1]"
