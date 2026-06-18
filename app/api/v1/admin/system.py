"""
Admin 系统监控路由（存储信息、指标、日志）
"""

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import verify_app_key
from app.core.config import get_config
from app.core.logger import logger
from app.core.storage import get_storage, LocalStorage, SQLStorage

router = APIRouter()


@router.get("/api/v1/admin/storage", dependencies=[Depends(verify_app_key)])
async def get_storage_info():
    """获取当前存储模式"""
    storage_type = os.getenv("SERVER_STORAGE_TYPE", "local").lower()
    logger.info(f"Storage type: {storage_type}")
    if not storage_type:
        storage_type = str(get_config("storage.type", "")).lower()
    if not storage_type:
        storage = get_storage()
        if isinstance(storage, LocalStorage):
            storage_type = "local"
        elif isinstance(storage, SQLStorage):
            storage_type = "mysql"
    return {"type": storage_type or "local"}


@router.get("/api/v1/admin/metrics", dependencies=[Depends(verify_app_key)])
async def get_metrics_api():
    """数据中心：聚合常用指标（token/cache/request_stats）。"""
    try:
        from app.services.request_stats import request_stats
        from app.services.token.manager import get_token_manager
        from app.services.token.models import TokenStatus
        from app.services.grok.assets import DownloadService

        mgr = await get_token_manager()
        await mgr.reload_if_stale()

        total = 0
        active = 0
        cooling = 0
        expired = 0
        disabled = 0
        chat_quota = 0
        total_calls = 0

        for pool in mgr.pools.values():
            for info in pool.list():
                total += 1
                total_calls += int(getattr(info, "use_count", 0) or 0)
                if info.status == TokenStatus.ACTIVE:
                    active += 1
                    chat_quota += int(getattr(info, "quota", 0) or 0)
                elif info.status == TokenStatus.COOLING:
                    cooling += 1
                elif info.status == TokenStatus.EXPIRED:
                    expired += 1
                elif info.status == TokenStatus.DISABLED:
                    disabled += 1

        dl = DownloadService()
        local_image = dl.get_stats("image")
        local_video = dl.get_stats("video")

        await request_stats.init()
        stats = request_stats.get_stats(hours=24, days=7)

        return {
            "tokens": {
                "total": total,
                "active": active,
                "cooling": cooling,
                "expired": expired,
                "disabled": disabled,
                "chat_quota": chat_quota,
                "image_quota": int(chat_quota // 2),
                "total_calls": total_calls,
            },
            "cache": {
                "local_image": local_image,
                "local_video": local_video,
            },
            "request_stats": stats,
        }
    except Exception:
        logger.exception("Admin API error")
        raise HTTPException(status_code=500, detail="Internal server error")


# ==================== Logs ====================


def _safe_log_file_path(name: str) -> Path:
    """Resolve a log file name under ./logs safely."""
    from app.core.logger import LOG_DIR

    name = (name or "").strip()
    if not name:
        raise ValueError("Missing log file")
    # Disallow path traversal.
    if "/" in name or "\\" in name or ".." in name:
        raise ValueError("Invalid log file name")

    p = (LOG_DIR / name).resolve()
    if LOG_DIR.resolve() not in p.parents:
        raise ValueError("Invalid log file path")
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(name)
    return p


def _format_log_line(raw: str) -> str:
    raw = (raw or "").rstrip("\r\n")
    if not raw:
        return ""

    # Try JSON log line (our file sink uses json lines).
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return raw
        ts = str(obj.get("time", "") or "")
        ts = ts.replace("T", " ")
        if len(ts) >= 19:
            ts = ts[:19]
        level = str(obj.get("level", "") or "").upper()
        caller = str(obj.get("caller", "") or "")
        msg = str(obj.get("msg", "") or "")
        if not (ts and level and msg):
            return raw
        return f"{ts} | {level:<8} | {caller} - {msg}".rstrip()
    except Exception:
        return raw


def _tail_lines(path: Path, max_lines: int = 2000, max_bytes: int = 1024 * 1024) -> list[str]:
    """Best-effort tail for a text file."""
    try:
        max_lines = int(max_lines)
    except Exception:
        max_lines = 2000
    max_lines = max(1, min(5000, max_lines))
    max_bytes = max(16 * 1024, min(5 * 1024 * 1024, int(max_bytes)))

    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        start = max(0, end - max_bytes)
        f.seek(start, os.SEEK_SET)
        data = f.read()

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    # If we read from the middle of a line, drop the first partial line.
    if start > 0 and lines:
        lines = lines[1:]
    lines = lines[-max_lines:]
    return [_format_log_line(ln) for ln in lines if ln is not None]


@router.get("/api/v1/admin/logs/files", dependencies=[Depends(verify_app_key)])
async def list_log_files_api():
    """列出可查看的日志文件（logs/*.log）。"""
    from app.core.logger import LOG_DIR

    try:
        items = []
        for p in LOG_DIR.glob("*.log"):
            try:
                stat = p.stat()
                items.append(
                    {
                        "name": p.name,
                        "size_bytes": stat.st_size,
                        "mtime_ms": int(stat.st_mtime * 1000),
                    }
                )
            except Exception:
                continue
        items.sort(key=lambda x: x["mtime_ms"], reverse=True)
        return {"files": items}
    except Exception:
        logger.exception("Admin API error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/api/v1/admin/logs/tail", dependencies=[Depends(verify_app_key)])
async def tail_log_api(file: str | None = None, lines: int = 500):
    """读取后台日志（尾部）。"""
    from app.core.logger import LOG_DIR

    try:
        # Default to latest log.
        if not file:
            candidates = sorted(LOG_DIR.glob("*.log"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
            if not candidates:
                return {"file": None, "lines": []}
            path = candidates[0]
            file = path.name
        else:
            path = _safe_log_file_path(file)

        data = await asyncio.to_thread(_tail_lines, path, lines)
        return {"file": str(file), "lines": data}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Log file not found")
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception:
        logger.exception("Admin API error")
        raise HTTPException(status_code=500, detail="Internal server error")
