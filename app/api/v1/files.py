"""
文件服务 API 路由
"""

import aiofiles.os
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.logger import logger
from app.core.paths import DATA_DIR

router = APIRouter(tags=["Files"])

# 缓存根目录
BASE_DIR = DATA_DIR / "tmp"
IMAGE_DIR = BASE_DIR / "image"
VIDEO_DIR = BASE_DIR / "video"

def _cache_filename(raw: str) -> str:
    """
    Map a request path (may contain slashes) to the flattened on-disk cache filename.

    Security note:
    - Windows treats `\\` as a path separator, so we normalize it as well.
    - The cache itself never needs backslashes in filenames; treating them as separators
      makes traversal attempts deterministic across OSes.
    """
    s = str(raw or "")
    # Normalize both separators, then flatten.
    s = s.replace("\\", "/").lstrip("/")
    return s.replace("/", "-")

def _safe_cached_path(base_dir: Path, raw: str) -> Path | None:
    name = _cache_filename(raw)
    if not name or name in {".", ".."}:
        return None

    base = base_dir.resolve()
    target = (base_dir / name).resolve()
    if not target.is_relative_to(base):
        return None
    return target


@router.get("/image/{filename:path}")
async def get_image(filename: str):
    """
    获取图片文件
    """
    file_path = _safe_cached_path(IMAGE_DIR, filename)
    
    if file_path and await aiofiles.os.path.exists(file_path):
        if await aiofiles.os.path.isfile(file_path):
            content_type = "image/jpeg"
            if file_path.suffix.lower() == ".png":
                content_type = "image/png"
            elif file_path.suffix.lower() == ".webp":
                content_type = "image/webp"
            
            # 增加缓存头，支持高并发场景下的浏览器/CDN缓存
            return FileResponse(
                file_path, 
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=31536000, immutable"
                }
            )

    logger.warning(f"Image not found: {filename}")
    raise HTTPException(status_code=404, detail="Image not found")


@router.get("/video/{filename:path}")
async def get_video(filename: str):
    """
    获取视频文件
    """
    file_path = _safe_cached_path(VIDEO_DIR, filename)
    
    if file_path and await aiofiles.os.path.exists(file_path):
        if await aiofiles.os.path.isfile(file_path):
            return FileResponse(
                file_path, 
                media_type="video/mp4",
                headers={
                    "Cache-Control": "public, max-age=31536000, immutable"
                }
            )

    logger.warning(f"Video not found: {filename}")
    raise HTTPException(status_code=404, detail="Video not found")
