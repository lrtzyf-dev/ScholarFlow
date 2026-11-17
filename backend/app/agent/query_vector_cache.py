"""Checkpoint-local query vectors bound to the actual Embedding connection.

Retrieval itself always runs. No mutable library result or source alias is
cached here, and no database transaction spans the remote request.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json

import numpy as np


_query_cache = ContextVar("scholarflow_query_vector_cache", default=None)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@contextmanager
def bind_query_cache(cache, authorize, scope):
    token = _query_cache.set((cache, authorize, deepcopy(scope)))
    try:
        yield
    finally:
        _query_cache.reset(token)


def _connection_identity(connection):
    from ..model_connection import normalize_base_url, single_line
    return {"base_url": normalize_base_url(connection["base_url"], "embeddings"),
            "model_id": single_line(connection["model_id"], "Embedding 模型名称"),
            "credential_sha256": hashlib.sha256(single_line(connection["api_key"], "API Key").encode()).hexdigest(),
            "mode": connection.get("mode")}


def _authorized_identity(context, connection, texts, synthetic, authorization_check):
    from ..pipeline.embeddings import embedding_connection
    _, authorize, scope = context
    authorize()
    if authorization_check is not None and authorization_check() is not True:
        raise ValueError("向量配置或凭据已变化，请重新运行检索")
    actual = _connection_identity(connection)
    # The embedder may have captured an older connection for a probe or an
    # earlier selection. A query must not borrow that stale configuration.
    if actual != _connection_identity(embedding_connection()):
        raise ValueError("向量配置或凭据已变化，请重新运行检索")
    return {"version": "embedding-query/1", "connection": actual, "scope": scope,
            "text_sha256": _digest(texts), "synthetic": bool(synthetic)}


def lookup(connection, texts, *, synthetic=False, authorization_check=None):
    context = _query_cache.get()
    if context is None or len(texts) != 1:
        return None, None
    identity = _authorized_identity(context, connection, texts, synthetic, authorization_check)
    key = "embedding-query/" + _digest(identity)
    cache, _, _ = context
    item = cache.get(key)
    if isinstance(item, dict) and item.get("identity") == identity:
        try:
            values = item["vectors"]
            if _digest(values) != item["vectors_sha256"]:
                raise ValueError("查询向量校验失败")
            if (not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], list)
                    or not 1 <= len(values[0]) <= 65536
                    or any(type(value) not in (float, int) for value in values[0])):
                raise ValueError("查询向量格式无效")
            array = np.asarray(values, dtype=np.float32)
            if not np.isfinite(array).all():
                raise ValueError("查询向量格式无效")
        except (KeyError, TypeError, ValueError, OverflowError):
            cache.pop(key, None)
        else:
            _authorized_identity(context, connection, texts, synthetic, authorization_check)
            from .readonly_cache import store_entry
            store_entry(cache, key, item)
            return None, array.copy()
    return (context, key, identity, dict(connection), tuple(texts), synthetic, authorization_check), None


def publish(ticket, vectors):
    if ticket is None:
        return
    context, key, identity, connection, texts, synthetic, authorization_check = ticket
    current = _authorized_identity(context, connection, list(texts), synthetic, authorization_check)
    if current != identity:
        return
    if (vectors.ndim != 2 or vectors.shape[0] != 1 or not 1 <= vectors.shape[1] <= 65536
            or not np.isfinite(vectors).all()):
        return
    cache, _, _ = context
    values = vectors.tolist()
    entry = {"identity": identity, "vectors": values, "vectors_sha256": _digest(values)}
    from .readonly_cache import store_entry
    store_entry(cache, key, entry)
