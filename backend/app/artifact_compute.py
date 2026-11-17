"""Fixed, exported stdlib analysis program; uploaded data never becomes code."""
from __future__ import annotations

import io
import json
import platform
import sys
import zipfile

from fastapi import HTTPException

from .artifact_models import ArtifactComputation

# This exact program is both executed and exported. It has no network, eval,
# dynamic imports, user paths, shell, or third-party runtime dependencies.
SCRIPT = '''"""Reproduce ScholarFlow descriptive data and editable SVG (Python 3.10+)."""
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from xml.sax.saxutils import escape


def calculate(content, config):
    rows = list(csv.reader(io.StringIO(content)))
    if len(rows) < 2 or len(rows) > 10001:
        raise ValueError("CSV must contain a header and 1-10000 data rows")
    columns = rows[0]
    if not columns or len(columns) > 100 or any(not c.strip() for c in columns) or len(set(columns)) != len(columns):
        raise ValueError("CSV headers must be nonempty, unique and at most 100 columns")
    if any(len(row) != len(columns) for row in rows[1:]):
        raise ValueError("CSV rows must have the same width as the header")
    names = config["numeric_columns"]
    if not names or len(set(names)) != len(names) or len(names) > 20 or any(c not in columns for c in names):
        raise ValueError("Select 1-20 distinct numeric columns")
    if config["category_column"] not in columns or config["chart_type"] not in ("bar", "line", "radar"):
        raise ValueError("Invalid chart configuration")
    if config.get("missing_policy") != "gap":
        raise ValueError("Missing values must be preserved as gaps")
    ci = columns.index(config["category_column"])
    points, stats, warnings = [], {}, []
    for row_number, row in enumerate(rows[1:], 2):
        values = {}
        for name in names:
            text = row[columns.index(name)].strip()
            if not text:
                values[name] = None
                continue
            try:
                value = float(text)
            except ValueError:
                raise ValueError("Non-numeric value in row %d, column %s" % (row_number, name)) from None
            if not math.isfinite(value):
                raise ValueError("Non-finite value in row %d, column %s" % (row_number, name))
            values[name] = value
        points.append({"category": row[ci], "values": values})
    for name in names:
        values = [p["values"][name] for p in points if p["values"][name] is not None]
        if not values:
            raise ValueError("Selected numeric column has no finite values: " + name)
        stats[name] = {"count": len(values), "missing": len(points) - len(values), "min": min(values),
                       "max": max(values), "mean": math.fsum(v / len(values) for v in values),
                       "unit": config.get("units", {}).get(name, "unspecified")}
    units = {stats[name]["unit"] for name in names}
    if len(units) > 1 and config["chart_type"] != "radar":
        raise ValueError("A shared numeric axis requires matching units; create separate charts or use radar normalization")
    if any(s["missing"] for s in stats.values()):
        warnings.append("Missing values are gaps, never zero or imputed")
    visible = points[:200]
    if len(points) > 200:
        warnings.append("SVG preview contains first 200 rows; statistics and result JSON contain all rows")
    width, height = 1000, 600
    all_values = [v for p in visible for v in p["values"].values() if v is not None]
    if not all_values:
        warnings.append("The displayed rows contain no finite values; preview is empty, full-data statistics remain available")
    low, high = min(0.0, min(all_values, default=0.0)), max(0.0, max(all_values, default=0.0))
    span = high - low
    if not math.isfinite(span):
        raise ValueError("Numeric range exceeds finite chart precision")
    span = span or 1.0
    def y(v): return 500 - (v - low) / span * 420
    colors = ["#355cf6", "#7699eb", "#526b96", "#0e8174", "#a55738"]
    svg = ['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 600" role="img">',
           '<rect width="1000" height="600" fill="white"/>',
           '<text x="60" y="30" font-size="18">' + escape(config.get("title", "Data chart")) + '</text>']
    if config["chart_type"] == "radar":
        if len(names) < 3:
            raise ValueError("Radar charts require at least three numeric columns")
        if len(visible) > 30:
            visible = visible[:30]
            warnings.append("Radar SVG shows first 30 rows to preserve legibility")
        warnings.append("Radar axes use per-column min/max normalization; original values and units are retained in result JSON")
        for index, name in enumerate(names):
            angle = -math.pi / 2 + 2 * math.pi * index / len(names)
            x, yy = 500 + 210 * math.cos(angle), 290 + 210 * math.sin(angle)
            svg.append('<line x1="500" y1="290" x2="%.3f" y2="%.3f" stroke="#ccd2dc"/>' % (x, yy))
            svg.append('<text x="%.3f" y="%.3f" font-size="12">%s</text>' % (x, yy, escape(name)))
        for index, point in enumerate(visible):
            if any(v is None for v in point["values"].values()):
                continue
            coords = []
            for ni, name in enumerate(names):
                s = stats[name]
                denominator = s["max"] - s["min"]
                if not math.isfinite(denominator):
                    raise ValueError("Radar range exceeds finite chart precision")
                fraction = (point["values"][name] - s["min"]) / denominator if denominator else 0.5
                angle = -math.pi / 2 + 2 * math.pi * ni / len(names)
                coords.append("%.3f,%.3f" % (500 + 210 * fraction * math.cos(angle), 290 + 210 * fraction * math.sin(angle)))
            svg.append('<polygon points="%s" fill="none" stroke="%s"><title>%s</title></polygon>' %
                       (" ".join(coords), colors[index % len(colors)], escape(point["category"])))
    else:
        svg.append('<line x1="60" y1="%.3f" x2="960" y2="%.3f" stroke="#6a7485"/>' % (y(0), y(0)))
        group_width = 900 / max(1, len(visible))
        for ni, name in enumerate(names):
            color = colors[ni % len(colors)]
            segments, current = [], []
            for index, point in enumerate(visible):
                value = point["values"][name]
                if value is None:
                    if current: segments.append(current)
                    current = []
                    continue
                x = 60 + (index + 0.5) * group_width
                yy = y(value)
                if config["chart_type"] == "bar":
                    bw = group_width * 0.8 / len(names)
                    svg.append('<rect x="%.3f" y="%.3f" width="%.3f" height="%.3f" fill="%s"><title>%s</title></rect>' %
                        (x - group_width * 0.4 + ni * bw, min(y(0), yy), bw, abs(y(0) - yy), color,
                         escape(point["category"] + " / " + name + ": " + str(value))))
                else:
                    current.append("%.3f,%.3f" % (x, yy))
                    svg.append('<circle cx="%.3f" cy="%.3f" r="2" fill="%s"/>' % (x, yy, color))
            if current: segments.append(current)
            for segment in segments:
                svg.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="2"/>' % (" ".join(segment), color))
            svg.append('<text x="%d" y="565" fill="%s" font-size="12">%s</text>' % (60 + ni * 140, color, escape(name)))
        for index, point in enumerate(visible):
            if index % max(1, len(visible) // 12) == 0:
                svg.append('<text x="%.3f" y="525" font-size="11">%s</text>' %
                    (60 + (index + 0.5) * group_width, escape(point["category"][:24])))
        svg.append('<text x="8" y="80" font-size="11">%s</text>' % escape(str(high)))
        svg.append('<text x="8" y="500" font-size="11">%s</text>' % escape(str(low)))
    svg.append('</svg>')
    return {"row_count": len(points), "points": points, "stats": stats, "warnings": warnings,
            "method": "Descriptive finite numeric statistics; no inference, imputation or causal analysis",
            "data_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "svg": "".join(svg)}


if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    result = calculate((base / "data.csv").read_bytes().decode("utf-8"),
                       json.loads((base / "config.json").read_text(encoding="utf-8")))
    (base / "chart.svg").write_text(result["svg"], encoding="utf-8", newline="\\n")
    (base / "result.json").write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2), encoding="utf-8", newline="\\n")
    print("Recomputed", result["row_count"], "rows; outputs: result.json, chart.svg")
'''


