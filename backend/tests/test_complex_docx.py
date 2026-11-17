"""Complex Word fixed sources/locators/consumers. Synthetic originals only."""
from copy import deepcopy
from io import BytesIO
import json
import threading
import zipfile

import pytest
from docx import Document
from docx.text.paragraph import Paragraph
from fastapi.testclient import TestClient

from app.main import app
from app.models import Conversation
from app import source_store as store
from app.source_parsing import MEDIA, Extraction, extract
from app.source_documents import read_document, save_extraction, add_document_locator
from app.source_runtime import freeze_sources, capture_bindings
from app.source_tools import execute_source_tool
from app.source_coverage import complete
from app.source_models import SourceExtraction
from app.runtime_context import bind_runtime_config
from tests.complex_docx_fixture import complex_docx


@pytest.fixture
def client(db, monkeypatch):
    from app import task_runner
    monkeypatch.setattr(task_runner, "_shutting_down", threading.Event())
    db.add_all([Conversation(id="complex-docx", title="Word"), Conversation(id="other-docx", title="Other")]); db.commit()
    with TestClient(app) as value: yield value


def upload(client, raw=None, **extra):
    response = client.post("/api/sources/upload", files={"file": ("complex.docx", raw or complex_docx())},
        data={"conversation_id": "complex-docx", "request_id": "complex-docx-upload", **extra})
    assert response.status_code == 200, response.text
    return response.json()


def url(rid, suffix):
    return f"/api/sources/revisions/{rid}/{suffix}?conversation_id=complex-docx"


def test_all_referenced_parts_without_rendered_pages_or_image_claims():
    raw = complex_docx(); result = extract(raw, MEDIA["docx"]); doc = result.document
    legacy = []
    for item in Document(BytesIO(raw)).iter_inner_content():
        legacy.extend([item.text] if isinstance(item, Paragraph) else ["\t".join(c.text for c in row.cells) for row in item.rows])
    assert [b["text"] for b in doc["blocks"] if "paragraph_index" in b] == legacy
    assert [b["paragraph_index"] for b in doc["blocks"] if "paragraph_index" in b] == list(range(1, len(legacy) + 1))
    for phrase in ("HEADER_0 ORIGINAL", "FOOTER ORIGINAL", "FOOTNOTE ORIGINAL", "ENDNOTE ORIGINAL", "NESTED_TABLE ORIGINAL", "AUTHOR_IMAGE_ALT ORIGINAL"):
        assert phrase in doc["text"]
    assert "UNREFERENCED" not in doc["text"] and "SEPARATOR_MUST" not in doc["text"]
    parts = [b for b in doc["blocks"] if b["kind"] == "word_part"]
    assert len({b["block_id"] for b in parts}) == len(parts)
    assert all("page" not in b and "paragraph_index" not in b and b["xml_path"] for b in parts)
    assert any(b.get("column_span") == 2 for b in parts)
    assert any(b.get("vertical_merge") == "continue" for b in parts)
    assert any(b.get("table_depth") == 1 and "NESTED_TABLE" in b["text"] for b in parts)
    assert doc["word_structure"]["external_requests"] == 0
    assert not doc["word_structure"]["image_understanding_performed"] and not doc["word_structure"]["ocr_performed"]
    assert "image_understanding" in doc["omitted_content"] and "rendered_page_layout" in doc["omitted_content"]
    assert doc["blocks"][0]["hyperlinks"][0]["text"] == "VISIBLE_LINK"
    assert doc["blocks"][0]["hyperlinks"][0]["visited"] is False


def test_deterministic_block_identity_and_body_compatibility():
    first = extract(complex_docx(), MEDIA["docx"]).document
    second = extract(complex_docx(variant="UPDATED"), MEDIA["docx"]).document
    assert [b.get("block_id") for b in first["blocks"]] == [b.get("block_id") for b in second["blocks"]]
    assert first["raw_sha256"] != second["raw_sha256"]
    assert first["blocks"][0]["text"] == second["blocks"][0]["text"]


