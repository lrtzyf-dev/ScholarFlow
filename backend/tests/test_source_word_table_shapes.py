"""Raw retained DOCX shape proofs; fabricated local data, no model calls."""
from copy import deepcopy
from io import BytesIO
import hashlib
import zipfile
from xml.etree import ElementTree as ET

import pytest
from docx import Document
from docx.shared import Inches
from sqlalchemy import update

from app import source_store as store
from app.docx_extraction import W, P, R
from app.source_word_table_shapes import verified_word_table_shape, SHAPE_KIND
from app.source_parsing import extract, MEDIA
from app.source_documents import save_extraction, add_document_locator, read_document
from app.source_models import SourceExtraction, SourceSnapshot


SCOPE = store.SourceScope("conversation", "word-shape-tests")
PATH = "/document/body/tbl[1]"


def raw_document(rows=2, columns=2):
    doc = Document()
    table = doc.add_table(rows=rows, cols=columns)
    table.cell(0, 0).text = "PUBLIC_FIXTURE_WORD_SHAPE"
    output = BytesIO(); doc.save(output)
    return output.getvalue()


def rewrite(raw, handler):
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    handler(parts)
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    return output.getvalue()


def modify_table(raw, handler):
    def change(parts):
        root = ET.fromstring(parts["word/document.xml"])
        handler(root.find(W + "body/" + W + "tbl"))
        parts["word/document.xml"] = ET.tostring(root)
    return rewrite(raw, change)


def capture(db, raw=None, **kwargs):
    return store.capture_source(db, scope=SCOPE,
        authorization=store.SourceAuthorization(SCOPE, "fabricated-shape-capture", "upload"),
        kind="upload", title="shape.docx", raw_bytes=raw or raw_document(),
        media_type=MEDIA["docx"], **kwargs)


def shape(db, item, *, part="word/document.xml", path=PATH, scope=SCOPE):
    return verified_word_table_shape(db, item["revision_id"], scope=scope,
        part_name=part, table_path=path)


def assert_unknown(value):
    assert value["complete"] is False and value["shape_kind"] == "unknown"
    assert "row_count" not in value and "column_count" not in value
    assert value["reason"] and value["authorizes_actions"] is False


