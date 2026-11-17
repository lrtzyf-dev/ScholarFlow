"""Runner mechanics only. All provider HTTP in these tests is synthetic."""
import json
from pathlib import Path
import subprocess
import sys
import time

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts import verify_real_capabilities as runner
from tests.test_model_capability_matrix_routing import target, official, response, stream, file_handler  # noqa: F401


def test_default_plan_reads_no_credential_config_or_network(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        pytest.fail("Plan accessed credentials or network")
    monkeypatch.setattr(runner, "model_configuration", forbidden)
    monkeypatch.setattr(runner, "verify_frozen_inputs", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    destination = tmp_path / "plan.json"
    value = runner.main(["--report", str(destination), "--credentials-dir", str(tmp_path / "absent")])
    assert value["credentials_read"] is False and value["real_provider_calls"] == 0
    assert value["commercial_acceptance"] is False and len(value["probes"]) == 9
    assert json.loads(destination.read_text(encoding="utf-8")) == value


def test_default_plan_in_fresh_interpreter_imports_no_application_or_credential_code(tmp_path):
    destination = tmp_path / "fresh-plan.json"
    program = """
import importlib.util, json, sys
from pathlib import Path
script, report, credentials = sys.argv[1:]
def audited(name, args):
    if name in ('socket.connect', 'socket.getaddrinfo'):
        raise AssertionError('Plan attempted network')
    if name == 'open' and isinstance(args[0], (str, bytes)) and Path(args[0]).name == 'config.json':
        raise AssertionError('Plan read a credential configuration')
sys.addaudithook(audited)
spec=importlib.util.spec_from_file_location('fresh_capability_runner',script)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
value=module.main(['--report',report,'--credentials-dir',credentials])
assert not any(n=='app' or n.startswith('app.') for n in sys.modules)
assert not value['credentials_read'] and value['real_provider_calls']==0
print(json.dumps({'application_modules_imported':0,'provider_calls':0,'credentials_read':False}))
"""
    result = subprocess.run([sys.executable, "-c", program, str(Path(runner.__file__).resolve()),
        str(destination), str(tmp_path / "absent-credentials")], capture_output=True, text=True,
        encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"application_modules_imported": 0, "provider_calls": 0, "credentials_read": False}


def test_real_helper_refuses_an_already_open_application_interpreter():
    with pytest.raises(RuntimeError, match="FreshIsolatedInterpreterRequired"):
        runner.run_real(None)


@pytest.mark.parametrize("extra", [[], ["--capability", "text"] * 4,
    ["--capability", "text", "--capability", "text"], ["--capability", "tools", "--max-http", "3"],
    ["--capability", "text", "--max-http", "17"], ["--capability", "text", "--max-http", "0"]])
def test_real_selection_rejected_before_source_or_credential_access(tmp_path, monkeypatch, extra):
    monkeypatch.setattr(runner, "run_real", lambda args: pytest.fail("Invalid selection executed"))
    with pytest.raises(SystemExit):
        runner.main(["--run-real", "--credentials-dir", str(tmp_path / "protected"),
                     "--report", str(tmp_path / "report.json"), *extra])


def test_outputs_cannot_mutate_original_configuration_directory(tmp_path):
    with pytest.raises(SystemExit):
        runner.main(["--report", str(tmp_path / "original" / "report.json"),
                     "--credentials-dir", str(tmp_path / "original")])


def test_present_protected_credential_is_authoritative_and_source_is_unchanged(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"llm_base_url": "https://api.deepseek.com/v1", "llm_model": "Exact-Model",
        "llm_api_key": "old-fixture", "protected_credentials": {"llm_api_key": "protected-fixture"}}), encoding="utf-8")
    before = path.read_bytes()
    seen = []
    result = runner.model_configuration(tmp_path, lambda value: seen.append(value) or "synthetic-protected-key")
    assert seen == ["protected-fixture"] and result["api_key"] == "synthetic-protected-key"
    assert result["storage_kind"] == "protected_configuration" and path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]
    def failed(value):
        raise ValueError("SyntheticDecryptionFailure")
    with pytest.raises(ValueError, match="SyntheticDecryptionFailure"):
        runner.model_configuration(tmp_path, failed)


