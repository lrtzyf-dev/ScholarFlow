"""Reauthorized byte-versioned reuse; all remote requests are synthetic."""
from copy import deepcopy
import json
import os

import httpx
import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import workspace, config
from app.agent import tools, loop
from app.agent.readonly_cache import verified_read
from app.agent import readonly_cache, readonly_versions
from app.models import Conversation, Chunk
from app.pipeline import embeddings, vectordb


@pytest.fixture
def project(db, tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    (root / 'a.txt').write_text('alpha\n' * 3000, encoding='utf-8')
    item = workspace.register(db, str(root))
    db.add(Conversation(id='cache-project', title='Cache', project_id=item.id)); db.commit()
    scope = workspace.capture(db, 'cache-project', project_id=item.id)
    return root, scope


def run(db, name, arguments, cache, *, cid='cache-project', scope=None, paper=None, action=None, run_id='logical-run'):
    return verified_read(db, name, arguments, binding={'conversation_id':cid,'run_id':run_id}, conversation_id=cid,
        project_scope=scope, scope_paper_id=paper, cache=cache,
        action=action or (lambda: tools.execute_tool(db,name,arguments,conversation_id=cid,project_scope=scope,scope_paper_id=paper)))


def test_unchanged_file_reuses_but_same_size_same_mtime_edit_invalidates(db, project):
    root, scope = project; cache, calls = {}, []
    args={'path':'a.txt','offset':0}
    action=lambda: calls.append('read') or tools.execute_tool(db,'read_project_file',args,project_scope=scope)
    first=run(db,'read_project_file',args,cache,scope=scope,action=action)
    assert run(db,'read_project_file',args,cache,scope=scope,action=action)==first and calls==['read']
    path=root/'a.txt'; metadata=path.stat(); path.write_bytes(path.read_bytes().replace(b'alpha',b'omega'))
    os.utime(path,ns=(metadata.st_atime_ns,metadata.st_mtime_ns))
    second=run(db,'read_project_file',args,cache,scope=scope,action=action)
    assert 'omega' in second and calls==['read','read'] and second!=first


def test_pages_rules_and_directory_membership_have_distinct_versions(db, project):
    root,scope=project;cache={}
    one=json.loads(run(db,'read_project_file',{'path':'a.txt'},cache,scope=scope))
    two=json.loads(run(db,'read_project_file',{'path':'a.txt','offset':8000},cache,scope=scope))
    assert one['offset']==0 and two['offset']==8000 and len(cache)==2
    (root/'AGENTS.md').write_text('Project-specific reading rules.',encoding='utf-8')
    updated=json.loads(run(db,'read_project_file',{'path':'a.txt'},cache,scope=scope))
    assert updated['rules'][0]['text']=='Project-specific reading rules.'
    before=run(db,'list_project_files',{},cache,scope=scope)
    (root/'new.txt').write_text('new',encoding='utf-8')
    after=run(db,'list_project_files',{},cache,scope=scope)
    assert 'new.txt' not in before and 'new.txt' in after


@pytest.mark.parametrize('path',['.','./','.\\'])
def test_root_aliases_work_through_verified_listing_and_invalidate_on_change(db,project,path):
    root,scope=project;cache,calls={},[];args={'path':path}
    def action():
        calls.append('list')
        return tools.execute_tool(db,'list_project_files',args,project_scope=scope)
    first=run(db,'list_project_files',args,cache,scope=scope,action=action)
    assert json.loads(first)==workspace.list_files(scope)
    assert json.loads(first)['path']==''
    assert run(db,'list_project_files',args,cache,scope=scope,action=action)==first
    assert calls==['list'] and cache
    (root/'new.txt').write_text('new',encoding='utf-8')
    current=run(db,'list_project_files',args,cache,scope=scope,action=action)
    assert 'new.txt' in current and current!=first and calls==['list','list']


def test_actual_rg_search_reuses_complete_snapshot_and_real_search_handles_ignore_rules(db, project, monkeypatch):
    from app import project_search
    root,scope=project;cache,calls={},[];original=project_search._run
    monkeypatch.setattr(project_search,'_run',lambda *a,**k:calls.append('rg') or original(*a,**k))
    args={'query':'needle'}
    assert json.loads(run(db,'search_project_files',args,cache,scope=scope))['matches']==[]
    run(db,'search_project_files',args,cache,scope=scope);assert calls==['rg'] and cache
    (root/'new.txt').write_text('needle match',encoding='utf-8')
    assert json.loads(run(db,'search_project_files',args,cache,scope=scope))['matches'][0]['path']=='new.txt'
    (root/'.gitignore').write_text('new.txt\n',encoding='utf-8')
    assert json.loads(run(db,'search_project_files',args,cache,scope=scope))['matches']==[]
    assert calls==['rg','rg','rg']


def test_search_pagination_keeps_snapshot_contract(db, project):
    root,scope=project;cache={};(root/'a.txt').write_text('needle\n'*100,encoding='utf-8')
    first=json.loads(run(db,'search_project_files',{'query':'needle'},cache,scope=scope))
    args={'query':'needle','offset':first['next_offset'],'snapshot':first['snapshot']}
    second=json.loads(run(db,'search_project_files',args,cache,scope=scope))
    assert first['matches'][0]['line']==1 and second['matches'][0]['line']==41
    (root/'b.txt').write_text('needle',encoding='utf-8')
    result=json.loads(run(db,'search_project_files',args,cache,scope=scope))
    assert result.get('error') and '变化' in result['error']


@pytest.mark.parametrize('change',['archive','project','disabled'])
def test_cached_project_observation_never_bypasses_current_authority(db,project,monkeypatch,change):
    from app.timeutil import utcnow
    root,scope=project;cache={};args={'path':'a.txt'}
    run(db,'read_project_file',args,cache,scope=scope)
    if change=='archive':db.get(Conversation,'cache-project').archived_at=utcnow();db.commit()
    elif change=='project':db.get(Conversation,'cache-project').project_id=None;db.commit()
    else:monkeypatch.setattr(tools,'tool_policy_error',lambda n:'Current tool disabled')
    with pytest.raises(Exception):
        run(db,'read_project_file',args,cache,scope=scope,action=lambda:pytest.fail('authorization bypass'))


def test_unknown_versions_and_unsupported_tools_always_execute_without_caching(db,project,monkeypatch):
    _,scope=project;cache,calls={},[]
    monkeypatch.setattr(readonly_versions,'project_version',lambda *a,**k:(_ for _ in ()).throw(readonly_versions.VersionUnavailable()))
    for _ in range(2):run(db,'read_project_file',{'path':'a.txt'},cache,scope=scope,action=lambda:calls.append(1) or '{"value":1}')
    for _ in range(2):run(db,'git_project_status',{},cache,scope=scope,action=lambda:calls.append(1) or '{"value":1}')
    assert calls==[1,1,1,1] and cache=={}


def test_tampered_cache_result_cannot_replace_actual_observation_or_double_dispatch(db,project):
    _,scope=project;cache,calls={},[];args={'path':'a.txt'}
    action=lambda:calls.append(1) or tools.execute_tool(db,'read_project_file',args,project_scope=scope)
    original=run(db,'read_project_file',args,cache,scope=scope,action=action)
    next(iter(cache.values()))['result']='{"text":"tampered"}'
    assert run(db,'read_project_file',args,cache,scope=scope,action=action)==original and calls==[1,1]


def test_cache_is_bounded_and_preserves_different_read_ranges(db,project,monkeypatch):
    _,scope=project;cache={};monkeypatch.setattr(readonly_cache,'MAX_ENTRIES',2)
    for offset in (0,1,2):run(db,'read_project_file',{'path':'a.txt','offset':offset},cache,scope=scope)
    assert len(cache)==2
    assert {item['identity']['arguments']['offset'] for item in cache.values()}=={1,2}


@pytest.fixture
def api_library(db,seeded_paper,monkeypatch):
    paper=seeded_paper;db.add(Conversation(id='cache-library',title='Library'));db.commit()
    monkeypatch.setattr(config.settings,'embedding_base_url','https://synthetic-cache.example.test/v1')
    monkeypatch.setattr(config.settings,'embedding_api_key','synthetic-cache-key')
    monkeypatch.setattr(config.settings,'embedding_model','Embedding-Case-Exact')
    monkeypatch.setattr(config.settings,'min_relevance_score',-1)
    base=vectordb.NumpyVectorStore();base.embedding_fingerprint=vectordb.current_embedding_fingerprint();base._save()
    vector=base.matrix[0].tolist();calls=[]
    def handle(request):
        body=json.loads(request.content);calls.append(body)
        return httpx.Response(200,json={'data':[{'index':i,'embedding':vector} for i in range(len(body['input']))],
            'usage':{'prompt_tokens':3,'total_tokens':3}})
    real=httpx.Client
    monkeypatch.setattr(httpx,'Client',lambda *a,**k:real(*a,transport=httpx.MockTransport(handle),**k))
    monkeypatch.setattr(embeddings,'_embedder',embeddings.APIEmbedder(synthetic=True))
    return paper,calls


def test_unchanged_retrieval_avoids_second_paid_embedding_and_survives_serialized_checkpoint(db,api_library):
    paper,calls=api_library;cache={};args={'query':'Transformer attention','k':2}
    first=run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    assert json.loads(first)['evidence'] and len(calls)==1 and cache
    restored=json.loads(json.dumps(cache))
    with Session(db.get_bind()) as fresh:
        assert run(fresh,'retrieve_evidence',args,restored,cid='cache-library',paper=paper.id)==first
    assert len(calls)==1
    from app.ledger_models import CallRecord
    assert db.query(CallRecord).count()==1 and db.query(CallRecord).first().synthetic


@pytest.mark.parametrize('change',['chunk','fts','numpy','embedding','range','scope'])
def test_library_inputs_and_scope_reliably_invalidate_cached_retrieval(db,api_library,monkeypatch,change):
    paper,calls=api_library;cache={};args={'query':'Transformer attention','k':2}
    run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id);assert len(calls)==1
    if change=='chunk':
        chunk=db.query(Chunk).filter_by(paper_id=paper.id).first();chunk.content+=' changed';db.commit()
    elif change=='fts':
        db.execute(text("INSERT INTO chunks_fts(chunk_id,paper_id,content) VALUES('synthetic-extra',:p,'attention')"),{'p':paper.id});db.commit()
    elif change=='numpy':
        store=vectordb.NumpyVectorStore();path=store._matrix_file();before=path.stat()
        store.matrix[0,0]+=.01;store._save();os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    elif change=='embedding':
        monkeypatch.setattr(config.settings,'embedding_model','Different-Exact-Case')
        # New model invalidates the existing vectors, so retrieval uses FTS
        # and must still execute the actual tool rather than reuse dense hits.
    elif change=='range':args['k']=1
    else:
        db.add(Conversation(id='cache-other',title='Other'));db.commit()
    invoked=[]
    result=run(db,'retrieve_evidence',args,cache,cid='cache-other' if change=='scope' else 'cache-library',paper=paper.id,
        action=lambda:invoked.append(1) or tools.execute_tool(db,'retrieve_evidence',args,scope_paper_id=paper.id,
            conversation_id='cache-other' if change=='scope' else 'cache-library'))
    assert invoked==[1] and 'evidence' in json.loads(result)
    assert len(calls)==(2 if change=='scope' else 1)


