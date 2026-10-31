"""Internal loopback activation; the per-instance credential is never returned."""
from fastapi import APIRouter, Header, HTTPException

from ..runtime_owner import owner

router = APIRouter(prefix="/api/runtime", tags=["runtime"])


@router.post("/activate", include_in_schema=False)
def activate(x_scholarflow_instance: str = Header(default="")):
    try:
        instance_id = owner.activate(x_scholarflow_instance)
    except PermissionError as exc:
        raise HTTPException(403, "实例凭据无效") from exc
    except Exception as exc:
        raise HTTPException(503, "窗口暂时无法显示，请稍后重试") from exc
    if instance_id is None:
        raise HTTPException(503, "窗口正在启动或关闭，请稍后重试")
    return {"instance_id": instance_id, "activated": True}
