"""Opt-in immutable writing workflow with public synthetic material only."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from urllib.parse import urlsplit


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--run-real',action='store_true',required=True)
    p.add_argument('--credentials-dir',required=True)
    p.add_argument('--report',required=True)
    args=p.parse_args()
    backend=Path(__file__).resolve().parents[1];sys.path.insert(0,str(backend))
    from app.credential_protection import unprotect
    saved=json.loads((Path(args.credentials_dir)/'config.json').read_text(encoding='utf-8'))
    protected=(saved.get('protected_credentials')or{}).get('llm_api_key')
    key=unprotect(protected)if protected else saved.get('llm_api_key','')
    base,model=saved.get('llm_base_url',''),saved.get('llm_model','')
    if not key or urlsplit(base).scheme!='https' or urlsplit(base).hostname!='api.deepseek.com':
        raise RuntimeError('ConfiguredOfficialDeepSeekCredentialRequired')
    files=['app/writing/framework.py','app/writing/evidence.py','app/writing/sources.py','app/api/writing.py','scripts/verify_real_framework.py']
    hashes=lambda:{name:hashlib.sha256((backend/name).read_bytes()).hexdigest()for name in files}
    report={'kind':'real_framework_fixed_evidence','maximum_real_requests':3,'provider':'api.deepseek.com','model_id':model,
        'synthetic_material_only':True,'private_documents_sent':False,'source_hashes_at_start':hashes()}
    started=time.monotonic();sent=0
    with tempfile.TemporaryDirectory(prefix='sf_writing_real_')as tmp:
        os.environ.update(DATA_DIR=str(Path(tmp)/'data'),APPDATA=str(Path(tmp)/'appdata'),LOCALAPPDATA=str(Path(tmp)/'localappdata'),
            LLM_API_KEY='',LLM_DEMO_MODE='0',LLM_BASE_URL=base,LLM_MODEL=model,EMBEDDING_API_KEY='',REDIS_URL='',
            DESKTOP_NOTIFICATIONS='false',MEMORY_CAPTURE='false',SCHOLARFLOW_TEST_TRANSPORT='1')
        from app.main import app
        from app import llm,model_profiles,source_store as store
        from app.db import SessionLocal,engine
        from app.models import Paper,Conversation
        from app.source_models import SourceLibraryBinding,SourceLibraryImport
        from app.call_ledger import list_calls,summarize_calls
        from fastapi.testclient import TestClient
        original_meter=llm.APIClient._meter
        original_chat=llm.APIClient.chat
        public_responses=[]
        def recorded_chat(self,*values,**kwargs):
            response=original_chat(self,*values,**kwargs)
            public_responses.append(response.content)
            return response
        @contextmanager
        def limited_meter(self,*values,**kwargs):
            nonlocal sent
            if sent>=3:raise RuntimeError('EvaluationRequestBudgetExceeded')
            sent+=1;self._timeout=60
            with original_meter(self,*values,**kwargs)as receipt:yield receipt
        llm.APIClient._meter=limited_meter
        llm.APIClient.chat=recorded_chat
        try:
            with TestClient(app)as client:
                with SessionLocal()as db:
                    profile=model_profiles.save_profile(db,{'name':'Public synthetic writing test','base_url':base,'model_id':model,'api_key':key})
                    model_profiles.set_default(db,profile.id)
                    db.add(Conversation(id='writing-real',title='Public synthetic writing test'))
                    raw=b'# Synthetic pilot study\nA fictional attention retrieval method was evaluated on 20 synthetic queries.\nTwelve of the twenty queries retrieved a relevant item in the top five.\nNo comparison with other methods was performed. The small synthetic sample cannot establish general effectiveness.\n'
                    paper=Paper(title='Synthetic attention retrieval pilot',abstract='Synthetic pilot without a baseline comparison.',status='parsed',content_hash=hashlib.sha256(raw).hexdigest())
                    db.add(paper);db.flush();scope=store.SourceScope('library',paper.id)
                    fixed=store.capture_source(db,scope=scope,authorization=store.SourceAuthorization(scope,'library:'+paper.id,'library'),
                        kind='library',title=paper.title,raw_bytes=raw,text=raw.decode(),media_type='text/markdown')
                    db.add_all([SourceLibraryBinding(paper_id=paper.id,revision_id=fixed['revision_id']),
                        SourceLibraryImport(revision_id=fixed['revision_id'],paper_id=paper.id,raw_sha256=paper.content_hash,request_id='writing-real-source')]);db.commit()
                response=client.post('/api/writing/framework',json={'idea':'根据这份合成检索试验资料给出论文框架，明确试验规模和缺少对照的局限，不编造优势或新指标。','max_sources':5,'conversation_id':'writing-real'})
                body=response.json();calls=list_calls()['items']
                evidences=[e for r in body.get('references',[])for e in r.get('evidence',[])]
                cited=[b for s in body.get('sections',[])for b in s.get('bullets',[])if b.get('evidence_ids')]
                checks={'http_ok':response.status_code==200,'model_generated':body.get('mode')=='llm',
                    'structure_valid':body.get('structure_valid')is True,'no_false_certification':body.get('verified')is False,
                    'fixed_source_available':bool(evidences),'located_claims':bool(cited),
                    'support_review_complete':body.get('semantic_verification')=='model_assessed',
                    'all_calls_completed':bool(calls)and all(c['state']=='completed'for c in calls),
                    'configured_model_used':all(c['model_id']==model for c in calls),'meter_matches_ledger':sent==len(calls)}
                with SessionLocal()as db:
                    checks['locators_resolve']=bool(evidences)and all(store.read_locator(db,e['locator_id'],scope=store.SourceScope('conversation','writing-real'))['revision_id']==e['revision_id']for e in evidences)
                report.update(checks=checks,passed=all(checks.values()),result=body,usage=summarize_calls(),public_synthetic_responses=public_responses)
        except Exception as exc:report.update(passed=False,error_kind=type(exc).__name__)
        finally:llm.APIClient._meter=original_meter;llm.APIClient.chat=original_chat;engine.dispose()
    report.update(real_requests=sent,elapsed_seconds=round(time.monotonic()-started,2),source_hashes_at_finish=hashes())
    report['source_unchanged_during_evaluation']=report['source_hashes_at_start']==report['source_hashes_at_finish']
    report['passed']=report['passed']and report['source_unchanged_during_evaluation']
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if key in text:raise RuntimeError('SecretRejectedFromReport')
    path=Path(args.report).resolve();path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
    print(json.dumps({'passed':report['passed'],'real_requests':sent,'checks':report.get('checks'),'report':str(path)},ensure_ascii=False),flush=True)
    if not report['passed']:raise SystemExit(1)


if __name__=='__main__':main()
