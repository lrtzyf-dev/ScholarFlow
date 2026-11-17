"""时间工具：统一的 UTC 时间获取，替代已弃用的 datetime.utcnow()。

数据库 DateTime 列当前为 naive（不含时区），故 utcnow() 返回 naive UTC，
与历史数据及所有时间比较逻辑保持一致；将来列升级为 timezone-aware 时，
只需在此处与模型定义处统一调整，调用点无需改动。
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """返回当前 UTC 时间（naive，不含 tzinfo），语义等价于旧 datetime.utcnow()。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)
