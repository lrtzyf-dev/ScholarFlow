"""Explicit attachment download, bounded bytes and no cross-origin credentials."""
from __future__ import annotations
import hashlib
import re
from urllib.parse import urljoin,urlsplit
import httpx
from sqlalchemy import select
from . import bibliography_network as net, source_store as sources
from .bibliography_models import ConnectorItem,ConnectorAttachmentVersion,ImportEntry,PaperIdentifier
from .models import Paper
from .source_models import SourceLibraryImport
from .source_runtime import conversation_scope
from .source_parsing import extract
from .source_documents import save_extraction
from . import config


def download_pdf(user_id,item_key,credential,expected_md5):
    from .netguard import request_guard_hook,assert_public_url
    if not isinstance(expected_md5,str) or not re.fullmatch(r'[a-fA-F0-9]{32}',expected_md5): raise net.ConnectorError('附件没有可信MD5版本，未下载')
    url=f'https://api.zotero.org/users/{user_id}/items/{item_key}/file'
    try:
        for hop in range(6):
            # The key is sent only on the original official API request. Each
            # signed public redirect uses a new client without any credentials.
            headers={'Zotero-API-Key':credential,'Zotero-API-Version':'3'} if hop==0 else {}
            with httpx.Client(timeout=httpx.Timeout(60,connect=8),follow_redirects=False,headers=headers,
                    event_hooks={'request':[request_guard_hook]}) as client:
                with client.stream('GET',url) as response:
                    if response.status_code in (301,302,303,307,308):
                        target=urljoin(url,response.headers.get('location',''))
                        parts=urlsplit(target)
                        if parts.scheme!='https' or parts.username or parts.password or target==url: raise net.ConnectorError('附件下载跳转无效')
                        assert_public_url(target);url=target;continue
                    if response.status_code in (401,403): raise net.ConnectorError('附件读取权限不可用，请重新连接',invalid_credential=True)
                    if response.status_code!=200: raise net.ConnectorError(f'附件服务返回HTTP {response.status_code}，原件未保存')
                    raw=bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw)>sources.MAX_RAW_BYTES: raise net.ConnectorError('附件超过100MiB，原件未保存')
                    raw=bytes(raw)
                    if hashlib.md5(raw).hexdigest()!=expected_md5.lower(): raise net.ConnectorError('附件字节与所选远端版本不匹配，请先重新同步')
                    if not raw.startswith(b'%PDF-'): raise net.ConnectorError('附件没有有效PDF标头，未作为论文保存')
                    return raw
        raise net.ConnectorError('附件跳转次数过多，原件未保存')
    except (httpx.HTTPError,OSError,ValueError) as exc:
        if isinstance(exc,net.ConnectorError): raise
        raise net.ConnectorError('附件下载失败；未显示凭据或临时下载链接') from None


def prepare(raw):
    return extract(raw,'application/pdf')


def save(db,binding,item,raw,extraction):
    scope=conversation_scope(db,binding.conversation_id)
    previous=db.scalar(select(ConnectorAttachmentVersion).where(ConnectorAttachmentVersion.item_id==item.id,
        ConnectorAttachmentVersion.remote_version==item.remote_version))
    if previous:
        sources.read_revision(db,previous.source_revision_id,scope=scope)
        return previous.source_revision_id
    data=item.data['data'];origin=f'https://api.zotero.org/users/{binding.user_id}/items/{item.remote_key}/file'
    record=sources.capture_source(db,scope=scope,authorization=sources.SourceAuthorization(scope,
        f'zotero:{item.id}:{item.remote_version}','web'),kind='web',title=str(data.get('title') or data.get('filename') or item.remote_key)[:200],
        raw_bytes=raw,text=extraction.text,media_type='application/pdf',origin_ref=origin,parser=extraction.parser,text_origin='parsed_text')
    save_extraction(db,record['revision_id'],scope=scope,extraction=extraction)
    db.add(ConnectorAttachmentVersion(item_id=item.id,remote_version=item.remote_version,remote_md5=data['md5'],source_revision_id=record['revision_id']))
    item.source_revision_id=record['revision_id'];db.flush()
    return record['revision_id']


def attach_to_metadata(db,binding,item,revision_id):
    """Attach explicitly to the already-adopted parent identity; never replace."""
    from . import bibliography_store as bib
    parent_key=item.data['data'].get('parentItem')
    if not parent_key: raise sources.SourceConflict('此附件没有父书目，请在来源阅读页单独选择入库')
    identifier=db.scalar(select(PaperIdentifier).where(PaperIdentifier.kind=='zotero',
        PaperIdentifier.value==f'user:{binding.user_id}:{parent_key}'))
    paper=db.get(Paper,identifier.paper_id) if identifier else None
    if paper is None: raise sources.SourceConflict('请先在书目预览中采纳父条目，再附加全文')
    scope=conversation_scope(db,binding.conversation_id)
    record=sources.read_revision(db,revision_id,scope=scope,include_raw=True)
    receipt=db.get(SourceLibraryImport,revision_id)
    if receipt:
        if receipt.paper_id!=paper.id: raise sources.SourceConflict('此原件已归属另一文献，请人工核对身份')
        return paper.id,None
    if paper.status!='metadata_only' or paper.content_hash or paper.pdf_path:
        raise sources.SourceConflict('该文献已有固定全文，不能自动覆盖；新版本原件仍可在来源阅读页查看')
    other=db.scalar(select(Paper).where(Paper.content_hash==record['raw_sha256'],Paper.id!=paper.id))
    if other: raise sources.SourceConflict('该全文已在文献库其他记录中，请先核对重复书目；未新建重复记录')
    path=config.PDF_DIR/f'{paper.id}.pdf'
    paper.pdf_path=str(path);paper.content_hash=record['raw_sha256'];paper.status='uploaded';paper.error=''
    db.add(SourceLibraryImport(revision_id=revision_id,raw_sha256=record['raw_sha256'],paper_id=paper.id,
        request_id=f'zotero-{item.id}-{item.remote_version}',chunk_locators={}))
    db.flush()
    from .library_sources import ensure_owned
    ensure_owned(db,paper.id,revision_id)
    created=False
    try:
        with path.open('xb') as stream:
            created=True;stream.write(record['raw_bytes'])
    except OSError:
        if created:path.unlink(missing_ok=True)
        raise sources.SourceError('无法保存文献原件；固定来源已保留，未替换已有文件') from None
    return paper.id,path
