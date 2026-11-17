"""Opt-in live tool smoke, with fixed public inputs and isolated application data.

The DeepSeek credential is read from stdin only, never an argument or file.
Exactly one call per selected tool; no retries. Reports omit credentials,
request headers and raw exception text. Only explicit fixed-public-fixture
inspection retains a bounded text excerpt. This is not release approval.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta


def ocr_fixture_checks(text):
    """Check words/values in recognized text, never in normalized coordinates."""
    text = re.sub(r"<｜box｜>.*?<｜/box｜>", " ", text, flags=re.S)
    text = re.sub(r"<｜/?[^｜]+｜>", " ", text)
    text = re.sub(r"[*_`#]", "", text)
    text = " ".join(text.split())
    return {
        "title": bool(re.search(r"\bscholarflow\s+ocr\s+test\b", text, re.I)),
        "sample_count": bool(re.search(r"\bsample\s+count\s*:\s*42\b", text, re.I)),
        "accuracy": bool(re.search(r"\baccuracy\s*:\s*97\s*%", text, re.I)),
    }


def fetch_fixture_checks(title, content, target):
    """The Fetch contract separates title and content; do not require duplication."""
    if target == "example":
        return {"title": title.strip().lower() == "example domain",
                "body": "documentation examples" in content.lower()}
    return {"title": "deepseek" in title.lower(), "body": "api" in content.lower()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real", action="store_true", required=True)
    parser.add_argument("--scope", choices=("deepseek", "public"), required=True)
    parser.add_argument("--tool", choices=("search", "fetch", "ocr"), help="Limit a DeepSeek diagnostic run to one tool")
    parser.add_argument("--fetch-target", choices=("example", "official_docs"), default="official_docs")
    parser.add_argument("--inspect-public-fixture", action="store_true",
                        help="Include at most 600 characters from the fixed example.com test page, with credentials redacted")
    parser.add_argument("--report", required=True)
    parser.add_argument("--credentials-dir", help="Read the already protected test credential in memory; never copy it to the report")
    args = parser.parse_args()
    if args.tool and args.scope != "deepseek":
        parser.error("--tool is only available with --scope deepseek")
    if args.inspect_public_fixture and (args.scope != "deepseek" or args.tool != "fetch" or args.fetch_target != "example"):
        parser.error("Fixture inspection requires the single fixed example.com fetch case")
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    key = ""
    if args.scope == "deepseek":
        if args.credentials_dir:
            from app.credential_protection import unprotect
            saved=json.loads((Path(args.credentials_dir)/'config.json').read_text(encoding='utf-8'))
            protected=(saved.get('protected_credentials') or {}).get('llm_api_key')
            key=unprotect(protected) if protected else saved.get('llm_api_key','')
        elif sys.stdin.isatty():
            import getpass
            key = getpass.getpass("DeepSeek test credential (hidden): ").strip()
        else:
            key = sys.stdin.readline(4098).strip()
    if args.scope == "deepseek" and (not key or len(key) > 4096 or not key.isascii() or any(c.isspace() for c in key)):
        raise SystemExit("A valid single-line credential must be provided through stdin.")
    watched = (["app/providers/deepseek_tools.py", "app/call_ledger.py"] if args.scope == "deepseek" else ["app/mcp_builtin.py", "app/netguard.py"])
    watched.append("scripts/verify_live_tools.py")

    def hashes():
        return {name: hashlib.sha256((backend / name).read_bytes()).hexdigest() for name in watched}

    report = {
        "kind": "live_tool_smoke", "scope": args.scope,
        "started_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "inputs": "fixed public search terms, public example page, synthetic OCR image",
        "private_documents_sent": False, "credential_persisted": False,
        "maximum_tool_invocations": (1 if args.tool else 3) if args.scope == "deepseek" else 4,
        "automatic_retries": 0, "release_approval": False,
        "source_hashes_at_start": hashes(), "cases": [],
    }
    report_path = Path(args.report).resolve()

    def save():
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        if key and key in encoded:
            raise RuntimeError("SecretRejectedFromReport")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(encoded, encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="scholarflow_tools_smoke_") as tmp:
        os.environ.update(DATA_DIR=str(Path(tmp) / "data"), APPDATA=str(Path(tmp) / "appdata"),
                          LOCALAPPDATA=str(Path(tmp) / "localappdata"), LLM_API_KEY="",
                          EMBEDDING_API_KEY="", REDIS_URL="", DESKTOP_NOTIFICATIONS="false",
                          DB_NAME="scholarflow-smoke.db", VECTOR_NAME="vectors-smoke.npy")
        engine = None
        try:
            if args.scope == "deepseek":
                import httpx
                from PIL import Image, ImageDraw, ImageFont
                from app.providers.deepseek_tools import DeepSeekToolsClient, DeepSeekToolsError, ENDPOINTS
                from app.db import Base, engine
                from app.call_ledger import summarize_calls
                from app.config import DB_PATH, VECTOR_PATH

                if not all(path.resolve().is_relative_to(Path(tmp).resolve()) for path in (DB_PATH, VECTOR_PATH)):
                    raise RuntimeError("IsolatedPathRequired")
                report["isolated_database_path_verified"] = True
                Base.metadata.create_all(engine)
                image = Image.new("RGB", (1000, 300), "white")
                draw = ImageDraw.Draw(image)
                font_file = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "arial.ttf"
                font = ImageFont.truetype(str(font_file), 36) if font_file.exists() else ImageFont.load_default()
                draw.multiline_text((40, 40), "ScholarFlow OCR test\nSample count: 42\nAccuracy: 97%", fill="black", font=font, spacing=18)
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                sample = buffer.getvalue()
                report["ocr_fixture"] = {"format": "png", "bytes": len(sample), "sha256": hashlib.sha256(sample).hexdigest()}

                for service in ((args.tool,) if args.tool else ("search", "fetch", "ocr")):
                    case = {"tool": "deepseek_" + service, "endpoint": ENDPOINTS[service], "http_requests": 0}

                    class CountingTransport(httpx.HTTPTransport):
                        def handle_request(self, request):
                            if str(request.url) != ENDPOINTS[service] or case["http_requests"]:
                                raise RuntimeError("UnexpectedEndpointOrRetry")
                            case["http_requests"] += 1
                            response = super().handle_request(request)
                            case["http_status"] = response.status_code
                            return response

                    start = time.monotonic()
                    try:
                        adapter = DeepSeekToolsClient(key, transport=CountingTransport(retries=0), timeout_seconds=90)
                        if service == "search":
                            result = adapter.search("DeepSeek API documentation", count=1, need_content=False)
                            case.update(result_count=len(result["results"]), passed=bool(result["results"]))
                        elif service == "fetch":
                            target = "https://example.com/" if args.fetch_target == "example" else "https://api-docs.deepseek.com/"
                            result = adapter.fetch(target)
                            checks = fetch_fixture_checks(result["title"], result["content"], args.fetch_target)
                            case.update(content_characters=len(result["content"]),
                                        target=target, expected_public_text_found=all(checks.values()), fixture_checks=checks,
                                        returned_title=result["title"].replace(key, "[redacted]")[:160],
                                        known_error_markers=[marker for marker in ("bad gateway", "access denied", "cloudflare", "forbidden", "404", "not found", "captcha", "sign in", "log in") if marker in result["content"].lower()])
                            if args.inspect_public_fixture:
                                case["public_fixture_excerpt"] = result["content"].replace(key, "[redacted]")[:600]
                            case["passed"] = case["expected_public_text_found"]
                        else:
                            result = adapter.ocr(sample, filename="scholarflow-synthetic-ocr.png", purpose="text_recognition")
                            extracted = "\n".join(page["text"] for page in result["pages"])
                            checks = ocr_fixture_checks(extracted)
                            case.update(page_count=result["page_count"], text_characters=len(extracted),
                                        expected_values_found=all(checks.values()), fixture_checks=checks,
                                        truncated=result["truncated"], coordinates_tested=False,
                                        source_coordinates_mapped=result["coordinates"]["mapped_to_source"])
                            case["passed"] = case["page_count"] == 1 and case["expected_values_found"] and not case["truncated"]
                        case.update(result_type=result["result_type"], status=result["status"],
                                    returned_fields=sorted(result), billing=result["billing"])
                    except DeepSeekToolsError as exc:
                        case.update(passed=False, error_kind=exc.code, http_status=exc.status_code or case.get("http_status"),
                                    may_be_billed=exc.may_be_billed, automatic_retry=False)
                    except Exception as exc:
                        case.update(passed=False, error_kind=type(exc).__name__, billing_status="unknown")
                    case["duration_seconds"] = round(time.monotonic() - start, 3)
                    report["cases"].append(case)
                    save()
                    print(json.dumps(case, ensure_ascii=False), flush=True)
                summary = summarize_calls()
                report["ledger_summary"] = summary
                report["actual_currency_amount"] = None
            else:
                import httpx
                from app import mcp_builtin

                async def check_public():
                    definitions = [
                        ("arxiv_search", {"query": "Attention Is All You Need", "max_results": 1}),
                        ("semantic_scholar_search", {"query": "Attention Is All You Need", "limit": 1}),
                        ("crossref_search", {"query": "Attention Is All You Need", "rows": 1}),
                        ("web_fetch", {"url": "https://api-docs.deepseek.com/"}),
                    ]
                    report["maximum_http_requests"] = 24
                    original_transport = httpx.AsyncHTTPTransport.handle_async_request
                    async def counted_transport(transport, request):
                        if case["http_requests"] >= 6:
                            raise RuntimeError("SmokeHttpRequestBudgetExceeded")
                        case["http_requests"] += 1
                        observed = {"host": request.url.host, "path": request.url.path, "method": request.method}
                        case["http_trace"].append(observed)
                        response = await original_transport(transport, request)
                        observed["status"] = response.status_code
                        return response
                    for name, kwargs in definitions:
                        case = {"tool": name, "tool_invocations": 1, "http_requests": 0, "http_trace": []}
                        start = time.monotonic()
                        httpx.AsyncHTTPTransport.handle_async_request = counted_transport
                        try:
                            result = json.loads(await getattr(mcp_builtin, name)(**kwargs))
                            case["returned_fields"] = sorted(result)
                            if "error" in result or result.get("status") in ("error", "failed"):
                                case.update(passed=False, tool_status_code=result.get("status_code"),
                                            error_kind=result.get("code", result.get("error_kind", "tool_reported_error")),
                                            reason_code=result.get("reason_code"))
                            elif name == "web_fetch":
                                case.update(content_characters=len(result.get("text", "")),
                                            tool_status_code=result.get("status_code"),
                                            status=result.get("status"), truncated=result.get("truncated"),
                                            expected_public_text_found="deepseek" in result.get("text", "").lower())
                                case["passed"] = case["expected_public_text_found"]
                            else:
                                case.update(result_count=len(result.get("papers", [])),
                                            passed=result.get("status") == "ok" and bool(result.get("papers")),
                                            tool_status_code=result.get("status_code"))
                        except Exception as exc:
                            case.update(passed=False, error_kind=type(exc).__name__)
                        finally:
                            httpx.AsyncHTTPTransport.handle_async_request = original_transport
                        case["http_status"] = case["http_trace"][-1].get("status") if case["http_trace"] else None
                        case["duration_seconds"] = round(time.monotonic() - start, 3)
                        report["cases"].append(case)
                        save()
                        print(json.dumps(case, ensure_ascii=False), flush=True)
                asyncio.run(check_public())
        finally:
            if engine is not None:
                engine.dispose()
    report["source_hashes_at_end"] = hashes()
    report["source_unchanged_during_run"] = report["source_hashes_at_start"] == report["source_hashes_at_end"]
    report["run_validation_valid"] = report["source_unchanged_during_run"]
    report["passed"] = sum(bool(c.get("passed")) for c in report["cases"])
    report["failed"] = len(report["cases"]) - report["passed"]
    report["finished_at"] = datetime.now(timezone(timedelta(hours=8))).isoformat()
    save()
    key = ""
    print(json.dumps({"report": str(report_path), "passed": report["passed"], "failed": report["failed"]}), flush=True)
    return 0 if not report["failed"] and report["run_validation_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