def test_cached_retrieval_refuses_archived_conversation_and_selected_paper_conflict(db,api_library):
    from app.timeutil import utcnow
    paper,calls=api_library;cache={};args={'query':'attention'}
    run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    with pytest.raises(PermissionError):
        run(db,'retrieve_evidence',{**args,'paper_id':'another'},cache,cid='cache-library',paper=paper.id)
    db.get(Conversation,'cache-library').archived_at=utcnow();db.commit()
    with pytest.raises(Exception):run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    assert len(calls)==1


def test_real_executor_repeated_retrieval_uses_one_embedding_call(db,api_library,monkeypatch):
    from app.llm import LLMResponse
    paper,calls=api_library;rounds,snapshots=[],[]
    class Model:
        def stream_chat(self,messages,tools=None):
            rounds.append(deepcopy(messages))
            yield 'final',LLMResponse('',tool_calls=[{'id':f'retrieve-{len(rounds)}','name':'retrieve_evidence',
                'arguments':{'query':'Transformer attention','k':2}}]) if len(rounds)<3 else LLMResponse('Understood.')
    monkeypatch.setattr(loop,'get_llm',lambda:Model())
    monkeypatch.setattr(loop,'get_tool_specs',lambda:[s for s in tools.get_tool_specs() if s['name']=='retrieve_evidence'])
    monkeypatch.setattr(loop.hooks_store,'run_hooks',lambda *a,**k:{'ran':0,'blocked':False,'feedback':''})
    events=list(loop.run_agent(db,'Find relevant passages.',paper_id=paper.id,conversation_id='cache-library',
        save_checkpoint=lambda s:snapshots.append(deepcopy(s))))
    assert len(calls)==1 and len([k for k,_ in events if k=='tool_result'])==2
    assert snapshots[-1]['readonly_cache'] and any(k=='answer' for k,_ in events)