@pytest.mark.parametrize("section", ["header", "footer", "footnote", "endnote", "body"])
def test_word_part_locates_original_and_annotation_survives_reload(client, db, section):
    source = upload(client); rid = source["revision_id"]
    doc = client.get(url(rid, "document")).json()
    block = next(b for b in doc["blocks"] if b.get("section_kind") == section and b["text"].strip())
    response = client.post(url(rid, "locators").split("?")[0], json={"conversation_id": "complex-docx", "kind": "word_block", "block_id": block["block_id"]})
    assert response.status_code == 200, response.text
    loc = response.json(); assert loc["quote"] == block["text"]
    assert not loc["location"]["rendered_page_available"] and loc["location"]["part_name"] == block["part_name"]
    annotation = client.post(f"/api/research/reading/{rid}/annotations", json={"conversation_id": "complex-docx",
        "locator_id": loc["locator_id"], "selected_text": block["text"], "kind": "highlight", "body": "Verified synthetic note"})
    assert annotation.status_code == 200, annotation.text
    db.expire_all()
    loaded = client.get(f"/api/research/reading/{rid}/annotations?conversation_id=complex-docx").json()
    assert loaded["items"][0]["locator"]["quote"] == block["text"]
    updated = upload(client, complex_docx(variant="UPDATED"), request_id="complex-docx-update", source_id=source["source_id"], expected_version=1)
    assert updated["revision_id"] != rid
    old = client.get(url(rid, "locators/" + loc["locator_id"])).json()
    assert old["quote"] == block["text"] and old["revision_id"] == rid
    import hashlib
    assert hashlib.sha256(client.get(url(rid, "file")).content).hexdigest() == doc["raw_sha256"]
    jump = client.get(url(rid, "document") + "&limit=1&locator_id=" + loc["locator_id"]).json()
    assert jump["blocks"][0]["block_id"] == block["block_id"]


def test_locator_scope_range_and_body_number_validation(client):
    source = upload(client); rid = source["revision_id"]
    doc = client.get(url(rid, "document")).json(); part = next(b for b in doc["blocks"] if b.get("block_id"))
    endpoint = url(rid, "locators").split("?")[0]
    base = {"conversation_id": "complex-docx", "kind": "word_block", "block_id": part["block_id"]}
    assert client.post(endpoint, json={**base, "paragraph_start": 1}).status_code == 422
    assert client.post(endpoint, json={**base, "block_id": "wb_" + "f" * 32}).status_code == 422
    assert client.post(endpoint, json={**base, "conversation_id": "other-docx"}).status_code == 404
    assert client.post(endpoint, json={"conversation_id": "complex-docx", "kind": "word_paragraphs",
        "paragraph_start": 1, "paragraph_end": doc["paragraph_count"] + 1}).status_code == 422
    loc = client.post(endpoint, json={"conversation_id": "complex-docx", "kind": "word_paragraphs", "paragraph_start": 1, "paragraph_end": 1}).json()
    assert loc["quote"] == doc["blocks"][0]["text"] and "HEADER_0" not in loc["quote"]


def test_model_reads_all_parts_and_coverage_does_not_skip_notes(client, db):
    source = upload(client); rid = source["revision_id"]
    binding = freeze_sources(db, "complex-docx", [rid], {"conversation_id": "complex-docx"}); db.rollback()
    scope = store.SourceScope("conversation", "complex-docx")
    doc = read_document(db, rid, scope=scope)
    with bind_runtime_config(binding):
        first = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": rid}))
        assert "error" not in first, first
        reads = [{"revision_id": rid, "range": first["read_range"]}]
        assert not complete(reads, rid)
        directory = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": rid, "block_offset": 0}))
        assert not directory["text_read"] and directory["word_blocks"]
        for block in doc["blocks"]:
            if block.get("kind") != "word_part": continue
            response = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": rid, "block_id": block["block_id"]}))
            assert "error" not in response, response
            assert response["locator"]["quote"] == block["text"] and not response["model_understanding_verified"]
            reads.append({"revision_id": rid, "range": response["read_range"]})
        assert complete(reads, rid)
        mixed = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": rid, "block_id": directory["word_blocks"][0]["block_id"], "paragraph_start": 1}))
        assert "error" in mixed


