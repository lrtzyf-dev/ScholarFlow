"""Explicit retention review and capacity reminders, never silent deletion.

Physical file sizes and logical record payloads are separate measurements.
Conversation-owned export BLOBs are logically removed with that conversation.
Already downloaded files, historical backups, remote files and disk blocks are
not erased by that operation.
"""
from __future__ import annotations
from datetime import timedelta, datetime, timezone
import hashlib
import base64
import json
import os
from pathlib import Path
import stat
from sqlalchemy import select, func, or_, and_
from fastapi import HTTPException
from .models import Conversation, DesktopState, DeletedConversation
from .storage_models import CleanupReceipt
from .timeutil import utcnow

KEY = "storage:retention-policy"
DEFAULT = {"backup_reminder_days":7,"archived_review_days":180,"backup_review_days":90,
           "capacity_warning_bytes":10*1024**3,"cleanup_receipt_review_days":365}


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(",",":"))


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def policy(db):
    row=db.get(DesktopState,KEY,populate_existing=True)
    return {"revision":row.revision if row else 0,"values":{**DEFAULT,**(row.value if row else {})},
            "mode":"review_and_remind","automatic_deletion":False}


def update_policy(db,values,expected):
    row=db.get(DesktopState,KEY,populate_existing=True)
    if expected!=(row.revision if row else 0):raise HTTPException(409,"保留策略已改变，请重新载入后保存")
    if set(values)!=set(DEFAULT):raise HTTPException(422,"保留策略字段不完整")
    for key,value in values.items():
        low,high=(1024**2,1024**5) if key=="capacity_warning_bytes" else (1,3650)
        if type(value)is not int or not low<=value<=high:raise HTTPException(422,"保留周期或容量阈值无效")
    if row is None:row=DesktopState(key=KEY,revision=0,value={});db.add(row)
    row.value=dict(values);row.revision+=1;row.updated_at=utcnow();db.flush()
    return policy(db)


def record_cleanup(db,cid,result):
    doc={"schema_version":1,"records":result["records"],"total_records":result["total_records"],
        "storage_preview":result["storage_preview"],"retained_project_journals":result["retained_project_journals"],
        "preserved":result["preserved"],"disk_erasure":result["disk_erasure"],
        "cloud_file_status":"no_unresolved_bindings_at_deletion","remote_erasure_verified":False,
        "backups_erased":False,"restoring_older_backup_may_restore_deleted_data":True}
    row=CleanupReceipt(owner_fingerprint=_hash(cid),document=doc,fingerprint=_hash(canonical(doc)))
    db.add(row);db.flush();return row.id


def receipts(db,cursor=None):
    query=select(CleanupReceipt)
    if cursor:
        try:
            value=json.loads(base64.b64decode(cursor,altchars=b"-_",validate=True))
            if not isinstance(value,list) or len(value)!=3 or value[0]!=1:
                raise ValueError()
            stamp=datetime.fromisoformat(value[1])
            identity=value[2]
            if stamp.tzinfo is not None or not isinstance(identity,str) or len(identity)!=32 or any(c not in "0123456789abcdef"for c in identity):
                raise ValueError()
        except (ValueError,TypeError,UnicodeError):
            raise HTTPException(422,"清理回执游标无效，请重新载入") from None
        query=query.where(or_(CleanupReceipt.created_at<stamp,and_(CleanupReceipt.created_at==stamp,CleanupReceipt.id<identity)))
    rows=db.scalars(query.order_by(CleanupReceipt.created_at.desc(),CleanupReceipt.id.desc()).limit(31)).all()
    review_before=utcnow()-timedelta(days=policy(db)["values"]["cleanup_receipt_review_days"])
    result=[]
    for row in rows[:30]:
        if row.fingerprint!=_hash(canonical(row.document)):raise HTTPException(409,"清理回执完整性检查失败")
        result.append({"id":row.id,"created_at":row.created_at.isoformat()+"Z","document":row.document,"fingerprint":row.fingerprint,
                       "review_due":row.created_at<=review_before})
    next_cursor=base64.urlsafe_b64encode(canonical([1,rows[29].created_at.isoformat(),rows[29].id]).encode()).decode()if len(rows)>30 else None
    return {"items":result,"next_cursor":next_cursor}


