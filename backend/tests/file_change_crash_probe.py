"""Child process dies after intent/before result; invoked with isolated DATA_DIR."""
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import init_db, SessionLocal
from app.models import Conversation, ProjectChange
from app import workspace, workspace_write, project_changes as changes
init_db()
root = Path(os.environ['DATA_DIR']) / 'project'
if sys.argv[1] == 'recover':
    changes.recover_interrupted()
    with SessionLocal() as db: print(db.query(ProjectChange).one().status, flush=True)
else:
    root.mkdir(); (root/'a.txt').write_bytes(b'before')
    with SessionLocal() as db:
        project = workspace.register(db, str(root)); scope=workspace.project_scope(project)
        db.add(Conversation(id='crash',project_id=project.id));db.commit()
    item=changes.prepare(scope,'crash',{'path':'a.txt','operation':'update','expected_sha256':changes.digest(b'before'),'content':'after'})
    original=workspace_write.write_content
    def crash(file,data):
        if sys.argv[1]=='partial': original(file,b'part')
        if sys.argv[1]=='after': original(file,data)
        os._exit(73)
    workspace_write.write_content=crash
    changes.apply(item['id'])