def test_library_indexes_parts_once_without_compatibility_table_duplicates(client, db):
    source = upload(client); rid = source["revision_id"]
    from app.source_library import _document_chunks
    record = store.read_revision(db, rid, scope=store.SourceScope("conversation", "complex-docx"))
    doc, locators = _document_chunks(db, record, store.SourceScope("conversation", "complex-docx"))
    assert any(l["kind"] == "word_block" and "FOOTNOTE" in l["quote"] for l in locators)
    assert any(l["kind"] == "word_block" and "NESTED_TABLE" in l["quote"] for l in locators)
    assert not any(l["quote"] == b["text"] for l in locators for b in doc["blocks"] if b.get("retrieval_role") == "compatibility" and "\t" in b["text"])


def test_writing_consumer_can_choose_real_footnote_word_block(client, db):
    source = upload(client)
    response = client.post("/api/sources/library-import", json={"conversation_id": "complex-docx",
        "revision_ids": [source["revision_id"]], "request_id": "complex-docx-library"})
    assert response.status_code == 200, response.text
    imported = response.json()["items"][0]; assert imported["status"] == "imported", imported
    from app.writing.sources import SourceRef
    from app.writing.evidence import collect_evidence
    ref = SourceRef(1, imported["paper_id"], "Complex Word", "Synthetic", None, "", None, 1.0)
    assert not collect_evidence(db, [ref], "FOOTNOTE", "complex-docx")
    found = next(e for e in ref.evidence if e["location"].get("section_kind") == "footnote")
    assert found["quote"] == "FOOTNOTE ORIGINAL" and found["location"]["block_id"].startswith("wb_")
    assert "page_number" not in found["location"]
    assert store.read_locator(db, found["locator_id"], scope=store.SourceScope("conversation", "complex-docx"))["quote"] == found["quote"]


def test_graph_context_can_locate_real_supplemental_word_structure():
    from types import SimpleNamespace
    from app.citation_contexts import extract_contexts
    doc = extract(complex_docx(variant="Actual research uses prior work [1]."), MEDIA["docx"]).document
    contexts = extract_contexts(doc, SimpleNamespace(ref_index=1, raw_text="[1] Smith, J. Research. 2020."))["contexts"]
    assert contexts and any(c["location"]["kind"] == "word_block" for c in contexts)
    assert all(c["semantic_support"] == "unverified" for c in contexts)


def test_old_extraction_is_never_reparsed_or_overwritten(db):
    raw = complex_docx(); current = extract(raw, MEDIA["docx"])
    old_doc = deepcopy(current.document)
    old_doc["blocks"] = [{k: b[k] for k in ("kind", "text", "paragraph_index")} for b in old_doc["blocks"] if "paragraph_index" in b]
    text = "\n".join(b["text"] for b in old_doc["blocks"])
    import hashlib
    old_doc.update(text=text, text_sha256=hashlib.sha256(text.encode()).hexdigest(), parser="python-docx/1")
    scope = store.SourceScope("conversation", "legacy-docx")
    record = store.capture_source(db, scope=scope, authorization=store.SourceAuthorization(scope, "legacy-upload", "upload"),
        kind="upload", title="legacy.docx", raw_bytes=raw, text=text, media_type=MEDIA["docx"], parser="python-docx/1", text_origin="parsed_text")
    save_extraction(db, record["revision_id"], scope=scope, extraction=Extraction(text, "python-docx/1", old_doc)); db.commit()
    before = db.get(SourceExtraction, record["revision_id"]).fingerprint
    loc = add_document_locator(db, record["revision_id"], scope=scope, kind="word_paragraphs", paragraph_start=1, paragraph_end=1)
    db.commit(); db.expire_all()
    assert read_document(db, record["revision_id"], scope=scope)["parser"] == "python-docx/1"
    assert db.get(SourceExtraction, record["revision_id"]).fingerprint == before
    assert loc["quote"] == old_doc["blocks"][0]["text"]


def test_large_part_directory_is_bounded_and_complete(client, db):
    source = upload(client, complex_docx(header_count=220)); rid = source["revision_id"]
    binding = freeze_sources(db, "complex-docx", [rid], {"conversation_id": "complex-docx"})
    assert len(binding["source_bindings"][0]["word_blocks"]) == 20
    assert binding["source_bindings"][0]["word_block_directory_next_offset"] == 20
    found, offset = [], 0
    with bind_runtime_config(binding):
        while offset is not None:
            page = json.loads(execute_source_tool(db, "read_attached_source", {"source_revision_id": rid, "block_offset": offset}))
            assert len(page["word_blocks"]) <= 100 and not page["text_read"]
            found.extend(b["block_id"] for b in page["word_blocks"]); offset = page["next_block_offset"]
    assert len(found) == len(set(found)) == binding["source_bindings"][0]["word_block_count"]


