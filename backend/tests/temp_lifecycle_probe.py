"""Real pytest child sessions for their own temporary DATA_DIR lifecycle."""
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile

_held = None


def test_session_storage_probe():
    from app.config import DATA_DIR
    global _held
    path = Path(DATA_DIR)
    (path / "owned-synthetic.txt").write_text("synthetic test state", encoding="utf-8")
    scope = path.parent
    print("TEMP_LIFECYCLE:" + json.dumps({"path": str(scope)}))
    mode = os.environ.get("TEST_TEMP_PROBE_MODE", "success")
    if mode == "identity":
        marker = scope / ".test-temp-owner.json"
        value = json.loads(marker.read_text(encoding="utf-8"))
        marker.write_text(json.dumps({**value, "token": "changed-by-owned-test"}), encoding="utf-8")
    elif mode == "locked":
        _held = (path / "owned-synthetic.txt").open("rb+")
    assert mode != "failure", "intentional synthetic failure preserves debugging material"


def verify():
    root = Path(__file__).resolve().parents[2]
    target = root / "verification" / "test-temp-current"
    target.mkdir(exist_ok=True)
    isolated = Path(tempfile.mkdtemp(prefix="temp-lifecycle-", dir=target))
    cases = []
    for mode in ("success", "keep", "failure", "identity", "locked"):
        env = {**os.environ, "TMP": str(isolated), "TEMP": str(isolated),
               "KEEP_TEST_TEMP": "1" if mode == "keep" else "0", "TEST_TEMP_PROBE_MODE": mode,
               "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run([sys.executable, "-m", "pytest", str(Path(__file__).resolve()), "-q", "-s", "--disable-warnings"],
                                cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
        lines = [line for line in result.stdout.splitlines() if line.startswith("TEMP_LIFECYCLE:")]
        if not lines:
            raise RuntimeError("Temporary lifecycle probe did not run: " + result.stderr[-1500:])
        path = Path(json.loads(lines[-1].split(":", 1)[1])["path"])
        assert path.resolve().parent == isolated.resolve()
        assert result.returncode == (1 if mode == "failure" else 0), result.stdout[-2500:]
        assert path.exists() == (mode != "success"), result.stdout[-2500:]
        if mode == "locked":
            assert (path / "data" / "owned-synthetic.txt").read_text(encoding="utf-8") == "synthetic test state"
        cases.append({"mode": mode, "exit_code": result.returncode, "retained": path.exists(), "path": str(path)})
    report = {"ok": True, "scope": "pytest-owned scratch only", "cases": cases,
              "real_provider_calls": 0, "user_directories_touched": False}
    output = root / "verification" / "model-capability-routing" / "test-temp-lifecycle-report.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("PASS pytest scratch lifecycle: successful cleanup; KEEP/failure/identity/active lock retained")


if __name__ == "__main__":
    verify()