@pytest.mark.parametrize('name',['read_project_file','list_project_files','search_project_files'])
def test_project_aba_never_labels_intermediate_result_as_original(db,project,name):
    root,scope=project;cache={};path=root/'a.txt';original=path.read_bytes()
    args={'path':'a.txt'} if name=='read_project_file' else {'query':'needle'} if name=='search_project_files' else {}
    def intermediate():
        if name=='read_project_file':path.write_text('intermediate needle',encoding='utf-8')
        else:(root/'transient.txt').write_text('needle',encoding='utf-8')
        try:return tools.execute_tool(db,name,args,project_scope=scope)
        finally:
            if name=='read_project_file':path.write_bytes(original)
            else:(root/'transient.txt').unlink()
    observed=run(db,name,args,cache,scope=scope,action=intermediate)
    if name=='search_project_files':
        assert json.loads(observed)['matches']==[] and cache
        assert run(db,name,args,cache,scope=scope)==observed
        return
    assert ('intermediate' in observed if name=='read_project_file' else 'transient.txt' in observed)
    assert cache=={}
    current=run(db,name,args,cache,scope=scope)
    assert observed!=current and 'transient.txt' not in current


def test_rule_aba_cannot_publish_another_rule_revision(db,project):
    root,scope=project;cache={};rule=root/'AGENTS.md';rule.write_text('rule A',encoding='utf-8')
    def intermediate():
        rule.write_text('rule B',encoding='utf-8')
        try:return tools.execute_tool(db,'read_project_file',{'path':'a.txt'},project_scope=scope)
        finally:rule.write_text('rule A',encoding='utf-8')
    result=run(db,'read_project_file',{'path':'a.txt'},cache,scope=scope,action=intermediate)
    assert 'rule B' in result and cache=={}
    assert 'rule A' in run(db,'read_project_file',{'path':'a.txt'},cache,scope=scope)