def test_complete_shape_counts_empty_trailing_rows_and_columns_from_original_grid(db):
    raw = raw_document(3, 4)
    item = capture(db, raw)
    value = shape(db, item)
    assert value["complete"] and value["shape_kind"] == SHAPE_KIND
    assert (value["row_count"], value["column_count"]) == (3, 4)
    assert value["revision_id"] == item["revision_id"]
    assert value["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert value["part_name"] == "word/document.xml" and value["table_path"] == PATH
    assert "PUBLIC_FIXTURE" not in str(value) and "text" not in value


def test_fixed_revision_and_scope_remain_authoritative_after_source_update(db):
    old = capture(db, raw_document(3, 2))
    new = capture(db, raw_document(1, 4), source_id=old["source_id"], expected_version=1)
    assert shape(db, old)["row_count"] == 3
    assert shape(db, new)["row_count"] == 1
    assert shape(db, new)["column_count"] == 4
    with pytest.raises(store.SourceAccessDenied):
        shape(db, old, scope=store.SourceScope("conversation", "another-scope"))


@pytest.mark.parametrize("kind", ["horizontal", "vertical", "legacy_horizontal"])
def test_merged_cells_never_certify_a_simple_rectangular_shape(db, kind):
    doc = Document(); table = doc.add_table(rows=2, cols=2)
    if kind == "horizontal":
        table.cell(0, 0).merge(table.cell(0, 1))
    elif kind == "vertical":
        table.cell(0, 0).merge(table.cell(1, 0))
    output = BytesIO(); doc.save(output); raw = output.getvalue()
    if kind == "legacy_horizontal":
        raw = modify_table(raw, lambda t: ET.SubElement(t.find(W + "tr/" + W + "tc/" + W + "tcPr"), W + "hMerge"))
    assert_unknown(shape(db, capture(db, raw)))


@pytest.mark.parametrize("kind", ["missing_grid", "irregular_row", "grid_before", "grid_after",
    "bad_span", "duplicate_span", "wrapped_row", "wrapped_cell"])
def test_omissions_invalid_spans_and_wrappers_are_unknown_without_dimensions(db, kind):
    def mutate(table):
        row = table.find(W + "tr");cell = row.find(W + "tc")
        if kind == "missing_grid":
            table.remove(table.find(W + "tblGrid"))
        elif kind == "irregular_row":
            row.remove(cell)
        elif kind in ("grid_before", "grid_after"):
            props = ET.Element(W + "trPr");row.insert(0, props)
            ET.SubElement(props, W + ("gridBefore" if kind == "grid_before" else "gridAfter"), {W + "val": "1"})
        elif kind in ("bad_span", "duplicate_span"):
            props = cell.find(W + "tcPr")
            ET.SubElement(props, W + "gridSpan", {W + "val": "0" if kind == "bad_span" else "1"})
            if kind == "duplicate_span":
                ET.SubElement(props, W + "gridSpan", {W + "val": "1"})
        elif kind == "wrapped_row":
            table.remove(row);wrapper = ET.SubElement(table, W + "sdt")
            ET.SubElement(wrapper, W + "sdtContent").append(row)
        else:
            row.remove(cell);ET.SubElement(row, W + "customXml").append(cell)
    raw = modify_table(raw_document(), mutate)
    assert_unknown(shape(db, capture(db, raw)))


def test_explicit_zero_grid_offsets_and_unit_span_are_not_actual_merges(db):
    def mutate(table):
        for row in table.findall(W + "tr"):
            props = ET.Element(W + "trPr");row.insert(0, props)
            for name in ("gridBefore", "gridAfter"):
                ET.SubElement(props, W + name, {W + "val": "0"})
            for cell in row.findall(W + "tc"):
                ET.SubElement(cell.find(W + "tcPr"), W + "gridSpan", {W + "val": "1"})
    value = shape(db, capture(db, modify_table(raw_document(), mutate)))
    assert value["complete"] and (value["row_count"], value["column_count"]) == (2, 2)


def test_hidden_direct_row_cannot_be_certified_as_ordinary_complete_rectangle(db):
    def mutate(table):
        row=table.findall(W+"tr")[1]
        props=ET.Element(W+"trPr");row.insert(0,props);ET.SubElement(props,W+"hidden")
    value=shape(db,capture(db,modify_table(raw_document(3,2),mutate)))
    assert_unknown(value)
    assert value['reason']=='hidden_row_structure'


def test_nested_table_inherits_hidden_ancestor_but_not_parent_merge(db):
    doc = Document(); outer = doc.add_table(rows=2, cols=2)
    outer.cell(0, 0).merge(outer.cell(0, 1)).add_table(rows=1, cols=4)
    output = BytesIO(); doc.save(output); raw = output.getvalue()
    nested_path = PATH + "/tr[1]/tc[1]/tbl[1]"
    normal = shape(db, capture(db, raw), path=nested_path)
    assert normal['complete'] and (normal['row_count'], normal['column_count']) == (1, 4)
    def hide(table):
        row = table.find(W + 'tr'); props = ET.Element(W + 'trPr'); row.insert(0, props)
        ET.SubElement(props, W + 'hidden')
    hidden = shape(db, capture(db, modify_table(raw, hide)), path=nested_path)
    assert_unknown(hidden)
    assert hidden['reason'] == 'wrapped_table_structure'


def test_nested_and_sibling_tables_have_separate_exact_paths(db):
    doc = Document();outer = doc.add_table(rows=2, cols=2)
    outer.cell(0, 0).add_table(rows=1, cols=3)
    doc.add_table(rows=4, cols=1)
    output = BytesIO();doc.save(output);item = capture(db, output.getvalue())
    outer_shape = shape(db, item)
    nested = shape(db, item, path=PATH + "/tr[1]/tc[1]/tbl[1]")
    sibling = shape(db, item, path="/document/body/tbl[2]")
    assert (outer_shape["row_count"], outer_shape["column_count"]) == (2, 2)
    assert (nested["row_count"], nested["column_count"]) == (1, 3)
    assert (sibling["row_count"], sibling["column_count"]) == (4, 1)
    assert_unknown(shape(db, item, path=PATH + "/tr[1]/tc[2]/tbl[1]"))


def test_header_and_referenced_footnote_tables_do_not_borrow_body_shape(db):
    doc = Document();doc.add_table(rows=2, cols=2)
    doc.sections[0].header.add_table(rows=3, cols=1, width=Inches(3))
    p=doc.add_paragraph("Note reference")
    output=BytesIO();doc.save(output)
    def change(parts):
        root=ET.fromstring(parts["word/document.xml"])
        paragraph=root.find(W+"body/"+W+"p")
        run=ET.SubElement(paragraph,W+"r");ET.SubElement(run,W+"footnoteReference",{W+"id":"7"})
        parts["word/document.xml"]=ET.tostring(root)
        notes=ET.Element(W+"footnotes");note=ET.SubElement(notes,W+"footnote",{W+"id":"7"})
        table=ET.SubElement(note,W+"tbl");grid=ET.SubElement(table,W+"tblGrid")
        for _ in range(4):ET.SubElement(grid,W+"gridCol")
        row=ET.SubElement(table,W+"tr")
        for _ in range(4):ET.SubElement(ET.SubElement(row,W+"tc"),W+"p")
        parts["word/footnotes.xml"]=ET.tostring(notes)
        rels=ET.fromstring(parts["word/_rels/document.xml.rels"])
        ET.SubElement(rels,P+"Relationship",{"Id":"fixtureFootnote","Type":"http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes","Target":"footnotes.xml"})
        parts["word/_rels/document.xml.rels"]=ET.tostring(rels)
    item=capture(db,rewrite(output.getvalue(),change))
    header=shape(db,item,part="word/header1.xml",path="/header/tbl[1]")
    note=shape(db,item,part="word/footnotes.xml",path="/footnotes/footnote[@id=7]/tbl[1]")
    assert (header["row_count"],header["column_count"])==(3,1)
    assert (note["row_count"],note["column_count"])==(1,4)
    assert_unknown(shape(db,item,part="word/header1.xml",path=PATH))
    assert_unknown(shape(db,item,part="word/footnotes.xml",path="/footnotes/footnote[@id=99]/tbl[1]"))


def test_table_behind_supported_extraction_control_wrapper_is_still_unknown(db):
    def change(parts):
        root=ET.fromstring(parts["word/document.xml"]);body=root.find(W+"body");table=body.find(W+"tbl")
        body.remove(table);wrapper=ET.Element(W+"sdt");body.insert(0,wrapper)
        ET.SubElement(wrapper,W+"sdtContent").append(table);parts["word/document.xml"]=ET.tostring(root)
    item=capture(db,rewrite(raw_document(),change))
    assert_unknown(shape(db,item,path="/document/body/sdt[1]/sdtContent/tbl[1]"))


def test_unreferenced_or_external_parts_are_not_read_or_returned(db,monkeypatch):
    def change(parts):
        parts["word/unusedHeader.xml"]=f'<w:hdr xmlns:w="{W[1:-1]}"><w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc><w:p/></w:tc></w:tr></w:tbl></w:hdr>'.encode()
        root=ET.fromstring(parts["word/document.xml"])
        ET.SubElement(root.find(W+"body/"+W+"sectPr"),W+"headerReference",{R+"id":"externalHeader"})
        parts["word/document.xml"]=ET.tostring(root)
        rels=ET.fromstring(parts["word/_rels/document.xml.rels"])
        ET.SubElement(rels,P+"Relationship",{"Id":"externalHeader","Type":"http://schemas.openxmlformats.org/officeDocument/2006/relationships/header","Target":"https://must-not-fetch.invalid/header.xml","TargetMode":"External"})
        parts["word/_rels/document.xml.rels"]=ET.tostring(rels)
        parts["word/media/pixels.png"]=b"pixels must never be read"
    item=capture(db,rewrite(raw_document(),change));reads=[];original=zipfile.ZipFile.read
    def observed(self,name,*args,**kwargs):
        reads.append(name);assert not str(name).endswith('.png')
        return original(self,name,*args,**kwargs)
    monkeypatch.setattr(zipfile.ZipFile,"read",observed)
    assert_unknown(shape(db,item,part="word/unusedHeader.xml",path="/header/tbl[1]"))
    assert_unknown(shape(db,item,part="https://must-not-fetch.invalid/header.xml",path="/header/tbl[1]"))
    assert shape(db,item)["complete"]


def test_old_and_tampered_extraction_cache_cannot_supply_shape_or_change_locators(db):
    raw=raw_document(3,2);extraction=extract(raw,MEDIA['docx'])
    item=capture(db,raw,text=extraction.text,text_origin='parsed_text',parser=extraction.parser)
    saved=save_extraction(db,item['revision_id'],scope=SCOPE,extraction=extraction)
    block=next(b for b in saved['blocks'] if b.get('table_path'))
    locator=add_document_locator(db,item['revision_id'],scope=SCOPE,kind='word_block',block_id=block['block_id'])
    old_location=deepcopy(locator['location']);old_fingerprint=locator['fingerprint']
    row=db.get(SourceExtraction,item['revision_id']);before=row.fingerprint
    value=shape(db,item)
    assert value['row_count']==3 and row.fingerprint==before
    tampered=deepcopy(row.document);tampered['word_structure']['table_shape']={'complete':True,'row_count':999,'column_count':999}
    # ORM mutation is correctly forbidden. Simulate corruption below that
    # guard, as a damaged/restored database would present it to readers.
    db.execute(update(SourceExtraction).where(SourceExtraction.revision_id==item['revision_id'])
        .values(document=tampered));db.expire_all()
    with pytest.raises(store.SourceCorruption):read_document(db,item['revision_id'],scope=SCOPE)
    assert shape(db,item)['row_count']==3
    located=store.read_locator(db,locator['locator_id'],scope=SCOPE)
    assert located['location']==old_location and located['fingerprint']==old_fingerprint
    assert row.document==tampered and row.fingerprint==before


def test_raw_snapshot_corruption_raises_and_never_falls_back_to_extraction(db):
    item=capture(db)
    db.execute(update(SourceSnapshot).where(SourceSnapshot.id==item['snapshot_id'])
        .values(raw_bytes=b"corrupted original"));db.expire_all()
    with pytest.raises(store.SourceCorruption):shape(db,item)


def test_dtd_and_entity_declarations_are_unknown_never_expanded(db):
    def change(parts):
        parts['word/document.xml']=b'<!DOCTYPE document [<!ENTITY x "must not expand">]><document>&x;</document>'
    value=shape(db,capture(db,rewrite(raw_document(),change)))
    assert_unknown(value)
    assert 'must not expand' not in str(value)