def execute(content, config):
    namespace = {"__name__": "scholarflow_reproduce"}
    exec(compile(SCRIPT, "reproduce.py", "exec"), namespace)
    return namespace["calculate"](content, config)


def validate_analysis(content, config):
    if not isinstance(config, dict) or set(config) - {"category_column", "numeric_columns", "chart_type", "units", "missing_policy", "title"}:
        raise HTTPException(422, "分析配置包含未知字段")
    fixed = dict(config)
    fixed.setdefault("units", {})
    fixed.setdefault("missing_policy", "gap")
    if (not isinstance(fixed.get("numeric_columns"), list) or not all(isinstance(n, str) for n in fixed["numeric_columns"])
            or not isinstance(fixed.get("category_column"), str) or not isinstance(fixed.get("units"), dict)
            or not all(isinstance(k, str) and isinstance(v, str) and len(v) <= 80 for k, v in fixed["units"].items())
            or not isinstance(fixed.get("title", ""), str) or len(fixed.get("title", "")) > 240):
        raise HTTPException(422, "请提供有效类别、数值列和单位")
    try:
        execute(content, fixed)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        raise HTTPException(422, str(exc)) from None
    return fixed


def recompute(db, row, rev):
    from .artifact_store import digest
    if row.kind != "analysis":
        raise HTTPException(422, "此成果不是数据分析")
    try:
        result = execute(rev.content, rev.analysis)
    except (ValueError, KeyError, TypeError, OverflowError) as exc:
        record = ArtifactComputation(artifact_id=row.id, revision_id=rev.id, script_sha256=digest(SCRIPT),
                                     status="failed", error=str(exc))
        db.add(record)
        db.commit()
        raise HTTPException(422, {"message": "复算失败", "computation_id": record.id, "error": str(exc)}) from None
    summary = {"data_sha256": result["data_sha256"], "stats": result["stats"], "row_count": result["row_count"],
        "warnings": result["warnings"], "execution_engine": "trusted_exported_python_script", "python_version": platform.python_version(),
        "tolerance": {"absolute": 1e-12, "relative": 1e-12}, "seed": None,
        "result_sha256": digest(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))}
    record = ArtifactComputation(artifact_id=row.id, revision_id=rev.id, script_sha256=digest(SCRIPT), status="succeeded", result=summary)
    db.add(record)
    db.commit()
    return {"id": record.id, "revision_id": rev.id, "status": record.status, "script_sha256": record.script_sha256, **summary}


