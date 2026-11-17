"""Independent attacks on saved native Word chart packages and their graph."""
from copy import deepcopy
import io
import zipfile
from xml.etree import ElementTree as ET
from uuid import uuid4

import pytest

from app import artifact_export_formats as formats
from app import artifact_store as store


PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
EMBEDDED_XLSX = "word/embeddings/ScholarFlow-chart-data.xlsx"


def source(db):
    return store.create(db, request_id=uuid4().hex, title="Relationship probe", kind="analysis",
        content="group,value\nA,1\nB,2\n", evidence=[],
        analysis={"category_column": "group", "numeric_columns": ["value"],
            "chart_type": "bar", "units": {"value": "points"}})


def rewrite(payload, mutate):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    mutate(members)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return output.getvalue()


@pytest.fixture
def exported(db):
    item = source(db)
    row = store.get(db, item["id"])
    revision = store.revision(db, row)
    payload, generation = formats.render(db, row, revision, row.title, "docx")
    assert formats.inspect(db, row, revision, row.title, "docx", payload, generation)
    return row, revision, payload, generation


def test_unknown_root_package_relationship_is_rejected(db, exported):
    row, revision, payload, generation = exported

    def inject(members):
        root = ET.fromstring(members["_rels/.rels"])
        root.append(ET.Element("{" + PKG + "}Relationship", {
            "Id": "rIdIndependentExtra", "Type": "urn:untrusted:package-part", "Target": EMBEDDED_XLSX}))
        members["_rels/.rels"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    damaged = rewrite(payload, inject)
    forged = deepcopy(generation)
    try:
        forged["descriptor"] = formats._docx(damaged, analysis_chart=True)
    except ValueError:
        return
    with pytest.raises(ValueError):
        formats.inspect(db, row, revision, row.title, "docx", damaged, forged)


def test_duplicate_root_office_document_relationship_is_rejected(exported):
    row, revision, payload, generation = exported

    def inject(members):
        root = ET.fromstring(members["_rels/.rels"])
        root.append(ET.Element("{" + PKG + "}Relationship", {
            "Id": "rIdIndependentSecondOfficeDocument", "Type": OFFICE_DOCUMENT,
            "Target": EMBEDDED_XLSX}))
        members["_rels/.rels"] = ET.tostring(root, encoding="utf-8", xml_declaration=True)

    damaged = rewrite(payload, inject)
    forged = deepcopy(generation)
    try:
        forged["descriptor"] = formats._docx(damaged, analysis_chart=True)
    except ValueError:
        return
    with pytest.raises(ValueError):
        formats.inspect(db, row, revision, row.title, "docx", damaged, forged)
