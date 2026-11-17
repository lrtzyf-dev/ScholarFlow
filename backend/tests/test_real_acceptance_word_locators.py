"""Public projection of actual synthetic fixed Word locators, not old R repair."""
from copy import deepcopy
import json
import threading

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.models import Conversation
from scripts import verify_real_acceptance as runner
from tests.complex_docx_fixture import complex_docx


@pytest.fixture
def client(db,monkeypatch):
    from app import task_runner
    monkeypatch.setattr(task_runner,"_shutting_down",threading.Event())
    db.add(Conversation(id="public-word-locators",title="Synthetic public locator projection"));db.commit()
    with TestClient(app)as value:yield value


def upload(client,name,raw):
    response=client.post("/api/sources/upload",files={"file":(name,raw)},
        data={"conversation_id":"public-word-locators","request_id":"upload-"+name.replace(".","-")})
    assert response.status_code==200,response.text
    return response.json()["revision_id"]


def locator(client,rid,**fields):
    response=client.post(f"/api/sources/revisions/{rid}/locators",
        json={"conversation_id":"public-word-locators",**fields})
    assert response.status_code==200,response.text
    return response.json()


def project(original):
    raw={"revision_id":original["revision_id"],"locator_id":original["locator_id"],
        "quote":original["quote"],"source_location":deepcopy(original["location"])}
    raw["source_location"].update(provider_context="PRIVATE-PROVIDER",agent_checkpoint="PRIVATE-CHECKPOINT",
        reasoning_content="PRIVATE-REASONING",api_key="PRIVATE-CREDENTIAL",tool_arguments={"value":"PRIVATE-ARGS"})
    value=runner.public_evidence([raw])[0]
    assert value["source_location"]==original["location"]
    assert value["revision_id"]==original["revision_id"]and value["locator_id"]==original["locator_id"]
    assert value["quote"]==original["quote"]and "PRIVATE-"not in json.dumps(value)
    return value


def test_actual_paragraph_locator_retains_bounds_base_and_extraction_hash(client):
    rid=upload(client,"protocol.docx",runner.fixture_bytes("protocol.docx"))
    original=locator(client,rid,kind="word_paragraphs",paragraph_start=2,paragraph_end=3)
    value=project(original);location=value["source_location"]
    assert location["paragraph_start"]==2 and location["paragraph_end"]==3 and location["paragraph_index_base"]==1
    assert len(location["extraction_fingerprint"])==64
    assert "Question:"in value["quote"]and "Method:"in value["quote"]
    assert "page_number"not in location and "page"not in location


def test_actual_parts_and_merged_nested_table_locators_keep_exact_production_fields(client):
    rid=upload(client,"complex.docx",complex_docx())
    response=client.get(f"/api/sources/revisions/{rid}/document",params={"conversation_id":"public-word-locators","limit":1000})
    assert response.status_code==200,response.text
    document=response.json()
    assert document["next_offset"]is None
    # A continued vertical merge is an actual addressable cell even when its
    # text is empty. Keep it in this API/projection test instead of filtering
    # out the very structural coordinate that is being checked.
    parts=[b for b in document["blocks"]if b.get("kind")=="word_part"]
    assert {"header","footer","footnote","endnote","body"}<={b["section_kind"]for b in parts}
    assert any(b.get("column_span")==2 for b in parts)
    assert any(b.get("vertical_merge")=="continue"for b in parts)
    assert any(b.get("table_depth")==1 for b in parts)
    for block in parts:
        original=locator(client,rid,kind="word_block",block_id=block["block_id"])
        value=project(original);location=value["source_location"]
        assert location["block_id"]==block["block_id"]and location["part_name"]==block["part_name"]
        assert location["xml_path"]==block["xml_path"]and location["rendered_page_available"]is False
        if "table_path"in block:
            for key in ("table_path","table_row","table_column","column_span","vertical_merge","table_depth","logical_grid"):
                assert location[key]==block[key]
        assert "page_number"not in location


def test_existing_xlsx_cell_and_pdf_geometry_fields_are_preserved(client):
    rid=upload(client,"budget.xlsx",runner.fixture_bytes("budget.xlsx"))
    original=locator(client,rid,kind="sheet_cells",sheet="Budget",cell_range="B2:B4")
    raw={"revision_id":rid,"locator_id":original["locator_id"],"source_location":original["location"]}
    location=runner.public_evidence([raw])[0]["source_location"]
    for key in ("sheet","cell_range","row_start","row_end","column_start","column_end","index_base",
                "raw_sha256","text_sha256","extraction_fingerprint"):
        assert location[key]==original["location"][key]
    geometry={"page_number":2,"page_index":1,"page_index_base":0,"page_count":7,"bbox":[5,6,20,30],
        "page_bbox":[0,0,595,842],"page_width":595,"page_height":842,"rotation":0,
        "coordinate_space":"pdf_points_top_left","unit":"pt","geometry_verified":False,
        "raw_sha256":"a"*64,"text_sha256":"b"*64}
    assert runner.public_evidence({"source_location":geometry})["source_location"]==geometry


def test_projection_never_reconstructs_coordinates_absent_from_historical_evidence():
    original={"revision_id":"historical-r","locator_id":"historical-locator","quote":"36",
        "source_location":{"raw_sha256":"a"*64,"text_sha256":"b"*64}}
    assert runner.public_evidence(original)==original
    oversized={"source_location":{"xml_path":"p"*25000,"provider_context":"PRIVATE-PROVIDER"}}
    value=runner.public_evidence(oversized)
    assert len(value["source_location"]["xml_path"])==24000 and "PRIVATE-"not in json.dumps(value)
