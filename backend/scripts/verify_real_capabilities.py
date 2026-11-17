"""Opt-in frozen-source capability diagnostics through the production HTTP API.

The default writes a plan: no app/config import, credential read, DB or network.
Real execution requires an explicit one-to-three item selection and a verified
code-only snapshot. Only the existing production probes generate test material.
This script never discovers models, retries a probe, or tests a user profile.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
PROBES = {"text": 1, "streaming": 1, "tools": 4, "vision": 1,
          "reasoning": 1, "structured": 1, "usage": 1, "native_tools": 2, "files": 3}
PUBLIC_RESULT = ("diagnostic_id", "profile_id", "profile_version", "model_id", "capability",
                 "status", "failure_kind", "endpoint", "protocol", "detail", "tested_at",
                 "request_count", "calls", "cost_summary", "observations", "remote_file")


def write_report(path, value, secrets=()):
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if any(secret and secret in payload for secret in secrets):
        raise ValueError("SecretRejectedFromReport")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.replace(path)


def source_hashes():
    files = sorted((BACKEND / "app").rglob("*.py")) + [Path(__file__).resolve()]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def verify_frozen_inputs(path):
    """Refuse unlisted app modules and altered snapshot files before key read."""
    path = Path(path).resolve()
    if path.parent != ROOT or path.name != "evaluation-inputs.json":
        raise ValueError("CodeOnlyEvaluationSnapshotRequired")
    value = json.loads(path.read_text(encoding="utf-8"))
    recorded = value.get("source_sha256")
    if (not isinstance(recorded, dict) or not recorded
            or value.get("credential_files_copied") != 0 or value.get("user_documents_copied") != 0):
        raise ValueError("InvalidCodeOnlyEvaluationSnapshot")
    current = source_hashes()
    if any(recorded.get(name) != digest for name, digest in current.items()):
        raise ValueError("UnfrozenCapabilityEvaluationSource")
    for name, digest in recorded.items():
        relative = Path(name)
        candidate = ROOT / relative
        if (relative.is_absolute() or ".." in relative.parts or not candidate.is_file()
                or candidate.is_symlink() or candidate.resolve().is_relative_to(ROOT) is False
                or any(p.is_symlink() or getattr(p, "is_junction", lambda: False)()
                       for p in [candidate, *candidate.parents] if p != ROOT and p.is_relative_to(ROOT))
                or hashlib.sha256(candidate.read_bytes()).hexdigest() != digest):
            raise ValueError("EvaluationSnapshotChecksumMismatch")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def model_configuration(directory, unprotect):
    """Read only config.json; a present protected field always wins or fails."""
    raw = (Path(directory) / "config.json").read_bytes()
    saved = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(saved, dict):
        raise ValueError("InvalidModelConfiguration")
    if "protected_credentials" in saved and not isinstance(saved["protected_credentials"], dict):
        raise ValueError("InvalidProtectedModelConfiguration")
    protected = saved.get("protected_credentials", {})
    if "llm_api_key" in protected:
        encoded = protected["llm_api_key"]
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("InvalidProtectedModelCredential")
        secret, storage = unprotect(encoded), "protected_configuration"
    else:
        secret = saved.get("llm_api_key")
        storage = "legacy_plaintext_read_only_memory_fallback_pending_migration"
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError("ConfiguredModelCredentialRequired")
    base, model = saved.get("llm_base_url"), saved.get("llm_model")
    if (not isinstance(base, str) or not isinstance(model, str) or not model.strip()
            or any(ord(c) < 32 for c in base + model)):
        raise ValueError("ConfiguredModelTargetRequired")
    target = urlsplit(base)
    if (target.scheme != "https" or not target.hostname or target.username or target.password
            or target.query or target.fragment or target.port not in (None, 443)):
        raise ValueError("ExplicitHTTPSModelTargetRequired")
    return {"api_key": secret, "base_url": base, "model_id": model,
            "storage_kind": storage, "config_sha256": hashlib.sha256(raw).hexdigest()}


class HTTPBudget:
    """Count actual sync transport attempts, including file upload/deletion.

    No transport is replaced by a mock here. The wrapped production transport
    sends exactly the request built by the production adapter. Unknown origins,
    routes, async requests and an exhausted budget are rejected before network.
    """
    def __init__(self, base_url, maximum, authorization_check=None):
        if type(maximum) is not int or not 1 <= maximum <= 16:
            raise ValueError("HTTPBudgetOutOfBounds")
        target = urlsplit(base_url)
        self.host = target.hostname
        self.chat_path = target.path.rstrip("/") + "/chat/completions"
        self.maximum, self.sent, self.blocked = maximum, [], []
        self.lock, self.stopped = threading.Lock(), threading.Event()
        self.authorization_check = authorization_check

    def admit(self, request):
        url = request.url
        route = (request.method == "POST" and url.path == self.chat_path)
        if self.host == "api.deepseek.com":
            route = route or (request.method == "POST" and url.path == "/files") or (
                request.method == "DELETE" and re.fullmatch(r"/files/file-[A-Za-z0-9_-]{1,200}", url.path))
        with self.lock:
            if (url.scheme != "https" or url.host != self.host or url.port not in (None, 443)
                    or url.query or not route):
                self.blocked.append("unconfigured_route")
                raise RuntimeError("UnconfiguredHTTPRouteBlocked")
            if self.stopped.is_set():
                self.blocked.append("cancelled")
                raise RuntimeError("EvaluationCancelledBeforeHTTP")
            if self.authorization_check is not None and self.authorization_check() is not True:
                self.blocked.append("original_configuration_changed")
                raise RuntimeError("OriginalConfigurationChangedBeforeHTTP")
            if len(self.sent) >= self.maximum:
                self.blocked.append("budget")
                raise RuntimeError("EvaluationHTTPBudgetExceeded")
            self.sent.append({"method": request.method, "host": url.host, "path": url.path})

    @contextmanager
    def installed(self):
        import httpx
        original, async_original = httpx.HTTPTransport.handle_request, httpx.AsyncHTTPTransport.handle_async_request
        def counted(transport, request):
            self.admit(request)
            return original(transport, request)
        async def no_async(transport, request):
            raise RuntimeError("UnexpectedAsyncEvaluationHTTPBlocked")
        httpx.HTTPTransport.handle_request = counted
        httpx.AsyncHTTPTransport.handle_async_request = no_async
        try:
            yield self
        finally:
            httpx.HTTPTransport.handle_request = original
            httpx.AsyncHTTPTransport.handle_async_request = async_original


def api_probe(client, profile_id, capability, budget, *, timeout_seconds, cancel_file=None):
    """Use actual preview/run/cancel routes. A cancellation never starts a retry."""
    prefix = f"/api/model-profiles/{profile_id}/capabilities"
    response = client.get(prefix)
    if response.status_code != 200:
        raise RuntimeError("CapabilityPreviewRejected")
    preview = response.json()
    if {p["capability"]: p["max_requests"] for p in preview["probes"]} != PROBES:
        raise RuntimeError("ProductionProbeContractChanged")
    diagnostic_id = "capability-" + uuid4().hex
    output = {"capability": capability, "diagnostic_id": diagnostic_id}
    if cancel_file is not None and Path(cancel_file).exists():
        budget.stopped.set()
        cancelled = client.post(prefix + f"/{diagnostic_id}/cancel")
        output["controller"] = {"reason": "cancel_file_before_start", "cancel_http": cancelled.status_code,
                                "cancel": cancelled.json() if cancelled.status_code == 200 else None}
    finished = threading.Event()
    def invoke():
        try:
            result = client.post(prefix, json={"capability": capability,
                "expected_config": preview["expected_config"], "diagnostic_id": diagnostic_id})
            output["http_status"] = result.status_code
            if result.status_code == 200:
                output["result"] = {k: v for k, v in result.json().items() if k in PUBLIC_RESULT}
        except Exception as exc:
            output["error_kind"] = type(exc).__name__
        finally:
            finished.set()
    worker = threading.Thread(target=invoke, name="fixed-capability-probe", daemon=True)
    worker.start()
    began = time.monotonic()
    reason = None
    try:
        while not finished.wait(0.05):
            if cancel_file is not None and Path(cancel_file).exists():
                reason = "cancel_file"
                break
            if time.monotonic() - began >= timeout_seconds:
                reason = "deadline"
                break
    except KeyboardInterrupt:
        reason = "keyboard_interrupt"
    if reason:
        budget.stopped.set()
        cancelled = client.post(prefix + f"/{diagnostic_id}/cancel")
        output["controller"] = {"reason": reason, "cancel_http": cancelled.status_code,
                                "cancel": cancelled.json() if cancelled.status_code == 200 else None}
        if not finished.wait(35):
            # Keep the directory/remote identity. No further probes or cleanup
            # are dispatched; the invoking process must be allowed to finish.
            raise RuntimeError("CancelledProbeStillFinishing")
    output["elapsed_seconds"] = round(time.monotonic() - began, 3)
    return output


def isolation_environment(directory, base, model):
    directory = Path(directory)
    return {"DATA_DIR": str(directory / "data"), "APPDATA": str(directory / "appdata"),
            "LOCALAPPDATA": str(directory / "localappdata"), "DB_NAME": "capabilities.db",
            "VECTOR_NAME": "capabilities.npy", "LLM_API_KEY": "", "LLM_BASE_URL": base,
            "LLM_MODEL": model, "LLM_DEMO_MODE": "0", "EMBEDDING_API_KEY": "",
            "EMBEDDING_BASE_URL": "", "REDIS_URL": "", "DESKTOP_NOTIFICATIONS": "false",
            "MEMORY_CAPTURE": "false", "MEMORY_RECALL": "false", "SCHOLARFLOW_TEST_TRANSPORT": "1"}


def run_real(args):
    # This guard also prevents a caller from importing this helper into a live
    # user backend and accidentally reusing its already-open DB/config objects.
    if any(name in sys.modules for name in ("app.config", "app.db", "app.model_profiles")):
        raise RuntimeError("FreshIsolatedInterpreterRequired")
    frozen = verify_frozen_inputs(args.frozen_inputs)
    sys.path.insert(0, str(BACKEND))
    from app.credential_protection import unprotect
    configuration = model_configuration(args.credentials_dir, unprotect)
    secret, base, model = (configuration[k] for k in ("api_key", "base_url", "model_id"))
    from app.model_connection import normalize_base_url, single_line
    base, model, secret = normalize_base_url(base), single_line(model, "模型ID"), single_line(secret, "API Key")
    before = source_hashes()
    if verify_frozen_inputs(args.frozen_inputs) != frozen:
        raise RuntimeError("SourceSnapshotChangedBeforeEvaluation")
    runtime_parent = Path(args.runtime_parent).resolve()
    runtime_parent.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="fixed-capability-real-", dir=runtime_parent))
    (runtime / "evaluation-owner.json").write_text(json.dumps({"pid": os.getpid(), "token": uuid4().hex,
        "kind": "isolated_capability_diagnostics", "synthetic_material_only": True}), encoding="utf-8")
    os.environ.update(isolation_environment(runtime, base, model))
    report = {"kind": "fixed_source_real_capability_diagnostics", "commercial_acceptance": False,
        "credentials_read": True, "credential_storage_kind": configuration["storage_kind"],
        "frozen_manifest_sha256": frozen,
        "model_id": model, "base_url": base, "selected_capabilities": args.capability,
        "maximum_http_requests": args.max_http, "synthetic_material_only": True,
        "private_documents_sent": False, "user_profiles_tested": False, "runtime_directory": str(runtime),
        "runtime_retained": True, "source_hashes_at_start": before, "probes": [], "confirmed_all_selected": False,
        "real_provider_calls": 0, "execution_status": "starting",
        "cost_scope": "Observed production ledger only; native service charges or absent usage may remain unknown.",
        "file_ttl_note": "TTL is requested, not proof of remote expiry. Unknown upload results are not retried.",
        "runtime_note": "Contains only isolated diagnostic state and OS-protected copied credential; retain for pending remote file identity/cleanup."}
    write_report(args.report, report, (secret,))
    def original_configuration_unchanged():
        try:
            return hashlib.sha256((Path(args.credentials_dir) / "config.json").read_bytes()).hexdigest() == configuration["config_sha256"]
        except OSError:
            return False
    budget, started = HTTPBudget(base, args.max_http, original_configuration_unchanged), time.monotonic()
    engine = None
    try:
        # Load only the production router; do not launch the full desktop
        # scheduler, background jobs, tracking, memory or user data lifecycle.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.model_profiles import router
        from app.db import init_db, engine, SessionLocal
        from app.call_ledger import list_calls, summarize_calls
        from app.config import DATA_DIR, settings
        init_db()
        if DATA_DIR.resolve() != (runtime / "data").resolve() or settings.llm_api_key or settings.embedding_api_key:
            raise RuntimeError("DiagnosticIsolationFailed")
        application = FastAPI()
        application.include_router(router)
        with budget.installed(), TestClient(application) as client:
            created = client.post("/api/model-profiles", json={"name": "Isolated fixed-source real capability diagnostics",
                "base_url": base, "model_id": model, "api_key": secret, "reasoning_effort": ""})
            if created.status_code != 200:
                raise RuntimeError("IsolatedProtectedProfileCreationRejected")
            profile_id = created.json()["id"]
            report["isolated_profile_id"] = profile_id
            report["execution_status"] = "running"
            write_report(args.report, report, (secret,))
            for capability in args.capability:
                entry = api_probe(client, profile_id, capability, budget,
                    timeout_seconds=args.timeout_seconds, cancel_file=args.cancel_file)
                report["probes"].append(entry)
                report["http_dispatches"] = list(budget.sent)
                write_report(args.report, report, (secret,))
                result = entry.get("result", {})
                if (entry.get("controller") or budget.blocked or result.get("failure_kind") in
                        ("authentication", "balance", "rate_limit", "configuration_changed")):
                    break
            preview = client.get(f"/api/model-profiles/{profile_id}/capabilities")
            if preview.status_code != 200:
                raise RuntimeError("FinalCapabilityPreviewRejected")
            report["pending_file_cleanup"] = preview.json()["cleanup_operations"]
            calls = list_calls(limit=100, include_synthetic=True)["items"]
            report["calls"] = calls
            report["cost_summary"] = summarize_calls(include_synthetic=True)
            report["ledger_matches_dispatches"] = len(calls) == len(budget.sent)
            report["ledger_has_no_synthetic_calls"] = all(not c["synthetic"] for c in calls)
            report["all_dispatched_calls_completed"] = bool(calls) and all(c["state"] == "completed" for c in calls)
            chats = [c for c in calls if c["service"] in ("chat", "chat_stream")]
            report["exact_model_on_chat_receipts"] = bool(chats) and all(c["model_id"] == model for c in chats)
            report["confirmed_all_selected"] = (len(report["probes"]) == len(args.capability)
                and all(e.get("http_status") == 200 and e.get("result", {}).get("status") == "supported"
                        and not e.get("controller") for e in report["probes"]))
    except Exception as exc:
        report["error_kind"] = type(exc).__name__
    finally:
        if engine is not None:
            engine.dispose()
    report.update(real_provider_calls=len(budget.sent), http_dispatches=budget.sent,
        blocked_http=budget.blocked, elapsed_seconds=round(time.monotonic() - started, 3),
        source_hashes_at_finish=source_hashes())
    report["source_unchanged_during_evaluation"] = before == report["source_hashes_at_finish"]
    source_config = Path(args.credentials_dir) / "config.json"
    try:
        report["source_config_unchanged"] = hashlib.sha256(source_config.read_bytes()).hexdigest() == configuration["config_sha256"]
    except OSError:
        report["source_config_unchanged"] = False
        report["source_config_recheck"] = "unavailable"
    report["passed_selected_diagnostics"] = (report["confirmed_all_selected"] and not report.get("error_kind")
        and report.get("ledger_matches_dispatches") and report.get("ledger_has_no_synthetic_calls")
        and report.get("all_dispatched_calls_completed")
        and report.get("exact_model_on_chat_receipts") and not report.get("pending_file_cleanup")
        and report["source_config_unchanged"] and report["source_unchanged_during_evaluation"])
    report["execution_status"] = "confirmed" if report["passed_selected_diagnostics"] else "not_fully_confirmed"
    write_report(args.report, report, (secret,))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-real", action="store_true")
    parser.add_argument("--capability", action="append", choices=list(PROBES))
    parser.add_argument("--credentials-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--frozen-inputs", type=Path, default=ROOT / "evaluation-inputs.json")
    parser.add_argument("--runtime-parent", type=Path)
    parser.add_argument("--max-http", type=int, default=16)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--cancel-file", type=Path)
    args = parser.parse_args(argv)
    selected = args.capability or list(PROBES)
    if not 1 <= args.max_http <= 16 or not 30 <= args.timeout_seconds <= 300:
        parser.error("HTTP budget must be 1..16; each capability deadline must be 30..300 seconds")
    if len(selected) != len(set(selected)):
        parser.error("Select each capability only once")
    if args.credentials_dir:
        protected = args.credentials_dir.resolve()
        paths = [args.report, args.runtime_parent, args.cancel_file]
        if any(p and p.resolve().is_relative_to(protected) for p in paths):
            parser.error("Evaluation outputs must be outside the original credential directory")
    if not args.run_real:
        value = {"kind": "real_capability_diagnostics_plan", "execution_status": "not_run",
            "credentials_read": False, "real_provider_calls": 0, "commercial_acceptance": False,
            "maximum_http_requests": args.max_http, "maximum_selected_in_real_mode": 3,
            "probes": [{"capability": name, "max_requests": PROBES[name]} for name in selected],
            "requires": ["explicit --run-real", "one to three explicit --capability values",
                "verified code-only evaluation-inputs.json", "isolated protected profile and database"],
            "source_hashes": source_hashes()}
        write_report(args.report, value)
        return value
    if not args.credentials_dir or not args.capability or not 1 <= len(selected) <= 3:
        parser.error("Real mode requires --credentials-dir and one to three explicit --capability selections")
    if sum(PROBES[name] for name in selected) > args.max_http:
        parser.error("HTTP budget is below the selected production probe maximum, including normal file deletion")
    args.runtime_parent = args.runtime_parent or args.report.resolve().parent / "runtime"
    if args.runtime_parent.resolve().is_relative_to(args.credentials_dir.resolve()):
        parser.error("Isolated runtime must be outside original credential storage")
    report = run_real(args)
    print(json.dumps({"passed_selected_diagnostics": report["passed_selected_diagnostics"],
        "real_provider_calls": report["real_provider_calls"], "report": str(args.report.resolve()),
        "commercial_acceptance": False}, ensure_ascii=False))
    if not report["passed_selected_diagnostics"]:
        raise SystemExit(1)
    return report


if __name__ == "__main__":
    main()