@pytest.mark.parametrize("protected", [None, "bad", {"llm_api_key": None}, {"llm_api_key": ""}])
def test_malformed_protection_never_falls_back(tmp_path, protected):
    (tmp_path / "config.json").write_text(json.dumps({"llm_api_key": "old-fixture",
        "protected_credentials": protected}), encoding="utf-8")
    with pytest.raises(ValueError, match="InvalidProtected"):
        runner.model_configuration(tmp_path, lambda value: pytest.fail("Bad protected field decrypted"))


def test_legacy_fallback_stays_in_memory_and_no_new_plaintext_file_is_written(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"llm_api_key": "synthetic-legacy-key", "llm_base_url": "https://api.deepseek.com/v1",
        "llm_model": "Exact-Model"}), encoding="utf-8")
    before = path.read_bytes()
    result = runner.model_configuration(tmp_path, lambda value: pytest.fail("Legacy path decrypted"))
    assert result["api_key"] == "synthetic-legacy-key" and "memory_fallback" in result["storage_kind"]
    assert path.read_bytes() == before and len(list(tmp_path.iterdir())) == 1
    with pytest.raises(ValueError, match="SecretRejected"):
        runner.write_report(tmp_path / "leak.json", result, (result["api_key"],))
    assert not (tmp_path / "leak.json").exists()


def test_budget_wraps_actual_transport_counts_files_and_blocks_unknown_routes(monkeypatch):
    sent = []
    def actual_transport(transport, request):
        sent.append(request)
        return httpx.Response(200, request=request, json={"ok": True})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", actual_transport)
    budget = runner.HTTPBudget("https://api.deepseek.com/v1", 3)
    with budget.installed(), httpx.Client(trust_env=False) as client:
        for method, url in [("POST", "https://api.deepseek.com/v1/chat/completions"),
                            ("POST", "https://api.deepseek.com/files"),
                            ("DELETE", "https://api.deepseek.com/files/file-synthetic")]:
            assert client.request(method, url).status_code == 200
        with pytest.raises(RuntimeError, match="HTTPBudgetExceeded"):
            client.post("https://api.deepseek.com/v1/chat/completions")
        with pytest.raises(RuntimeError, match="UnconfiguredHTTPRoute"):
            client.post("https://other.example.test/v1/chat/completions")
        with pytest.raises(RuntimeError, match="UnconfiguredHTTPRoute"):
            client.get("https://api.deepseek.com/models")
    assert len(sent) == len(budget.sent) == 3 and budget.blocked == ["budget", "unconfigured_route", "unconfigured_route"]
    assert httpx.HTTPTransport.handle_request is actual_transport


def test_stopped_budget_blocks_before_transport_and_async_is_rejected(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *a: pytest.fail("Stopped guard sent network"))
    budget = runner.HTTPBudget("https://api.deepseek.com/v1", 1)
    budget.stopped.set()
    with budget.installed(), httpx.Client(trust_env=False) as client:
        with pytest.raises(RuntimeError, match="CancelledBeforeHTTP"):
            client.post("https://api.deepseek.com/v1/chat/completions")
        import asyncio
        async def request():
            async with httpx.AsyncClient(trust_env=False) as async_client:
                with pytest.raises(RuntimeError, match="UnexpectedAsync"):
                    await async_client.post("https://api.deepseek.com/v1/chat/completions")
        asyncio.run(request())
    assert not budget.sent


