"""Synthetic projection benchmark. It does not claim document import/OCR throughput."""
import sys,platform,os
from datetime import datetime,timedelta
from pydantic import BaseModel
from provider_adapter_ui_server import app
from app.db import SessionLocal
from app.models import Paper,Reference,Conversation,QARecord
from app.product_journey import OnboardingState

class Dataset(BaseModel):
    kind:str
    size:int

@app.post('/__test__/seed-scale')
def seed(req:Dataset):
    assert req.kind in ('library','graph','history') and req.size in (100,500,1000,2000)
    with SessionLocal() as db:
        assert db.query(Paper).count()==0 and db.query(QARecord).count()==0,'one dataset per fresh process'
        db.add(OnboardingState(id='first-research',revision=1,status='dismissed',events={}))
        if req.kind in ('library','graph'):
            for i in range(req.size):
                db.add(Paper(id=f'scale-{i:05}',title=f'Projection scale {i:05} optics measurement',authors='Synthetic Benchmark',
                    abstract='Synthetic list metadata; no document has been parsed or OCR tested.',page_count=0,
                    arxiv_id=f'2611.{i:05}',status='parsed' if req.kind=='graph' else 'metadata_only'))
            if req.kind=='graph':
                for i in range(req.size-1):
                    db.add(Reference(id=f'scale-ref-{i:05}',paper_id=f'scale-{i:05}',ref_index=1,title=f'Projection scale {i+1:05} optics measurement',
                        raw_text=f'arXiv:2611.{i+1:05}',linked_paper_id=f'scale-{i+1:05}',page=None))
        else:
            db.add(Conversation(id='scale-history',title='规模会话'))
            now=datetime(2026,1,1)
            answer='## 可追踪记录\n\n'+'此处是用于测量界面滚动和恢复的合成文本，不代表真实科研结论。'*40
            for i in range(req.size):
                db.add(QARecord(id=f'scale-qa-{i:05}',conversation_id='scale-history',question=f'第 {i} 条合成问题',answer=answer,
                    created_at=now+timedelta(seconds=i),trace=[{'kind':'commentary','text':f'合成进展 {j}，第 {i} 轮'} for j in range(30)]))
        db.commit()
    return {'kind':req.kind,'size':req.size,'documents_imported':0,'pages':0,'ocr_ratio':None,'file_bytes':0,
        'messages':req.size if req.kind=='history' else 0,'answer_chars_each':len(answer) if req.kind=='history' else 0,
        'events_each':30 if req.kind=='history' else 0,'model_calls':0,'python':sys.version,'platform':platform.platform(),'logical_cpus':os.cpu_count()}

app.router.routes.insert(0,app.router.routes.pop())

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[1]))
