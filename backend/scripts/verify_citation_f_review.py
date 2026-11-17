"""Opt-in, at most four HTTP requests to review the two public F final drafts.

Default writes a plan without importing application configuration, reading
credentials or contacting a provider. Execution rebuilds fabricated sources and
actual successful local tool receipts in isolated storage; it never reruns the
agent or stores raw provider responses/private reasoning.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
from scripts.verify_real_acceptance import (DEFAULT_MANIFEST, fixture_bytes, load_manifest,
    model_configuration, public_review, source_hashes, write_report)

FIXTURE = BACKEND / "tests/fixtures/citation-f-public-drafts.json"
CASE_FILES = {"DOC05": ["budget.xlsx"], "DOC12": ["alpha-a.md", "alpha-b.md"]}


def inputs(case_ids):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    manifest = load_manifest(DEFAULT_MANIFEST)
    drafts = {case["id"]: case["drafts"][-1] for case in fixture["cases"]}
    definitions = {case["id"]: case for case in manifest["cases"]}
    return [{"id": cid, "question": definitions[cid]["prompts"][0], "answer": drafts[cid],
             "draft_sha256": hashlib.sha256(drafts[cid].encode()).hexdigest(), "files": CASE_FILES[cid]}
            for cid in case_ids]


def review_hashes():
    result = source_hashes()
    for path in (Path(__file__).resolve(), FIXTURE):
        result[path.relative_to(BACKEND).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def public_claims(case):
    # citation.py is a pure parser (stdlib only); default plans still do not
    # import configuration/storage or read credentials.
    from app.rag.citation import parse_claims
    return [{"text": claim["claim"], "evidence_ids": claim["evidence_ids"]}
        for claim in parse_claims(case["answer"], ["e1", "e2"])
        if claim["evidence_ids"]]


def estimated_requests(cases):
    # One scope request plus one bounded audit per four actual cited claims.
    # The default paid cap remains explicit; a short cap is never advertised
    # as covering all claims after the parser starts retaining more content.
    return {case["id"]: 1 + (len(public_claims(case)) + 3) // 4 for case in cases}


def rebuild_case(client, case):
    """Recreate actual source-tool receipts, never borrow F's claimed actions."""
    from app.db import SessionLocal
    from app.model_profiles import capture_run_config
    from app.runtime_context import bind_runtime_config
    from app.source_runtime import freeze_sources
    from app.source_tools import execute_source_tool
    from app.agent.citation_scope import process_receipts

    created = client.post("/api/conversations", json={"title": "F citation review " + case["id"]})
    if created.status_code != 200:
        raise RuntimeError("FixtureConversationFailed")
    cid = created.json()["id"]
    revisions, materials = {}, {}
    for name in case["files"]:
        raw = fixture_bytes(name)
        materials[name] = hashlib.sha256(raw).hexdigest()
        uploaded = client.post("/api/sources/upload", files={"file": (name, raw)},
            data={"conversation_id": cid, "request_id": "f_review_" + uuid4().hex})
        if uploaded.status_code != 200:
            raise RuntimeError("FixtureUploadFailed")
        revisions[name] = uploaded.json()["revision_id"]
    history, evidence = [], []
    with SessionLocal() as db:
        binding = freeze_sources(db, cid, list(revisions.values()), {
            **capture_run_config(db, cid), "conversation_id": cid})
        db.commit()
        operations = ([
            ("read_attached_source", {"source_revision_id": revisions["budget.xlsx"], "sheet": "Budget", "cell_range": "A1:C4"}),
            ("verify_attached_table", {"source_revision_id": revisions["budget.xlsx"], "sheet": "Budget", "cell_range": "B2:B4"}),
        ] if case["id"] == "DOC05" else [
            ("read_attached_source", {"source_revision_id": revisions[name], "line_start": 1, "line_end": 3})
            for name in case["files"]])
        with bind_runtime_config(binding):
            for number, (tool, arguments) in enumerate(operations, 1):
                payload = json.loads(execute_source_tool(db, tool, arguments, conversation_id=cid))
                if payload.get("error") or not payload.get("evidence"):
                    raise RuntimeError("FixtureActualToolFailed")
                call_id = f"f_review_{number}"
                history += [{"role": "assistant", "tool_calls": [{"id": call_id, "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(arguments)}}]},
                    {"role": "tool", "tool_call_id": call_id, "content": json.dumps(payload, ensure_ascii=False)}]
                if len(payload["evidence"]) != 1:
                    raise RuntimeError("FixtureEvidenceMappingChanged")
                evidence.append({**payload["evidence"][0], "id": f"e{number}"})
        db.commit()
    receipts, truncated = process_receipts(history)
    if truncated or len(receipts) != 2:
        raise RuntimeError("FixtureProcessReceiptsIncomplete")
    return {**case, "conversation_id": cid, "binding": binding, "history": history,
        "evidence": evidence, "process_receipts": receipts, "fixture_sha256": materials}


def review_case(bundle, model, *, claim_range=None):
    from app.agent.audit import audit_answer, review_citation_scope
    from app.db import SessionLocal
    from app.rag.citation import citation_gate
    from app.runtime_context import bind_runtime_config

    with SessionLocal() as db, bind_runtime_config(bundle["binding"]):
        if claim_range is not None:
            from app.agent.audit import _audit_batch
            from app.agent.citation_scope import process_receipts, verified_source_metadata
            start, end = claim_range
            claims = public_claims(bundle)
            if bundle["id"] != "DOC05" or not 0 <= start < end <= len(claims) or end - start > 4:
                raise ValueError("InvalidPublicClaimSubrange")
            metadata = verified_source_metadata(db, bundle["evidence"], bundle["conversation_id"])
            receipts, truncated = process_receipts(bundle["history"])
            if truncated:
                raise ValueError("PublicClaimProcessContextTruncated")
            audit = _audit_batch(claims[start:end], {entry["id"]: entry for entry in bundle["evidence"]},
                llm=model, metadata=metadata, receipts=receipts, answer=bundle["answer"], claim_offset=start)
            return {"not_whole_answer_acceptance": True, "claim_range": {"start": start, "end_exclusive": end},
                "selected_claims": [{"global_index": index, **claims[index]} for index in range(start, end)],
                "audit": public_review(audit), "selected_audit_passed": bool(audit.get("passed") and audit.get("verified")),
                "machine_review_passed": False}
        gate = citation_gate(bundle["answer"], [entry["id"] for entry in bundle["evidence"]])
        scoped = review_citation_scope(bundle["question"], bundle["answer"], bundle["evidence"],
            gate, model, messages=bundle["history"])
        audit = audit_answer(db, bundle["answer"], bundle["evidence"], llm=model,
            messages=bundle["history"], conversation_id=bundle["conversation_id"])
    return {"mechanical_gate": public_review(gate), "scope_gate": public_review(scoped),
        "audit": public_review(audit), "machine_review_passed": bool(scoped.get("passed")
            and audit.get("passed") and audit.get("verified"))}


def execute(args, cases):
    began = time.monotonic()
    claim_range = (args.claim_start, args.claim_end) if args.claim_start is not None else None
    report = {"kind": "real_f_citation_review", "real_provider_calls": 0, "metered_model_attempts": 0,
        "maximum_real_requests": 1 if claim_range else min(args.max_calls, sum(estimated_requests(cases).values())), "fabricated_material_only": True,
        "private_documents_sent": False, "semantic_acceptance": "pending_independent_human_review",
        "commercial_acceptance": False, "passed": False, "cases": [],
        "source_hashes_at_start": review_hashes(),
        "reconstruction": "Preserved public F final answers; freshly fixed equivalent fabricated sources and actual successful local receipts. No agent replay."}
    if claim_range:
        report.update(kind="public_claim_subrange_diagnostic", not_whole_answer_acceptance=True,
            claim_range={"start": claim_range[0], "end_exclusive": claim_range[1]})
    secret = None
    with tempfile.TemporaryDirectory(prefix="sf_citation_review_") as temporary:
        isolated = Path(temporary)
        os.environ.update(DATA_DIR=str(isolated / "data"), APPDATA=str(isolated / "appdata"),
            LOCALAPPDATA=str(isolated / "localappdata"), LLM_API_KEY="", LLM_DEMO_MODE="0",
            EMBEDDING_API_KEY="", REDIS_URL="", DESKTOP_NOTIFICATIONS="false", MEMORY_CAPTURE="false",
            SCHOLARFLOW_TEST_TRANSPORT="1")
        from app.credential_protection import unprotect
        configuration = model_configuration(args.credentials_dir, unprotect)
        secret, base, model_id = (configuration[key] for key in ("api_key", "base_url", "model_id"))
        parsed = urlsplit(base)
        if parsed.scheme != "https" or parsed.hostname != "api.deepseek.com" or not model_id:
            raise ValueError("ConfiguredDeepSeekHTTPSModelRequired")
        report.update(provider=parsed.hostname, model_id=model_id,
            credential_storage_kind=configuration["storage_kind"])
        from app.main import app
        from app import llm, model_profiles
        from app.call_ledger import summarize_calls
        from app.config import settings
        from app.db import SessionLocal, engine
        from fastapi.testclient import TestClient
        import httpx

        settings.reasoning_effort = ""
        original_meter = llm.APIClient._meter
        original_transport = httpx.HTTPTransport.handle_request
        attempts, dispatched = 0, 0

        @contextmanager
        def limited_meter(client, *values, **kwargs):
            nonlocal attempts
            if attempts >= report["maximum_real_requests"] or time.monotonic() - began > args.timeout_seconds:
                raise RuntimeError("EvaluationRequestBudgetExceeded")
            attempts += 1
            with original_meter(client, *values, **kwargs) as receipt:
                yield receipt

        def counted_transport(transport, request):
            nonlocal dispatched
            if (request.url.scheme != "https" or request.url.host != parsed.hostname
                    or request.method != "POST" or request.url.path not in ("/chat/completions", "/v1/chat/completions")):
                raise RuntimeError("UnconfiguredExternalRequestBlocked")
            if dispatched >= report["maximum_real_requests"] or time.monotonic() - began > args.timeout_seconds:
                raise RuntimeError("EvaluationHTTPBudgetExceeded")
            dispatched += 1
            return original_transport(transport, request)

        llm.APIClient._meter = limited_meter
        httpx.HTTPTransport.handle_request = counted_transport
        try:
            with TestClient(app) as client:
                with SessionLocal() as db:
                    profile = model_profiles.save_profile(db, {"name": "Isolated F citation review", "base_url": base,
                        "model_id": model_id, "api_key": secret})
                    model_profiles.set_default(db, profile.id)
                    db.commit()
                for case in cases:
                    before, entry = dispatched, {"id": case["id"], "draft_sha256": case["draft_sha256"]}
                    try:
                        bundle = rebuild_case(client, case)
                        entry.update(fixture_sha256=bundle["fixture_sha256"],
                            process_receipts=bundle["process_receipts"])
                        if claim_range:
                            report["active_dispatch"] = {"state": "pending", "case": case["id"],
                                "claim_range": report["claim_range"]}
                            write_report(args.report, report, (secret,))
                        entry.update(review_case(bundle, llm.APIClient(timeout=min(90, args.timeout_seconds),
                            runtime_config=bundle["binding"]), claim_range=claim_range))
                        if claim_range:
                            report["active_dispatch"]["state"] = "returned"
                            entry["status"] = ("selected_claims_passed" if entry["selected_audit_passed"] else
                                "selected_claims_failed" if entry["audit"].get("known_failed_count") else "selected_claims_unverified")
                        else:
                            entry["status"] = "needs_semantic_review" if entry["machine_review_passed"] else "machine_failed"
                    except Exception as exc:
                        entry.update(status="execution_failed", error_kind=type(exc).__name__)
                    entry["real_requests"] = dispatched - before
                    report["cases"].append(entry)
                    report.update(real_provider_calls=dispatched, metered_model_attempts=attempts,
                                  usage=summarize_calls())
                    write_report(args.report, report, (secret,))
        except Exception as exc:
            report.update(status="execution_failed", error_kind=type(exc).__name__)
        finally:
            report.update(real_provider_calls=dispatched, metered_model_attempts=attempts, usage=summarize_calls())
            llm.APIClient._meter = original_meter
            httpx.HTTPTransport.handle_request = original_transport
            engine.dispose()
    report.update(elapsed_seconds=round(time.monotonic() - began, 2), source_hashes_at_finish=review_hashes())
    report["source_unchanged_during_evaluation"] = report["source_hashes_at_start"] == report["source_hashes_at_finish"]
    report["machine_review_passed"] = (len(report["cases"]) == len(cases)
        and all(case.get("machine_review_passed") for case in report["cases"]) and report["source_unchanged_during_evaluation"])
    if claim_range:
        report["selected_audit_passed"] = (len(report["cases"]) == 1
            and report["cases"][0].get("selected_audit_passed", False) and report["source_unchanged_during_evaluation"])
    write_report(args.report, report, (secret,))
    print(json.dumps({"real_provider_calls": report["real_provider_calls"],
        "case_statuses": {case["id"]: case["status"] for case in report["cases"]},
        "commercial_acceptance": False}, ensure_ascii=False))
    return 0 if report.get("selected_audit_passed") or report["machine_review_passed"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--case", choices=tuple(CASE_FILES), action="append")
    parser.add_argument("--run-real", action="store_true")
    parser.add_argument("--credentials-dir", type=Path)
    parser.add_argument("--max-calls", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    parser.add_argument("--claim-start", type=int)
    parser.add_argument("--claim-end", type=int, help="Exclusive global claim index; opt-in diagnostic only")
    args = parser.parse_args(argv)
    if not 1 <= args.max_calls <= 4 or not 15 <= args.timeout_seconds <= 600:
        parser.error("1..4 calls and 15..600 seconds required")
    case_ids = args.case or list(CASE_FILES)
    if len(case_ids) != len(set(case_ids)):
        parser.error("Duplicate case selection")
    cases = inputs(case_ids)
    claim_range = None
    if args.claim_start is not None or args.claim_end is not None:
        if args.claim_start is None or args.claim_end is None or case_ids != ["DOC05"]:
            parser.error("Public subrange diagnostics require only DOC05 and both global bounds")
        claims = public_claims(cases[0])
        if not 0 <= args.claim_start < args.claim_end <= len(claims) or args.claim_end - args.claim_start > 4:
            parser.error("Public subrange must contain 1..4 actual DOC05 claims")
        claim_range = (args.claim_start, args.claim_end)
    if not args.run_real:
        estimates = estimated_requests(cases)
        value = {"kind": "f_citation_review_plan", "credentials_read": False, "real_provider_calls": 0,
            "execution_status": "not_run", "maximum_real_requests": min(args.max_calls, sum(estimates.values())),
            "estimated_requests_per_case": estimates,
            "budget_covers_all_selected_cases": args.max_calls >= sum(estimates.values()),
            "cases": [{key: case[key] for key in ("id", "files", "draft_sha256")} for case in cases],
            "source_hashes": review_hashes(), "commercial_acceptance": False}
        if claim_range:
            value.update(kind="public_claim_subrange_diagnostic_plan", maximum_real_requests=1,
                not_whole_answer_acceptance=True, estimated_requests_per_case={"DOC05": 1},
                budget_covers_all_selected_cases=True,
                claim_range={"start": claim_range[0], "end_exclusive": claim_range[1]},
                selected_claims=[{"global_index": index, **claims[index]} for index in range(*claim_range)])
        write_report(args.report, value)
        print(json.dumps({"kind": value["kind"], "real_provider_calls": 0, "credentials_read": False}))
        return 0
    if not args.credentials_dir:
        parser.error("Explicit authorized configuration, 1..4 calls and 15..600 seconds required")
    return execute(args, cases)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_kind": type(exc).__name__}))
        raise SystemExit(1)
