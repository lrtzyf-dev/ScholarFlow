"""Checkpoint-local reuse of reauthorized, version-verified read results."""
import hashlib
import json
from copy import deepcopy

from sqlalchemy.orm import Session

READONLY_VERSIONED = {"read_project_file", "list_project_files"}
READONLY_REVALIDATED = READONLY_VERSIONED | {"search_project_files", "retrieve_evidence", "list_papers"}
MAX_ENTRIES, MAX_BYTES = 64, 8 * 1024 * 1024


def store_entry(cache, key, entry):
    """One serialized budget across result bodies, identities and vectors."""
    cache.pop(key, None)
    size = len(json.dumps(entry, ensure_ascii=False).encode())
    if size <= MAX_BYTES:
        cache[key] = entry
    while len(cache) > MAX_ENTRIES or len(json.dumps(cache, ensure_ascii=False).encode()) > MAX_BYTES:
        cache.pop(next(iter(cache)))


def _authorize(db, name, arguments, *, binding, conversation_id, project_scope, scope_paper_id):
    from .tools import tool_policy_error
    denied = tool_policy_error(name)
    if denied:
        raise PermissionError(denied)
    if binding.get("conversation_id") not in (None, conversation_id):
        raise PermissionError("只读缓存的会话范围与本轮不一致")
    if conversation_id:
        from ..source_runtime import conversation_scope
        conversation_scope(db, conversation_id)
    if binding.get("goal_id"):
        from ..goal_ledger import snapshot_goal
        from ..goal_candidates import tool_constraint
        constraint = tool_constraint(snapshot_goal(db, binding["goal_id"]), name, arguments)
        if constraint:
            raise PermissionError(constraint.get("error") or "当前用户要求不允许此读取")
    if name in ("read_project_file", "list_project_files", "search_project_files"):
        from ..workspace import validate_scope
        validate_scope(db, conversation_id, project_scope)
    else:
        from .paper_scope import resolve
        requested = arguments.get("paper_id") if name == "retrieve_evidence" else None
        if scope_paper_id and requested and requested != scope_paper_id:
            raise PermissionError("不能通过缓存覆盖用户选择的文献范围")
        resolve(db, scope_paper_id or requested)
        if name == "retrieve_evidence":
            from ..config import require_available_credentials, settings
            require_available_credentials("embedding_api_key")
            if settings.embedding_mode in ("api", "litellm"):
                from ..pipeline.embeddings import embedding_connection
                embedding_connection()  # Revocation and fallback-origin checks, no HTTP.


def _identity(db, name, arguments, *, binding, conversation_id, project_scope, scope_paper_id):
    from .readonly_versions import project_version
    with Session(db.get_bind()) as reader:
        _authorize(reader, name, arguments, binding=binding, conversation_id=conversation_id,
                   project_scope=project_scope, scope_paper_id=scope_paper_id)
        version = project_version(name, arguments, project_scope)
    return {"tool": name, "version": "verified-read/2", "source_version": version,
        "arguments": deepcopy(arguments), "conversation_id": conversation_id, "run_id": binding.get("run_id"),
        "policy_revision": binding.get("policy_revision"), "workspace": deepcopy(project_scope),
        "scope_paper_id": scope_paper_id}


def _eligible(result):
    try:
        payload = json.loads(result)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and not any(payload.get(key) for key in (
        "error", "limited", "truncated", "output_is_partial", "execution_unknown"))


