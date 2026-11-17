"""Real immutable source excerpts and adversarial writing/model checks; no network."""
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from app import source_store as store
from app.models import Conversation, DeletedConversation, Paper
from app.source_models import SourceLibraryBinding, SourceLibraryImport
from app.pipeline.embeddings import LocalEmbedder
from app.source_documents import read_document
from app.llm import LLMResponse
from app.writing.framework import build_framework, _validate_output
from app.artifact_store import create, revision, get
from tests.test_source_document_runtime import word_bytes, excel_bytes


CID = "framework-evidence"


def seed(db, raw, media):
    db.add(Conversation(id=CID, title="Writing test"))
    paper = Paper(title="Neural evidence", abstract="attention results", status="parsed",
                  content_hash=hashlib.sha256(raw).hexdigest())
    db.add(paper); db.flush()
    scope = store.SourceScope("library", paper.id)
    fixed = store.capture_source(db, scope=scope,
        authorization=store.SourceAuthorization(scope, "library:" + paper.id, "library"),
        kind="library", title=paper.title, raw_bytes=raw,
        text=raw.decode("utf-8") if media in ("text/plain", "text/markdown") else None, media_type=media)
    db.add_all([SourceLibraryBinding(paper_id=paper.id, revision_id=fixed["revision_id"]),
        SourceLibraryImport(revision_id=fixed["revision_id"], paper_id=paper.id,
            raw_sha256=paper.content_hash, request_id="framework-owned-test")])
    db.commit()
    return paper.id


def response(eids=None):
    return {"sections": [{"key": key, "bullets": [
        {"text": "attention is measured", "citations": [1], "evidence_ids": eids or ["F1E1"]}
    ] if key == "abstract" else []} for key in ("abstract", "introduction", "related_work")]}


class WritingModel:
    def __init__(self, *, status="supported", invalid_review=False, first=None):
        self.calls = []
        self.status, self.invalid_review, self.first = status, invalid_review, first

    def chat(self, messages, temperature=0):
        self.calls.append(deepcopy(messages))
        if len(self.calls) == 1:
            return LLMResponse(content=json.dumps(self.first or response()))
        return LLMResponse(content=json.dumps({"checks": [{"claim_id": "abstract:0",
            "status": self.status, "explanation": "Compare only the quoted excerpt.",
            "evidence_ids": ["invented"] if self.invalid_review else ["F1E1"]}]}))


@pytest.mark.parametrize("fmt,kind", [("txt","text_lines"),("md","text_lines"),("docx","word_paragraphs"),
                                       ("xlsx","sheet_cells"),("csv","sheet_cells"),("pdf","pdf_page")])
