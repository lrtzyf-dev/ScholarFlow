"""Bounded execution metadata for paired independent-tool success results.

Provider prose, snippets and OCR text remain source data. A successful service
response proves only that returned material was received, never original-page
completeness, OCR accuracy, image understanding or scientific validity.
"""
import re
from urllib.parse import urlsplit


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{%d}" % length, value) is not None


def _web_origin(value):
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        if parsed is not None:
            parsed.port
        return (parsed is not None and len(value) <= 8192 and parsed.scheme in {"http", "https"}
            and bool(parsed.hostname) and parsed.username is None and parsed.password is None
            and "\\" not in value
            and not any(char.isspace() or ord(char) < 32 or ord(char) == 127
                or 0xD800 <= ord(char) <= 0xDFFF for char in value))
    except ValueError:
        return False


def project_service_receipt(name, payload, *, arguments=None):
    service = name.removeprefix("deepseek_")
    if (service not in {"search", "fetch", "ocr"} or not isinstance(payload, dict)
            or payload.get("provider") != "deepseek"
            or payload.get("integration") != "independent_tool_api"
            or payload.get("service") != service or payload.get("status") not in {"ok", "empty"}
            or payload.get("response_complete") is not True or not _hex(payload.get("call_id"), 32)
            or payload.get("error") or payload.get("execution_unknown")):
        return None
    if not isinstance(arguments, dict):
        return None
    # These records were created by the local source adapter after the provider
    # result; do not promote any provider-authored prose into process proof.
    raw_sources = payload.get("sources")
    if not isinstance(raw_sources, list) or len(raw_sources) > 50:
        return None
    sources = []
    for source in raw_sources:
        if (not isinstance(source, dict) or not _hex(source.get("revision_id"), 32)
                or not _hex(source.get("raw_sha256"), 64)
                or ("text_sha256" in source and not _hex(source["text_sha256"], 64))):
            return None
        sources.append({key: source[key] for key in ("revision_id", "raw_sha256", "text_sha256") if key in source})
        origin = source.get("origin_ref")
        if service in {"search", "fetch"}:
            if not _web_origin(origin):
                return None
            sources[-1]["url"] = origin
    result = {"protocol": "independent_service_process_v1", "provider": "deepseek", "service": service,
        "call_id": payload["call_id"], "response_complete": True, "status": payload["status"],
        "returned_sources": sources, "original_page_coverage_verified": False,
        "source_accuracy_verified": False, "model_understanding_verified": False,
        "whole_extracted_text_returned": False}
    if service == "fetch":
        if (payload.get("status") != "ok" or payload.get("result_type") != "web_extracted_text"
                or len(sources) != 1 or payload.get("returned_markdown_is_retained_raw") is not True
                or not _web_origin(arguments.get("url")) or sources[0]["url"] != arguments["url"]):
            return None
        result.update(returned_markdown_retained=True, coverage_scope="returned_web_text_only")
        title = payload.get("title")
        if isinstance(title, str):
            result["returned_metadata"] = {"title": title[:1024], "title_truncated": len(title) > 1024,
                "title_source": "provider_fetch_response_not_verified_html_title"}
    elif service == "search":
        rows = payload.get("results")
        count = arguments.get("count", 10)
        need_content = arguments.get("need_content", False)
        if (payload.get("result_type") != "search_results" or not isinstance(rows, list)
                or len(rows) > 50 or any(not isinstance(row, dict) for row in rows)
                or (payload["status"] == "empty") != (len(rows) == 0)
                or type(count) is not int or not 1 <= count <= 50 or len(rows) > count
                or type(need_content) is not bool or (not need_content and bool(sources))
                or not isinstance(arguments.get("query"), str) or not arguments["query"].strip()):
            return None
        row_sources = []
        for row in rows:
            if type(row.get("content_saved")) is not bool:
                return None
            if row["content_saved"]:
                source = row.get("source")
                if (not need_content or not isinstance(source, dict)
                        or row.get("url") != source.get("origin_ref")):
                    return None
                row_sources.append({key: source[key] for key in ("revision_id", "raw_sha256", "text_sha256") if key in source})
                row_sources[-1]["url"] = source.get("origin_ref")
            elif row.get("source") is not None:
                return None
        if row_sources != sources:
            return None
        result.update(returned_result_count=len(rows), summary_is_source_text=False,
            coverage_scope="returned_search_results_only")
    else:
        count, statuses = payload.get("page_count"), payload.get("page_statuses")
        if (payload.get("status") != "ok" or payload.get("result_type") != "ocr_document"
                or payload.get("truncated") is not False or type(count) is not int or not 1 <= count <= 1000
                or not isinstance(statuses, list) or len(statuses) != count or len(sources) != 1
                or not _hex(payload.get("original_revision_id"), 32)
                or not _hex(payload.get("original_sha256"), 64)
                or arguments.get("source_revision_id") != payload["original_revision_id"]
                or payload.get("purpose") not in {"text_recognition", "rich_layout_recognition"}
                or arguments.get("purpose", "text_recognition") != payload["purpose"]
                or payload["original_sha256"] != sources[0]["raw_sha256"]):
            return None
        if (any(not isinstance(page, dict) or type(page.get("index")) is not int
                or page.get("finish_reason") != "stop" or page.get("truncated") is not False for page in statuses)
                or sorted(page["index"] for page in statuses) != list(range(count))):
            return None
        result.update(purpose=payload["purpose"], returned_page_count=count, original_revision_id=payload["original_revision_id"],
            original_sha256=payload["original_sha256"], coordinates_mapped_to_original=False,
            coverage_scope="returned_ocr_text_only", ocr_text_accuracy_verified=False)
    return result
