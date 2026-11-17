"""Synthetic fixed originals; no external provider or user data."""
from copy import deepcopy
import hashlib
import json
import threading

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import config, library_index_service as service, library_index_store as store, source_library, source_store as sources
from app.db import SessionLocal
from app.library_index_models import LibraryIndexOperation, LibraryIndexReceipt, LibraryIndexVector, LibraryChunkArchive, LibraryIndexPreparedBatch
from app.library_sources import scope_of
from app.main import app
from app.models import Chunk, Conversation, Paper
from app.pipeline import embeddings, indexer, vectordb
from app.source_documents import read_document, save_extraction
from app.source_models import SourceLibraryBinding, SourceLibraryImport, SourceExtraction
from app.source_parsing import MEDIA, Extraction, extract
from tests.complex_docx_fixture import complex_docx


@pytest.fixture
def setup(db, monkeypatch, tmp_path):
    parsed = tmp_path / "parsed"; parsed.mkdir()
    monkeypatch.setattr(source_library, "PARSED_DIR", parsed)
    real = vectordb.NumpyVectorStore
    vector = tmp_path / "vectors.npy"
    monkeypatch.setattr(vectordb, "NumpyVectorStore", lambda: real(vector))
    monkeypatch.setattr(embeddings, "_embedder", None)
    monkeypatch.setattr(config.settings, "embedding_api_key", "")
    service._stopping.clear()
    db.add_all([Conversation(id="index-owner", title="Index"), Conversation(id="index-other", title="Other")]); db.commit()
    return sources.SourceScope("conversation", "index-owner"), real, vector


def versions(db, setup):
    scope, _, _ = setup
    raw = complex_docx(); current = extract(raw, MEDIA["docx"])
    old = deepcopy(current.document)
    old["blocks"] = [{k: b[k] for k in ("kind", "text", "paragraph_index")} for b in old["blocks"] if "paragraph_index" in b]
    value = "\n".join(b["text"] for b in old["blocks"])
    old.update(text=value, text_sha256=hashlib.sha256(value.encode()).hexdigest(), parser="python-docx/1")
    result = []
    for n, extraction in enumerate((Extraction(value, "python-docx/1", old), current)):
        record = sources.capture_source(db, scope=scope, authorization=sources.SourceAuthorization(scope, f"upload-{n}", "upload"),
            kind="upload", title="complex.docx", raw_bytes=raw, text=extraction.text, media_type=MEDIA["docx"],
            text_origin="parsed_text", parser=extraction.parser)
        save_extraction(db, record["revision_id"], scope=scope, extraction=extraction); db.commit()
        imported = source_library.import_selected(db, scope=scope, revision_ids=[record["revision_id"]], request_id=f"import-{n}")["items"][0]
        assert imported["status"] in ("imported", "existing"), imported
        result.append(record)
    pid = imported["paper_id"]
    owned = list(db.scalars(select(SourceLibraryImport).where(SourceLibraryImport.paper_id == pid)))
    owned = [row for row in owned if scope_of(db, row.revision_id).scope_type == "library"]
    assert len(owned) == 2 and db.query(Paper).count() == 1
    by_parser = {sources.read_revision(db, row.revision_id, scope=sources.SourceScope("library", pid))["parser"]: row.revision_id for row in owned}
    db.commit()
    return pid, by_parser["python-docx/1"], by_parser["ooxml-source/2"], result


def begin(db, pid, rid, *, request="index-request-1", mode="text", primary=False):
    state = store.state(db, pid)
    op, fresh = store.create_operation(db, pid, rid, conversation_id="index-owner", request_id=request,
        expected_version=state["version"], expected_primary_id=state["primary_revision_id"], set_primary=primary, index_mode=mode)
    ident = op.id; db.commit(); assert fresh
    return ident


def snapshot(db, pid):
    db.expire_all()
    return {"state": store.state(db, pid), "chunks": [(r.id,r.content) for r in db.scalars(select(Chunk).where(Chunk.paper_id == pid).order_by(Chunk.id))],
        "fts": [tuple(r) for r in db.execute(text("SELECT chunk_id,content FROM chunks_fts WHERE paper_id=:p ORDER BY chunk_id"), {"p":pid})]}