def verified_read(db, name, arguments, *, binding, conversation_id, project_scope=None,
                  scope_paper_id=None, cache, action):
    """Every hit proves current scope and bytes; unknown versions never hit."""
    if name not in READONLY_REVALIDATED:
        return action()
    def authorize():
        with Session(db.get_bind()) as reader:
            _authorize(reader, name, arguments, binding=binding, conversation_id=conversation_id,
                       project_scope=project_scope, scope_paper_id=scope_paper_id)
    def actual():
        authorize()
        result = action()
        authorize()
        return result
    authorize()
    if name == "retrieve_evidence":
        # A mutable index cannot prove which generation an arbitrary action
        # observed. Re-run retrieval; only the model-bound query vector is reused.
        from .query_vector_cache import bind_query_cache
        with bind_query_cache(cache, authorize, {
                "conversation_id": conversation_id, "run_id": binding.get("run_id"),
                "policy_revision": binding.get("policy_revision"), "scope_paper_id": scope_paper_id}):
            result = actual()
        authorize()
        return result
    if name == "search_project_files":
        from .search_snapshot_cache import cached_search
        return cached_search(arguments, scope=project_scope, binding=binding, conversation_id=conversation_id,
            cache=cache, authorize=authorize, action=actual)
    if name not in READONLY_VERSIONED:
        # Current directory search and cheap library listing execute against
        # their real producers, never a pre/post-hash-only result cache.
        result = actual()
        authorize()
        return result
    from .readonly_versions import VersionUnavailable
    try:
        identity = _identity(db, name, arguments, binding=binding, conversation_id=conversation_id,
            project_scope=project_scope, scope_paper_id=scope_paper_id)
    except PermissionError:
        raise
    except (VersionUnavailable, OSError, UnicodeError):
        return actual()
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    key = "verified/" + hashlib.sha256(encoded.encode()).hexdigest()
    existing = cache.get(key)
    hit = bool(isinstance(existing, dict) and isinstance(existing.get("result"), str) and existing.get("identity") == identity
            and hashlib.sha256(existing["result"].encode()).hexdigest() == existing.get("result_sha256"))
    if hit:
        result = existing["result"]
    else:
        result = actual()
    # A scope/version change during lookup or execution cannot publish an old
    # observation as current. No writer transaction is held across an API call.
    try:
        current = _identity(db, name, arguments, binding=binding, conversation_id=conversation_id,
            project_scope=project_scope, scope_paper_id=scope_paper_id)
    except PermissionError:
        cache.pop(key, None)
        raise
    except (VersionUnavailable, OSError, UnicodeError):
        cache.pop(key, None)
        authorize()
        return result if not hit else actual()
    authorize()
    if current != identity:
        cache.pop(key, None)
        # A miss already performed a real read; it remains a historical snapshot.
        # A hit has not read its old body and must obtain a current observation.
        return actual() if hit else result
    from .readonly_versions import digest
    if (not _eligible(result) or digest(json.loads(result)) != identity["source_version"]["result_sha256"]):
        cache.pop(key, None)
        return result
    store_entry(cache, key, {"identity": identity, "result": result,
        "result_sha256": hashlib.sha256(result.encode()).hexdigest()})
    return result


def attached_read(db, arguments, *, binding, conversation_id, cache, action):
    from ..source_runtime import require_attached_revision
    def current_identity():
        with Session(db.get_bind()) as reader:
            _authorize(reader, "read_attached_source", arguments, binding=binding,
                conversation_id=conversation_id, project_scope=None, scope_paper_id=None)
            # Fresh sessions recheck conversation revocation and original bytes;
            # a retained ORM object must never authorize an archived source.
            record = require_attached_revision(reader, arguments.get("source_revision_id"),
                conversation_id=conversation_id, binding=binding, allow_service_derived=True)
        source = (record["local_read_authorization"] if record.get("local_read_authorization") else
                  next(item for item in binding.get("source_bindings", [])
                       if item["revision_id"] == record["revision_id"]))
        retained_ocr = bool(record.get("local_read_authorization")) or (
            record.get("text_origin") == "ocr_text" and source.get("document_format") == "text")
        return {"tool": "read_attached_source", "tool_version": "source-reader-v3",
            "conversation_id": conversation_id, "run_id": binding.get("run_id"),
            "policy_revision": binding.get("policy_revision"), "source": deepcopy(source), "arguments": deepcopy(arguments),
            "read_representation": "retained_ocr_text_lines" if retained_ocr else "frozen_source"}
    try:
        identity = current_identity()
    except Exception:
        # A revoked/damaged derivative must invalidate its prior retained body
        # even when authorization fails before a new cache key can be built.
        for stale_key, entry in list(cache.items()):
            saved = entry.get("identity", {}) if isinstance(entry, dict) else {}
            if (saved.get("tool") == "read_attached_source" and saved.get("conversation_id") == conversation_id
                    and saved.get("arguments", {}).get("source_revision_id") == arguments.get("source_revision_id")):
                cache.pop(stale_key, None)
        raise
    key = "attached/" + hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    existing = cache.get(key)
    hit = bool(isinstance(existing, dict) and existing.get("identity") == identity
        and isinstance(existing.get("result"), str)
        and hashlib.sha256(existing["result"].encode()).hexdigest() == existing.get("result_sha256"))
    if hit and identity["read_representation"] != "retained_ocr_text_lines":
        from ..source_cached_read import matches_saved_read
        # The checkpoint is mutable state, not a signature on producer output.
        # Check every public field against independently loaded fixed records;
        # a valid hit still avoids calling the original expensive producer.
        with Session(db.get_bind()) as reader:
            hit = matches_saved_read(reader, existing["result"], arguments, conversation_id=conversation_id)
        if not hit:
            cache.pop(key, None)
    # OCR rows are cheap local reads. Re-derive text/locator from the fixed store
    # even when an old checkpoint body has a matching self-hash: that hash does
    # not prove the cached quote agrees with the retained source.
    result = existing["result"] if hit and identity["read_representation"] != "retained_ocr_text_lines" else action()
    try:
        current = current_identity()
        if current != identity:
            raise PermissionError("附件固定版本或授权在读取期间发生变化，请重新核对")
    except Exception:
        cache.pop(key, None)
        raise
    if _eligible(result):
        store_entry(cache, key, {"identity": identity, "result": result,
            "result_sha256": hashlib.sha256(result.encode()).hexdigest()})
    else:
        cache.pop(key, None)
    return result
