"""Local version facts. No update download, install or user-data mutation."""
from fastapi import APIRouter,HTTPException
from ..release_info import public_release,ReleaseError

router=APIRouter(prefix='/api/release',tags=['release'])


@router.get('')
def release():
    try:return public_release()
    except (ReleaseError,OSError):raise HTTPException(409,'构建身份无法核对，请保留安装包并检查发行清单') from None
