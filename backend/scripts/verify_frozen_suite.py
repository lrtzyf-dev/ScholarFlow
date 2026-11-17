"""Run every collected test once in isolated file-based shards.

Checks source hashes before/after, collected IDs against JUnit IDs, and reports
failures without adding overlapping historical test counts. No provider calls
are authorized by this runner; normal conftest isolation remains in force.
"""
from __future__ import annotations
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


def main():
    backend = Path(__file__).resolve().parents[1]
    root = backend.parent
    output = root / "verification" / ("final-suite-" + time.strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, PYTHONUTF8="1", LLM_API_KEY="", EMBEDDING_API_KEY="", REDIS_URL="")
    def hashes():
        paths = sorted([*backend.glob("app/**/*.py"), *backend.glob("tests/**/*.py"), backend / "requirements.txt", backend / "pytest.ini"])
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    before = hashes()
    collected = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts="],
        cwd=backend, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    (output / "collection.log").write_text(collected.stdout + collected.stderr, encoding="utf-8")
    if collected.returncode:
        raise RuntimeError("Test collection failed; inspect collection.log")
    ids = [line.strip() for line in collected.stdout.splitlines() if line.startswith("tests/") and "::" in line]
    if not ids or len(set(ids)) != len(ids):
        raise RuntimeError("Test collection is empty or contains duplicate IDs")
    (output / "collected.json").write_text(json.dumps(ids, ensure_ascii=False, indent=2), encoding="utf-8")
    files = sorted({i.split("::")[0] for i in ids})
    # Historical timings only balance execution; they are never pass evidence.
    durations = {}
    for path in sorted((root / "verification").glob("*results.xml"), key=lambda p:p.stat().st_mtime):
        try:
            rows = ET.parse(path).findall(".//testcase")
        except (ET.ParseError, OSError):
            continue
        totals = {}
        for row in rows:
            module = row.attrib.get("classname", "")
            name = next((file for file in files if module == file[:-3].replace("/", ".") or module.startswith(file[:-3].replace("/", ".") + ".")), None)
            if name:
                totals[name] = totals.get(name, 0) + float(row.attrib.get("time", 0))
        for name, duration in totals.items():
            durations[name] = max(durations.get(name, 0), duration)
    groups = [[], [], []]
    weights = [0, 0, 0]
    for file in sorted(files, key=lambda f:durations.get(f, 5), reverse=True):
        index = min(range(3), key=lambda i: weights[i])
        groups[index].append(file)
        weights[index] += max(durations.get(file, 5), 1)
    started = time.time()
    print(json.dumps({"output": str(output), "collected_tests": len(ids), "shard_file_counts": [len(g) for g in groups]}, ensure_ascii=False), flush=True)
    def execute(index):
        log = output / f"shard-{index}.log"
        xml = output / f"shard-{index}.xml"
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", "--junitxml=" + str(xml), *groups[index]],
                                    cwd=backend, env=env, stdout=stream, stderr=subprocess.STDOUT)
        print(json.dumps({"shard": index, "exit_code": result.returncode, "elapsed_seconds": round(time.time() - started)}), flush=True)
        return {"shard": index, "exit_code": result.returncode, "files": groups[index], "junit": str(xml)}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(execute, range(3)))
    cases = {}
    duplicates = []
    for shard in results:
        xml = Path(shard["junit"])
        if not xml.exists():
            continue
        for item in ET.parse(xml).findall(".//testcase"):
            classname = item.attrib.get("classname", "")
            file = next((f for f in shard["files"] if classname == f[:-3].replace("/", ".") or classname.startswith(f[:-3].replace("/", ".") + ".")), None)
            if not file:
                continue
            suffix = classname[len(file[:-3].replace("/", ".")):].strip(".").replace(".", "::")
            node = file + "::" + (suffix + "::" if suffix else "") + item.attrib["name"]
            failure = item.find("failure")
            error = item.find("error")
            skipped = item.find("skipped")
            status = "failed" if failure is not None or error is not None else "skipped" if skipped is not None else "passed"
            if node in cases:
                duplicates.append(node)
            cases[node] = {"status": status, "seconds": float(item.attrib.get("time", 0)), "shard": shard["shard"]}
    after = hashes()
    report = {"collected_count": len(ids), "observed_count": len(cases), "passed": sum(v["status"] == "passed" for v in cases.values()),
        "failed": [k for k,v in cases.items() if v["status"] == "failed"], "skipped": [k for k,v in cases.items() if v["status"] == "skipped"],
        "missing": sorted(set(ids) - set(cases)), "unexpected": sorted(set(cases) - set(ids)), "duplicates": duplicates,
        "source_unchanged": before == after, "source_hashes_before": before, "source_hashes_after": after,
        "elapsed_seconds": round(time.time() - started, 2), "shards": results, "cases": cases}
    report["ok"] = not (report["failed"] or report["missing"] or report["unexpected"] or duplicates) and before == after and all(r["exit_code"] == 0 for r in results)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "verification/final-suite-latest.json").write_text(json.dumps({"report": str(output / "report.json"), "ok": report["ok"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k not in ("cases", "shards", "source_hashes_before", "source_hashes_after")}, ensure_ascii=False), flush=True)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
