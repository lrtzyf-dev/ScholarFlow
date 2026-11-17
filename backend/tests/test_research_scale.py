"""Observed local API/storage tiers, not claimed commercial capacity."""
import json
from pathlib import Path
from time import perf_counter
from app.api.graph import get_graph
from app.models import Paper, Reference, Conversation
from app.source_store import SourceScope, SourceAuthorization, capture_source
from app.source_models import SourceLibraryImport
from app.research_store import matrix_document, save_matrix
from app.research_models import ResearchMatrix, MatrixRevision
import pytest


OUTPUT = Path(__file__).resolve().parents[2] / "verification/research-persistence/scale"


@pytest.mark.parametrize("count", [100, 500, 1000])
def test_graph_observed_tier(db, count):
    items=[Paper(title=f"Synthetic graph paper {i}",arxiv_id=f"2401.{i:05d}",status="parsed",content_hash=f"synthetic-{i}") for i in range(count)]
    db.add_all(items);db.flush()
    ids=[p.id for p in items]
    db.add_all([Reference(paper_id=ids[i],ref_index=1,title=items[(i+1)%count].title,
        raw_text=f"arXiv:2401.{(i+1)%count:05d}") for i in range(count)])
    db.commit()
    timings=[]
    for index in range(3):
        start=perf_counter();graph=get_graph(db=db,limit=100,offset=0);timings.append(round((perf_counter()-start)*1000,2))
        assert graph["total_nodes"]==count and len(graph["nodes"])==100
        assert graph["edges"] and all(e["relation_id"] and e["relation_version"] for e in graph["edges"])
    OUTPUT.mkdir(parents=True,exist_ok=True)
    (OUTPUT/f"graph-{count}.json").write_text(json.dumps({"kind":"graph_api","synthetic_nodes":count,
        "synthetic_references":count,"visible_node_page":100,"elapsed_ms_cold_then_warm":timings,
        "real_provider_calls":0,
        "projection":"One persistent full-library graph; warm reads validate input content without reference rematching or relation updates.",
        "limitation":"Cold API includes full-library matching and centrality before pagination. Timings are three local observations; browser FPS and p95 not established."},indent=2),encoding="utf-8")


@pytest.mark.parametrize("count", [20, 100, 200])
def test_matrix_storage_observed_tier(db,count):
    import hashlib
    cid="matrix-scale"; db.add(Conversation(id=cid,title="Synthetic scale"));db.commit()
    scope=SourceScope("conversation",cid);ids=[]
    for i in range(count):
        raw=f"Synthetic source {i}: method and evaluation.\n".encode()
        record=capture_source(db,scope=scope,authorization=SourceAuthorization(scope,f"scale-{i}","upload"),
            kind="upload",title=f"sample-{i}.md",raw_bytes=raw,text=raw.decode(),media_type="text/markdown")
        p=Paper(title=f"sample-{i}",status="parsed",content_hash=hashlib.sha256(raw).hexdigest());db.add(p);db.flush();ids.append(p.id)
        db.add(SourceLibraryImport(revision_id=record["revision_id"],raw_sha256=p.content_hash,paper_id=p.id,request_id=f"scale-{i}",chunk_locators={}))
    db.commit()
    fields=[{"key":f"field_{i}","label":f"Field {i}","query":f"method {i}"} for i in range(8)]
    start=perf_counter();document=matrix_document(db,cid,ids,fields)
    m=ResearchMatrix(conversation_id=cid,title="Scale matrix",document=document);db.add(m);db.flush()
    db.add(MatrixRevision(matrix_id=m.id,version=1,action="create",document={"title":m.title,"data":document}));db.commit()
    create_ms=round((perf_counter()-start)*1000,2)
    start=perf_counter();save_matrix(db,m,document,1,"cell_review");db.commit();edit_ms=round((perf_counter()-start)*1000,2)
    assert len(m.document["papers"])==count and len(m.document["rows"])==8
    OUTPUT.mkdir(parents=True,exist_ok=True)
    (OUTPUT/f"matrix-{count}.json").write_text(json.dumps({"kind":"matrix_storage","synthetic_sources":count,
        "fields":8,"cells":count*8,"create_ms":create_ms,"revision_commit_ms":edit_ms,
        "document_json_bytes":len(json.dumps(document).encode()),"real_provider_calls":0,
        "limitation":"Empty-cell fixed-source matrix storage only; retrieval/embeddings, browser rendering and document semantic accuracy not measured."},indent=2),encoding="utf-8")
