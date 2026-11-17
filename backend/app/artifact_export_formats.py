"""Bounded generated containers and read-only checks of saved export bytes.

This module does not certify scientific semantics, Office rendering or formulas.
Only the application-owned analysis program is used while creating an export;
reading an export never executes a program or writes a computation record.
"""
from __future__ import annotations

import csv
import io
import json
import math
import zipfile
from xml.etree import ElementTree as ET

from fastapi import HTTPException

from . import artifact_store as store
from .artifact_compute import SCRIPT, bundle, execute, validate_analysis
from .artifact_models import ArtifactComputation

MAX_BYTES = 16 * 1024 * 1024
MAX_ZIP_EXPANDED = 64 * 1024 * 1024
FORMATS = {
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
    "svg": ("image/svg+xml", "svg"),
    "bundle": ("application/zip", "zip"),
    "md": ("text/markdown; charset=utf-8", "md"),
    "manifest": ("application/json", "json"),
}


def sources(rev):
    return [{"id": x.get("id"), "title": x.get("source_title") or x.get("paper_title") or "来源",
        "url": x.get("url", ""), "revision_id": x.get("revision_id", ""),
        "source_id": x.get("source_id", ""), "source_fingerprint": x.get("source_fingerprint", ""),
        "location": x.get("location", {}), "locator_id": x.get("locator_id", ""),
        "quote": x.get("quote", ""), "verification": x.get("verification", "unverified")}
        for x in rev.evidence]


def manifest(row, rev, title):
    return {"artifact_id": row.id, "revision_id": rev.id, "version": rev.version,
        "kind": row.kind, "title": title, "content": rev.content,
        "content_sha256": rev.content_sha256, "manifest_sha256": rev.manifest_sha256,
        "evidence": rev.evidence, "analysis": rev.analysis}


def _json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def _zip(payload, required):
    archive = zipfile.ZipFile(io.BytesIO(payload))
    names = archive.namelist()
    if (len(names) != len(set(names)) or len(names) > 1000 or not set(required) <= set(names)
            or any(n.startswith(("/", "\\")) or ".." in n.replace("\\", "/").split("/")
                   or "\\" in n for n in names)
            or sum(z.file_size for z in archive.infolist()) > MAX_ZIP_EXPANDED
            or any(z.flag_bits & 1 for z in archive.infolist()) or archive.testzip() is not None):
        archive.close()
        raise ValueError("invalid or oversized container")
    return archive


def _svg(payload):
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("SVG entities are not allowed")
    root = ET.fromstring(payload)
    namespace = "{http://www.w3.org/2000/svg}"
    allowed = {"svg", "rect", "text", "line", "polygon", "title", "circle", "polyline"}
    if root.tag != namespace + "svg":
        raise ValueError("not SVG")
    count = 0
    for element in root.iter():
        count += 1
        if count > 200000 or element.tag not in {namespace + name for name in allowed}:
            raise ValueError("unsupported SVG element")
        for key, value in element.attrib.items():
            if key.lower().startswith("on") or "href" in key.lower() or "url(" in value.lower():
                raise ValueError("active or external SVG resource")
    return {"elements": count}


def _docx(payload, *, analysis_chart=False):
    from docx import Document
    with _zip(payload, {"[Content_Types].xml", "word/document.xml", "_rels/.rels"}) as archive:
        if any("vbaproject" in name.lower() or ("/embeddings/" in name
                and not (analysis_chart and name == "word/embeddings/ScholarFlow-chart-data.xlsx"))
                for name in archive.namelist()):
            raise ValueError("active Office package")
        xml = archive.read("word/document.xml")
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise ValueError("document entities")
        root = ET.fromstring(xml)
        w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        descriptor = {"document_xml_sha256": store.digest(xml),
            "paragraph_count": len(root.findall(".//" + w + "p")),
            "table_count": len(root.findall(".//" + w + "tbl")),
            "text_sha256": store.digest("".join(root.itertext()))}
    doc = Document(io.BytesIO(payload))
    if not doc.paragraphs:
        raise ValueError("empty document")
    return descriptor