def test_permission_revoked_during_version_lookup_rejects_cached_body(db,project,monkeypatch):
    _,scope=project;cache={};args={'path':'a.txt'}
    run(db,'read_project_file',args,cache,scope=scope)
    real=readonly_versions.project_version;calls=[]
    def revoke(*a,**k):
        result=real(*a,**k);calls.append(1)
        if len(calls)==2:monkeypatch.setattr(tools,'tool_policy_error',lambda n:'revoked during lookup')
        return result
    monkeypatch.setattr(readonly_versions,'project_version',revoke)
    with pytest.raises(PermissionError):
        run(db,'read_project_file',args,cache,scope=scope,action=lambda:pytest.fail('cached body escaped'))


def test_directory_links_disable_observation_reuse(db,project,monkeypatch):
    from types import SimpleNamespace
    root,scope=project;cache={};calls=[]
    real=readonly_versions.os.scandir
    class Linked:
        def __enter__(self):return iter([SimpleNamespace(name='changing-link',is_symlink=lambda:True)])
        def __exit__(self,*a):pass
    # Inject only at identity construction; the actual producer still lists
    # its real directory and is always called.
    monkeypatch.setattr(readonly_versions.os,'scandir',lambda p:Linked())
    monkeypatch.setattr(workspace,'list_files',lambda *a,**k:{'path':'','entries':[],'next_offset':None,'limited':False})
    for _ in range(2):
        run(db,'list_project_files',{},cache,scope=scope,action=lambda:calls.append(1) or '{}')
    assert calls==[1,1] and cache=={}


def test_file_hash_race_checks_same_size_modification_metadata(tmp_path,monkeypatch):
    from types import SimpleNamespace
    path=tmp_path/'race.txt';path.write_text('original',encoding='utf-8');actual=path.stat();calls=[]
    def status(fd):
        calls.append(1)
        return SimpleNamespace(st_dev=actual.st_dev,st_ino=actual.st_ino,st_size=actual.st_size,
            st_mtime_ns=actual.st_mtime_ns+(1 if len(calls)>1 else 0),st_ctime_ns=actual.st_ctime_ns)
    monkeypatch.setattr(readonly_versions.os,'fstat',status)
    with pytest.raises(readonly_versions.VersionUnavailable):
        readonly_versions.file_bytes(path,readonly_versions.Budget())


