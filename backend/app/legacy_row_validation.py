"""Check retained old20 values against current read contracts, without repairs.

Only the private static-schema copy uses this module. It imports neither the
application models/configuration nor historical code, opens no asset paths,
and leaves accepted SQLite values byte-for-byte unchanged.
"""
from __future__ import annotations

from datetime import datetime
import json
import math

from .release_info import ReleaseError


_QA_LISTS = frozenset(("plan_steps", "plan_images", "claims", "evidence", "trace", "skills"))
_TASK_OBJECTS = frozenset(("payload", "result", "checkpoint"))
_TASK_LISTS = frozenset(("plan", "events"))


def _reject(table, column, kind):
    # Values can contain credentials or private research; never echo them.
    raise ReleaseError("旧20表数据不能由当前读取器安全读取：" + table + "." + column + "（" + kind + "）；未发布迁移目录")


def _json_value(table, column, value):
    try:
        # SQLite JSON affinity can turn scalar JSON into a numeric storage
        # class; json.loads (also used by the current ORM) cannot read that.
        if not isinstance(value, (str, bytes)):
            _reject(table, column, "JSON存储类型")
        decoded = json.loads(value)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _reject(table, column, "JSON编码")
    pending = [decoded]
    while pending:
        item = pending.pop()
        # Python's JSON decoder admits NaN/Infinity, including exponent
        # overflow. Current API JSON responses cannot serialize these.
        if isinstance(item, float) and not math.isfinite(item):
            _reject(table, column, "非有限JSON数值")
        if isinstance(item, str):
            try:
                # Escaped unpaired UTF-16 surrogates are accepted by the
                # decoder but cannot be encoded by current UTF-8 responses.
                item.encode("utf-8")
            except UnicodeEncodeError:
                _reject(table, column, "JSON字符编码")
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return decoded


def _workspace(table, column, value):
    if value is None:
        return
    if not isinstance(value, dict):
        _reject(table, column, "工作区对象")
    if "files" in value:
        files = value["files"]
        if not isinstance(files, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("path"), str)
                for item in files):
            _reject(table, column, "工作区文件列表")


def _reader_shape(table, column, value):
    if column == "workspace" and table in ("qa_records", "foreground_runs"):
        _workspace(table, column, value)
    if value is None:
        return
    if table == "qa_records":
        if column in _QA_LISTS and not isinstance(value, list):
            _reject(table, column, "历史消息列表")
        if column == "usage" and not isinstance(value, dict):
            _reject(table, column, "用量对象")
    if table == "tasks":
        if column in _TASK_OBJECTS and not isinstance(value, dict):
            _reject(table, column, "任务对象")
        if column in _TASK_LISTS and not isinstance(value, list):
            _reject(table, column, "任务列表")


def validate_row(table, columns, row, types):
    """Validate every declared JSON/DATETIME old column before copying it.

    ``types`` comes from the freshly created, built-in target schema; neither
    schema declarations nor SQL from an archive become executable here.
    Broader historical journal JSON shapes are retained as-is. Only message,
    workspace and task shapes directly consumed by current readers are fixed.
    """
    for column, value in zip(columns, row):
        if value is None:
            continue  # The reviewed target still enforces NOT NULL columns.
        kind = types[column]
        if kind == "JSON":
            _reader_shape(table, column, _json_value(table, column, value))
        elif kind == "DATETIME":
            try:
                if not isinstance(value, str):
                    _reject(table, column, "日期存储类型")
                # Current SQLAlchemy SQLite DATETIME decoding uses this same
                # parser. Preserve accepted date text instead of normalizing.
                datetime.fromisoformat(value)
            except (ValueError, TypeError, OverflowError):
                _reject(table, column, "日期编码")
