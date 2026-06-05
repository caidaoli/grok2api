"""
Admin 认证路由（登录 + 配置 CRUD）
"""

import hmac

from fastapi import APIRouter, Depends, HTTPException, Request, Body
from pydantic import BaseModel

from app.core.auth import (
    verify_app_key,
    create_session_token,
    check_login_rate_limit,
    record_login_failure,
)
from app.core.config import config, get_config
from app.core.logger import logger


router = APIRouter()


class AdminLoginBody(BaseModel):
    username: str | None = None
    password: str | None = None


@router.post("/api/v1/admin/login")
async def admin_login_api(request: Request, body: AdminLoginBody | None = Body(default=None)):
    """管理后台登录验证（用户名+密码）

    - 默认账号/密码：admin/admin（用于首次部署引导，登录后应立即修改）
    - 兼容旧版本：允许 Authorization: Bearer <password> 仅密码登录（用户名默认为 admin）
    - 返回 HMAC session token（不暴露原始密码）
    """

    # Rate limit check
    client_ip = request.client.host if request.client else "unknown"
    await check_login_rate_limit(client_ip)

    admin_username = str(get_config("app.admin_username", "admin") or "admin").strip() or "admin"
    admin_password = str(get_config("app.app_key", "admin") or "").strip()
    if not admin_password:
        raise HTTPException(status_code=401, detail="App key is not configured")

    username = (body.username.strip() if body and isinstance(body.username, str) else "").strip()
    password = (body.password.strip() if body and isinstance(body.password, str) else "").strip()

    # Legacy: password-only via Bearer token.
    if not password:
        auth = request.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            password = auth[7:].strip()
            if not username:
                username = "admin"

    if not username or not password:
        raise HTTPException(status_code=400, detail="Missing username or password")

    if not (hmac.compare_digest(username, admin_username) and hmac.compare_digest(password, admin_password)):
        await record_login_failure(client_ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    # Issue a session token derived from app_key (never return the raw password)
    session_token = create_session_token(admin_password)
    return {"status": "success", "api_key": session_token}


@router.get("/api/v1/admin/config", dependencies=[Depends(verify_app_key)])
async def get_config_api():
    """获取当前配置"""
    return config._config


@router.post("/api/v1/admin/config", dependencies=[Depends(verify_app_key)])
async def update_config_api(data: dict):
    """更新配置"""
    try:
        await config.update(data)
        return {"status": "success", "message": "配置已更新"}
    except Exception:
        logger.exception("Admin API error")
        raise HTTPException(status_code=500, detail="Internal server error")
