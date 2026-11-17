"""Isolated storage settings/cleanup UI harness; synthetic local content only."""
import sys
from datetime import timedelta
from pathlib import Path
from app.main import app
from app.db import init_db,SessionLocal
from app.models import Conversation,QARecord
from app.provider_models import ProviderFileBinding
from app.timeutil import utcnow
from app.config import DATA_DIR
from app.storage_policy import record_cleanup
from app.storage_models import CleanupReceipt
init_db()
with SessionLocal()as db:
    for cid,title in [('storage-ui-old','Synthetic archived review'),('storage-ui-cloud','Synthetic cloud review')]:
        db.add(Conversation(id=cid,title=title,archived_at=utcnow()-timedelta(days=200)))
        db.add(QARecord(conversation_id=cid,question='SYNTHETIC_PRIVATE_QUESTION',answer='SYNTHETIC_PRIVATE_ANSWER'))
    db.add(Conversation(id='storage-ui-active',title='Active conversation remains'))
    db.add(ProviderFileBinding(id='storage-cloud-binding',conversation_id='storage-ui-cloud',revision_id='synthetic-revision',
        raw_sha256='synthetic-hash',title='synthetic-cloud.pdf',media_type='application/pdf',size_bytes=8,
        purpose='file-extract',expires_seconds=3600,request_id='storage-cloud-request',configuration_version=1,
        credential_ref='unconfigured',policy={},state='unknown'))
    receipt=record_cleanup(db,'old-synthetic-cleanup',{'records':{},'total_records':0,'storage_preview':{'logical_payload_bytes':0},'retained_project_journals':{},'preserved':[],'disk_erasure':'logical_removal_only'})
    db.get(CleanupReceipt,receipt).created_at=utcnow()-timedelta(days=400)
    db.commit()
(DATA_DIR/'synthetic-capacity.bin').write_bytes(b'x'*(1024**2+1))
if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[1]),log_level='warning')
