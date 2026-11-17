"""Fixed translation input is provenance, never scientific support or authority."""
from fastapi import HTTPException
from .. import source_store as store
from ..api.sources import _scope, _source_errors


def fixed_translation_source(db, *, conversation_id, revision_id, locator_id, text):
    scope = _scope(db, conversation_id)
    with _source_errors(db):
        value = store.read_locator(db, locator_id, scope=scope)
        if value["revision_id"] != revision_id:
            raise store.SourceNotFound("定位不属于此固定来源版本")
        if value["quote"] != text:
            raise HTTPException(409, "原文已被编辑或与固定定位不一致，请重新载入选段，或转为自由文本")
        revision = store.read_revision(db, revision_id, scope=scope)
    return {**value, "id": "T1", "evidence_id": "T1", "title": revision["title"],
        "version": revision["version"], "provenance_status": "fixed_source",
        "semantic_verification": "not_assessed", "verified": False,
        "purpose": "translation_input", "conversation_id": conversation_id}
