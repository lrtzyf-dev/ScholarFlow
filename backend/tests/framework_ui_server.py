"""Isolated real HTTP/UI harness. Synthetic model, no provider or user data."""
import hashlib
import json
import sys

from app.main import app
from app.db import init_db, SessionLocal
from app.models import Paper
from app.source_models import SourceLibraryBinding, SourceLibraryImport
from app import source_store as store
from app.llm import LLMResponse
from app.writing import framework, framework_run


class SyntheticWritingModel:
    def chat(self, messages, temperature=0):
        if "claim_id" in messages[-1]["content"]:
            payload = {"checks": [{"claim_id": "abstract:0", "status": "insufficient",
                "explanation": "A located excerpt requires review before drawing a conclusion.", "evidence_ids": ["F1E1"]}]}
        else:
            payload = {"sections": [{"key": key, "bullets": [{"text": "A synthetic research outline for review.",
                "citations": [1], "evidence_ids": ["F1E1"]}] if key == "abstract" else []}
                for key in framework.SECTION_KEYS]}
        return LLMResponse(content=json.dumps(payload))


framework.get_llm = lambda: SyntheticWritingModel()
framework_run.get_llm = lambda: SyntheticWritingModel()
init_db()
with SessionLocal() as db:
    raw = b"# Fixed research source\nAttention methods were evaluated on synthetic data.\n"
    paper = Paper(title="Synthetic writing source", abstract="attention synthetic", status="parsed",
        content_hash=hashlib.sha256(raw).hexdigest())
    db.add(paper); db.flush()
    scope = store.SourceScope("library", paper.id)
    fixed = store.capture_source(db, scope=scope,
        authorization=store.SourceAuthorization(scope, "library:"+paper.id, "library"),
        kind="library", title=paper.title, raw_bytes=raw, text=raw.decode(), media_type="text/markdown")
    db.add_all([SourceLibraryBinding(paper_id=paper.id, revision_id=fixed["revision_id"]),
        SourceLibraryImport(revision_id=fixed["revision_id"], paper_id=paper.id,
            raw_sha256=paper.content_hash, request_id="writing-ui-original")])
    db.commit()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