def test_library_aba_reruns_retrieval_but_reuses_model_bound_vector(db,api_library):
    paper,calls=api_library;cache={};args={'query':'Transformer attention','k':2}
    chunk=db.query(Chunk).filter_by(paper_id=paper.id).first();original=chunk.content
    def intermediate():
        chunk.content='intermediate Transformer attention evidence';db.commit()
        try:return tools.execute_tool(db,'retrieve_evidence',args,scope_paper_id=paper.id,conversation_id='cache-library')
        finally:chunk.content=original;db.commit()
    observed=run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id,action=intermediate)
    assert 'intermediate' in observed
    current=run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    assert current!=observed and 'intermediate' not in current and len(calls)==1
    assert all(key.startswith('embedding-query/') for key in cache)


@pytest.mark.parametrize('change',['key','model','endpoint'])
def test_query_vector_identity_uses_actual_connection_and_reauthorizes(db,api_library,monkeypatch,change):
    paper,calls=api_library;cache={};args={'query':'Transformer attention','k':2}
    run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id);assert len(calls)==1
    field={'key':'embedding_api_key','model':'embedding_model','endpoint':'embedding_base_url'}[change]
    value={'key':'synthetic-rotated-key','model':'Case-sensitive-Other','endpoint':'https://other.example.test/v1'}[change]
    monkeypatch.setattr(config.settings,field,value)
    # Preserve a compatible synthetic index so this checks query caching,
    # rather than the separate stale-index FTS-only fallback.
    store=vectordb.NumpyVectorStore();store.embedding_fingerprint=vectordb.current_embedding_fingerprint();store._save()
    run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    assert len(calls)==2 and len(cache)==2


def test_revocation_or_stale_captured_embedder_cannot_reuse_query_vector(db,api_library,monkeypatch):
    paper,calls=api_library;cache={};args={'query':'Transformer attention'}
    captured=embeddings.embedding_connection()
    run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    monkeypatch.setattr(config.settings,'embedding_api_key','synthetic-rotated-key')
    store=vectordb.NumpyVectorStore();store.embedding_fingerprint=vectordb.current_embedding_fingerprint();store._save()
    monkeypatch.setattr(embeddings,'_embedder',embeddings.APIEmbedder(connection=captured,synthetic=True))
    with pytest.raises(ValueError,match='凭据已变化'):
        run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    assert len(calls)==1
    def revoked(*a,**k):raise ValueError('credential revoked')
    monkeypatch.setattr(config,'require_available_credentials',revoked)
    with pytest.raises(ValueError,match='credential revoked'):
        run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    assert len(calls)==1


def test_query_cache_rejects_tampered_vectors_and_isolates_runs(db,api_library):
    paper,calls=api_library;cache={};args={'query':'Transformer attention'}
    run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    item=next(iter(cache.values()));item['vectors'][0][0]+=.123
    run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    assert len(calls)==2
    run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id,run_id='another-run')
    assert len(calls)==3


