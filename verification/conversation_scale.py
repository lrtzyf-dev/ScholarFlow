"""Disposable SQLite scale probe; timings are evidence for this machine only."""
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from time import perf_counter

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
from app.db import Base
from app.models import Conversation, QARecord
from app.conversation_query import query_conversations

with tempfile.TemporaryDirectory(prefix="scholarflow-search-scale-") as folder:
    engine = create_engine(f"sqlite:///{Path(folder) / 'scale.db'}")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            at = datetime(2026, 1, 1)
            db.add_all(Conversation(id=f"c_{index:05}", title=f"Review {index}", created_at=at, updated_at=at) for index in range(5000))
            db.add_all(QARecord(id=f"qa_{index:05}", conversation_id=f"c_{index:05}", question="Question",
                                answer="Evidence content. " * 250 + "中文短语 100%_", created_at=at) for index in range(5000))
            db.commit()
            measurements = []
            for params in ({"sort": "sidebar_updated_at"}, {"q": "中文短语"}, {"ids": ["c_00001"], "scope": "all"}):
                started = perf_counter()
                result = query_conversations(db, **params)
                elapsed = perf_counter() - started
                size = len(json.dumps(result, ensure_ascii=False).encode())
                assert len(result["conversations"]) <= 40 and size < 60000
                assert "Evidence content. " * 100 not in json.dumps(result)
                measurements.append({"query": params, "milliseconds": round(elapsed * 1000, 2), "bytes": size, "total": result["total"]})
            print(json.dumps({"conversations": 5000, "answer_characters": 5000 * (250 * 18 + 10), "measurements": measurements}, ensure_ascii=False))
    finally:
        engine.dispose()
