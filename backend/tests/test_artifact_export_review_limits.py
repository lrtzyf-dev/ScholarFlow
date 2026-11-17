"""Untrusted persisted export volume must fail closed in the small ART03 fixture."""
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import insert

from app.artifact_models import ArtifactExport
from scripts import verify_real_acceptance as runner
from tests.test_artifact_export_review_harness import actual_review, create_export


@pytest.mark.parametrize("boundary", ["rows", "bytes"])
def test_export_review_rejects_oversized_database_fixture_without_a_read_only_verdict(db, boundary):
    with actual_review(db) as (client, cid, result, _):
        before = result["artifact_history_review"]["before"]
        exported = create_export(client, cid, before)
        row = db.get(ArtifactExport, exported["export_id"])
        original = {column.name: deepcopy(getattr(row, column.name)) for column in ArtifactExport.__table__.columns}
        # Trusted SQL seeds simulate an oversized/corrupt restored database.
        # This bypasses the application's per-file checks deliberately: the
        # acceptance observer must bound persisted data before trusting it.
        payload, count = (b"x", 1000) if boundary == "rows" else (b"x" * (16 * 1024 * 1024), 2)
        values = [{**original, "id": uuid4().hex, "request_id": uuid4().hex,
            "payload": payload, "payload_size": len(payload)} for _ in range(count)]
        db.execute(insert(ArtifactExport), values)
        db.commit()
        with pytest.raises(RuntimeError, match="^ArtifactReviewFixtureTooLarge$"):
            runner.artifact_review_snapshot(client, cid, before["artifact_id"])
