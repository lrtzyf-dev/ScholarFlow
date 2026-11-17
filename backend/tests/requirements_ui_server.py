"""Real isolated requirement API; the fixture never dispatches a provider."""
import sys

from app.main import app
from app.db import SessionLocal
from app.models import Conversation, QARecord
from app.runtime_models import RuntimeRun
from app import goal_ledger


@app.post("/__test__/seed-requirements")
def seed():
    result = []
    with SessionLocal() as db:
        for key in ("a", "b"):
            cid, run = "requirements-ui-" + key, "requirements-run-" + key
            db.add(Conversation(id=cid, title="任务要求验收 " + key)); db.flush()
            goal = goal_ledger.ensure_run_goal(db, run, cid, "解释方法并说明局限 " + key)
            db.add(RuntimeRun(id=run, kind="foreground", conversation_id=cid, goal_id=goal["id"], status="ended"))
            db.add(QARecord(id="requirements-qa-" + key, conversation_id=cid,
                question="解释方法并说明局限 " + key, answer="已保留初始答复 " + key,
                runtime_config={"run_id": run}))
            result.append({"conversation_id": cid, "goal_id": goal["id"], "run_id": run})
        db.commit()
    return result


app.router.routes.insert(0, app.router.routes.pop())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