def test_managed_db_generation_changes_rerun_search_with_same_query_vector(db,api_library):
    from app import source_store as sources, source_library
    from app import library_index_store as index_store, library_index_service as index_service
    from app.source_documents import save_extraction
    from app.source_parsing import extract, MEDIA
    from app.library_index_models import LibraryIndexOperation
    # Earlier API cases may have closed a TestClient and correctly stopped the
    # service. This direct-service case models a fresh application startup;
    # never weaken the production shutdown fence for a test's ordering.
    index_service.recover_startup()
    _,calls=api_library;scope=sources.SourceScope('conversation','cache-library')
    raw=b'Transformer attention managed paper.\n';parsed=extract(raw,MEDIA['md'])
    record=sources.capture_source(db,scope=scope,authorization=sources.SourceAuthorization(scope,'managed-upload','upload'),
        kind='upload',title='managed.md',raw_bytes=raw,text=parsed.text,media_type=MEDIA['md'],text_origin='parsed_text',parser=parsed.parser)
    save_extraction(db,record['revision_id'],scope=scope,extraction=parsed);db.commit()
    imported=source_library.import_selected(db,scope=scope,revision_ids=[record['revision_id']],request_id='managed-import')['items'][0]
    assert imported['status']=='imported';pid=imported['paper_id']
    def rebuild(request):
        state=index_store.state(db,pid)
        op,_=index_store.create_operation(db,pid,state['indexed_revision_id'],conversation_id='cache-library',request_id=request,
            expected_version=state['version'],expected_primary_id=state['primary_revision_id'],set_primary=False,index_mode='semantic')
        ident=op.id;db.commit();index_service.execute(ident);db.expire_all()
        assert db.get(LibraryIndexOperation,ident).status=='done'
        return ident
    rebuild('first-generation');cache={};args={'query':'Transformer attention','k':2}
    first=json.loads(run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=pid))['evidence']
    old_ids={item['chunk_id'] for item in first};assert old_ids
    rebuild('second-generation');before=len(calls)
    current=json.loads(run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=pid))['evidence']
    assert current and not old_ids&{item['chunk_id'] for item in current} and len(calls)==before


def test_durable_child_uses_one_actual_embedding_and_persists_query_vector(db,api_library,monkeypatch):
    from app.agent import subagent
    from app import subtask_store
    from app.runtime_context import bind_runtime_config
    from app.llm import LLMResponse
    from app.runtime_models import RuntimeRun,RunAttempt
    from app.goal_ledger import ensure_run_goal
    paper,calls=api_library
    goal=ensure_run_goal(db,'cache-parent','cache-library','Find attention passages.')
    db.add(RuntimeRun(id='cache-parent',kind='foreground',conversation_id='cache-library',
        goal_id=goal['id'],current_attempt_id='first',status='running'))
    db.add(RunAttempt(id='first',run_id='cache-parent',number=1,writer_token='cache-writer',status='running'));db.commit()
    monkeypatch.setattr(subagent,'_safe_tool_specs',lambda:[spec for spec in tools.get_tool_specs() if spec['name']=='retrieve_evidence'])
    class Model:
        def chat(self,messages,**kwargs):
            n=sum(m['role']=='tool' for m in messages)
            return LLMResponse('Child findings.') if n==2 else LLMResponse('',[
                {'id':str(n),'name':'retrieve_evidence','arguments':{'query':'Transformer attention','k':2}}])
    with bind_runtime_config({'run_id':'cache-parent','attempt_id':'first','conversation_id':'cache-library','goal_id':goal['id']}):
        result=subagent.run_subagents(db,Model(),[{'topic':'Find attention passages'}],scope_paper_id=paper.id)[0]
    assert len(calls)==1 and len(result['evidence'])==2 and result['outcome']=='partial',result
    child=subtask_store.list_children('cache-parent')[0]
    assert child['state']=='completed'
    assert child['checkpoint']['readonly_cache'] and all(key.startswith('embedding-query/') for key in child['checkpoint']['readonly_cache'])


def test_long_lived_session_retrieval_refreshes_current_chunk_and_paper(db,api_library):
    from app.models import Paper
    paper,calls=api_library;cache={};args={'query':'Transformer attention','k':2}
    chunk=db.query(Chunk).filter_by(paper_id=paper.id).first()
    run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    old_text,old_title=chunk.content,paper.title
    with Session(db.get_bind()) as writer:
        writer.get(Chunk,chunk.id).content='Transformer attention fresh current evidence.'
        writer.get(Paper,paper.id).title='Current paper title';writer.commit()
    assert chunk.content==old_text and paper.title==old_title
    result=json.loads(run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id))
    assert any('fresh current evidence' in item['quote'] for item in result['evidence'])
    assert all(item['paper_title']=='Current paper title' for item in result['evidence']) and len(calls)==1
    assert json.loads(run(db,'list_papers',{},cache,cid='cache-library',paper=paper.id))['papers'][0]['title']=='Current paper title'


def test_query_vector_byte_budget_counts_the_serialized_entry(db,api_library,monkeypatch):
    paper,calls=api_library;cache={};args={'query':'Transformer attention'}
    monkeypatch.setattr(readonly_cache,'MAX_BYTES',100)
    for _ in range(2):run(db,'retrieve_evidence',args,cache,cid='cache-library',paper=paper.id)
    assert len(calls)==2 and not cache


