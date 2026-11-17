"""Library-owned fixed originals and reading data, with scoped conversation aliases."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
import hashlib
from sqlalchemy import select
from . import source_store as store
from .source_models import Source,SourceRevision,SourceLocator,SourceExtraction,SourceLibraryImport,SourceLibraryBinding,SourceLibraryAnnotationOrigin,SourceLibraryRevisionOrigin
from .source_documents import read_document,save_extraction
from .source_parsing import Extraction
from .models import Paper


def scope_of(db,rid):
    revision=db.get(SourceRevision,rid)
    source=db.get(Source,revision.source_id) if revision else None
    if source is None:raise store.SourceCorruption('固定来源已缺失，未删除或替换文献原件')
    return store.SourceScope(source.scope_type,source.scope_id)


def copy_locator(db,locator_id,source_scope,target_rid,target_scope):
    loc=store.read_locator(db,locator_id,scope=source_scope)
    old=store.read_revision(db,loc['revision_id'],scope=source_scope)
    new=store.read_revision(db,target_rid,scope=target_scope)
    if (old['raw_sha256'],old['text_sha256'])!=(new['raw_sha256'],new['text_sha256']):
        raise store.SourceCorruption('原件或提取文本不同，不能跨版本复制定位')
    location=deepcopy(loc['location'])
    if location.get('extraction_fingerprint'):
        location['extraction_fingerprint']=read_document(db,target_rid,scope=target_scope)['fingerprint']
    return store._save_locator(db,target_rid,loc['kind'],location,loc['quote'])


def copy_record(db,record,origin_scope,target_scope,origin):
    result=store.capture_source(db,scope=target_scope,authorization=store.SourceAuthorization(target_scope,origin,'library'),
        kind='library',title=record['title'],raw_bytes=record['raw_bytes'],text=record['text'],media_type=record['media_type'],
        origin_ref=origin,text_origin=record['text_origin'],parser=record['parser'])
    if db.get(SourceExtraction,record['revision_id']):
        doc=read_document(db,record['revision_id'],scope=origin_scope)
        saved={k:v for k,v in doc.items() if k not in ('source_id','revision_id','fingerprint','title')}
        save_extraction(db,result['revision_id'],scope=target_scope,extraction=Extraction(doc['text'],doc['parser'],saved))
    return result


def equivalent_extraction(db,first,second):
    a=db.get(SourceExtraction,first['revision_id']);b=db.get(SourceExtraction,second['revision_id'])
    return (all(first.get(k)==second.get(k) for k in ('raw_sha256','text_sha256','media_type','parser','text_origin'))
        and (a.fingerprint if a else None)==(b.fingerprint if b else None))


def preserve_reading(db,origin_rid,target_rid):
    from .research_models import ReadingState,ReadingAnnotation,AnnotationRevision
    if origin_rid==target_rid:return
    origin_scope,target_scope=scope_of(db,origin_rid),scope_of(db,target_rid)
    old=db.get(ReadingState,origin_rid)
    if old and db.get(ReadingState,target_rid) is None:
        position=deepcopy(old.position)
        if position.get('locator_id'):position['locator_id']=copy_locator(db,position['locator_id'],origin_scope,target_rid,target_scope)['locator_id']
        db.add(ReadingState(revision_id=target_rid,version=old.version,position=position,user_read=old.user_read,updated_at=old.updated_at))
    for note in db.scalars(select(ReadingAnnotation).where(ReadingAnnotation.revision_id==origin_rid)):
        if db.get(SourceLibraryAnnotationOrigin,note.id):continue
        loc=copy_locator(db,note.locator_id,origin_scope,target_rid,target_scope)
        clone=ReadingAnnotation(revision_id=target_rid,locator_id=loc['locator_id'],version=note.version,
            kind=note.kind,body=note.body,selected_text=note.selected_text,deleted=note.deleted,created_at=note.created_at,updated_at=note.updated_at)
        db.add(clone);db.flush()
        for revision in db.scalars(select(AnnotationRevision).where(AnnotationRevision.annotation_id==note.id)):
            document={**deepcopy(revision.document),'id':clone.id,'revision_id':target_rid,'locator_id':loc['locator_id'],
                'preserved_from_annotation_id':note.id}
            db.add(AnnotationRevision(annotation_id=clone.id,version=revision.version,document=document,created_at=revision.created_at))
        db.add(SourceLibraryAnnotationOrigin(origin_annotation_id=note.id,library_annotation_id=clone.id))
    db.flush()


def ensure_owned(db,paper_id,origin_rid=None):
    paper=db.get(Paper,paper_id)
    if paper is None:raise store.SourceNotFound('文献不存在')
    binding=db.get(SourceLibraryBinding,paper_id)
    lib_scope=store.SourceScope('library',paper_id)
    if binding:
        record=store.read_revision(db,binding.revision_id,scope=lib_scope,include_raw=True)
        if record['raw_sha256']!=paper.content_hash:raise store.SourceCorruption('文献库固定原件与文献版本不一致')
    else:
        receipt=db.get(SourceLibraryImport,origin_rid) if origin_rid else db.scalar(select(SourceLibraryImport).where(
            SourceLibraryImport.paper_id==paper_id).order_by(SourceLibraryImport.created_at))
        if receipt:
            origin_rid=receipt.revision_id;origin_scope=scope_of(db,origin_rid)
            original=store.read_revision(db,origin_rid,scope=origin_scope,include_raw=True)
            if original['raw_sha256']!=paper.content_hash:raise store.SourceCorruption('入库回执原件与文献版本不一致')
            record=copy_record(db,original,origin_scope,lib_scope,'library:'+paper_id)
        else:
            from .config import PDF_DIR
            path=Path(paper.pdf_path or '').resolve()
            if not path.is_relative_to(PDF_DIR.resolve()) or not path.is_file():raise store.SourceConflict('文献原件缺失，无法建立独立固定版本')
            with path.open('rb') as stream:raw=stream.read(store.MAX_RAW_BYTES+1)
            if hashlib.sha256(raw).hexdigest()!=paper.content_hash:raise store.SourceCorruption('文献文件已变化，不能冒充旧版本')
            record=store.capture_source(db,scope=lib_scope,authorization=store.SourceAuthorization(lib_scope,'library:'+paper_id,'library'),
                kind='library',title=paper.title or path.name,raw_bytes=raw,text=None,media_type='application/pdf',origin_ref='library:'+paper_id)
        db.add(SourceLibraryBinding(paper_id=paper_id,revision_id=record['revision_id']))
        mapping={}
        if receipt:
            for chunk,locator in (receipt.chunk_locators or {}).items():
                mapping[chunk]=copy_locator(db,locator,scope_of(db,receipt.revision_id),record['revision_id'],lib_scope)['locator_id']
        db.add(SourceLibraryImport(revision_id=record['revision_id'],raw_sha256=record['raw_sha256'],paper_id=paper_id,
            request_id='library-owned-'+paper_id,chunk_locators=mapping));db.flush()
    # Legacy notes are copied once. New API writes target the library copy,
    # so deleting/reopening conversations cannot resurrect an old annotation.
    requested=record
    for old in list(db.scalars(select(SourceLibraryImport).where(SourceLibraryImport.paper_id==paper_id))):
        old_scope=scope_of(db,old.revision_id)
        if old_scope.scope_type=='library':continue
        previous=db.get(SourceLibraryRevisionOrigin,old.revision_id)
        if previous:
            if previous.paper_id!=paper_id:raise store.SourceCorruption('库来源映射所属文献不一致')
            fixed=store.read_revision(db,previous.library_revision_id,scope=lib_scope,include_raw=True)
            original=store.read_revision(db,old.revision_id,scope=old_scope,include_raw=True)
            if not equivalent_extraction(db,original,fixed):raise store.SourceCorruption('库来源映射的原件或解析版本不一致')
        else:
            original=store.read_revision(db,old.revision_id,scope=old_scope,include_raw=True)
            if original['raw_sha256']!=paper.content_hash:raise store.SourceCorruption('旧来源原件哈希与文献不一致，不能丢弃旧来源')
            fixed=None
            for known in db.scalars(select(SourceLibraryImport).where(SourceLibraryImport.paper_id==paper_id)):
                if scope_of(db,known.revision_id).scope_type!='library':continue
                possible=store.read_revision(db,known.revision_id,scope=lib_scope,include_raw=True)
                if equivalent_extraction(db,original,possible):fixed=possible;break
            if fixed is None:
                fixed=copy_record(db,original,old_scope,lib_scope,'library:'+paper_id+':'+old.revision_id)
                mapping={chunk:copy_locator(db,loc,old_scope,fixed['revision_id'],lib_scope)['locator_id']
                    for chunk,loc in (old.chunk_locators or {}).items()}
                db.add(SourceLibraryImport(revision_id=fixed['revision_id'],raw_sha256=fixed['raw_sha256'],paper_id=paper_id,
                    request_id='library-version-'+fixed['revision_id'],chunk_locators=mapping))
            db.add(SourceLibraryRevisionOrigin(origin_revision_id=old.revision_id,library_revision_id=fixed['revision_id'],paper_id=paper_id));db.flush()
        preserve_reading(db,old.revision_id,fixed['revision_id'])
        if old.revision_id==origin_rid:requested=fixed
    return store.read_revision(db,requested['revision_id'],scope=lib_scope,include_raw=True),db.get(SourceLibraryImport,requested['revision_id'])


def reading_target(db,rid,scope):
    store.read_revision(db,rid,scope=scope)  # Never use a paper ID to bypass current source authorization.
    receipt=db.get(SourceLibraryImport,rid)
    if receipt is None:return rid,scope
    record,_=ensure_owned(db,receipt.paper_id,rid)
    return record['revision_id'],store.SourceScope('library',receipt.paper_id)


def authorize_annotation(db,annotation,cid):
    from .source_runtime import conversation_scope
    scope=conversation_scope(db,cid)
    origin=db.get(SourceLibraryAnnotationOrigin,annotation.id)
    if origin:
        from .research_models import ReadingAnnotation
        annotation=db.get(ReadingAnnotation,origin.library_annotation_id)
    own_scope=scope_of(db,annotation.revision_id)
    if own_scope.scope_type!='library':
        store.read_revision(db,annotation.revision_id,scope=scope);return annotation
    binding=db.get(SourceLibraryBinding,own_scope.scope_id)
    allowed=db.scalar(select(SourceLibraryImport.revision_id).join(SourceRevision,SourceRevision.id==SourceLibraryImport.revision_id)
        .join(Source,Source.id==SourceRevision.source_id).where(SourceLibraryImport.paper_id==own_scope.scope_id,
            Source.scope_type=='conversation',Source.scope_id==cid).limit(1))
    owned=db.get(SourceLibraryImport,annotation.revision_id)
    if not binding or not owned or owned.paper_id!=own_scope.scope_id or not allowed:
        raise store.SourceAccessDenied('请先在当前会话打开该文献，再编辑其批注')
    store.read_revision(db,annotation.revision_id,scope=own_scope)
    return annotation


def preserve_conversation_library(db,conversation_id):
    """Call within the deletion writer before planning/removing old aliases."""
    ids=list(db.scalars(select(SourceLibraryImport.paper_id).join(SourceRevision,SourceRevision.id==SourceLibraryImport.revision_id)
        .join(Source,Source.id==SourceRevision.source_id).where(Source.scope_type=='conversation',Source.scope_id==conversation_id).distinct()))
    for pid in ids:ensure_owned(db,pid)
    from .bibliography_store import preserve_conversation_metadata
    preserve_conversation_metadata(db,conversation_id)
    db.flush()
    return {'preserved_library_papers':ids}