def _analysis_values(rev):
    """Read fixed input and independently derive the exported numeric cells."""
    config = rev.analysis
    rows = list(csv.reader(io.StringIO(rev.content)))
    headers = rows[0]
    numeric = config["numeric_columns"]
    category = headers.index(config["category_column"])
    parsed, stats = [], []
    for raw in rows[1:]:
        parsed.append([raw[category], *[None if not raw[headers.index(name)].strip()
            else float(raw[headers.index(name)]) for name in numeric]])
    for index, name in enumerate(numeric, 1):
        values = [row[index] for row in parsed if row[index] is not None]
        if not values or any(not math.isfinite(value) for value in values):
            raise ValueError("nonfinite analysis")
        stats.append([name, len(values), len(parsed) - len(values), min(values),
            math.fsum(value / len(values) for value in values), max(values),
            config.get("units", {}).get(name, "unspecified")])
    return rows, [[config["category_column"], *numeric], *parsed], [
        ["column", "count", "missing", "min", "mean", "max", "unit"], *stats]


def _xlsx_rows(row, rev, title, computation, warnings, *, word_chart=False):
    raw, numeric, stats = _analysis_values(rev)
    details = {"artifact_id": row.id, "revision_id": rev.id, "version": rev.version,
        "title": title, "content_sha256": rev.content_sha256, "manifest_sha256": rev.manifest_sha256,
        "data_sha256": store.digest(rev.content), "script_sha256": store.digest(SCRIPT),
        "analysis": rev.analysis, "evidence": rev.evidence,
        "recorded_execution": bool(computation), "computation_id": computation["id"] if computation else None,
        "result_sha256": computation["result_sha256"] if computation else None,
        "missing_policy": "gap", "preview_row_limit": 30 if rev.analysis["chart_type"] == "radar" else 200,
        "method": "Descriptive finite numeric statistics; no inference, imputation or causal analysis",
        "warnings": warnings, "native_editable_chart": word_chart, "native_open": "not_assessed",
        "formula_recalculation": "not_assessed", "semantic_verification": "not_assessed"}
    return {"RawData": raw, "NumericData": numeric, "Statistics": stats,
        "Provenance": [["key", "value"], *[[key, store.canonical(value)] for key, value in details.items()]]}


def _write_xlsx(sheets):
    from openpyxl import Workbook
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for ri, values in enumerate(rows, 1):
            for ci, value in enumerate(values, 1):
                cell = sheet.cell(ri, ci, value)
                if isinstance(value, str):
                    if len(value) > 32767:
                        raise ValueError("XLSX text cell exceeds 32767 characters")
                    # Explicitly override openpyxl's =formula and #error inference.
                    cell.data_type = "s"
        sheet.freeze_panes = "A2"
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _read_xlsx(payload, expected):
    from openpyxl import load_workbook
    with _zip(payload, {"[Content_Types].xml", "xl/workbook.xml"}) as archive:
        if any(any(part in n for part in ("vbaProject", "externalLinks/", "charts/", "connections.xml",
                "queryTables/", "embeddings/", "oleObjects/")) for n in archive.namelist()):
            raise ValueError("unsupported XLSX active/link/chart part")
        for name in archive.namelist():
            if name.endswith('.rels'):
                xml = archive.read(name)
                if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                    raise ValueError('workbook relationship entities')
                if any(node.get('TargetMode') == 'External' for node in ET.fromstring(xml)):
                    raise ValueError('external workbook relationship')
    workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=False, keep_links=False)
    try:
        if workbook.sheetnames != list(expected):
            raise ValueError("worksheet identity mismatch")
        for name, rows in expected.items():
            sheet = workbook[name]
            if sheet.max_row != len(rows) or sheet.max_column != max(map(len, rows)):
                raise ValueError("worksheet dimensions mismatch")
            actual = sheet.iter_rows()
            for want in rows:
                got = next(actual)
                for cell, value in zip(got, want):
                    # An empty CSV field is a literal empty cell, never zero.
                    equal = cell.value in (None, "") if value == "" else cell.value == value
                    if isinstance(value, float) and isinstance(cell.value, (int, float)):
                        equal = math.isclose(cell.value, value, rel_tol=1e-14, abs_tol=1e-14)
                    if type(value) in (int, float) and (type(cell.value) not in (int, float) or cell.data_type != "n"):
                        equal = False
                    if not equal or cell.data_type == "f" or (isinstance(value, str) and value
                            and cell.data_type not in ("s", "inlineStr")):
                        raise ValueError("worksheet cell mismatch")
        return {"sheets": list(expected), "native_editable_chart": False,
                "formula_recalculation": "not_assessed"}
    finally:
        workbook.close()


