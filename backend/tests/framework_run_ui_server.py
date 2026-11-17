"""Actual HTTP/UI process; all model requests intercepted as synthetic data."""
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time

import httpx

from app.main import app
from app import model_profiles, source_store as sources
from app.db import init_db, SessionLocal
from app.models import Paper
from app.source_models import SourceLibraryBinding, SourceLibraryImport
from app.llm import APIClient
from app.runtime_context import current_runtime_config
from app.writing import framework_run

CONTROL = Path(os.environ["FRAMEWORK_RUN_CONTROL"])
CONTROL.mkdir(parents=True, exist_ok=True)
RECEIPTS = CONTROL / "synthetic-calls.jsonl"
_lock = threading.Lock()


def provider(request):
    assert request.url.host == "framework-run.synthetic.test" and request.url.path == "/v1/chat/completions"
    body = json.loads(request.content)
    assert body["model"] == "Framework-Bound-Case"
    review = "独立的论点支持审查者" in body["messages"][0]["content"]
    phase = "review" if review else "generation"
    with _lock:
        previous = RECEIPTS.read_text().splitlines() if RECEIPTS.exists() else []
        number = len(previous) + 1
        with RECEIPTS.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"number": number, "phase": phase, "model": body["model"], "synthetic": True}) + "\n")
    marker = CONTROL / ("release-" + phase)
    while not marker.exists(): time.sleep(.02)
    payload = ({"checks": [{"claim_id": "abstract:0", "status": "insufficient",
        "explanation": "Synthetic excerpt requires human review.", "evidence_ids": ["F1E1"]}]} if review else
        {"sections": [{"key": key, "bullets": [{"text": "Persistent synthetic framework draft.", "citations": [1], "evidence_ids": ["F1E1"]}] if key == "abstract" else []}
            for key in ("abstract", "introduction", "related_work")]})
    return httpx.Response(200, json={"id": "synthetic-"+str(number), "choices": [{"index": 0,
        "message": {"role": "assistant", "content": json.dumps(payload)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})


_client = httpx.Client
httpx.Client = lambda *a, **k: _client(*a, transport=httpx.MockTransport(provider), **k)
framework_run.get_llm = lambda: APIClient(runtime_config=current_runtime_config(), synthetic=True)

init_db()
with SessionLocal() as db:
    if model_profiles.default_profile_id(db) == model_profiles.LEGACY_PROFILE_ID:
        profile = model_profiles.save_profile(db, {"name": "Synthetic bound model", "base_url": "https://framework-run.synthetic.test/v1",
            "model_id": "Framework-Bound-Case", "api_key": "synthetic-run-key"})
        model_profiles.set_default(db, profile.id)
    raw = b"# Fixed research source\nAttention results in a synthetic experiment.\n"
    content_hash = hashlib.sha256(raw).hexdigest()
    paper = db.query(Paper).filter(Paper.content_hash == content_hash).first()
    if paper is None:
        paper = Paper(title="Persistent synthetic research source", abstract="attention synthetic results", status="parsed", content_hash=content_hash)
        db.add(paper); db.flush()
        scope = sources.SourceScope("library", paper.id)
        fixed = sources.capture_source(db, scope=scope, authorization=sources.SourceAuthorization(scope, "synthetic-framework-source", "library"),
            kind="library", title=paper.title, raw_bytes=raw, text=raw.decode(), media_type="text/markdown")
        db.add_all([SourceLibraryBinding(paper_id=paper.id, revision_id=fixed["revision_id"]),
            SourceLibraryImport(revision_id=fixed["revision_id"], paper_id=paper.id, raw_sha256=content_hash, request_id="synthetic-framework-fixed")])
    db.commit()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
