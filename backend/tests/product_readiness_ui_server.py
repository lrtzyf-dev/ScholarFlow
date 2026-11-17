"""Fresh-install browser fixture. No real HTTP is possible through the provider transport."""
import sys
from provider_adapter_ui_server import app
from sqlalchemy import func, select
from app.db import SessionLocal
from app.models import Conversation, QARecord, Paper
from app.source_models import Source
from app import source_store
from app.source_runtime import freeze_sources


@app.get('/__test__/counts')
def counts():
    with SessionLocal() as db:
        return {model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in (QARecord,Source,Paper)}


@app.post('/__test__/seed-citation')
def seed():
    with SessionLocal() as db:
        db.add(Conversation(id='journey-source',title='首次引用验收'));db.flush()
        scope=source_store.SourceScope('conversation','journey-source')
        item=source_store.capture_source(db,scope=scope,authorization=source_store.SourceAuthorization(scope,'journey-ui','upload'),
            kind='upload',title='首次资料.md',media_type='text/markdown',raw_bytes='# 观测\n实验组 17，对照组 12。'.encode(),
            text='# 观测\n实验组 17，对照组 12。',text_origin='provided_text')
        locator=source_store.add_text_locator(db,item['revision_id'],scope=scope,line_start=2,line_end=2)
        db.add(QARecord(id='journey-ui-qa',conversation_id='journey-source',question='两组的差值？',answer='相差 5。',evidence=[locator],
            runtime_config=freeze_sources(db,'journey-source',[item['revision_id']],{'mode':'api'})))
        db.commit()
        return {'conversation_id':'journey-source','revision_id':item['revision_id'],'locator_id':locator['locator_id']}


@app.post('/__test__/seed-probe')
def seed_probe():
    from app import call_ledger,diagnostic_records
    with SessionLocal() as db:
        original=db.query(diagnostic_records.DiagnosticRecord).first()
        call_id=call_ledger.begin_call(provider='synthetic',service='llm',endpoint='https://synthetic.example.test/v1',synthetic=True,db=db)
        call_ledger.finish_call(call_id,state='failed',error_kind='TimeoutException',db=db)
        result=diagnostic_records.save(db,{'service':'llm','status':'error','failure_kind':'timeout','calls':[{'call_id':call_id}]},
            kind='probe',conversation_id=original.conversation_id)
        db.commit();return result


for _ in range(3):
    app.router.routes.insert(0,app.router.routes.pop())

if __name__=='__main__':
    import uvicorn
    uvicorn.run(app,host='127.0.0.1',port=int(sys.argv[1]))
