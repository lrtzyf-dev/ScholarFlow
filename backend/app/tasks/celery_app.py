"""
Celery 封装：
- 配置 REDIS_URL 时走真实 Celery worker（需另开 worker 进程）
- 未配置时 .delay() 直接在当前线程同步执行（W1 零依赖可跑）
"""
from __future__ import annotations

from ..config import settings
from ..db import SessionLocal

celery_app = None

if settings.redis_url:
    try:
        from celery import Celery
    except ImportError:
        celery_app = None  # 未安装 celery 时静默降级为同步执行
    else:
        celery_app = Celery("scholarflow", broker=settings.redis_url, backend=settings.redis_url)
        celery_app.conf.update(task_track_started=True)


def _run_parse(paper_id: str) -> None:
    from ..models import Paper
    from ..pipeline.indexer import ingest_pdf

    db = SessionLocal()
    try:
        paper = db.get(Paper, paper_id)
        if paper:
            ingest_pdf(db, paper)
    finally:
        db.close()


if celery_app:
    @celery_app.task(name="parse_pdf")
    def parse_pdf_task(paper_id: str) -> None:
        _run_parse(paper_id)

    def schedule_parse(paper_id: str) -> None:
        parse_pdf_task.delay(paper_id)
else:
    def schedule_parse(paper_id: str) -> None:
        _run_parse(paper_id)
