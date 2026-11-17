"""Isolated real API fixture. Synthetic history; no model/provider requests."""
from datetime import datetime, timedelta
import sys

from app.main import app
from app.db import init_db, SessionLocal
from app.models import Conversation, QARecord
from app.conversation_memory import load_history

init_db()
with SessionLocal() as db:
    if db.get(Conversation, 'memory-ui') is None:
        db.add(Conversation(id='memory-ui', title='记忆管理验收'))
        for i in range(23):
            db.add(QARecord(id=f'qa_memory_{i:02}', conversation_id='memory-ui',
                question=f'历史问题 {i + 1}', answer=f'原始聊天内容 {i + 1}，始终保留。', in_memory=1,
                created_at=datetime(2026, 1, 1) + timedelta(minutes=i)))
        db.commit()
        load_history(db, 'memory-ui', None, run_id='synthetic-prior-run')

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), log_level='warning')
