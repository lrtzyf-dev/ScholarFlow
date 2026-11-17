"""Explicit live acceptance of production capability/native/file workflows.

Uses only fixed public URLs, synthetic material and an existing protected
credential read in memory. Temporary app data is isolated. Uploaded synthetic
files are deleted through the same production lifecycle in finally. No paid
retry, user library access, raw provider payload or private reasoning report.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-real',action='store_true',required=True)
    parser.add_argument('--credentials-dir',required=True)
    parser.add_argument('--suite',choices=['native','files','capabilities','all'],default='all')
    parser.add_argument('--report',required=True)
    args=parser.parse_args()
    backend=Path(__file__).resolve().parents[1];sys.path.insert(0,str(backend))
    from app.credential_protection import unprotect
    saved=json.loads((Path(args.credentials_dir)/'config.json').read_text(encoding='utf-8'))
    protected=(saved.get('protected_credentials') or {}).get('llm_api_key')
    key=unprotect(protected) if protected else saved.get('llm_api_key','')
    if not key:raise RuntimeError('AuthorizedCredentialUnavailable')
    report={'kind':'production_provider_workflows','model_id':'deepseek-flash','suite':args.suite,
        'private_documents_sent':False,'inputs':'synthetic text/image and fixed official public documentation',
        'maximum_real_http_requests':20,'release_approval':False,'cases':[],'http_trace':[],
        'started_at':time.strftime('%Y-%m-%dT%H:%M:%S%z')}
    output=Path(args.report).resolve()
    def hashes():
        paths=sorted((backend/'app').rglob('*.py'))+[Path(__file__).resolve()]
        return {p.relative_to(backend).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    report['source_hashes_at_start']=hashes()
    def save():
        payload=json.dumps(report,ensure_ascii=False,indent=2)
        if key in payload:raise RuntimeError('SecretRejectedFromReport')
        output.parent.mkdir(parents=True,exist_ok=True);output.write_text(payload,encoding='utf-8')
    def case(name,passed,**details):
        report['cases'].append({'name':name,'passed':bool(passed),**details});save()
        print(json.dumps({'case':name,'passed':bool(passed)},ensure_ascii=False),flush=True)
    with tempfile.TemporaryDirectory(prefix='sf-live-production-provider-') as temp:
        os.environ.update(DATA_DIR=str(Path(temp)/'data'),APPDATA=str(Path(temp)/'appdata'),
            LOCALAPPDATA=str(Path(temp)/'localappdata'),LLM_API_KEY='',EMBEDDING_API_KEY='',REDIS_URL='',
            DESKTOP_NOTIFICATIONS='false',SCHOLARFLOW_TEST_TRANSPORT='1',LLM_DEMO_MODE='0')
        import httpx
        from fastapi.testclient import TestClient
        from app.main import app
        from app.db import SessionLocal,engine
        from app import service_tools,provider_files
        from app.source_runtime import freeze_sources
        from app.runtime_context import bind_runtime_config
        from app.provider_models import ProviderOperationContext, ProviderFileBinding
        from app.call_ledger import summarize_calls
        original=httpx.HTTPTransport.handle_request
        def counted(transport,request):
            if (request.url.scheme!='https' or request.url.host!='api.deepseek.com'
                    or len(report['http_trace'])>=report['maximum_real_http_requests']):
                raise RuntimeError('UnexpectedExternalRequestOrEvaluationBudget')
            entry={'method':request.method,'path':request.url.path};report['http_trace'].append(entry)
            response=original(transport,request);entry['status']=response.status_code
            return response
        httpx.HTTPTransport.handle_request=counted
        cloud=None;cid=None
        try:
            with TestClient(app) as client:
                def api(method,path,**kwargs):
                    r=client.request(method,'/api'+path,**kwargs)
                    if r.status_code!=200:raise RuntimeError('LocalApiStatus'+str(r.status_code))
                    return r.json()
                cid=api('POST','/conversations',json={'title':'Live synthetic provider acceptance'})['id']
                options=api('GET','/conversation-options/'+cid)
                options=api('PUT','/conversation-options/'+cid,json={'expected_revision':options['revision'],'approval_mode':'full_access'})
                binding={'conversation_id':cid,'approval_mode':'full_access','policy_revision':options['revision']}
                api('PUT','/service-tools/deepseek',json={'expected_version':0,'enabled_services':['research','files'],'api_key':key})
                def execute(name,arguments,operation,bound=None):
                    use=bound or binding
                    with SessionLocal() as db:
                        prepared=service_tools.prepare_deepseek_tool(db,name,arguments,conversation_id=cid,operation_key=operation,binding=use)
                        events=[]
                        with bind_runtime_config(use):
                            iterator=service_tools.execute_deepseek_tool_events(db,name,arguments,conversation_id=cid,prepared=prepared)
                            try:
                                while True:events.append(next(iterator))
                            except StopIteration as done:result=json.loads(done.value)
                            finally:iterator.close()
                    return result,events,prepared.receipt_id
                if args.suite in ('native','all'):
                    result,events,rid=execute('deepseek_research',{'query':
                        '请使用官方链接阅读能力读取 https://api-docs.deepseek.com/zh-cn/guides/search ，用100字说明Search API用途，保留来源链接。只核对这个公开文档。'},'live:native:first')
                    with SessionLocal() as db:
                        context=db.get(ProviderOperationContext,rid)
                        replay_saved=bool(context and context.payload.get('messages'))
                    okay=bool(result.get('answer') and result.get('activities') and result.get('citations') and replay_saved)
                    case('native_research',okay,activity_count=len(result.get('activities',[])),citation_count=len(result.get('citations',[])),
                        public_event_kinds=sorted({v.get('kind','') for v in events}),private_replay_saved=replay_saved,error_code=result.get('code'))
                    if result.get('answer'):
                        follow,activity,_=execute('deepseek_research',{'query':'基于同一个文档，简短指出独立Search API与模型内置联网工具的区别。保留链接，不要再研究其他话题。','followup_id':rid},'live:native:followup')
                        case('native_followup',bool(follow.get('answer') and follow.get('research_id')),
                            citation_count=len(follow.get('citations',[])),activity_count=len(activity),error_code=follow.get('code'))
                if args.suite in ('files','all'):
                    raw=b'# Synthetic cloud file\nSample count: 42\nAccuracy: 97%\nNo significance test was performed.\n'
                    uploaded=api('POST','/sources/upload',files={'file':('synthetic-provider.md',raw,'text/markdown')},
                        data={'conversation_id':cid,'request_id':'live-synthetic-source'})
                    revision=uploaded['revision_id']
                    cloud=provider_files.prepare(cid,revision,'user_data',3600,'live-cloud-prepare')
                    expected_filename='sf-'+cloud['id']+'.md'
                    cloud=provider_files.upload(cid,cloud['id'],cloud['version'])
                    with SessionLocal() as db:
                        stored=db.get(ProviderFileBinding,cloud['id'])
                        metadata={k:v for k,v in (stored.remote_metadata or {}).items()
                            if k in ('bytes','created_at','filename','purpose','expires_at')}
                    case('cloud_upload',cloud['state']=='available' and cloud['identity_verified'],state=cloud['state'],
                        fixed_sha256=hashlib.sha256(raw).hexdigest(),expires_seconds=cloud['expires_seconds'],
                        expected={'filename':expected_filename,'bytes':len(raw),'purpose':'user_data'},
                        synthetic_remote_metadata=metadata,identity_basis=cloud.get('identity_basis'),
                        remote_original_bytes_verified=cloud.get('remote_original_bytes_verified'))
                    if cloud['state']=='available':
                        with SessionLocal() as db:bound=freeze_sources(db,cid,[revision],binding)
                        result,_,_=execute('deepseek_file_read',{'source_revision_id':revision,
                            'query':'Read the synthetic file. Give the sample count, accuracy and whether a significance test was performed. Be concise.'},'live:file:read',bound)
                        answer=result.get('answer','')
                        case('fixed_cloud_file_read',bool('42' in answer and '97' in answer and result.get('source_revision_id')==revision),
                            has_answer=bool(answer),fixed_source_bound=result.get('source_revision_id')==revision,error_code=result.get('code'))
                if args.suite in ('capabilities','all'):
                    profile=api('POST','/model-profiles',json={'name':'Live synthetic DeepSeek profile','base_url':'https://api.deepseek.com/v1',
                        'model_id':'deepseek-flash','api_key':key})
                    pid=profile['id']
                    for capability in ('text','streaming','tools','vision','reasoning'):
                        preview=api('GET',f'/model-profiles/{pid}/capabilities')
                        result=api('POST',f'/model-profiles/{pid}/capabilities',json={'capability':capability,'expected_config':preview['expected_config']})
                        case('capability_'+capability,result.get('status')=='supported',status=result.get('status'),
                            actual_requests=result.get('request_count'),failure_kind=result.get('failure_kind'))
        except Exception as exc:
            report['error_kind']=type(exc).__name__;save()
        finally:
            if cloud is not None:
                try:
                    current=next(v for v in provider_files.list_bindings(cid)['items'] if v['id']==cloud['id'])
                    if current.get('remote_file_id'):
                        clean=provider_files.operate(cid,current['id'],current['version'],'delete')
                        case('cloud_cleanup',clean['state']=='deleted' and clean['cleanup_verification']=='provider_confirmed',
                            state=clean['state'],verification=clean['cleanup_verification'])
                    else:case('cloud_cleanup',current['state']=='prepared',state=current['state'],remote_absence_verified=False)
                except Exception as exc:case('cloud_cleanup',False,error_kind=type(exc).__name__,expiry_fallback_seconds=3600)
            report['usage']=summarize_calls();report['real_http_requests']=len(report['http_trace'])
            report['passed']=bool(report['cases']) and all(v['passed'] for v in report['cases']) and not report.get('error_kind')
            report['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')
            report['source_hashes_at_finish']=hashes()
            report['source_unchanged_during_evaluation']=report['source_hashes_at_start']==report['source_hashes_at_finish']
            httpx.HTTPTransport.handle_request=original;save();engine.dispose()
    print(json.dumps({'passed':report['passed'],'real_http_requests':report['real_http_requests'],'report':str(output)},ensure_ascii=False),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'evaluation_error':type(exc).__name__}),flush=True)
        raise SystemExit(2) from None