def test_search_progress_ignores_query_snapshot_noise_but_keeps_new_ranges_and_bytes():
    from app.agent.active_context import progress_fingerprint as fingerprint
    empty={'matches':[],'total':0,'files':0,'snapshot':'query-a','next_offset':None,'limited':False,'warnings':[]}
    assert fingerprint('search_project_files',{'query':'A'},empty)==fingerprint(
        'search_project_files',{'query':'B','snapshot':'query-b'},{**empty,'snapshot':'query-b'})
    one={'path':'a.md','sha256':'bytes-a','line':1,'parts':[{'text':'fact'}]}
    two={'path':'b.md','sha256':'bytes-b','line':2,'parts':[{'text':'other'}]}
    left={**empty,'matches':[one,two],'total':2,'files':2}
    right={**left,'snapshot':'different-options','matches':[two,one]}
    assert fingerprint('search_project_files',{},left)==fingerprint('search_project_files',{},right)
    assert fingerprint('search_project_files',{'offset':0},left)!=fingerprint('search_project_files',{'offset':40},left)
    assert fingerprint('search_project_files',{},left)!=fingerprint('search_project_files',{},
        {**left,'matches':[{**one,'sha256':'new-byte-version'},two]})


@pytest.mark.parametrize('options',[
    {},{'case_sensitive':True},{'whole_word':True},{'regex':True,'query':'[Nn]eedle'},
    {'hidden':True},{'ignored':True},{'path':'nested'}])
def test_snapshot_search_equals_actual_rg_for_options_and_unicode(db,project,options):
    from app import project_search
    root,scope=project;cache={}
    (root/'a.txt').write_bytes('Needle nEeDle needlework\r\n中文 needle café\n'.encode('utf-8'))
    (root/'.hidden.txt').write_text('needle hidden',encoding='utf-8')
    (root/'nested').mkdir();(root/'nested'/'b.txt').write_text('needle nested',encoding='utf-8')
    (root/'binary.bin').write_bytes(b'needle\x00hidden')
    args={'query':'needle',**options}
    real=project_search.search(scope,project_search.SearchRequest.model_validate(args))
    cached=json.loads(run(db,'search_project_files',args,cache,scope=scope))
    assert cached==real and cache
    restored=json.loads(json.dumps(cache))
    assert json.loads(run(db,'search_project_files',args,restored,scope=scope,
        action=lambda:pytest.fail('complete source snapshot should reuse')))==real


@pytest.mark.parametrize('cause',['ignore','glob','size','files'])
def test_search_without_complete_input_proof_runs_real_engine_each_time(db,project,monkeypatch,cause):
    from app.agent import search_snapshot_cache as snapshots
    root,scope=project;cache={};args={'query':'alpha'};calls=[]
    if cause=='ignore':(root/'.gitignore').write_text('ignored.txt\n',encoding='utf-8')
    elif cause=='glob':args['include']='*.txt'
    elif cause=='size':monkeypatch.setattr(snapshots,'MAX_INPUT_BYTES',10)
    else:
        monkeypatch.setattr(snapshots,'MAX_INPUT_FILES',1);(root/'b.txt').write_text('alpha',encoding='utf-8')
    def actual():
        calls.append(1);return tools.execute_tool(db,'search_project_files',args,project_scope=scope)
    for _ in range(2):assert json.loads(run(db,'search_project_files',args,cache,scope=scope,action=actual))['matches']
    assert calls==[1,1] and cache=={}


def test_search_cache_requires_actual_producer_receipt_and_rejects_tampering(db,project):
    root,scope=project;cache={};args={'query':'alpha'};calls=[]
    (root/'a.txt').write_text('alpha\n',encoding='utf-8')
    for _ in range(2):
        run(db,'search_project_files',args,cache,scope=scope,
            action=lambda:calls.append(1) or '{"matches":[],"total":0}')
    assert calls==[1,1] and cache=={}
    original=run(db,'search_project_files',args,cache,scope=scope)
    next(iter(cache.values()))['receipt']['input_fingerprint']='tampered'
    assert run(db,'search_project_files',args,cache,scope=scope)==original
    next(iter(cache.values()))['result']='{"matches":[]}'
    assert run(db,'search_project_files',args,cache,scope=scope)==original


