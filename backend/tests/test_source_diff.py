"""Version comparison retains all text; scope and corruption fail closed."""
from io import BytesIO
import random
import time
from unittest.mock import patch
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from app.main import app
from app.models import Conversation, DeletedConversation
from app.source_models import Source, SourceExtraction, SourceSnapshot
from app.source_diff import _spans, _units
from app.timeutil import utcnow


@pytest.fixture
def client(db):
    db.add_all([Conversation(id="diff-a"),Conversation(id="diff-b")]);db.commit()
    return TestClient(app)


def upload(client,raw,name="paper.md",prior=None,request="diff-first"):
    data={"conversation_id":"diff-a","request_id":request}
    if prior:data.update(source_id=prior["source_id"],expected_version=prior["version"])
    r=client.post("/api/sources/upload",data=data,files={"file":(name,raw)})
    assert r.status_code==200,r.text
    return r.json()


def diff(client,left,right,**kwargs):
    return client.get("/api/source-versions/compare",params={"conversation_id":"diff-a",
        "left_id":left["revision_id"],"right_id":right["revision_id"],**kwargs})


def test_exact_full_coverage_for_random_repeated_insert_delete_reorder():
    rng=random.Random(42)
    for _ in range(150):
        a=[rng.choice("abcd")for _ in range(rng.randrange(70))]
        b=[rng.choice("abcde")for _ in range(rng.randrange(70))]
        spans,_=_spans(a,b);x=u=0
        for kind,start,end,new_start,new_end in spans:
            assert (start,new_start)==(x,u)
            if kind=="equal":assert a[start:end]==b[new_start:new_end]
            x,u=end,new_end
        assert (x,u)==(len(a),len(b))


def test_markdown_changes_pagination_and_fixed_heads(client,db):
    old=upload(client,b"# Heading\nunchanged\nold finding\nlast line")
    new=upload(client,b"# Heading\nnew preface\nunchanged\nnew finding\nlast line",prior=old,request="diff-second")
    response=diff(client,old,new,limit=1)
    assert response.status_code==200,response.text
    value=response.json();rows=value["rows"]
    while value["next_offset"]is not None:
        value=diff(client,old,new,limit=1,offset=value["next_offset"]).json();rows+=value["rows"]
    assert "old finding"in[row["text"]for row in rows if row["status"]=="removed"]
    assert "new finding"in[row["text"]for row in rows if row["status"]=="added"]
    assert value["counts"]=={"unchanged":3,"removed":1,"added":2}
    assert [r["row"]for r in rows]==list(range(len(rows)))
    assert db.get(Source,old["source_id"]).current_revision_id==new["revision_id"]
    assert db.scalar(select(func.count()).select_from(SourceExtraction))==2
    full=diff(client,old,new,changes_only=False).json()
    assert "unchanged"in[r["status"]for r in full["rows"]]
    assert full["comparison_scope"]=="fixed_extraction_text_and_structure"
    assert not full["rendered_layout_compared"]and not full["semantic_equivalence_verified"]


def test_text_long_lines_not_truncated_or_executed(client):
    text="<script>alert('untrusted')</script>"+"汉"*10001
    old=upload(client,b"old",name="text.txt")
    new=upload(client,text.encode(),name="text.txt",prior=old,request="diff-long-line")
    value=diff(client,old,new).json()
    assert ''.join(r["text"]for r in value["rows"]if r["status"]=="added")==text
    assert max(len(r["text"])for r in value["rows"])<=2000


def test_formatting_only_raw_changes_not_claimed_identical(client):
    old=upload(client,b"same line\r\nnext\r\n")
    new=upload(client,b"same line\nnext\n",prior=old,request="diff-line-ending")
    value=diff(client,old,new).json()
    assert value["total_rows"]==0 and not value["raw_bytes_identical"]


def test_scope_both_versions_checked_before_structure_and_archive_readable(client,db):
    old=upload(client,b"old");new=upload(client,b"new",prior=old,request="diff-scope-new")
    with patch("app.source_diff.documents.read_document",side_effect=AssertionError("unauthorized parse")):
        assert diff(client,old,new,conversation_id="diff-b").status_code==404
    db.get(Conversation,"diff-a").archived_at=utcnow();db.commit()
    assert diff(client,old,new).status_code==200
    db.add(DeletedConversation(id="diff-a"));db.commit()
    assert diff(client,old,new).status_code==410


def test_different_sources_refused(client):
    a=upload(client,b"private A")
    b=upload(client,b"private B",request="diff-other-source")
    assert diff(client,a,b).status_code==422


@pytest.mark.parametrize("kind",["blob","structure"])
def test_tampered_fixed_data_refused(client,db,kind):
    a=upload(client,b"old");b=upload(client,b"new",prior=a,request="diff-corruption")
    if kind=="blob":db.execute(update(SourceSnapshot).where(SourceSnapshot.raw_sha256==a["raw_sha256"]).values(raw_bytes=b"changed"))
    else:db.execute(update(SourceExtraction).where(SourceExtraction.revision_id==a["revision_id"]).values(fingerprint="0"*64))
    db.commit()
    assert diff(client,a,b).status_code==409


@pytest.mark.parametrize("fmt",["csv","docx","xlsx","pdf"])
def test_structured_documents_compare_original_versions(client,fmt):
    def content(value):
        if fmt=="csv":return f"method,value\nA,{value}\n".encode()
        output=BytesIO()
        if fmt=="docx":
            from docx import Document
            doc=Document();doc.add_paragraph("unchanged");doc.add_paragraph(f"value {value}");doc.save(output)
        elif fmt=="xlsx":
            from openpyxl import Workbook
            wb=Workbook();wb.active.title="Trial";wb.active.append(["method","value"]);wb.active.append(["A",value]);wb.save(output);wb.close()
        else:
            from reportlab.pdfgen import canvas
            pdf=canvas.Canvas(output);pdf.drawString(50,750,f"value {value}");pdf.save()
        return output.getvalue()
    old=upload(client,content(12),name=f"source.{fmt}")
    new=upload(client,content(15),name=f"source.{fmt}",prior=old,request="diff-structured-new")
    response=diff(client,old,new)
    assert response.status_code==200,response.text
    value=response.json()
    assert any("12"in r["text"]for r in value["rows"]if r["status"]=="removed")
    assert any("15"in r["text"]for r in value["rows"]if r["status"]=="added")
    assert value["left"]["revision_id"]==old["revision_id"]and value["right"]["revision_id"]==new["revision_id"]


def test_repeated_large_regions_use_bounded_coarse_replacement():
    a=["a","b"]*50000;b=["b","a"]*50000
    start=time.monotonic();spans,coarse=_spans(a,b)
    assert coarse and spans==[("replace",0,100000,0,100000)]
    assert time.monotonic()-start<5


@pytest.mark.parametrize("text", ["\r"*12000, "\n"*12000,
                                  "\r\n"*6000, "\n"*2_000_000])
def test_newline_heavy_fixed_block_is_packed_without_text_loss(text):
    db=Mock()
    document={"format":"pdf","blocks":[{"text":text,"page_index":0}]}
    with patch("app.source_diff.documents.read_document",return_value=document):
        units,_=_units(db,{"revision_id":"fixed"},"owner")
    actual=[unit for unit in units if unit.kind=="text"]
    assert ''.join(unit.text for unit in actual)==text
    assert len(actual)<=(len(text)+1999)//2000
    assert all(len(unit.text)<=2000 for unit in actual)