def test_primary_choice_is_independent_cas_and_old_alias_stays_fixed(db, setup):
    pid, old, new, _ = versions(db, setup)
    before = snapshot(db, pid); assert before["state"]["indexed_revision_id"] == old
    alias, _ = source_library.open_library_source(db, pid, "index-owner"); db.commit()
    current = store.select_primary(db, pid, new, conversation_id="index-owner", expected_version=0, expected_primary_id=old); db.commit()
    assert current["indexed_revision_id"] == old and current["primary_revision_id"] == new and current["version"] == 1
    assert snapshot(db,pid)["chunks"] == before["chunks"]
    with pytest.raises(sources.SourceConflict): store.select_primary(db, pid, old, conversation_id="index-owner", expected_version=0, expected_primary_id=old)
    db.rollback()
    latest, _ = source_library.open_library_source(db, pid, "index-owner"); db.commit()
    assert latest["revision_id"] != alias["revision_id"] and latest["parser"] == "ooxml-source/2"
    assert sources.read_revision(db, alias["revision_id"], scope=setup[0])["parser"] == "python-docx/1"


def test_text_rebuild_atomic_archive_old_evidence_and_new_footnotes(db, setup):
    pid, old, new, _ = versions(db, setup)
    before = snapshot(db, pid); old_chunk = before["chunks"][0][0]
    extraction = db.get(SourceExtraction, old).fingerprint
    ident = begin(db, pid, new, primary=True)
    service.execute(ident); db.expire_all()
    op = db.get(LibraryIndexOperation, ident); assert op.status == "done"
    state = store.state(db,pid); assert state["indexed_revision_id"] == new == state["primary_revision_id"]
    assert state["version"] == 1 and state["index_mode"] == "text"
    assert db.get(SourceExtraction,old).fingerprint == extraction
    assert db.get(LibraryChunkArchive,old_chunk).document["content"] == before["chunks"][0][1]
    assert db.scalar(text("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'FOOTNOTE'")) > 0
    assert db.get(LibraryIndexReceipt,ident).revision_id == new
    old_evidence = source_library.augment_library_evidence(db,[{"paper_id":pid,"chunk_id":old_chunk}],"index-owner")[0]
    assert sources.read_revision(db,old_evidence["revision_id"],scope=setup[0])["parser"] == "python-docx/1"
    foot = db.scalar(select(Chunk).where(Chunk.paper_id==pid,Chunk.content.contains("FOOTNOTE")))
    new_evidence = source_library.augment_library_evidence(db,[{"paper_id":pid,"chunk_id":foot.id}],"index-owner")[0]
    assert new_evidence["locator_kind"] == "word_block" and new_evidence["quote"] == foot.content
    assert sources.read_revision(db,new_evidence["revision_id"],scope=setup[0])["parser"] == "ooxml-source/2"


def test_semantic_union_excludes_old_cache_and_model_change_falls_back(db, setup, monkeypatch):
    pid, old, new, _ = versions(db, setup)
    ident = begin(db,pid,new,mode="semantic"); service.execute(ident); db.expire_all()
    from app.library_index_vectors import query_store
    base = setup[1](setup[2]); union = query_store(db,base)
    query = embeddings.get_embedder().encode(["FOOTNOTE ORIGINAL"])[0]
    hits = union.search(query,topk=100)
    current_ids = set(db.scalars(select(Chunk.id).where(Chunk.paper_id==pid)))
    assert hits and {i for i,_ in hits} <= current_ids and not set(base.ids)&current_ids
    assert union.has_compatible_dimension(query) and union.embedding_cache_matches_current()
    assert set(union.vectors_for(list(current_ids))) == current_ids
    monkeypatch.setattr(config.settings,"vector_dim",config.settings.vector_dim+1)
    changed = query_store(db,base)
    assert not changed.embedding_cache_matches_current() and changed.search(query) == []


def test_new_query_refreshes_managed_generation_held_in_same_session(db, setup, monkeypatch):
    from app.library_index_models import LibraryIndexState
    from app.library_index_vectors import query_store
    monkeypatch.setattr(db,"expire_on_commit",False)
    pid,old,new,_=versions(db,setup)
    first=begin(db,pid,old,request="stale-state-first",mode="semantic")
    service.execute(first);db.expire_all()
    held=db.get(LibraryIndexState,pid)
    assert held.current_build_id==first
    initial=query_store(db,setup[1](setup[2]))
    assert set(initial.receipts)=={first}
    db.commit()
    with SessionLocal() as writer:
        next_build=begin(writer,pid,new,request="stale-state-next",mode="semantic")
    service.execute(next_build)
    assert held.current_build_id==first,"Probe must actually retain stale ORM identity"
    latest=query_store(db,setup[1](setup[2]))
    assert held.current_build_id==next_build and set(latest.receipts)=={next_build}