def test_search_file_same_size_same_mtime_change_never_hits_previous_snapshot(db,project):
    root,scope=project;cache={};path=root/'a.txt';path.write_text('needle',encoding='utf-8')
    original=run(db,'search_project_files',{'query':'needle'},cache,scope=scope)
    stamp=path.stat();path.write_text('absent',encoding='utf-8');os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    current=run(db,'search_project_files',{'query':'needle'},cache,scope=scope)
    assert json.loads(original)['matches'] and json.loads(current)['matches']==[]


def test_empty_snapshot_validates_regex_and_partial_search_is_never_cached(db,project,monkeypatch):
    from app import project_search
    root,scope=project;cache={};(root/'a.txt').unlink()
    invalid=json.loads(run(db,'search_project_files',{'query':'[','regex':True},cache,scope=scope))
    assert invalid.get('error') and not cache
    (root/'a.txt').write_text('needle\n'*20,encoding='utf-8')
    monkeypatch.setattr(project_search,'MAX_OUTPUT',100)
    partial=json.loads(run(db,'search_project_files',{'query':'needle'},cache,scope=scope))
    assert partial['limited'] and not cache


def test_search_body_modified_after_real_producer_cannot_be_cached(db,project):
    root,scope=project;cache={};(root/'a.txt').write_text('needle\n',encoding='utf-8');args={'query':'needle'}
    def modified():
        payload=json.loads(tools.execute_tool(db,'search_project_files',args,project_scope=scope))
        payload['matches'][0]['parts']=[{'text':'forged after actual producer'}]
        return json.dumps(payload)
    assert 'forged after actual producer' in run(db,'search_project_files',args,cache,scope=scope,action=modified)
    assert not cache
    assert 'forged after actual producer' not in run(db,'search_project_files',args,cache,scope=scope)


def test_search_hit_rechecks_archive_after_capturing_source_version(db,project,monkeypatch):
    from app.agent import search_snapshot_cache as snapshots
    from app.timeutil import utcnow
    root,scope=project;cache={};(root/'a.txt').write_text('needle\n',encoding='utf-8');args={'query':'needle'}
    run(db,'search_project_files',args,cache,scope=scope)
    real=snapshots.capture;checks=[]
    def revoked(*a,**k):
        result=real(*a,**k);checks.append(1)
        if len(checks)==2:
            with Session(db.get_bind()) as writer:
                writer.get(Conversation,'cache-project').archived_at=utcnow();writer.commit()
        return result
    monkeypatch.setattr(snapshots,'capture',revoked)
    with pytest.raises(Exception):
        run(db,'search_project_files',args,cache,scope=scope,action=lambda:pytest.fail('cache hit should only reauthorize'))


@pytest.mark.parametrize('filename',['.GitIgnore','.IGNORE','.RgIgNoRe'])
def test_nested_windows_case_variant_ignore_file_matches_real_rg_selection(db,project,filename):
    from app import project_search
    root,scope=project;cache={};(root/'a.txt').write_text('unrelated',encoding='utf-8')
    nested=root/'nested';nested.mkdir();(nested/'b.txt').write_text('needle',encoding='utf-8')
    (nested/filename).write_text('b.txt\n',encoding='utf-8')
    args={'query':'needle'}
    actual=project_search.search(scope,project_search.SearchRequest.model_validate(args))
    observed=json.loads(run(db,'search_project_files',args,cache,scope=scope))
    assert observed==actual
    assert not cache


@pytest.mark.parametrize('filename,canonical',[('.gıtignore','.gitignore'),('.ıgnore','.ignore'),('.rgıgnore','.rgignore')])
def test_windows_filesystem_case_equivalence_is_checked_beyond_python_casefold(db,project,filename,canonical):
    from app import project_search
    root,scope=project;cache={};(root/'a.txt').write_text('unrelated',encoding='utf-8')
    nested=root/'nested';nested.mkdir();(nested/'b.txt').write_text('needle',encoding='utf-8')
    (nested/filename).write_text('b.txt\n',encoding='utf-8')
    args={'query':'needle'}
    actual=project_search.search(scope,project_search.SearchRequest.model_validate(args))
    observed=json.loads(run(db,'search_project_files',args,cache,scope=scope))
    assert observed==actual
    if (nested/canonical).exists():assert not cache
