"""Two real interpreter processes; no real provider/network or user data."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def worker(phase):
    sys.path.insert(0, str(ROOT / "backend"))
    import httpx
    from app.db import SessionLocal, init_db
    from app import model_capabilities as probes, model_profiles as profiles
    init_db()
    marker = Path(os.environ["DATA_DIR"]) / "synthetic-probe.json"
    if phase == "create":
        # Initial state simulates a process ending after persisting the upload
        # receipt. This process does not make even a synthetic HTTP request.
        with SessionLocal() as db:
            row = profiles.save_profile(db, {"name": "Synthetic restart probe", "base_url": "https://api.deepseek.com/v1",
                "model_id": "Case-Restart-Model", "api_key": "synthetic-restart-probe-key"})
            db.commit()
            profile_id = row.id
            values = probes._values(db, profile_id)
        diagnostic_id = "capability-" + "9" * 32
        preview = probes.preview(profile_id)
        probes._start(profile_id, "files", diagnostic_id)
        client = probes._Client(values, preview["expected_config"], diagnostic_id)
        client._synthetic = True
        probes._record_remote(client, {"id": "file-restart-synthetic", "state": "available", "requested_ttl_seconds": 3600})
        marker.write_text(json.dumps({"profile_id": profile_id, "diagnostic_id": diagnostic_id}), encoding="utf-8")
        return {"ok": True, "phase": phase, "provider_requests": 0, "process_id": os.getpid()}
    target = json.loads(marker.read_text(encoding="utf-8"))
    calls, original = [], httpx.Client
    def dispatch(request):
        calls.append({"method": request.method, "path": request.url.path})
        assert request.url.host == "api.deepseek.com" and request.method == "DELETE"
        assert request.url.path == "/files/file-restart-synthetic"
        return httpx.Response(200, json={"id": "file-restart-synthetic", "deleted": True})
    httpx.Client = lambda *a, **k: original(*a, **{**k, "transport": httpx.MockTransport(dispatch)})
    preview = probes.preview(target["profile_id"])
    assert not calls and preview["cleanup_operations"][0]["diagnostic_id"] == target["diagnostic_id"]
    assert preview["cleanup_operations"][0]["remote_file"]["state"] == "cleanup_pending"
    result = probes.cleanup(target["profile_id"], target["diagnostic_id"])
    assert result["remote_file"]["state"] == "deleted" and result["request_count"] == 1
    assert not probes.preview(target["profile_id"])["cleanup_operations"]
    return {"ok": True, "phase": phase, "process_id": os.getpid(), "real_provider_calls": 0,
            "synthetic_provider_calls": calls, "repeated_uploads": 0, "repeated_model_requests": 0}


def main():
    output = ROOT / "verification" / "model-capability-routing"
    output.mkdir(parents=True, exist_ok=True)
    runtime = ROOT / "verification" / "runtime"
    runtime.mkdir(exist_ok=True)
    data = tempfile.mkdtemp(prefix="capability-restart-", dir=runtime)
    env = {**os.environ, "DATA_DIR": data, "LLM_API_KEY": "", "LLM_DEMO_MODE": "0", "EMBEDDING_API_KEY": "",
           "REDIS_URL": "", "DESKTOP_NOTIFICATIONS": "false", "PYTHONIOENCODING": "utf-8"}
    phases = []
    for phase in ("create", "recover"):
        result = subprocess.run([sys.executable, str(Path(__file__)), phase], env=env, cwd=ROOT,
                                capture_output=True, text=True, encoding="utf-8", timeout=60)
        if result.returncode:
            raise RuntimeError("Synthetic restart verification failed: " + result.stderr[-3000:])
        phases.append(json.loads(result.stdout.strip().splitlines()[-1]))
    assert phases[0]["process_id"] != phases[1]["process_id"]
    report = {"ok": True, "scope": "REM-27 persisted diagnostic file cleanup after backend restart",
              "real_provider_calls": 0, "synthetic_provider_calls": 1, "data_directory": data, "phases": phases}
    (output / "restart-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS capability diagnostic restart; 0 real calls, 1 synthetic delete, no repeated paid work")


if __name__ == "__main__":
    if len(sys.argv) == 2:
        print(json.dumps(worker(sys.argv[1])))
    else:
        main()