def test_mixed_legacy_and_new_generation_retrieval_share_query_without_stale_chunks(db,setup,monkeypatch):
    pid,old,new,_=versions(db,setup)
    raw=b"# Separate source\nFOOTNOTE in another legacy library paper\n"
    parsed=extract(raw,MEDIA["md"])
    record=sources.capture_source(db,scope=setup[0],authorization=sources.SourceAuthorization(setup[0],"mixed-source","upload"),
        kind="upload",title="other.md",raw_bytes=raw,text=parsed.text,media_type=MEDIA["md"],text_origin="parsed_text",parser=parsed.parser)
    save_extraction(db,record["revision_id"],scope=setup[0],extraction=parsed);db.commit()
    imported=source_library.import_selected(db,scope=setup[0],revision_ids=[record["revision_id"]],request_id="mixed-import")["items"][0]
    assert imported["status"]=="imported"
    other=imported["paper_id"]
    stale=set(db.scalars(select(Chunk.id).where(Chunk.paper_id==pid)))
    ident=begin(db,pid,new,mode="semantic");service.execute(ident);db.expire_all()
    from app.library_index_vectors import query_store
    union=query_store(db,setup[1](setup[2]));query=embeddings.get_embedder().encode(["FOOTNOTE"])[0]
    hits=union.search(query,topk=100)
    assert not stale&{i for i,_ in hits}
    assert set(db.scalars(select(Chunk.id).where(Chunk.paper_id==other)))<={i for i,_ in hits}
    from app.rag import retriever
    monkeypatch.setattr(retriever,"NumpyVectorStore",lambda:setup[1](setup[2]))
    result=retriever.retrieve(db,"FOOTNOTE ORIGINAL",topk=6,paper_id=pid)
    assert result and any("FOOTNOTE ORIGINAL" in item.quote for item in result)
    assert all(item.paper_id==pid and item.chunk_id not in stale for item in result)


def test_publish_failure_rolls_back_and_paid_preparation_reused(db, setup, monkeypatch):
    pid, old, new, _ = versions(db,setup); before=snapshot(db,pid)
    ident=begin(db,pid,new,mode="semantic",primary=True)
    real=store._advance_state
    monkeypatch.setattr(store,"_advance_state",lambda *a,**k: (_ for _ in ()).throw(OSError("simulated disk error")))
    with pytest.raises(OSError): service.execute(ident)
    service.mark_error(ident,OSError())
    assert snapshot(db,pid)==before and db.query(LibraryIndexReceipt).count()==0 and db.query(LibraryChunkArchive).count()==0
    db.rollback(); monkeypatch.setattr(store,"_advance_state",real)
    monkeypatch.setattr(indexer,"_prepare_embedding_vectors",lambda *a,**k: (_ for _ in ()).throw(AssertionError("paid results repeated")))
    service.execute(ident);db.expire_all();assert db.get(LibraryIndexOperation,ident).status=="done"


@pytest.mark.parametrize("action",["archive","delete","cancel","primary"])
def test_mid_preparation_changes_block_publication(db,setup,monkeypatch,action):
    pid,old,new,_=versions(db,setup);before=snapshot(db,pid)
    ident=begin(db,pid,new,mode="semantic")
    prepare=indexer._prepare_embedding_vectors
    def changed(*a,**k):
        vectors,fp=prepare(*a,**k)
        with SessionLocal.begin() as other:
            if action=="archive":
                from app.timeutil import utcnow
                other.get(Conversation,"index-owner").archived_at=utcnow()
            elif action=="delete": other.delete(other.get(Conversation,"index-owner"))
            elif action=="cancel": other.get(LibraryIndexOperation,ident).status="cancelled"
            else: store.select_primary(other,pid,new,conversation_id="index-owner",expected_version=0,expected_primary_id=old)
        return vectors,fp
    monkeypatch.setattr(indexer,"_prepare_embedding_vectors",changed)
    if action=="cancel":service.execute(ident)
    else:
        with pytest.raises(sources.SourceError):service.execute(ident)
    after=snapshot(db,pid)
    assert after["chunks"]==before["chunks"] and after["fts"]==before["fts"] and after["state"]["current_build_id"] is None