def test_original_configuration_change_fences_frozen_credential_before_next_http(monkeypatch):
    state, sent = {"authorized": True}, []
    def transport(owner, request):
        sent.append(request)
        return httpx.Response(200, request=request, json={"ok": True})
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    budget = runner.HTTPBudget("https://api.deepseek.com/v1", 3, lambda: state["authorized"])
    with budget.installed(), httpx.Client(trust_env=False) as client:
        client.post("https://api.deepseek.com/v1/chat/completions")
        state["authorized"] = False
        with pytest.raises(RuntimeError, match="OriginalConfigurationChanged"):
            client.post("https://api.deepseek.com/v1/chat/completions")
    assert len(sent) == len(budget.sent) == 1
    assert budget.blocked == ["original_configuration_changed"]


def test_frozen_inputs_require_script_and_every_app_module_before_key_read(tmp_path, monkeypatch):
    root = tmp_path / "snapshot"
    app = root / "backend" / "app"
    app.mkdir(parents=True)
    module = app / "sample.py"
    module.write_text("# synthetic module\n", encoding="utf-8")
    manifest = root / "evaluation-inputs.json"
    monkeypatch.setattr(runner, "ROOT", root)
    current = {"backend/app/sample.py": "expected", "backend/scripts/verify_real_capabilities.py": "script"}
    monkeypatch.setattr(runner, "source_hashes", lambda: current)
    manifest.write_text(json.dumps({"credential_files_copied": 0, "user_documents_copied": 0,
                                  "source_sha256": {"backend/app/sample.py": "expected"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="UnfrozenCapability"):
        runner.verify_frozen_inputs(manifest)


def test_frozen_inputs_detect_changed_file_and_escaping_entry(tmp_path, monkeypatch):
    import hashlib
    root = tmp_path / "snapshot"
    root.mkdir()
    module = root / "synthetic.py"
    module.write_text("# first\n", encoding="utf-8")
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    manifest = root / "evaluation-inputs.json"
    value = {"credential_files_copied": 0, "user_documents_copied": 0,
             "source_sha256": {"synthetic.py": digest}}
    manifest.write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(runner, "ROOT", root)
    monkeypatch.setattr(runner, "source_hashes", lambda: {"synthetic.py": digest})
    assert runner.verify_frozen_inputs(manifest)
    module.write_text("# changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="SnapshotChecksumMismatch"):
        runner.verify_frozen_inputs(manifest)
    module.write_text("# first\n", encoding="utf-8")
    value["source_sha256"]["../outside.py"] = "irrelevant"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="SnapshotChecksumMismatch"):
        runner.verify_frozen_inputs(manifest)


def test_runtime_environment_clears_both_keys_and_uses_isolated_paths(tmp_path):
    env = runner.isolation_environment(tmp_path, "https://api.deepseek.com/v1", "Exact-Model")
    assert env["LLM_API_KEY"] == env["EMBEDDING_API_KEY"] == env["REDIS_URL"] == ""
    assert Path(env["DATA_DIR"]).is_relative_to(tmp_path)
    assert env["LLM_DEMO_MODE"] == "0" and env["DESKTOP_NOTIFICATIONS"] == env["MEMORY_CAPTURE"] == "false"


def test_full_runner_in_fresh_frozen_interpreter_is_isolated_and_rejects_synthetic_acceptance(tmp_path):
    """Exercise setup/reporting too; fake HTTP is labelled only as mechanics."""
    import hashlib
    import shutil
    snapshot = tmp_path / "frozen"
    files = sorted((runner.BACKEND / "app").rglob("*.py"))
    files += [Path(runner.__file__).resolve(), runner.ROOT / "release.json"]
    manifest = {}
    for path in files:
        name = path.relative_to(runner.ROOT)
        destination = snapshot / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
        manifest[name.as_posix()] = hashlib.sha256(destination.read_bytes()).hexdigest()
    (snapshot / "evaluation-inputs.json").write_text(json.dumps({"source_sha256": manifest,
        "credential_files_copied": 0, "user_documents_copied": 0}), encoding="utf-8")
    credentials = tmp_path / "synthetic-original"
    credentials.mkdir()
    original = credentials / "config.json"
    original.write_text(json.dumps({"llm_base_url": "https://api.deepseek.com/v1", "llm_model": "Exact-Model",
        "llm_api_key": "synthetic-full-harness-key"}), encoding="utf-8")
    original_bytes = original.read_bytes()
    program = """
import argparse, importlib.util, json, sys
from pathlib import Path
import httpx
script, credentials, runtime = sys.argv[1:]
def audited(name,args):
    # Windows Proactor creates its internal wake-up socket pair on loopback.
    # No provider DNS/connection is allowed; those use synthetic HTTP below.
    host=(args[1][0] if name=='socket.connect' else args[0]) if name in ('socket.connect','socket.getaddrinfo') else None
    if name in ('socket.connect','socket.getaddrinfo') and host not in ('127.0.0.1','::1','localhost'):
        raise AssertionError('Mechanism test attempted real network')
sys.addaudithook(audited)
spec=importlib.util.spec_from_file_location('frozen_capability_runner',script)
runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
requests=[]
def synthetic_transport(owner,request):
    assert request.url.host=='api.deepseek.com'
    body=json.loads(request.content);assert body['model']=='Exact-Model'
    requests.append({'method':request.method,'path':request.url.path})
    content='{"answer":17,"ok":true}' if body.get('response_format') else 'OK'
    return httpx.Response(200,request=request,json={'choices':[{'message':{'content':content},'finish_reason':'stop'}],
        'usage':{'prompt_tokens':2,'completion_tokens':1,'total_tokens':3}})
httpx.HTTPTransport.handle_request=synthetic_transport
original_probe=runner.api_probe
marked=[False]
def synthetic_probe(*args,**kwargs):
    if not marked[0]:
        from app import model_capabilities
        init=model_capabilities._Client.__init__
        def synthetic(self,*a,**k):
            init(self,*a,**k);self._synthetic=True
        model_capabilities._Client.__init__=synthetic
        marked[0]=True
    return original_probe(*args,**kwargs)
runner.api_probe=synthetic_probe
captured=[]
def capture_only(path,value,secrets=()):
    encoded=json.dumps(value)
    assert all(not secret or secret not in encoded for secret in secrets)
    captured.append(json.loads(encoded))
runner.write_report=capture_only
args=argparse.Namespace(frozen_inputs=runner.ROOT/'evaluation-inputs.json',credentials_dir=Path(credentials),
    runtime_parent=Path(runtime),report=Path(runtime)/'unused.json',capability=['text','structured','usage'],
    max_http=3,timeout_seconds=30,cancel_file=None)
result=runner.run_real(args)
assert result['confirmed_all_selected'] and result.get('ledger_matches_dispatches'), {k:result.get(k) for k in ('error_kind','execution_status','probes','blocked_http')}
assert result['exact_model_on_chat_receipts'] and result['all_dispatched_calls_completed']
assert not result['ledger_has_no_synthetic_calls'] and not result['passed_selected_diagnostics']
assert result['source_config_unchanged'] and result['source_unchanged_during_evaluation']
assert len(requests)==3 and len(captured)>=5
from app.config import DATA_DIR
assert DATA_DIR.is_relative_to(Path(runtime))
assert not (DATA_DIR/'config.json').exists()
assert not (Path(runtime)/'unused.json').exists()
print(json.dumps({'real_provider_calls':0,'synthetic_dispatches':len(requests),
    'isolated':True,'synthetic_cannot_pass_real_acceptance':True,'secret_free_reports':True}))
"""
    result = subprocess.run([sys.executable, "-c", program,
        str(snapshot / "backend/scripts/verify_real_capabilities.py"), str(credentials), str(tmp_path / "runtime")],
        cwd=snapshot, capture_output=True, text=True, encoding="utf-8", timeout=90)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"real_provider_calls": 0, "synthetic_dispatches": 3,
        "isolated": True, "synthetic_cannot_pass_real_acceptance": True, "secret_free_reports": True}
    assert original.read_bytes() == original_bytes


@pytest.fixture
def api():
    from app.api.model_profiles import router
    application = FastAPI()
    application.include_router(router)
    with TestClient(application) as client:
        yield client


@pytest.mark.parametrize("capability", list(runner.PROBES))
def test_runner_calls_actual_production_preview_and_probe_routes_with_synthetic_http(db, target, api, capability):
    official(db, target)
    stage = 0
    def handle(request):
        nonlocal stage
        body = json.loads(request.content)
        if capability == "structured":
            assert body["response_format"] == {"type": "json_object"}
            return response('{"answer":17,"ok":true}')
        if capability == "usage":
            return response(usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3})
        if capability == "vision":
            assert body["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;")
            return response("red")
        if capability == "reasoning":
            return httpx.Response(200, json={"choices": [{"message": {"content": "391", "reasoning_content": "Synthetic calculation"}, "finish_reason": "stop"}]})
        if capability in ("streaming", "native_tools"):
            return stream(body["tools"][0]["type"] if capability == "native_tools" else None)
        if capability == "tools":
            stage += 1
            message = {"content": "OK" if stage == 4 else "", "reasoning_content": "Synthetic stage"}
            if stage < 4:
                message["tool_calls"] = [{"id": "synthetic-stage-" + str(stage), "type": "function", "function": {
                    "name": "capability_echo", "arguments": json.dumps({"stage": stage})}}]
            return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop" if stage == 4 else "tool_calls"}]})
        return response()
    target[2][0] = handle
    if capability == "files":
        file_handler(target)
    budget = runner.HTTPBudget("https://api.deepseek.com/v1", 16)
    result = runner.api_probe(api, target[0], capability, budget, timeout_seconds=30)
    assert result["http_status"] == 200 and result["result"]["status"] == "supported"
    assert result["result"]["request_count"] == len(target[1]) == runner.PROBES[capability]
    assert all(c["synthetic"] is True for c in result["result"]["calls"])
    assert "reasoning_content" not in json.dumps(result)


def test_preexisting_cancel_uses_real_tombstone_route_and_sends_zero_http(db, target, api, tmp_path):
    marker = tmp_path / "cancel"
    marker.touch()
    budget = runner.HTTPBudget("https://matrix.example.test/v1", 16)
    result = runner.api_probe(api, target[0], "tools", budget, timeout_seconds=30, cancel_file=marker)
    assert result["controller"]["cancel_http"] == 200
    assert result["result"]["failure_kind"] == "cancelled" and not target[1]
    assert budget.stopped.is_set()


def test_mid_probe_cancel_calls_production_api_and_preserves_known_file_cleanup(db, target, api, tmp_path):
    official(db, target)
    marker = tmp_path / "cancel"
    from app import model_capabilities as probes
    original = probes._record_remote
    def pending(client, value):
        original(client, value)
        if value.get("state") == "available":
            marker.touch()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    client.check()
                except probes.ProbeCancelled:
                    return
                time.sleep(0.01)
            pytest.fail("Controller never sent production cancellation")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(probes, "_record_remote", pending)
        file_handler(target)
        budget = runner.HTTPBudget("https://api.deepseek.com/v1", 16)
        result = runner.api_probe(api, target[0], "files", budget, timeout_seconds=30, cancel_file=marker)
    assert result["controller"]["cancel_http"] == 200 and len(target[1]) == 1
    remote = result["result"]["remote_file"]
    assert remote["state"] == "cleanup_pending" and remote["id"] == "file-capability-synthetic"
    assert remote["requested_ttl_seconds"] == 3600
    preview = api.get(f"/api/model-profiles/{target[0]}/capabilities").json()
    assert preview["cleanup_operations"][0]["diagnostic_id"] == result["diagnostic_id"]
