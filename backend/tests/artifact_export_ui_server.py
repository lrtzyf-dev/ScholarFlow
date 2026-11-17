"""Isolated real UI fixture and independent byte readers; no provider dispatch."""
import hashlib
import io
import json
import sys
import zipfile
import xml.etree.ElementTree as ET

from fastapi import HTTPException
from app.main import app
from app.db import SessionLocal
from app.models import Conversation, QARecord
from app.runtime_models import RuntimeRun
from app.artifact_models import ArtifactExport, ArtifactManifest, ArtifactRevision, ArtifactChange, ArtifactComputation
from app import artifact_store as store, goal_ledger

_route_start = len(app.router.routes)
CID = "artifact-export-ui-isolated"


@app.post("/__test__/artifact-export-seed")
def seed():
    with SessionLocal() as db:
        if db.get(Conversation, CID):
            raise HTTPException(409, "UI fixture already seeded; start a new isolated server")
        db.add(Conversation(id=CID, title="固定导出浏览器验收"))
        db.flush()
        goal = goal_ledger.ensure_run_goal(db, "artifact-export-ui-run", CID, "检查固定成果的界面交互")
        db.add(RuntimeRun(id="artifact-export-ui-run", kind="foreground", conversation_id=CID, goal_id=goal["id"], status="ended"))
        db.add(QARecord(id="artifact-export-ui-qa", conversation_id=CID, question="检查固定成果的界面交互",
            answer="本地合成界面验收记录。", runtime_config={"run_id": "artifact-export-ui-run"}))
        db.commit()
        first = store.create(db, request_id="ui-document-create", title="浏览器固定文档", kind="document",
            content="# 第一版\n固定内容甲。\n\n| 项目 | 数值 |\n| --- | --- |\n| A | 3 |\n", evidence=[], analysis={}, conversation_id=CID)
        row = store.get(db, first["id"], CID)
        change = store.propose(db, row, request_id="ui-document-second", base_revision_id=first["current_revision_id"],
            content="# 第二版\n固定内容乙。\n\n| 项目 | 数值 |\n| --- | --- |\n| B | 7 |\n")
        store.decide(db, row, db.get(ArtifactChange, change["id"]), accept=True, expected_revision_id=first["current_revision_id"])
        second_revision = row.current_revision_id
        other = store.create(db, request_id="ui-other-create", title="切换目标文档", kind="document", content="# 其他成果\n当前目标正文。",
            evidence=[], analysis={}, conversation_id=CID)
        content = "category,A,B,C\n=1+1,1,2,3\n+literal,,4,6\n@literal,3,,9\n"
        analysis = store.create(db, request_id="ui-analysis-create", title="缺值与文字安全分析", kind="analysis", content=content,
            evidence=[], analysis={"category_column": "category", "numeric_columns": ["A", "B", "C"],
                "chart_type": "line", "units": {"A": "units", "B": "units", "C": "units"}, "missing_policy": "gap"}, conversation_id=CID)
        return {"conversation_id": CID, "goal_id": goal["id"], "document": {"id": first["id"], "v1": first["current_revision_id"], "v2": second_revision},
                "other": {"id": other["id"], "revision": other["current_revision_id"]},
                "analysis": {"id": analysis["id"], "revision": analysis["current_revision_id"]}}


@app.get("/__test__/artifact-export-observe")
def observe():
    with SessionLocal() as db:
        return {"exports": [{"id": row.id, "artifact_id": row.artifact_id, "revision_id": row.revision_id, "format": row.format,
                    "sha256": hashlib.sha256(row.payload).hexdigest(), "size": len(row.payload)}
                for row in db.query(ArtifactExport).order_by(ArtifactExport.id)],
                "artifacts": [{"id": row.id, "current_revision_id": row.current_revision_id, "version": row.version}
                    for row in db.query(ArtifactManifest).order_by(ArtifactManifest.id)],
                "revisions": [{"id": row.id, "content_sha256": row.content_sha256, "manifest_sha256": row.manifest_sha256}
                    for row in db.query(ArtifactRevision).order_by(ArtifactRevision.id)],
                "changes": [{"id": row.id, "status": row.status} for row in db.query(ArtifactChange).order_by(ArtifactChange.id)],
                "computations": [{"id": row.id, "status": row.status} for row in db.query(ArtifactComputation).order_by(ArtifactComputation.id)]}


@app.get("/__test__/artifact-export-inspect/{export_id}")
def inspect_export(export_id: str):
    with SessionLocal() as db:
        row = db.get(ArtifactExport, export_id)
        if row is None:
            raise HTTPException(404, "No test export")
        result = {"sha256": hashlib.sha256(row.payload).hexdigest(), "format": row.format, "size": len(row.payload)}
        if row.format == "docx":
            from docx import Document
            doc = Document(io.BytesIO(row.payload))
            result.update(paragraphs=[p.text for p in doc.paragraphs], tables=[[[cell.text for cell in r.cells] for r in t.rows] for t in doc.tables])
        elif row.format == "xlsx":
            from openpyxl import load_workbook
            workbook = load_workbook(io.BytesIO(row.payload), data_only=False, read_only=False, keep_links=False)
            try:
                result.update(sheets=workbook.sheetnames,
                    raw=[[{"value": c.value, "type": c.data_type} for c in line] for line in workbook["RawData"]],
                    numeric=[[c.value for c in line] for line in workbook["NumericData"]],
                    statistics=[[c.value for c in line] for line in workbook["Statistics"]],
                    formula_count=sum(c.data_type == "f" for sheet in workbook for line in sheet for c in line),
                    native_chart_count=sum(len(sheet._charts) for sheet in workbook))
            finally:
                workbook.close()
        elif row.format == "svg":
            element = ET.fromstring(row.payload)
            result.update(root=element.tag, forbidden_elements=[e.tag for e in element.iter() if e.tag.rsplit("}", 1)[-1] in ("script", "foreignObject")])
        elif row.format == "bundle":
            with zipfile.ZipFile(io.BytesIO(row.payload)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                result.update(crc_error=archive.testzip(), members=sorted(archive.namelist()), manifest=manifest,
                    data=archive.read("data.csv").decode("utf-8"), calculation=json.loads(archive.read("result.json")),
                    file_hashes={name: hashlib.sha256(archive.read(name)).hexdigest() for name in manifest["files"]})
        return result


_test_routes = app.router.routes[_route_start:]
del app.router.routes[_route_start:]
app.router.routes[0:0] = _test_routes

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
