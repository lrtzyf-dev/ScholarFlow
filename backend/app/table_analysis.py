"""Local formula receipts and exact-script export, never a rewritten source."""
from __future__ import annotations

import hashlib
import io
import json
import platform
import zipfile

from sqlalchemy import select, text
from . import source_store as store
from .source_documents import read_document
from .table_models import TableComputation
from .table_formula import PROGRAM, execute


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def fingerprint(row):
    return digest(canonical([row.id, row.conversation_id, row.revision_id, row.request_id, row.request_sha256,
        row.raw_sha256, row.extraction_fingerprint, row.program_sha256, row.config, row.result]))


def public(row):
    if row.program_sha256 != digest(row.program) or row.fingerprint != fingerprint(row):
        raise store.SourceCorruption("表格复算记录校验失败，不能作为已执行结果使用")
    return {"id": row.id, "revision_id": row.revision_id, "raw_sha256": row.raw_sha256,
            "extraction_fingerprint": row.extraction_fingerprint, "program_sha256": row.program_sha256,
            "fingerprint": row.fingerprint, "config": row.config, "result": row.result,
            "created_at": row.created_at.isoformat() + "Z", "execution_performed": True}


def compute(db, revision_id, *, scope, request_id, expected_fingerprint, config, manage_transaction=True):
    doc = read_document(db, revision_id, scope=scope)
    if doc["format"] != "excel" or not doc.get("table_metadata"):
        raise store.SourceError("此版本尚无可复算的 Excel 结构；请上传新版本，旧版本保持可阅读")
    if doc["fingerprint"] != expected_fingerprint:
        raise store.SourceConflict("表格解析版本已变化，请重新载入后核对")
    payload = digest(canonical([revision_id, expected_fingerprint, config]))
    # API callers release their read transaction for calculation. Agent callers
    # participate in the executor's checkpoint transaction and must never roll it back.
    if manage_transaction:
        db.rollback()
    try:
        result = execute(doc, config)
    except (ValueError, TypeError, KeyError) as exc:
        raise store.SourceError("复算范围或参数无效，未生成已执行记录") from None
    result["python_version"] = platform.python_version()
    if manage_transaction:
        db.execute(text("BEGIN IMMEDIATE"))
    from .source_runtime import conversation_scope
    current_scope = conversation_scope(db, scope.scope_id)
    if current_scope != scope or read_document(db, revision_id, scope=scope)["fingerprint"] != expected_fingerprint:
        raise store.SourceConflict("表格范围或解析版本已变化，未保存复算结果")
    prior = db.scalar(select(TableComputation).where(TableComputation.conversation_id == scope.scope_id,
        TableComputation.request_id == request_id))
    if prior:
        if prior.request_sha256 != payload:
            raise store.SourceConflict("此复算请求已绑定其他范围或容差，请使用新请求编号")
        return public(prior)
    row = TableComputation(conversation_id=scope.scope_id, revision_id=revision_id, request_id=request_id,
        request_sha256=payload, raw_sha256=doc["raw_sha256"], extraction_fingerprint=expected_fingerprint,
        program=PROGRAM, program_sha256=digest(PROGRAM), config=config, result=result)
    from uuid import uuid4
    row.id = uuid4().hex
    row.fingerprint = fingerprint(row)
    db.add(row); db.flush()
    return public(row)


def history(db, revision_id, *, scope):
    doc = read_document(db, revision_id, scope=scope)
    rows = db.scalars(select(TableComputation).where(TableComputation.revision_id == revision_id,
        TableComputation.conversation_id == scope.scope_id).order_by(TableComputation.created_at.desc(), TableComputation.id.desc()).limit(30)).all()
    results = []
    for row in rows:
        value = public(row)
        if (row.raw_sha256, row.extraction_fingerprint) != (doc["raw_sha256"], doc["fingerprint"]):
            raise store.SourceCorruption("表格复算记录不属于此固定原件和解析版本")
        results.append(value)
    return {"items": results}


RUNNER = '''import hashlib,json,math
from pathlib import Path
base=Path(__file__).resolve().parent
manifest=json.loads((base/'manifest.json').read_text(encoding='utf-8'))
for name,expected in manifest['files'].items():
    if hashlib.sha256((base/name).read_bytes()).hexdigest()!=expected: raise ValueError('Bundle file changed: '+name)
from table_reproduce import calculate
document=json.loads((base/'document.json').read_text(encoding='utf-8'))
config=json.loads((base/'config.json').read_text(encoding='utf-8'))
result=calculate(document,config)
expected=json.loads((base/'result.json').read_text(encoding='utf-8'))
for old,new in zip(expected['outputs'],result['outputs']):
    if old['cell']!=new['cell'] or old['status']!=new['status']: raise ValueError('Result identity/status differs')
    if new['status']=='computed' and not math.isclose(old['value'],new['value'],rel_tol=config['relative_tolerance'],abs_tol=config['absolute_tolerance']): raise ValueError('Numeric result differs')
if len(expected['outputs'])!=len(result['outputs']): raise ValueError('Result count differs')
(base/'recomputed.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
print('Recomputed',result['computed_count'],'formulas;',result['unresolved_count'],'unresolved. This is not a complete Excel engine.')
'''


def bundle(db, revision_id, computation_id, *, scope):
    record = store.read_revision(db, revision_id, scope=scope, include_raw=True)
    doc = read_document(db, revision_id, scope=scope)
    row = db.get(TableComputation, computation_id)
    if row is None or row.revision_id != revision_id or row.conversation_id != scope.scope_id:
        raise store.SourceNotFound("此固定版本没有该复算记录")
    value = public(row)
    if (row.raw_sha256, row.extraction_fingerprint) != (doc["raw_sha256"], doc["fingerprint"]):
        raise store.SourceCorruption("复算记录与原件不一致，未导出")
    files = {"original.xlsx": record["raw_bytes"], "document.json": canonical(doc).encode(),
        "config.json": canonical(row.config).encode(), "result.json": canonical(row.result).encode(),
        "receipt.json": canonical(value).encode(), "table_reproduce.py": row.program.encode(), "reproduce.py": RUNNER.encode(),
        "README.txt": b"Run python reproduce.py with Python 3.10+. Standard library only.\nRecomputes the fixed extracted numeric cells; it does not run Excel, macros, external links or a full workbook calculation engine.\nThe original workbook is included for manual verification. document.json is the frozen extraction bound to its raw SHA-256 and extraction fingerprint.\nUnsupported formulas remain unresolved. Formula caches are unverified comparison data, never computational input. Units are not converted.\n"}
    manifest = {"schema_version": 1, "revision_id": revision_id, "computation_id": row.id,
        "raw_sha256": row.raw_sha256, "extraction_fingerprint": row.extraction_fingerprint,
        "program_sha256": row.program_sha256, "recorded_execution": True,
        "files": {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
    files["manifest.json"] = canonical(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)
    return output.getvalue()