def _computation(db, row, rev, result):
    result_hash = store.digest(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
    record = db.query(ArtifactComputation).filter_by(artifact_id=row.id, revision_id=rev.id,
        status="succeeded", script_sha256=store.digest(SCRIPT)).order_by(ArtifactComputation.created_at.desc()).first()
    if (record and record.result.get("result_sha256") == result_hash
            and record.result.get("data_sha256") == rev.content_sha256
            and record.result.get("stats") == result["stats"] and record.result.get("row_count") == result["row_count"]
            and record.result.get("warnings") == result["warnings"]
            and record.result.get("execution_engine") == "trusted_exported_python_script"):
        return {"id": record.id, "result_sha256": result_hash, "script_sha256": record.script_sha256,
            "data_sha256": record.result["data_sha256"], "receipt_sha256": store.digest(store.canonical(record.result))}
    return None


def _saved_computation(db, row, rev, saved):
    if saved is None:
        return None
    if not isinstance(saved, dict) or set(saved) != {"id", "result_sha256", "script_sha256", "data_sha256", "receipt_sha256"}:
        raise ValueError("invalid computation binding")
    record = db.get(ArtifactComputation, saved["id"], populate_existing=True)
    if (record is None or record.artifact_id != row.id or record.revision_id != rev.id
            or record.status != "succeeded" or record.script_sha256 != saved["script_sha256"]
            or saved["script_sha256"] != store.digest(SCRIPT) or saved["data_sha256"] != rev.content_sha256
            or record.result.get("data_sha256") != saved["data_sha256"]
            or record.result.get("result_sha256") != saved["result_sha256"]
            or store.digest(store.canonical(record.result)) != saved["receipt_sha256"]):
        raise ValueError("computation receipt no longer valid")
    return saved


def render(db, row, rev, title, format):
    """Called exactly on the creating POST; never on retrieval/revalidation."""
    from .exporter import build_docx, build_markdown
    computation, warnings, descriptor = None, [], {}
    result = None
    if format in ("xlsx", "svg", "bundle") or (format == "docx" and row.kind == "analysis"):
        if row.kind != "analysis":
            raise HTTPException(422, "此格式要求已保存的 CSV 数据分析成果")
        validate_analysis(rev.content, rev.analysis)
        result = execute(rev.content, rev.analysis)
        warnings = list(result["warnings"])
        computation = _computation(db, row, rev, result)
    if format == "docx":
        if row.kind == "analysis":
            from .word_charts import build_analysis_docx
            raw, numeric, statistics = _analysis_values(rev)
            payload = build_analysis_docx(row, rev, title, raw, numeric, statistics,
                _xlsx_rows(row, rev, title, computation, warnings, word_chart=True), sources(rev))
            descriptor = _docx(payload, analysis_chart=True)
        elif row.kind == "document":
            payload = build_docx(title, rev.content, sources(rev))
            descriptor = _docx(payload)
            warnings = ["Images remain textual placeholders; unsupported formulas retain original source.",
                        "Native Office open/edit/render and scientific semantics have not been assessed."]
        else:
            raise HTTPException(422, "Word 导出要求文档或数据分析成果")
    elif format == "xlsx":
        payload = _write_xlsx(_xlsx_rows(row, rev, title, computation, warnings))
    elif format == "svg":
        payload = result["svg"].encode("utf-8")
        descriptor = {"svg_sha256": store.digest(payload), "result_sha256": store.digest(
            json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))}
    elif format == "bundle":
        payload = bundle(db, row, rev)
        descriptor = {"result_sha256": store.digest(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))}
    elif format == "md":
        payload = build_markdown(title, rev.content, sources(rev)).encode("utf-8")
    else:
        payload = store.canonical(manifest(row, rev, title)).encode("utf-8")
    return payload, {"descriptor": descriptor, "computation": computation, "warnings": warnings}