def _files(root):
    """Do not follow symlinks or Windows reparse points, including junctions."""
    root=Path(root)
    try:
        if not root.exists():return [],0
        if root.is_symlink() or getattr(root.lstat(),"st_file_attributes",0)&getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",1024):return [],1
        root=root.resolve()
    except OSError:return [],1
    files=[];skipped=0
    def unreadable(_error):
        nonlocal skipped
        skipped+=1
    for current,dirs,names in os.walk(root,followlinks=False,onerror=unreadable):
        kept=[]
        for name in dirs:
            target=Path(current)/name
            try:
                info=target.lstat()
                if target.is_symlink()or getattr(info,"st_file_attributes",0)&getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",1024):
                    skipped+=1;continue
                kept.append(name)
            except OSError:skipped+=1
        dirs[:]=kept
        for name in names:
            target=Path(current)/name
            try:
                info=target.lstat()
                if (not stat.S_ISREG(info.st_mode) or target.is_symlink()
                    or getattr(info,"st_file_attributes",0)&getattr(stat,"FILE_ATTRIBUTE_REPARSE_POINT",1024)
                    or not target.resolve().is_relative_to(root)):
                    skipped+=1;continue
                files.append((target.relative_to(root),info.st_size,info.st_mtime))
            except OSError:skipped+=1
    return files,skipped


def status(db):
    from .config import DATA_DIR
    from .data_backup import recovery_root
    now=utcnow();current=policy(db);values=current["values"]
    files,skipped=_files(DATA_DIR);backups,backup_skipped=_files(recovery_root(DATA_DIR))
    sizes={"database":0,"originals":0,"parsed":0,"indexes":0,"other":0}
    for path,size,_ in files:
        category=("originals"if path.parts[0]=="pdf"else"parsed"if path.parts[0]=="parsed"else
            "database"if path.suffix in (".db",".sqlite",".sqlite3")or path.name.endswith(("-wal","-shm"))else
            "indexes"if path.suffix in (".npy",".npz")else"other")
        sizes[category]+=size
    backup_total=sum(size for _,size,_ in backups);total=sum(sizes.values())+backup_total
    archive_before=now-timedelta(days=values["archived_review_days"])
    candidates=db.scalars(select(Conversation).where(Conversation.archived_at.isnot(None),
        Conversation.archived_at<=archive_before,~Conversation.id.in_(select(DeletedConversation.id)))
        .order_by(Conversation.archived_at,Conversation.id).limit(51)).all()
    # The latest successful backup may precede the 30-job UI history window.
    completed=[]
    for path,size,_ in backups:
        if len(path.parts)!=2 or path.parts[0]!="jobs"or path.suffix!=".json"or size>256*1024:
            continue
        try:
            job=json.loads((recovery_root(DATA_DIR)/path).read_text(encoding="utf-8"))
            if job.get("kind")!="create"or job.get("status")!="succeeded":continue
            stamp=datetime.fromisoformat(job.get("finished_at")or job["created_at"])
            completed.append(stamp.replace(tzinfo=stamp.tzinfo or timezone.utc).astimezone(timezone.utc))
        except (OSError,ValueError,TypeError,KeyError,AttributeError):continue
    latest=max(completed,default=None)
    last=latest.isoformat()if latest else None
    last_date=latest.replace(tzinfo=None)if latest else None
    backup_due=last_date is None or now-last_date>=timedelta(days=values["backup_reminder_days"])
    timestamp=now.replace(tzinfo=timezone.utc).timestamp()
    old_backups=[{"id":path.stem,"size_bytes":size,"age_days":max(0,int((timestamp-mtime)/86400))}
        for path,size,mtime in backups if len(path.parts)==2 and path.parts[0]=="exports"
        and path.suffix==".sfbak"and len(path.stem)==32 and all(c in"0123456789abcdef"for c in path.stem)
        and timestamp-mtime>=values["backup_review_days"]*86400]
    old_receipts=db.scalar(select(func.count()).select_from(CleanupReceipt).where(
        CleanupReceipt.created_at<=now-timedelta(days=values["cleanup_receipt_review_days"])))
    return {"policy":current,"measured_at":now.isoformat(),"storage":{"application_file_bytes":sum(sizes.values()),
        "by_category":sizes,"recovery_file_bytes":backup_total,"total_file_bytes":total,
        "skipped_entries":skipped+backup_skipped,"database_bytes_include_fixed_original_blobs":True,
        "measurement":"observed_file_sizes_not_atomic_snapshot"},"reminders":{"backup_due":backup_due,
        "last_successful_backup":last,"capacity_warning":total>=values["capacity_warning_bytes"],
        "old_backup_count":len(old_backups),"old_receipt_count":old_receipts},
        "archived_review":{"items":[{"id":r.id,"title":r.title,"archived_at":r.archived_at.isoformat()}for r in candidates[:50]],
                           "more":len(candidates)>50},"backup_review":sorted(old_backups,key=lambda r:r["age_days"],reverse=True)[:50],
        "automatic_deletion":False,"restored_backups_do_not_certify_remote_deletion":True}