@pytest.mark.parametrize("change", ["missing_notes", "traversal", "duplicate", "entity"])
def test_hostile_or_missing_structure_preserves_original_without_claiming_text(client, change):
    raw = complex_docx()
    with zipfile.ZipFile(BytesIO(raw)) as archive: members = {n: archive.read(n) for n in archive.namelist()}
    if change == "missing_notes": del members["word/footnotes.xml"]
    elif change == "traversal": members["word/_rels/document.xml.rels"] = members["word/_rels/document.xml.rels"].replace(b'Target="header1.xml"', b'Target="../../outside.xml"')
    elif change == "entity": members["word/header1.xml"] = b'<!DOCTYPE z [<!ENTITY x "secret">]><z>&x;</z>'
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, value in members.items(): archive.writestr(name, value)
        if change == "duplicate": archive.writestr("word/document.xml", members["word/document.xml"])
    source = upload(client, output.getvalue())
    assert source["parse_status"] == "original_only" and source.get("parse_error")
    assert client.get(url(source["revision_id"], "file")).content == output.getvalue()


def replace_xml_part(raw, name, content):
    with zipfile.ZipFile(BytesIO(raw)) as archive: members = {n: archive.read(n) for n in archive.namelist()}
    members[name] = content
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for part, value in members.items(): archive.writestr(part, value)
    return output.getvalue()


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"])
def test_encoded_dtd_and_entities_cannot_bypass_office_prevalidation(client, encoding):
    declaration = 'UTF-16' if encoding.startswith('utf-16') else 'UTF-32'
    malicious = f'''<?xml version="1.0" encoding="{declaration}"?>
<!DOCTYPE hdr [<!ENTITY injected "ENCODED_ENTITY_MUST_NOT_EXPAND">]>
<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>&injected;</w:t></w:r></w:p></w:hdr>'''.encode(encoding)
    raw = replace_xml_part(complex_docx(), "word/header1.xml", malicious)
    from app.source_parsing import _office_zip
    with pytest.raises(store.SourceError): _office_zip(raw)
    source = upload(client, raw)
    assert source["parse_status"] == "original_only" and source["parse_error"]
    assert client.get(url(source["revision_id"], "file")).content == raw
    assert client.get(url(source["revision_id"], "text")).status_code == 409


def test_utf16_without_declarations_is_parsed_without_rewriting_original():
    raw = complex_docx()
    with zipfile.ZipFile(BytesIO(raw)) as archive: original = archive.read("word/header1.xml").decode("utf-8")
    import re
    xml = re.sub(r"<\?xml[^?]*\?>", "", original)
    raw = replace_xml_part(raw, "word/header1.xml", ('<?xml version="1.0" encoding="UTF-16"?>' + xml).encode("utf-16"))
    result = extract(raw, MEDIA["docx"])
    assert "HEADER_0 ORIGINAL" in result.text and "&injected;" not in result.text


def test_utf16_dtd_in_relationships_is_also_rejected():
    xml = '''<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE Relationships SYSTEM "https://docx-safety.invalid/forbidden.dtd"><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'''.encode("utf-16")
    raw = replace_xml_part(complex_docx(), "word/_rels/header1.xml.rels", xml)
    from app.source_parsing import _office_zip
    with pytest.raises(store.SourceError): _office_zip(raw)


def test_utf16_xlsx_entity_is_rejected_by_shared_office_guard():
    from openpyxl import Workbook
    workbook = Workbook(); workbook.active.append(["saved cell"])
    data = BytesIO(); workbook.save(data); workbook.close()
    xml = '''<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE worksheet [<!ENTITY injected "DO_NOT_EXPAND">]><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>&injected;</sheetData></worksheet>'''.encode("utf-16")
    raw = replace_xml_part(data.getvalue(), "xl/worksheets/sheet1.xml", xml)
    with pytest.raises(store.SourceError): extract(raw, MEDIA["xlsx"])