def inspect(db, artifact, rev, title, format, payload, generation):
    """Read saved bytes and fixed inputs without rendering or executing code."""
    from .exporter import build_markdown
    from .artifact_export_content_checks import analysis_expected, verify_docx, verify_svg
    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_BYTES:
        raise ValueError("导出文件须为不超过16 MiB的非空字节")
    if not isinstance(generation, dict) or set(generation) != {"descriptor", "computation", "warnings"}:
        raise ValueError("invalid generation descriptor")
    descriptor, warnings = generation["descriptor"], generation["warnings"]
    if not isinstance(descriptor, dict) or not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
        raise ValueError("invalid generation metadata")
    computation = _saved_computation(db, artifact, rev, generation["computation"])
    details = {}
    expected_analysis = None
    if format in ("xlsx", "svg", "bundle") or (format == "docx" and artifact.kind == "analysis"):
        _, numeric, statistics = _analysis_values(rev)
        expected_analysis = analysis_expected(rev, numeric, statistics)
        if warnings != expected_analysis["warnings"]:
            raise ValueError("analysis warnings do not match fixed inputs")
    if format == "docx":
        if artifact.kind not in ("document", "analysis") or _docx(payload, analysis_chart=artifact.kind == "analysis") != descriptor:
            raise ValueError("document content descriptor mismatch")
        if artifact.kind == "analysis":
            from .word_chart_checks import verify
            details = {**descriptor, **verify(payload, artifact, rev, title, numeric, statistics,
                _xlsx_rows(artifact, rev, title, computation, warnings, word_chart=True))}
        else:
            details = {**descriptor, **verify_docx(payload, title, rev)}
    elif format == "xlsx":
        if artifact.kind != "analysis":
            raise ValueError("not analysis")
        details = _read_xlsx(payload, _xlsx_rows(artifact, rev, title, computation, warnings))
    elif format == "svg":
        details = _svg(payload)
        if artifact.kind != "analysis" or store.digest(payload) != descriptor.get("svg_sha256"):
            raise ValueError("SVG content mismatch")
        details.update(verify_svg(payload, rev.analysis, expected_analysis))
    elif format == "bundle":
        required = {"data.csv", "config.json", "reproduce.py", "result.json", "chart.svg", "sources.json", "README.txt", "manifest.json"}
        with _zip(payload, required) as archive:
            if set(archive.namelist()) != required or artifact.kind != "analysis":
                raise ValueError("bundle members mismatch")
            packaged = _json(archive.read("manifest.json"))
            result = _json(archive.read("result.json"))
            if (packaged.get("artifact_id") != artifact.id or packaged.get("revision_id") != rev.id
                    or packaged.get("manifest_sha256") != rev.manifest_sha256 or packaged.get("version") != rev.version
                    or packaged.get("recorded_execution") is not bool(computation)
                    or packaged.get("computation_id") != (computation["id"] if computation else None)
                    or set(packaged.get("files", {})) != required - {"manifest.json"}
                    or any(store.digest(archive.read(name)) != digest for name, digest in packaged["files"].items())
                    or archive.read("data.csv") != rev.content.encode("utf-8")
                    or _json(archive.read("config.json")) != rev.analysis
                    or archive.read("reproduce.py") != SCRIPT.encode("utf-8")
                    or _json(archive.read("sources.json")) != rev.evidence
                    or result.get("data_sha256") != rev.content_sha256
                    or {key: value for key, value in result.items() if key != "svg"} != expected_analysis
                    or store.digest(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)) != descriptor.get("result_sha256")
                    or result.get("svg", "").encode("utf-8") != archive.read("chart.svg")):
                raise ValueError("bundle source/result binding mismatch")
            _svg(archive.read("chart.svg"))
            checked_svg = verify_svg(archive.read("chart.svg"), rev.analysis, expected_analysis)
            details = {"members": sorted(required), "member_hashes_checked": True,
                "result_sha256": descriptor["result_sha256"], **checked_svg}
    elif format == "md":
        if payload != build_markdown(title, rev.content, sources(rev)).encode("utf-8"):
            raise ValueError("Markdown content mismatch")
    elif format == "manifest":
        if _json(payload) != manifest(artifact, rev, title):
            raise ValueError("manifest content mismatch")
    else:
        raise ValueError("unknown format")
    return {"container_readable": True, "content_checked": True, "source_binding_checked": True,
        "source_binding_scope": "fixed_artifact_revision_content_and_manifest",
        "computation_recorded": bool(computation), "native_open": "not_assessed", "render": "not_assessed",
        "semantic": "not_assessed", "content_check_scope": "actual_container_against_fixed_input_content_and_structure",
        "details": details, **generation}