def test_six_fixed_formats_feed_original_and_save_locators(db, sample_pdf, fmt, kind):
    from app.source_parsing import MEDIA
    raw = {"txt": b"attention is measured\noriginal result", "md": b"# Results\nattention is measured",
           "csv": b"method,result\nattention,28.4", "docx": word_bytes(), "xlsx": excel_bytes(),
           "pdf": sample_pdf.read_bytes()}[fmt]
    seed(db, raw, MEDIA[fmt])
    model = WritingModel()
    result = build_framework(db, "attention measured", model, LocalEmbedder(), conversation_id=CID)
    evidence = result["references"][0]["evidence"]
    assert evidence and result["verified"] is False, result["warnings"]
    assert result["structure_valid"] is True and result["semantic_verification"] == "model_assessed"
    assert result["sections"][0]["bullets"][0]["support_status"] == "model_supported"
    scope = store.SourceScope("conversation", CID)
    for e in evidence:
        loc = store.read_locator(db, e["locator_id"], scope=scope)
        assert loc["kind"] == kind and e["quote"] in loc["quote"]
        assert e["quote"] in model.calls[0][1]["content"] or json.dumps(e["quote"], ensure_ascii=False)[1:-1] in model.calls[0][1]["content"]
    artifact = create(db, request_id="framework-save-test", title="Generated framework", kind="document",
        content="## Abstract\nattention is measured [F1E1]", evidence=evidence, analysis={}, conversation_id=CID)
    saved = revision(db, get(db, artifact["id"], CID))
    assert saved.evidence[0]["locator_id"] == evidence[0]["locator_id"]
    assert saved.evidence[0]["verification"] == "source_frozen_semantics_unverified"
    db.expire_all()
    assert read_document(db, evidence[0]["revision_id"], scope=scope)["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    repeated = build_framework(db, "attention measured", WritingModel(), LocalEmbedder(), conversation_id=CID)
    assert [e["locator_id"] for e in repeated["references"][0]["evidence"]] == [e["locator_id"] for e in evidence]


def test_wrong_or_unowned_evidence_is_removed(db):
    seed(db, b"attention is measured", "text/plain")
    model = WritingModel(first=response(["F1E1", "F2E1", "invented"]))
    result = build_framework(db, "attention", model, LocalEmbedder(), conversation_id=CID)
    assert result["sections"][0]["bullets"][0]["evidence_ids"] == ["F1E1"]
    assert any("剔除" in w for w in result["warnings"])


def test_new_word_part_evidence_uses_part_locator_without_fake_paragraph_page(db):
    from io import BytesIO
    from docx import Document
    from app.source_parsing import MEDIA
    document=Document();document.add_paragraph('Ordinary body text.')
    document.sections[0].header.paragraphs[0].text='HEADER_ONLY_TARGET evidence.'
    output=BytesIO();document.save(output)
    seed(db,output.getvalue(),MEDIA['docx'])
    result=build_framework(db,'HEADER_ONLY_TARGET',WritingModel(),LocalEmbedder(),conversation_id=CID)
    evidence=next(e for e in result['references'][0]['evidence'] if 'HEADER_ONLY_TARGET' in e['quote'])
    loc=store.read_locator(db,evidence['locator_id'],scope=store.SourceScope('conversation',CID))
    assert loc['kind']=='word_block'
    assert 'page_number' not in loc['location']


def test_sheet_window_preserves_widest_row_columns(db):
    seed(db,b'method,value,DESIRED_TARGET\nnarrow,0\n','text/csv')
    result=build_framework(db,'DESIRED_TARGET',WritingModel(),LocalEmbedder(),conversation_id=CID)
    evidence=result['references'][0]['evidence'][0]
    assert 'DESIRED_TARGET' in evidence['quote']
    assert evidence['location']['cell_range']=='A1:C2'


@pytest.mark.parametrize("status", ["conflict", "insufficient"])
def test_semantic_review_does_not_upgrade_conflict_to_verified(db, status):
    seed(db, b"attention is measured", "text/plain")
    result = build_framework(db, "attention", WritingModel(status=status), LocalEmbedder(), conversation_id=CID)
    assert result["sections"][0]["bullets"][0]["support_status"] == "model_" + status
    assert result["verified"] is False


def test_invented_semantic_review_never_certifies_claim(db):
    seed(db, b"attention is measured", "text/plain")
    result = build_framework(db, "attention", WritingModel(invalid_review=True), LocalEmbedder(), conversation_id=CID)
    assert result["semantic_verification"] == "incomplete"
    assert result["sections"][0]["bullets"][0]["support_status"] == "unverified"


def test_semantic_timeout_keeps_draft_and_fixed_sources(db):
    seed(db, b"attention is measured", "text/plain")
    class TimeoutModel(WritingModel):
        def chat(self, messages, temperature=0):
            if self.calls:
                raise httpx.ReadTimeout("not returned")
            return super().chat(messages, temperature)
    result = build_framework(db, "attention", TimeoutModel(), LocalEmbedder(), conversation_id=CID)
    assert result["sections"][0]["bullets"] and result["references"][0]["evidence"]
    assert result["semantic_verification"] == "incomplete"


def test_deleted_scope_cannot_be_used_to_collect_sources(db):
    seed(db, b"attention is measured", "text/plain")
    db.add(DeletedConversation(id=CID)); db.commit()
    model = WritingModel()
    class ForbiddenEmbedder:
        def encode(self, texts):
            raise AssertionError("An inaccessible conversation must not send metadata to embedding")
    with pytest.raises(store.SourceAccessDenied):
        build_framework(db, "attention", model, ForbiddenEmbedder(), conversation_id=CID)
    assert not model.calls


@pytest.mark.parametrize("state",["missing","archived"])
def test_unavailable_scope_checked_before_any_embedding(db,state):
    from app.timeutil import utcnow
    seed(db,b"attention is measured","text/plain")
    cid="nonexistent-writing" if state=="missing" else CID
    if state=="archived":
        db.get(Conversation,CID).archived_at=utcnow();db.commit()
    class ForbiddenEmbedder:
        def encode(self,texts):raise AssertionError("Unauthorized embedding call")
    with pytest.raises(store.SourceError):
        build_framework(db,"attention",WritingModel(),ForbiddenEmbedder(),conversation_id=cid)


@pytest.mark.parametrize("sections", [[], [{"key":"abstract","bullets":[]}]*3,
    [{"key":key,"bullets":[]} for key in ("abstract","introduction","invented")]])
def test_incomplete_or_duplicate_sections_fail_structure(sections):
    with pytest.raises(ValueError):
        _validate_output(json.dumps({"sections":sections}), [1])
