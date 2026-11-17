"""Local guarded backup/restore API; clients cannot choose arbitrary disk paths."""
from __future__ import annotations

import os
from uuid import uuid4
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .. import backup_service as service, data_backup as core

router = APIRouter(prefix="/api/backups", tags=["backups"])


def _call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except core.BackupError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("")
def status():
    return service.status()


@router.get("/jobs/{job_id}")
def job(job_id: str):
    return _call(service.job, job_id)


@router.post("/create", status_code=202)
def create():
    return _call(service.start, "create")


@router.get("/exports/{backup_id}")
def download(backup_id: str):
    path = _call(service.export_path, backup_id)
    return FileResponse(path, media_type="application/octet-stream", filename=f"ScholarFlow-{backup_id[:8]}.sfbak",
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/restore/prepare", status_code=202)
async def prepare(file: UploadFile = File(...)):
    if service.active():
        raise HTTPException(409, "另一项备份或恢复操作正在进行")
    uploads = service._root() / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    path = uploads / (uuid4().hex + ".sfbak")
    handed_off = False
    try:
        with path.open("xb") as output:
            total = 0
            while block := await file.read(core.BLOCK):
                total += len(block)
                if total > core.MAX_TOTAL_BYTES:
                    raise HTTPException(413, "备份文件过大")
                await run_in_threadpool(output.write, block)
            if not total:
                raise HTTPException(422, "请选择非空的研序备份文件")
            await run_in_threadpool(output.flush)
            await run_in_threadpool(os.fsync, output.fileno())
        result = _call(service.start, "prepare", upload=path)
        handed_off = True
        return result
    finally:
        await file.close()
        if not handed_off:
            path.unlink(missing_ok=True)


class RestoreConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    restore_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    confirm_replace: bool = False


@router.post("/restore/confirm", status_code=202)
def confirm(body: RestoreConfirmation):
    if not body.confirm_replace:
        raise HTTPException(422, "请先阅读恢复预览，再明确确认替换应用数据")
    return _call(service.start, "schedule", restore_id=body.restore_id)


@router.post("/restore/cancel", status_code=202)
def cancel():
    return _call(service.start, "cancel")


@router.post("/rollback/prepare", status_code=202)
def rollback():
    return _call(service.start, "rollback")