def test_startup_preserves_prepared_and_marks_unfinished_without_requests(db,setup):
    pid,old,new,_=versions(db,setup);ident=begin(db,pid,new)
    service.recover_startup();db.expire_all();assert db.get(LibraryIndexOperation,ident).status=="interrupted"
    service.execute(ident);db.expire_all();assert db.get(LibraryIndexOperation,ident).status=="done"


def test_api_scoping_idempotency_resume_and_receipt_usage(db,setup,monkeypatch):
    pid,old,new,_=versions(db,setup);starts=[]
    monkeypatch.setattr(service,"start",starts.append)
    from app import task_runner
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    with TestClient(app) as client:
        payload={"conversation_id":"index-owner","revision_id":new,"expected_version":0,"expected_primary_id":old,"request_id":"api-request-1","index_mode":"text"}
        first=client.post(f"/api/library-index/{pid}/builds",json=payload)
        assert first.status_code==200,first.text
        ident=first.json()["id"]
        assert client.post(f"/api/library-index/{pid}/builds",json=payload).json()["replayed"] and starts==[ident]
        assert client.post(f"/api/library-index/{pid}/builds",json={**payload,"set_primary":True}).status_code==409
        assert client.get(f"/api/library-index/builds/{ident}?conversation_id=index-other").status_code==404
        service.execute(ident)
        output=client.get(f"/api/library-index/builds/{ident}?conversation_id=index-owner")
        assert output.status_code==200,output.text
        assert output.json()["status"]=="done" and output.json()["usage"]["call_count"]==0
        assert client.post(f"/api/library-index/builds/{ident}/cancel",json={"conversation_id":"index-owner"}).status_code==409


def api_embeddings(monkeypatch, dispatch):
    monkeypatch.setattr(config.settings,"embedding_api_key","synthetic-index-key")
    monkeypatch.setattr(config.settings,"embedding_base_url","https://synthetic-embed.example.test/v1")
    monkeypatch.setattr(config.settings,"embedding_model","Embedding-Exact-Case")
    monkeypatch.setattr(embeddings,"_embedder",embeddings.APIEmbedder(synthetic=True))
    monkeypatch.setattr(indexer,"EMBED_BATCH_SIZE",2)
    real_client=httpx.Client
    monkeypatch.setattr(httpx,"Client",lambda *a,**k:real_client(*a,transport=httpx.MockTransport(dispatch),**k))


def test_paid_batches_survive_interruption_and_resume_only_missing_batches(db,setup,monkeypatch):
    pid,old,new,_=versions(db,setup);before=snapshot(db,pid);calls=[];fail=[True]
    def dispatch(request):
        # A separate writer must be free while waiting on/using a provider.
        with SessionLocal() as writer:
            writer.execute(text("BEGIN IMMEDIATE"));writer.rollback()
        body=json.loads(request.content);calls.append(body)
        if len(calls)==2 and fail[0]:return httpx.Response(503,text="PRIVATE_SYNTHETIC_PROVIDER_ERROR")
        return httpx.Response(200,json={"data":[{"index":i,"embedding":[.1,.2,.3]} for i in range(len(body["input"]))],"usage":{"prompt_tokens":5,"total_tokens":5}})
    api_embeddings(monkeypatch,dispatch)
    ident=begin(db,pid,new,mode="semantic")
    with pytest.raises(httpx.HTTPStatusError) as caught:service.execute(ident)
    service.mark_error(ident,caught.value)
    db.expire_all();op=db.get(LibraryIndexOperation,ident)
    assert op.status=="partial" and "PRIVATE" not in op.error
    assert db.query(LibraryIndexPreparedBatch).count()==1 and snapshot(db,pid)==before
    db.rollback();fail[0]=False
    service.execute(ident);db.expire_all();assert db.get(LibraryIndexOperation,ident).status=="done"
    assert len(calls)==1+(len(db.get(LibraryIndexReceipt,ident).chunk_locators)+1)//2
    assert calls[0]["input"] not in [item["input"] for item in calls[2:]]
    from app.call_ledger import list_calls,summarize_calls
    rows=list_calls(run_id="library-index:"+ident,include_synthetic=True)["items"]
    assert len(rows)==len(calls) and all(row["synthetic"] for row in rows)
    assert summarize_calls(run_id="library-index:"+ident)["call_count"]==0
    assert db.query(LibraryIndexPreparedBatch).count()==0


