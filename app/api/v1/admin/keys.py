"""
Admin API Key 管理路由
"""

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import verify_app_key
from app.services.api_keys import api_key_manager
from app.api.v1.admin.common import _display_key, _normalize_limit

router = APIRouter()


@router.get("/api/v1/admin/keys", dependencies=[Depends(verify_app_key)])
async def list_api_keys():
    """List API keys + daily usage/remaining (for admin UI)."""
    await api_key_manager.init()
    day, usage_map = await api_key_manager.usage_today()

    out = []
    for row in api_key_manager.get_all_keys():
        key = str(row.get("key") or "")
        used = usage_map.get(key) or {}
        chat_used = int(used.get("chat_used", 0) or 0)
        heavy_used = int(used.get("heavy_used", 0) or 0)
        image_used = int(used.get("image_used", 0) or 0)
        video_used = int(used.get("video_used", 0) or 0)

        chat_limit = _normalize_limit(row.get("chat_limit", -1))
        heavy_limit = _normalize_limit(row.get("heavy_limit", -1))
        image_limit = _normalize_limit(row.get("image_limit", -1))
        video_limit = _normalize_limit(row.get("video_limit", -1))

        remaining = {
            "chat": None if chat_limit < 0 else max(0, chat_limit - chat_used),
            "heavy": None if heavy_limit < 0 else max(0, heavy_limit - heavy_used),
            "image": None if image_limit < 0 else max(0, image_limit - image_used),
            "video": None if video_limit < 0 else max(0, video_limit - video_used),
        }

        out.append({
            **row,
            "is_active": bool(row.get("is_active", True)),
            "display_key": _display_key(key),
            "usage_today": {
                "chat_used": chat_used,
                "heavy_used": heavy_used,
                "image_used": image_used,
                "video_used": video_used,
            },
            "remaining_today": remaining,
            "day": day,
        })

    return {"success": True, "data": out}


@router.post("/api/v1/admin/keys", dependencies=[Depends(verify_app_key)])
async def create_api_key(data: dict):
    """Create a new API key (optional name/key/limits)."""
    await api_key_manager.init()
    data = data or {}

    name = str(data.get("name") or "").strip() or api_key_manager.generate_name()
    key_val = str(data.get("key") or "").strip() or None
    is_active = bool(data.get("is_active", True))

    limits = data.get("limits") if isinstance(data.get("limits"), dict) else {}
    try:
        row = await api_key_manager.add_key(
            name=name,
            key=key_val,
            is_active=is_active,
            limits={
                "chat_per_day": limits.get("chat_per_day"),
                "heavy_per_day": limits.get("heavy_per_day"),
                "image_per_day": limits.get("image_per_day"),
                "video_per_day": limits.get("video_per_day"),
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"success": True, "data": {**row, "display_key": _display_key(row.get("key", ""))}}


@router.post("/api/v1/admin/keys/update", dependencies=[Depends(verify_app_key)])
async def update_api_key(data: dict):
    """Update name/status/limits for an API key."""
    await api_key_manager.init()
    data = data or {}
    key = str(data.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Missing key")

    existing = api_key_manager.get_key_row(key)
    if not existing:
        raise HTTPException(status_code=404, detail="Key not found")

    if "name" in data and data.get("name") is not None:
        name = str(data.get("name") or "").strip()
        if name:
            await api_key_manager.update_key_name(key, name)

    if "is_active" in data:
        await api_key_manager.update_key_status(key, bool(data.get("is_active")))

    limits = data.get("limits") if isinstance(data.get("limits"), dict) else None
    if limits is not None:
        await api_key_manager.update_key_limits(
            key,
            {
                "chat_per_day": limits.get("chat_per_day"),
                "heavy_per_day": limits.get("heavy_per_day"),
                "image_per_day": limits.get("image_per_day"),
                "video_per_day": limits.get("video_per_day"),
            },
        )

    return {"success": True}


@router.post("/api/v1/admin/keys/delete", dependencies=[Depends(verify_app_key)])
async def delete_api_key(data: dict):
    """Delete an API key."""
    await api_key_manager.init()
    data = data or {}
    key = str(data.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="Missing key")

    ok = await api_key_manager.delete_key(key)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"success": True}
