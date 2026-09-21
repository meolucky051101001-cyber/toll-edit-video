from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from backend.providers.capabilities import (
    get_platform_capabilities,
    list_all_capabilities,
)
from backend.schemas.contracts import PlatformCapabilities

router = APIRouter(prefix="/api/platforms", tags=["platforms"])


class PlatformListResponse(BaseModel):
    platforms: list[PlatformCapabilities]
    total: int


@router.get("", response_model=PlatformListResponse)
def list_platforms():
    """Lists all supported social video platforms and their architectural capabilities."""
    items = list_all_capabilities()
    return PlatformListResponse(platforms=items, total=len(items))


@router.get("/{platform_id}", response_model=PlatformCapabilities)
def get_platform(platform_id: str):
    """Returns capabilities and status of a specific video platform."""
    cap = get_platform_capabilities(platform_id)
    if not cap:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Nền tảng '{platform_id}' không được hỗ trợ.",
        )
    return cap
