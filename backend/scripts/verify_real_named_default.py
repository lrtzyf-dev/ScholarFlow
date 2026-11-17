"""Opt-in, one-request named-default smoke using only synthetic text."""
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-real", action="store_true", required=True)
    parser.add_argument("--credentials-dir", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(backend))
    from app.credential_protection import unprotect
    saved = json.loads((Path(args.credentials_dir) / "config.json").read_text(encoding="utf-8"))
    protected = (saved.get("protected_credentials") or {}).get("llm_api_key")
    key = unprotect(protected) if protected else saved.get("llm_api_key", "")
    base, model = saved.get("llm_base_url", ""), saved.get("llm_model", "")
    if not key or urlsplit(base).scheme != "https" or urlsplit(base).hostname != "api.deepseek.com":
        raise RuntimeError("ConfiguredOfficialDeepSeekCredentialRequired")
    files = ["app/llm.py", "app/model_profiles.py", "app/main.py", "app/translate/translator.py",
             "scripts/verify_real_named_default.py"]
    def hashes():
        return {name: hashlib.sha256((backend / name).read_bytes()).hexdigest() for name in files}
    report = {"kind": "named_default_real_smoke", "maximum_real_requests": 1,
              "synthetic_material_only": True, "private_documents_sent": False,
              "provider": "api.deepseek.com", "model_id": model, "source_hashes_at_start": hashes()}
    sent = 0
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="scholarflow_named_default_eval_") as temporary:
        os.environ.update(DATA_DIR=str(Path(temporary) / "data"), APPDATA=str(Path(temporary) / "appdata"),
                          LOCALAPPDATA=str(Path(temporary) / "localappdata"),
                          LLM_API_KEY="", LLM_DEMO_MODE="0", LLM_BASE_URL=base, LLM_MODEL=model,
                          EMBEDDING_API_KEY="", REDIS_URL="", DESKTOP_NOTIFICATIONS="false",
                          MEMORY_CAPTURE="false", SCHOLARFLOW_TEST_TRANSPORT="1")
        from app.main import app
        from app import llm, model_profiles
        from app.config import settings
        from app.call_ledger import list_calls, summarize_calls
        from app.db import SessionLocal, engine
        from fastapi.testclient import TestClient
        original_meter = llm.APIClient._meter
        @contextmanager
        def limited_meter(self, *values, **kwargs):
            nonlocal sent
            if sent >= 1:
                raise RuntimeError("EvaluationRequestBudgetExceeded")
            sent += 1
            self._timeout = 45
            with original_meter(self, *values, **kwargs) as receipt:
                yield receipt
        llm.APIClient._meter = limited_meter
        try:
            with TestClient(app) as client:
                with SessionLocal() as db:
                    profile = model_profiles.save_profile(db, {"name": "Synthetic named default acceptance",
                        "base_url": base, "model_id": model, "api_key": key, "reasoning_effort": ""})
                    model_profiles.set_default(db, profile.id)
                    db.commit()
                health = client.get("/api/health").json()
                response = client.post("/api/translate", json={"text": "这是一个合成测试句子。"})
                body = response.json()
                calls = list_calls()["items"]
                checks = {"legacy_key_absent": not settings.llm_api_key,
                          "health_uses_named_model": health.get("llm_mode") == "api" and health.get("llm_model") == model,
                          "http_ok": response.status_code == 200,
                          "actual_translation": body.get("mode") == "llm" and bool(body.get("translation", "").strip()),
                          "one_metered_call": sent == len(calls) == 1,
                          "configured_model_used": bool(calls) and calls[0]["model_id"] == model,
                          "call_completed": bool(calls) and calls[0]["state"] == "completed"}
                report.update(checks=checks, passed=all(checks.values()), translation=body.get("translation"),
                              usage=summarize_calls())
        except Exception as exc:
            report.update(passed=False, error_kind=type(exc).__name__)
        finally:
            llm.APIClient._meter = original_meter
            engine.dispose()
    report.update(real_requests=sent, elapsed_seconds=round(time.monotonic() - started, 2),
                  source_hashes_at_finish=hashes())
    report["source_unchanged_during_evaluation"] = report["source_hashes_at_start"] == report["source_hashes_at_finish"]
    report["passed"] = report["passed"] and report["source_unchanged_during_evaluation"]
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if key in payload:
        raise RuntimeError("SecretRejectedFromReport")
    output = Path(args.report).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "real_requests": sent, "checks": report.get("checks"),
                      "report": str(output)}, ensure_ascii=False), flush=True)
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
