"""后台任务：Celery（配置 REDIS_URL）或同步降级。"""
from .celery_app import schedule_parse

__all__ = ["schedule_parse"]
