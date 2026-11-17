"""CSV 解析与数值列识别（标准库实现，无需 pandas）。

纯函数、离线可用，输出前端图表所需的结构化数据与基础统计。
数值列判定基于该列非空值的可转换比例，避免偶然数字导致误判。
"""
from __future__ import annotations

import csv
import io

MAX_ROWS = 10_000
# 一列非空值中至少该比例可转 float，才判定为数值列
NUMERIC_RATIO = 0.8


def parse_csv(content: str, max_rows: int = MAX_ROWS) -> dict:
    if content.startswith("\ufeff"):  # 去除 Excel 常见 BOM
        content = content[1:]
    all_rows = list(csv.reader(io.StringIO(content)))
    if not all_rows:
        return {
            "columns": [], "rows": [], "numeric_columns": [],
            "stats": {}, "row_count": 0, "truncated": False,
        }

    columns = all_rows[0]
    body = all_rows[1:]
    truncated = len(body) > max_rows
    data_rows = body[:max_rows]

    numeric_columns: list[str] = []
    stats: dict[str, dict] = {}
    for ci, col in enumerate(columns):
        numbers: list[float] = []
        nonempty = 0
        for row in data_rows:
            if ci >= len(row) or row[ci].strip() == "":
                continue
            nonempty += 1
            try:
                numbers.append(float(row[ci]))
            except ValueError:
                continue
        if nonempty and numbers and len(numbers) / nonempty >= NUMERIC_RATIO:
            numeric_columns.append(col)
            mean = sum(numbers) / len(numbers)
            stats[col] = {
                "count": len(numbers),
                "min": min(numbers),
                "max": max(numbers),
                "mean": mean,
            }

    return {
        "columns": columns,
        "rows": data_rows,
        "numeric_columns": numeric_columns,
        "stats": stats,
        "row_count": len(data_rows),
        "truncated": truncated,
    }