def bundle(db, row, rev):
    from .artifact_store import digest
    if row.kind != "analysis":
        raise HTTPException(422, "此成果不是数据分析")
    record = db.query(ArtifactComputation).filter_by(revision_id=rev.id, status="succeeded", script_sha256=digest(SCRIPT)).order_by(ArtifactComputation.created_at.desc()).first()
    result = execute(rev.content, rev.analysis)
    verified = bool(record and record.result.get("result_sha256") == digest(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)))
    files = {"data.csv": rev.content, "config.json": json.dumps(rev.analysis, ensure_ascii=False, indent=2),
             "reproduce.py": SCRIPT, "result.json": json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2),
             "chart.svg": result["svg"], "sources.json": json.dumps(rev.evidence, ensure_ascii=False, indent=2),
             "README.txt": "Run: python reproduce.py\nRequires Python 3.10+ standard library only.\nMissing values remain gaps. No statistical inference is performed.\nEdit data.csv/config.json and rerun to create a new result; original hashes then no longer match.\n"}
    manifest = {"artifact_id": row.id, "revision_id": rev.id, "version": rev.version,
        "manifest_sha256": rev.manifest_sha256, "method": result["method"], "dependencies": {"python": ">=3.10", "packages": []},
        "recorded_execution": verified, "computation_id": record.id if verified else None,
        "files": {name: digest(content) for name, content in files.items()}}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content.encode("utf-8"))
    return output.getvalue()