@pytest.mark.parametrize("change",["key","archive","cancel","delete"])
def test_after_paid_batch_rechecks_authorization_before_next_http(db,setup,monkeypatch,change):
    pid,old,new,_=versions(db,setup);before=snapshot(db,pid);calls=[];operation=[None]
    def dispatch(request):
        body=json.loads(request.content);calls.append(body)
        if change=="key":monkeypatch.setattr(config.settings,"embedding_api_key","changed-synthetic-key")
        else:
            with SessionLocal.begin() as writer:
                if change=="archive":
                    from app.timeutil import utcnow
                    writer.get(Conversation,"index-owner").archived_at=utcnow()
                elif change=="cancel":writer.get(LibraryIndexOperation,operation[0]).status="cancelled"
                else:
                    from app import data_cleanup
                    data_cleanup.remove(writer,"index-owner")
        return httpx.Response(200,json={"data":[{"index":i,"embedding":[.1,.2,.3]} for i in range(len(body["input"]))],"usage":{"total_tokens":5}})
    api_embeddings(monkeypatch,dispatch)
    ident=operation[0]=begin(db,pid,new,mode="semantic")
    with pytest.raises((sources.SourceError,ValueError)):service.execute(ident)
    assert len(calls)==1
    service.mark_error(ident,ValueError("synthetic"));db.expire_all()
    assert snapshot(db,pid)["chunks"]==before["chunks"] and db.query(LibraryIndexReceipt).count()==0
    if change=="delete":
        assert db.get(LibraryIndexOperation,ident) is None and db.query(LibraryIndexPreparedBatch).count()==0
    elif change=="cancel":assert db.get(LibraryIndexOperation,ident).status=="cancelled"
    else:assert db.query(LibraryIndexPreparedBatch).count()==1


def test_completed_library_index_survives_deleting_initiating_chat(db,setup):
    pid,old,new,_=versions(db,setup);ident=begin(db,pid,new,mode="semantic",primary=True);service.execute(ident)
    from app import data_cleanup
    db.rollback();db.execute(text("BEGIN IMMEDIATE"));data_cleanup.remove(db,"index-owner");db.commit()
    assert db.get(LibraryIndexOperation,ident) is None
    assert db.get(LibraryIndexReceipt,ident) is not None and db.get(Paper,pid) is not None
    assert store.state(db,pid)["indexed_revision_id"]==new
    record,_=source_library.open_library_source(db,pid,"index-other");db.commit()
    assert read_document(db,record["revision_id"],scope=sources.SourceScope("conversation","index-other"))["parser"]=="ooxml-source/2"


@pytest.mark.parametrize("corruption",["receipt","vector","pointer"])
def test_corrupt_current_index_fails_closed(db,setup,corruption):
    pid,old,new,_=versions(db,setup);ident=begin(db,pid,new,mode="semantic");service.execute(ident)
    if corruption=="receipt":db.execute(text("UPDATE library_index_receipts SET fingerprint='bad' WHERE id=:id"),{"id":ident})
    elif corruption=="vector":db.execute(text("UPDATE library_index_vectors SET sha256='bad' WHERE build_id=:id"),{"id":ident})
    else:db.execute(text("UPDATE library_index_states SET indexed_revision_id=:rid WHERE paper_id=:p"),{"rid":old,"p":pid})
    db.commit();db.expire_all()
    from app.library_index_vectors import query_store
    with pytest.raises(sources.SourceCorruption):query_store(db,setup[1](setup[2])).search(embeddings.get_embedder().encode(["FOOTNOTE"])[0])


