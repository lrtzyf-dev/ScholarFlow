"""Read-only Zotero library snapshots with explicit local adoption and cursors."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import re
from sqlalchemy import select
from . import bibliography_network as net, bibliography_store as bib, source_store as sources
from .bibliography_models import ConnectorBinding, ConnectorItem, ConnectorItemRevision, ImportEntry
from .bibliography_parse import BibliographyError
from .credential_protection import protect, unprotect, CredentialProtectionError
from .source_runtime import conversation_scope
from .timeutil import utcnow

KEY=re.compile(r'^[A-Z0-9]{8}$')
MAX_ITEMS=2000


def get_binding(db,binding_id,cid,*,writing=False):
    conversation_scope(db,cid,writing=writing)
    row=db.get(ConnectorBinding,binding_id,populate_existing=True)
    if row is None or row.conversation_id!=cid: raise sources.SourceNotFound('当前会话没有此Zotero连接')
    return row


def output(row):
    return {k:getattr(row,k) for k in ('id','conversation_id','version','user_id','collection_key',
        'collection_name','status','access','cursor','last_batch_id','last_error')}


def credential(row):
    if row.status!='active' or not row.protected_credential: raise sources.SourceConflict('连接已暂停、失效或撤销')
    try: return unprotect(row.protected_credential)
    except CredentialProtectionError: raise sources.SourceError('系统凭据不可用，请重新连接Zotero') from None


def library_version(headers):
    value=headers.get('last-modified-version') or headers.get('Last-Modified-Version')
    if not isinstance(value,str) or not value.isdigit(): raise net.ConnectorError('Zotero响应缺少有效资料库版本，未推进同步游标')
    return int(value)


def list_collections(user_id,key,*,start=0):
    access=net.verify_zotero(user_id,key)
    rows,headers=net.zotero_request(f'/users/{user_id}/collections',key,{'limit':100,'start':start})
    if not isinstance(rows,list): raise net.ConnectorError('资料集响应无效')
    items=[]
    for row in rows:
        data=row.get('data',{}) if isinstance(row,dict) else {}
        if not isinstance(data,dict) or not KEY.fullmatch(str(row.get('key',''))): raise net.ConnectorError('资料集条目无效')
        items.append({'key':row['key'],'name':str(data.get('name','')),'parent':data.get('parentCollection')})
    return {'items':items,'access':access,'next_start':start+len(rows) if len(rows)==100 else None}


def snapshot(user_id,collection,key,*,check_cancel=lambda:None):
    """Read bounded selected members plus their children at one library version.

    Requests never carry a database writer. A changed library invalidates the
    whole candidate snapshot, and the committed cursor remains unchanged.
    """
    check_cancel()
    versions,headers=net.zotero_request(f'/users/{user_id}/collections/{collection}/items',key,{'format':'versions'})
    version=library_version(headers)
    if not isinstance(versions,dict) or len(versions)>500: raise net.ConnectorError('每个连接最多同步500个直接成员；请在Zotero拆分资料集')
    if any(not KEY.fullmatch(str(k)) or type(v) is not int for k,v in versions.items()): raise net.ConnectorError('条目版本映射无效')
    items={}
    def accept(rows,head):
        if library_version(head)!=version: raise net.ConnectorError('读取期间资料库已变化，请重试；同步游标未推进')
        if not isinstance(rows,list): raise net.ConnectorError('条目列表响应无效')
        for item in rows:
            if (not isinstance(item,dict) or not KEY.fullmatch(str(item.get('key',''))) or type(item.get('version')) is not int
                    or not isinstance(item.get('data'),dict)): raise net.ConnectorError('Zotero条目格式无效')
            if item['version']>version: raise net.ConnectorError('条目版本超出资料库快照')
            if item['key'] in items and (items[item['key']]['version']!=item['version'] or items[item['key']]['data']!=item['data']):
                raise net.ConnectorError('重复条目内容不一致')
            items[item['key']]=item
        if len(items)>MAX_ITEMS: raise net.ConnectorError('资料集及附件超过2000项，请拆分后连接')
    keys=sorted(versions)
    for start in range(0,len(keys),100):
        check_cancel()
        rows,head=net.zotero_request(f'/users/{user_id}/items',key,{'itemKey':','.join(keys[start:start+100]),'limit':100})
        accept(rows,head)
    if any(k not in items or items[k]['version']!=v for k,v in versions.items()): raise net.ConnectorError('资料集缺少请求的条目版本，未提交部分快照')
    parents=[k for k in keys if items[k]['data'].get('itemType') not in ('attachment','note','annotation')]
    # Fetch child attachments explicitly; collection membership alone does not
    # establish that all children were returned by the collection endpoint.
    for parent in parents:
        start=0
        while True:
            check_cancel()
            rows,head=net.zotero_request(f'/users/{user_id}/items/{parent}/children',key,{'limit':100,'start':start})
            accept(rows,head)
            if any(r['data'].get('parentItem')!=parent for r in rows): raise net.ConnectorError('子条目父记录不一致')
            if len(rows)<100: break
            start+=len(rows)
            if start>=MAX_ITEMS: raise net.ConnectorError('单条文献附件过多，请拆分资料集')
    check_cancel()
    final,head=net.zotero_request(f'/users/{user_id}/collections/{collection}/items',key,{'format':'versions'})
    if library_version(head)!=version or final!=versions: raise net.ConnectorError('读取期间资料集已变化，未推进游标')
    return version,items


def commit_snapshot(db,row,version,items):
    if version<row.cursor: raise sources.SourceConflict('远端版本早于已保存游标，未覆盖本地记录')
    existing={x.remote_key:x for x in db.scalars(select(ConnectorItem).where(ConnectorItem.binding_id==row.id))}
    changed=[]
    for key,item in items.items():
        old=existing.get(key)
        if old and item['version']<old.remote_version: raise sources.SourceConflict('条目版本倒退，未提交')
        if old and item['version']==old.remote_version and old.data.get('data')!=item.get('data'):
            raise sources.SourceConflict('相同条目版本内容变化，未覆盖旧版本')
        if old is None or old.remote_version!=item['version']: changed.append(item)
    entries=[];entry_keys=[]
    for item in changed:
        if item['data'].get('itemType') in ('attachment','note','annotation'): continue
        try: parsed={'metadata':net.zotero_metadata(item,row.user_id),'raw':json.dumps(item,ensure_ascii=False)}
        except BibliographyError as exc: parsed={'error':str(exc),'raw':json.dumps(item,ensure_ascii=False)}
        entries.append(parsed);entry_keys.append(item['key'])
    batch=None;entries_by_key={}
    if changed:
        raw=json.dumps(changed,ensure_ascii=False,sort_keys=True)
        if len(raw.encode())>4*1024*1024: raise sources.SourceError('同步快照超过4MiB，请缩小资料集')
        batch=bib.create_batch(db,cid=row.conversation_id,request_id=f'zotero-{row.id}-{version}-{hashlib.sha256(raw.encode()).hexdigest()[:12]}',
            title='Zotero · '+row.collection_name,format='zotero',raw=raw,entries=entries)
        saved=list(db.scalars(select(ImportEntry).where(ImportEntry.batch_id==batch.id).order_by(ImportEntry.position)))
        entries_by_key=dict(zip(entry_keys,saved));row.last_batch_id=batch.id
    for key,item in items.items():
        saved=existing.get(key)
        if saved is None:
            saved=ConnectorItem(binding_id=row.id,remote_key=key,remote_version=item['version'],data=deepcopy(item))
            db.add(saved);db.flush()
        elif saved.remote_version!=item['version']:
            saved.remote_version=item['version'];saved.data=deepcopy(item)
            saved.source_revision_id=None  # Older originals remain in attachment-version receipts.
        saved.state='available';saved.updated_at=utcnow()
        if key in entries_by_key: saved.import_entry_id=entries_by_key[key].id
        if item in changed:
            db.add(ConnectorItemRevision(item_id=saved.id,remote_version=item['version'],data_sha256=bib.digest(item),
                data=deepcopy(item),source_revision_id=batch.source_revision_id if batch else None))
    for key,saved in existing.items():
        if key not in items: saved.state='not_in_selected_collection';saved.updated_at=utcnow()
    row.cursor=version;row.version+=1;row.last_error='';row.updated_at=utcnow()
    db.flush()
    return {'binding':output(row),'changed_items':len(changed),'current_items':len(items),
        'batch_id':batch.id if batch else None,'remote_deleted_locally':False}


def item_output(row):
    data=row.data.get('data',{})
    return {'id':row.id,'key':row.remote_key,'version':row.remote_version,'state':row.state,
        'title':data.get('title') or data.get('filename') or row.remote_key,'item_type':data.get('itemType'),
        'parent_key':data.get('parentItem'),'content_type':data.get('contentType'),
        'link_mode':data.get('linkMode'),'import_entry_id':row.import_entry_id,'source_revision_id':row.source_revision_id,
        'fulltext_status':'fixed_original' if row.source_revision_id else 'not_acquired'}
