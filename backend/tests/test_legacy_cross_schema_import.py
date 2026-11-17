"""Old20 named-row migration with isolated, nonempty synthetic archives only.

The candidate is signed synthetic bytes, never an executed program. Historical
DDL is an already reviewed literal; no old Python, user data or provider runs.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import socket
import sqlite3

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app import legacy_import as module, legacy_migrations as migrations
from app import legacy_cross_schema as cross, legacy_fixed_sources as fixed
from app.legacy_schema_contracts import OLD20_FTS_OBJECTS, OLD20_NO_FTS_OBJECTS, OLD20_COLUMNS, TARGET_OBJECTS
from app.atomic_files import atomic_write_json
from app.release_files import digest, file_record
from app.release_info import ReleaseError, read_json
from app.release_schema import schema_snapshot
from .test_legacy_install import legacy, setup, prepare, files
from .test_legacy_import import inputs, review, run, recover
from .test_release_install import PowerLoss


PID, CID, CHUNK, QID = (c * 32 for c in "1234")
QUOTE = "syntheticword uses eight heads."
EVIDENCE = [{"id":"e1", "paper_id":PID, "chunk_id":CHUNK, "page":1,
    "bbox":[40,80,300,100], "quote":QUOTE}]
ANSWER = "The synthetic source uses eight heads.[^e1]"


def pdf_bytes():
    content = b"BT /F1 12 Tf 40 700 Td (syntheticword uses eight heads.) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
    raw, offsets = b"%PDF-1.4\n", [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(raw)); raw += str(i).encode()+b" 0 obj\n"+obj+b"\nendobj\n"
    xref=len(raw);raw+=b"xref\n0 6\n0000000000 65535 f \n"
    raw+=b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    return raw+b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"+str(xref).encode()+b"\n%%EOF\n"


def put(db, table, **values):
    for _,name,typ,required,default,_ in db.execute('PRAGMA table_info("'+table+'")'):
        if name in values or not required or default is not None:continue
        values[name]=(0 if typ in ("INTEGER","FLOAT") else "{}" if typ=="JSON"
            else "2026-11-01 12:00:00.000000" if typ=="DATETIME" else "")
    names=list(values)
    db.execute('INSERT INTO "'+table+'" ('+','.join('"'+n+'"' for n in names)+') VALUES ('+
        ','.join('?' for _ in names)+')',[values[n] for n in names])


def seed(s, has_fts=True):
    database=s["data"] / "scholarflow.db"
    database.unlink()  # Exact test-owned fixture file, not a user database.
    raw=pdf_bytes();raw_hash=hashlib.sha256(raw).hexdigest()
    (s["data"] / "pdf").mkdir();(s["data"] / "parsed").mkdir()
    pdf=s["data"] / "pdf/original.pdf";pdf.write_bytes(raw)
    parsed=s["data"] / "parsed/original.json";parsed.write_text('{"historical":"retained, not trusted as fresh extraction"}')
    (s["data"] / "vectors.npy").write_bytes(b"synthetic old vector bytes, never loaded")
    (s["data"] / "vectors.json").write_text('{"ids":["'+CHUNK+'"]}')
    with closing(sqlite3.connect(database)) as db:
        for typ in ("table","index"):
            for kind,name,_,sql in (OLD20_FTS_OBJECTS if has_fts else OLD20_NO_FTS_OBJECTS):
                if kind==typ and sql and not name.startswith("chunks_fts_"):db.execute(sql)
        put(db,"projects",id="a"*32,name="Synthetic project",root_path="Z:/never-open-this-project",root_identity='{"historical":true}')
        put(db,"conversations",id=CID,title="Historical conversation",project_id="a"*32)
        put(db,"papers",id=PID,title="Synthetic old paper",pdf_path=str(pdf),parsed_path=str(parsed),
            content_hash=raw_hash,page_count=1,status="parsed")
        put(db,"chunks",id=CHUNK,paper_id=PID,chunk_index=0,parent_index=0,page=1,
            bbox=json.dumps(EVIDENCE[0]["bbox"]),content=QUOTE,char_count=len(QUOTE),kind="text")
        put(db,"references",id="5"*32,paper_id=PID,ref_index=1,raw_text="Synthetic reference",title="Cited",page=1)
        put(db,"qa_records",id=QID,conversation_id=CID,paper_id=PID,question="How many?",answer=ANSWER,
            evidence=json.dumps(EVIDENCE),claims='[{"historical":true}]',trace='[{"old":true}]',in_memory=0)
        put(db,"foreground_runs",id="old-foreground",conversation_id=CID,question="unfinished",status="interrupted",partial_answer="retained partial")
        put(db,"tasks",id="6"*32,conversation_id=CID,type="agent",status="interrupted",checkpoint='{"old":"never resume"}',result='{"partial":"retained result"}')
        put(db,"subscriptions",id="7"*32,topic="Synthetic",active=1)
        put(db,"recommendations",id="8"*32,subscription_id="7"*32,title="Synthetic recommendation")
        put(db,"conversation_memory",conversation_id=CID,summary="Historical memory",sources='["history"]',mode="rolling")
        put(db,"compaction_calls",id="9"*32,conversation_id=CID,status="started",usage_kind="unknown",usage='{"historic":"unknown"}')
        put(db,"deleted_conversations",id="d"*32)
        put(db,"desktop_state",key="synthetic",value='{"preserve":true}')
        for i,table in enumerate(("workspace_switches","workspace_transfers","managed_worktrees","project_changes","project_commands","git_operations")):
            values={"id":f"{i+20:032x}","status":"applying"}
            cols=OLD20_COLUMNS[table]
            for col in ("project_id","source_project_id","target_project_id"):
                if col in cols:values[col]="a"*32
            if "conversation_id" in cols:values["conversation_id"]=CID
            if table=="project_commands":values.update(command="NEVER_EXECUTE_THIS_SYNTHETIC_COMMAND",cwd="Z:/never-open",status="running",pid=1234567)
            put(db,table,**values)
        if has_fts:db.execute("INSERT INTO chunks_fts(chunk_id,paper_id,content) VALUES (?,?,?)",(CHUNK,PID,"stale synthetic FTS must be rebuilt"))
        db.commit()
    manifest=read_json(s["candidate"] / "install-manifest.json")
    target=cross._body(TARGET_OBJECTS);target["sha256"]=digest(target)
    manifest["target_schema"]=target
    manifest["candidate_id"]=digest({k:v for k,v in manifest.items() if k!="candidate_id"})
    atomic_write_json(s["candidate"] / "install-manifest.json",manifest);s["sign"](s["candidate"],manifest)
    (s["data"] / "hooks.json").write_text('{"post_tool_use":[{"command":"NEVER_EXECUTE"}]}')
    s.update(raw=raw,raw_hash=raw_hash,database=database,pdf=pdf,parsed=parsed)
    return s


def finalize(s, monkeypatch, has_fts=True):
    s["prepared"]=prepare(s);s["output"]=s["data"].parent / "cross-independent-data"
    s["options"]={"trusted_keys":s["trust"],"migration_id":cross.FTS_ID if has_fts else cross.NO_FTS_ID,
        "expected_preparation_sha256":s["prepared"]["preparation_sha256"]}
    def forbidden(*a,**k):pytest.fail("Network/process forbidden during separate synthetic migration")
    monkeypatch.setattr("subprocess.run",forbidden);monkeypatch.setattr("httpx.Client.send",forbidden)
    monkeypatch.setattr(socket.socket,"connect",forbidden);monkeypatch.setattr(socket,"create_connection",forbidden)
    return s


@pytest.fixture(params=[True,False],ids=["fts","no-fts"])
def crossed(legacy,monkeypatch,request):
    return finalize(seed(legacy,request.param),monkeypatch,request.param)


def test_nonempty_exact_old20_migration_preserves_all_history_and_reads_new_fixed_original(crossed,monkeypatch):
    from app import source_store,source_documents,library_sources,library_index_store,source_library
    from app.models import Paper
    s=crossed;old,data=files(s["legacy"]),files(s["data"])
    plan=review(s)
    assert plan["structure_change_planned"] and not plan["cross_schema_migration_performed"]
    assert run(s)["cross_schema_migration_performed"]
    assert files(s["legacy"])==old and files(s["data"])==data and not s["new_target"].exists()
    target=s["output"] / "scholarflow.db"
    assert schema_snapshot(target)["sha256"]==migrations.CURRENT_SCHEMA
    with closing(sqlite3.connect(target)) as db:
        assert all(db.execute('SELECT count(*) FROM "'+table+'"').fetchone()[0]>0 for table in OLD20_COLUMNS)
        assert db.execute("SELECT answer,evidence,in_memory,provider_context FROM qa_records").fetchone()==(ANSWER,json.dumps(EVIDENCE),0,"{}")
        assert db.execute("SELECT status,agent_checkpoint,runtime_config,display_snapshot,partial_answer FROM foreground_runs").fetchone()==("cancelled",None,None,None,"retained partial")
        assert db.execute("SELECT status,checkpoint,runtime_config,result FROM tasks").fetchone()==("cancelled",None,None,'{"partial":"retained result"}')
        assert db.execute("SELECT status,usage_kind FROM compaction_calls").fetchone()==("failed","unknown")
        assert db.execute("SELECT active FROM subscriptions").fetchone()==(0,)
        for table in migrations._EFFECTS[:6]:assert db.execute('SELECT status FROM "'+table+'"').fetchone()==("unknown",)
        assert db.execute("SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH 'syntheticword'").fetchone()==(CHUNK,)
        assert db.execute("SELECT count(*) FROM source_locators").fetchone()==(0,)
        assert db.execute("SELECT count(*) FROM library_index_receipts").fetchone()==(0,)
        assert db.execute("SELECT vector_data,vector_fingerprint FROM library_chunk_archives").fetchone()==(None,None)
    engine=sa.create_engine("sqlite:///"+target.as_posix())
    try:
        with Session(engine) as db:
            record,receipt=library_sources.ensure_owned(db,PID)
            assert receipt.chunk_locators=={} and record["raw_bytes"]==s["raw"]
            assert record["text"] is None and record["text_origin"]=="none"
            scope=source_store.SourceScope("library",PID)
            doc=source_documents.read_document(db,record["revision_id"],scope=scope)
            assert QUOTE in doc["text"] and doc["pages"][0]["geometry_verified"]
            assert not doc["extraction_accuracy_verified"] and not doc["model_understanding_verified"]
            state=library_index_store.state(db,PID)
            assert state["index_mode"]=="legacy_unknown" and state["indexed_revision_id"] is None
            assert source_library.augment_library_evidence(db,EVIDENCE.copy(),CID)==EVIDENCE
            assert Path(db.get(Paper,PID).pdf_path)==s["output"] / "pdf/original.pdf"
            assert db.get(Paper,PID).parsed_path==""
    finally:engine.dispose()
    report=read_json(s["output"] / "legacy-schema-migration.json")
    assert report["source_migration"]["old_chunk_alignment_verified"] is False
    assert report["full_text_index"]["new_embedding_calls"]==0
    assert {row["original_path"] for row in report["legacy_vectors"]["assets"]}=={"vectors.npy","vectors.json"}
    for row in report["legacy_vectors"]["assets"]:
        assert not (s["output"] / row["original_path"]).exists()
        assert (s["output"] / row["dormant_path"]).read_bytes()==data[row["original_path"]]
    from app.pipeline.vectordb import NumpyVectorStore
    # The fixture's intentionally invalid old npy bytes cannot break startup
    # or the lexical route because they are never opened as a current cache.
    assert not NumpyVectorStore(s["output"] / "vectors.npy").embedding_cache_matches_current()
    assert (s["output"] / "parsed/original.json").read_bytes()==data["parsed/original.json"]
    run_result=recover(s,plan)
    assert set(module.BASE_BLOCKERS)<=set(run_result["legacy_blockers"])
    assert run_result["state"]=="committed" and not run_result["public_release_ready"]


@pytest.mark.parametrize("boundary",["preparing","staged","ready","before_publish","published","committed"])
def test_cross_schema_power_loss_is_rebuilt_deterministically(crossed,monkeypatch,boundary):
    s=crossed;plan=review(s);old,data=files(s["legacy"]),files(s["data"])
    monkeypatch.setattr(module,"_checkpoint",lambda name:(_ for _ in ()).throw(PowerLoss()) if name==boundary else None)
    with pytest.raises(PowerLoss):module.import_data(*inputs(s),**s["options"],expected_review_sha256=plan["review_sha256"])
    monkeypatch.setattr(module,"_checkpoint",lambda name:None)
    assert recover(s,plan)["state"]=="committed"
    assert files(s["legacy"])==old and files(s["data"])==data and not s["new_target"].exists()


@pytest.mark.parametrize("kind",["missing","mismatch","external","corrupt_pdf","unknown_hash","parsing"])
def test_unavailable_or_unparsed_original_never_certifies_historical_evidence(legacy,monkeypatch,kind):
    s=seed(legacy)
    with closing(sqlite3.connect(s["database"])) as db:
        if kind=="missing":s["pdf"].unlink()
        elif kind=="mismatch":db.execute("UPDATE papers SET content_hash=?",("f"*64,))
        elif kind=="external":db.execute("UPDATE papers SET pdf_path='Z:/never-read-external.pdf',parsed_path='Z:/never-read.json'")
        elif kind=="unknown_hash":db.execute("UPDATE papers SET content_hash=NULL")
        elif kind=="parsing":db.execute("UPDATE papers SET status='parsing'")
        else:
            s["raw"]=b"not a PDF, retained as hash-matching original";s["pdf"].write_bytes(s["raw"])
            db.execute("UPDATE papers SET content_hash=?",(hashlib.sha256(s["raw"]).hexdigest(),))
        db.commit()
    finalize(s,monkeypatch);run(s)
    with closing(sqlite3.connect(s["output"] / "scholarflow.db")) as db:
        valid=kind in ("corrupt_pdf","parsing")
        assert db.execute("SELECT count(*) FROM source_library_bindings").fetchone()==(int(valid),)
        assert db.execute("SELECT count(*) FROM source_extractions").fetchone()==(int(kind=="parsing"),)
        if not valid:assert db.execute("SELECT pdf_path,status FROM papers").fetchone()==("","failed")
        # The legacy page API follows parsed_path without a status/hash check.
        # Unbound old parsed assets must remain history, not an active page.
        assert db.execute("SELECT parsed_path FROM papers").fetchone()==("",)
        assert db.execute("SELECT answer,evidence FROM qa_records").fetchone()==(ANSWER,json.dumps(EVIDENCE))
        assert db.execute("SELECT count(*) FROM source_locators").fetchone()==(0,)
    record=read_json(s["output"] / "legacy-schema-migration.json")["source_migration"]["papers"][0]
    assert record["historical_chunk_alignment"]=="not_verified"
    if kind=="corrupt_pdf":assert record["structured_extraction"]=="local_parse_unavailable_raw_retained"


def test_synthetic_active_old_run_remains_blocked_before_preparation(legacy):
    s=seed(legacy)
    with closing(sqlite3.connect(s["database"])) as db:db.execute("UPDATE tasks SET status='running'");db.commit()
    with pytest.raises(ReleaseError,match="活动/排队"):prepare(s)
    assert not s["new_target"].exists()


@pytest.mark.parametrize("source",[
    "430c08377dd55dde219f4b95127074f8bf7f360654f4e5d94cd9bd17fa5b7176",
    "f10af69631f9940e5f159c73982b07e75fd72d30745b9f986c56a16f5f8d0177",
    "e64ad130c71a20ad06978ba4d1786831833729fc08ae588b6a78bc6a79b861b8",
    "c05302ca80a7fcae3be2279036b095b9cf9df70b16ac456c03dffccd2be93e54",
    "a64c5b9d6f85b46843d1cb8047f2a9a213b2916bfefdb5cb1056f78356b25a22",
    "80603241307183aa84e1c2dcc9532c2a9c86d12aa710b84b7a9cd015a58129ca",
    "a9159dea6c118d83421756c6605262660b1faff0ce705069aaf1a46109c6bf1a",
    "db69c958e6ad407136da9cd4e75a971bda06678fba3ec9c9b76ff47777172082"])
def test_other_eight_catalogued_histories_are_not_implicitly_admitted(source):
    for ident in (cross.FTS_ID,cross.NO_FTS_ID,migrations.MIGRATION_ID):
        with pytest.raises(ReleaseError):migrations.resolve(ident,source,migrations.CURRENT_SCHEMA,migrations.CONTRACT)


def test_cross_schema_new_tables_cannot_gain_an_implicit_runtime_record(tmp_path):
    database=tmp_path / "synthetic-current.db";cross.create_target(database)
    with closing(sqlite3.connect(database)) as db:
        put(db,"run_inputs",input_id="e"*32,status="cancelled")
        db.commit()
    with pytest.raises(ReleaseError,match="非审核"):cross.verify_new_tables(database)


def test_archive_without_config_gets_read_only_credential_free_configuration(legacy,monkeypatch):
    s=seed(legacy);(s["data"] / "config.json").unlink();finalize(s,monkeypatch);run(s)
    assert read_json(s["output"] / "config.json")=={"approval_mode":"read_only","llm_api_key":"",
        "embedding_api_key":"","legacy_credential_generation":1}


def test_disk_failure_after_new_schema_creation_keeps_partial_evidence_and_recovers(crossed,monkeypatch):
    s=crossed;plan=review(s);old,data=files(s["legacy"]),files(s["data"])
    create=cross.create_target
    def interrupted(database):
        create(database)
        raise sqlite3.OperationalError("controlled synthetic database or disk is full")
    monkeypatch.setattr(cross,"create_target",interrupted)
    with pytest.raises(sqlite3.OperationalError,match="controlled synthetic"):
        module.import_data(*inputs(s),**s["options"],expected_review_sha256=plan["review_sha256"])
    state=s["output"].parent / ("."+s["output"].name+"-imports");pointer=read_json(state / "pending.json")
    partial=state / pointer["transaction_id"] / "stage/.legacy-current-schema.sqlite3"
    assert partial.is_file() and not s["output"].exists()
    assert files(s["legacy"])==old and files(s["data"])==data
    monkeypatch.setattr(cross,"create_target",create)
    assert recover(s,plan)["state"]=="committed" and partial.is_file()


@pytest.mark.parametrize("name",["VECTORS.NPY","vectors.npz","vectors.npz.npy","VeCtOrS.JsOn"])
def test_default_and_compatibility_vector_assets_are_retained_but_never_activated(tmp_path,name):
    raw=b"Synthetic vector bytes, neither parsed nor executed";path=tmp_path / name;path.write_bytes(raw)
    record=file_record(tmp_path,path)
    result=cross.quarantine_vectors(tmp_path,{"files":[record]},"a"*32)
    assert not path.exists() and result["current_default_vector_cache_active"] is False
    assert result["new_embedding_fingerprint_created"] is False
    assert len(result["assets"])==1
    row=result["assets"][0]
    assert row["original_path"]==name and row["sha256"]==hashlib.sha256(raw).hexdigest()
    assert (tmp_path / row["dormant_path"]).read_bytes()==raw


@pytest.mark.parametrize("kind",["occupied","changed"])
def test_vector_quarantine_does_not_overwrite_archive_assets_or_accept_changed_bytes(tmp_path,kind):
    path=tmp_path / "vectors.npy";path.write_bytes(b"original synthetic vectors")
    record=file_record(tmp_path,path);manifest={"files":[record]}
    if kind=="changed":path.write_bytes(b"changed synthetic vectors")
    else:
        protected=tmp_path / ("legacy-dormant-indexes-"+"b"*32);protected.mkdir()
        asset=protected / "vectors.npy";asset.write_bytes(b"old owned bytes")
        manifest["files"].append(file_record(tmp_path,asset))
    before=files(tmp_path)
    with pytest.raises(ReleaseError):cross.quarantine_vectors(tmp_path,manifest,"b"*32)
    assert files(tmp_path)==before


@pytest.mark.parametrize("kind",["foreign_key","history_mutation","contract","report_collision","low_space","target_tamper"])
def test_cross_schema_faults_fail_closed_and_keep_originals(legacy,monkeypatch,kind):
    s=seed(legacy)
    if kind=="foreign_key":
        with closing(sqlite3.connect(s["database"])) as db:db.execute("UPDATE chunks SET paper_id='missing'");db.commit()
    if kind=="report_collision":(s["data"] / "legacy-schema-migration.json").write_text("do not overwrite old asset")
    finalize(s,monkeypatch);old,data=files(s["legacy"]),files(s["data"]);plan=review(s)
    if kind=="history_mutation":
        original=migrations.transform
        def changed(database,migration_id):
            original(database,migration_id)
            with closing(sqlite3.connect(database)) as db:db.execute("UPDATE qa_records SET answer='changed'");db.commit()
        monkeypatch.setattr(migrations,"transform",changed)
    elif kind=="contract":monkeypatch.setattr(cross,"TARGET_OBJECTS",cross.TARGET_OBJECTS+(("table","unreviewed","unreviewed","CREATE TABLE unreviewed(x)"),))
    elif kind=="low_space":monkeypatch.setattr(module.shutil,"disk_usage",lambda p:type("Disk",(),{"free":0})())
    elif kind=="target_tamper":
        run(s);(s["output"] / "pdf/original.pdf").write_bytes(b"changed output retained")
        state=s["output"].parent / ("."+s["output"].name+"-imports");pointer=read_json(state / "pending.json")
        journal=state / pointer["transaction_id"] / "journal.json";value=read_json(journal)
        value["evidence"]["files_sha256"]=digest(module._files(s["output"]));atomic_write_json(journal,value)
        changed=files(s["output"])
        with pytest.raises(ReleaseError):recover(s,plan)
        assert files(s["output"])==changed
        assert files(s["legacy"])==old and files(s["data"])==data
        return
    with pytest.raises(ReleaseError):module.import_data(*inputs(s),**s["options"],expected_review_sha256=plan["review_sha256"])
    assert not s["output"].exists() and files(s["legacy"])==old and files(s["data"])==data