def test_full_backup_restores_prepared_vectors_and_publishes_without_reembedding(db,setup,monkeypatch,tmp_path):
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import data_backup
    from app.db import engine
    pid,old,new,_=versions(db,setup);ident=begin(db,pid,new,mode="semantic",primary=True)
    publish=store.publish
    monkeypatch.setattr(store,"publish",lambda *a,**k:(_ for _ in ()).throw(OSError("synthetic publication interruption")))
    with pytest.raises(OSError):service.execute(ident)
    monkeypatch.setattr(store,"publish",publish)
    db.expire_all();assert db.get(LibraryIndexOperation,ident).status=="prepared"
    # Only the generated fixture manifest lies outside the managed data root.
    db.get(Paper,pid).parsed_path="";db.commit()
    source=tmp_path/"backup-source";source.mkdir()
    data_backup._database_snapshot(Path(engine.url.database),source/config.settings.db_name)
    exported=data_backup.create_backup(source,database=config.settings.db_name,origin_data_dir=config.DATA_DIR)
    target=tmp_path/"restored-index"
    preview=data_backup.prepare_restore(target,exported["path"],expected_database=config.settings.db_name)
    data_backup.schedule_restore(target,preview["id"]);data_backup.apply_pending_restore(target,expected_database=config.settings.db_name)
    restored_engine=create_engine("sqlite:///"+str(target/config.settings.db_name))
    factory=sessionmaker(bind=restored_engine)
    monkeypatch.setattr(service,"SessionLocal",factory)
    monkeypatch.setattr(indexer,"_prepare_embedding_vectors",lambda *a,**k:(_ for _ in ()).throw(AssertionError("restored completed vectors repeated")))
    try:
        service.recover_startup();service.execute(ident)
        with factory() as restored:
            assert store.state(restored,pid)["indexed_revision_id"]==new
            assert restored.get(LibraryIndexOperation,ident).status=="done"
            assert restored.query(LibraryChunkArchive).count()>0
            fixed=sources.read_revision(restored,old,scope=sources.SourceScope("library",pid),include_raw=True)
            assert fixed["raw_sha256"]==hashlib.sha256(fixed["raw_bytes"]).hexdigest()
    finally:restored_engine.dispose()


@pytest.mark.parametrize("stage",["queued","published"])
def test_legacy_pdf_reparse_cannot_overwrite_managed_versions(db,setup,stage,monkeypatch):
    pid,old,new,_=versions(db,setup);ident=begin(db,pid,new)
    if stage=="published":service.execute(ident)
    before=snapshot(db,pid);paper=db.get(Paper,pid);status=paper.status
    monkeypatch.setattr(indexer,"_prepare_embedding_vectors",lambda *a,**k:(_ for _ in ()).throw(AssertionError("unexpected paid preparation")))
    with pytest.raises(indexer.IndexVersionManaged):indexer.ingest_pdf(db,paper)
    assert paper.status==status and snapshot(db,pid)==before
    with pytest.raises(indexer.IndexVersionManaged):indexer._commit_pdf_index(db,None,None,pid,None,None,None,None,None,None,None,None)
    with pytest.raises(indexer.IndexVersionManaged):indexer._finish_needs_ocr(db,paper,None,None,None,"synthetic OCR")
    db.rollback()
    from app.api.papers import reparse_paper
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as caught:reparse_paper(pid,db)
    assert caught.value.status_code==409


def test_thread_start_failure_retains_resumable_operation_without_false_active_worker(db,setup,monkeypatch):
    pid,old,new,_=versions(db,setup);ident=begin(db,pid,new)
    monkeypatch.setattr(threading.Thread,"start",lambda self:(_ for _ in ()).throw(RuntimeError("synthetic thread exhaustion")))
    with pytest.raises(sources.SourceConflict):service.start(ident)
    db.expire_all();assert db.get(LibraryIndexOperation,ident).status=="interrupted"
    assert ident not in service.active()


def test_readonly_cache_identity_separates_selection_from_published_numpy_exclusion(db,setup):
    pid,old,new,_=versions(db,setup)
    initial=store.query_index_identity(db)
    assert initial["pointers"]==[] and initial["managed_paper_ids"]==[]
    store.select_primary(db,pid,new,conversation_id="index-owner",expected_version=0,expected_primary_id=old);db.commit()
    selected=store.query_index_identity(db,[pid])
    assert selected["managed_paper_ids"]==[] and selected["fingerprint"]!=initial["fingerprint"]
    ident=begin(db,pid,new);service.execute(ident);db.expire_all()
    published=store.query_index_identity(db,[pid])
    assert published["managed_paper_ids"]==[pid] and published["fingerprint"]!=selected["fingerprint"]
    assert published==store.query_index_identity(db) and not db.new and not db.dirty
    assert store.query_index_identity(db,[])["pointers"]==[]
    db.execute(text("UPDATE library_index_receipts SET fingerprint='bad' WHERE id=:id"),{"id":ident});db.commit()
    with pytest.raises(sources.SourceCorruption):store.query_index_identity(db,[pid])
