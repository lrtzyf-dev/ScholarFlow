"""Isolated local quality UI fixture; never requests any model or service."""
import hashlib
import os
from pathlib import Path
import sys
import time

from app.main import app
from app import source_store as sources
from app.db import init_db, SessionLocal
from app.models import Paper, Reference
from app.source_models import SourceLibraryBinding, SourceLibraryImport
from app.writing import quality_run

CONTROL = Path(os.environ["QUALITY_RUN_CONTROL"])
CONTROL.mkdir(parents=True, exist_ok=True)
original_score = quality_run.score_occurrence


def score(quote):
    if "[7]" in quote:
        (CONTROL / "second-started").touch()
        while not (CONTROL / "release-second").exists(): time.sleep(.02)
    return original_score(quote)


quality_run.score_occurrence = score
init_db()
with SessionLocal() as db:
    raw = (b"Our method compares model performance [2].\nAnother model compares limitations [7].\n"
        b"References\n[2] Alpha A. First work. 2020.\n[7] Beta B. Second work. 2021.\n")
    sha = hashlib.sha256(raw).hexdigest()
    paper = db.query(Paper).filter_by(content_hash=sha).first()
    if paper is None:
        paper = Paper(title="Fixed citation signal fixture", status="parsed", content_hash=sha)
        db.add(paper); db.flush()
        scope = sources.SourceScope("library", paper.id)
        fixed = sources.capture_source(db, scope=scope,
            authorization=sources.SourceAuthorization(scope, "quality-ui-fixture", "library"), kind="library",
            title=paper.title, raw_bytes=raw, text=raw.decode(), media_type="text/markdown")
        db.add_all([SourceLibraryBinding(paper_id=paper.id, revision_id=fixed["revision_id"]),
            SourceLibraryImport(revision_id=fixed["revision_id"], paper_id=paper.id, raw_sha256=sha, request_id="quality-ui-fixed"),
            Reference(paper_id=paper.id, ref_index=2, raw_text="Alpha A. First work. 2020."),
            Reference(paper_id=paper.id, ref_index=7, raw_text="Beta B. Second work. 2021.")])
    db.commit()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
