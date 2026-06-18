"""
API 认证模块
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from typing import Optional, Set

from fastapi import HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_config
from app.core.paths import DATA_DIR

# 定义 Bearer Scheme
security = HTTPBearer(
    auto_error=False,
    scheme_name="API Key",
    description="Enter your API Key in the format: Bearer <key>",
)

LEGACY_API_KEYS_FILE = DATA_DIR / "api_keys.json"
_legacy_api_keys_cache: Set[str] | None = None
_legacy_api_keys_mtime: float | None = None
_legacy_api_keys_lock = asyncio.Lock()
_legacy_keys_checked_at: float = 0.0
_LEGACY_KEYS_COOLDOWN: float = 10.0


# ============= Session Token =============
# Stateless HMAC-SHA256 session tokens so the admin login endpoint
# never returns the raw app_key / admin password.
#
# Format:  hex(timestamp_seconds) + "." + hex(hmac_sha256(app_key, timestamp_hex))
# Verify:  re-derive the HMAC and check timestamp < SESSION_TTL_SEC.

SESSION_TTL_SEC = 7 * 24 * 3600  # 7 days


def create_session_token(app_key: str) -> str:
    """Sign a stateless session token derived from app_key."""
    ts_hex = format(int(time.time()), "x")
    sig = hmac.new(
        app_key.encode(), ts_hex.encode(), hashlib.sha256
    ).hexdigest()
    return f"{ts_hex}.{sig}"


def verify_session_token(token: str, app_key: str) -> bool:
    """Verify a session token against the current app_key."""
    parts = token.split(".", 1)
    if len(parts) != 2:
        return False
    ts_hex, sig = parts
    try:
        ts = int(ts_hex, 16)
    except ValueError:
        return False
    # Check expiry
    if time.time() - ts > SESSION_TTL_SEC:
        return False
    expected = hmac.new(
        app_key.encode(), ts_hex.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(sig, expected)


# ============= Login Rate Limiter =============
# Simple in-memory sliding-window rate limiter for the login endpoint.
# Tracks failed attempts per IP; blocks after MAX_FAILURES within WINDOW_SEC.

_LOGIN_MAX_FAILURES = 10
_LOGIN_WINDOW_SEC = 300  # 5 min
_login_failures: dict[str, list[float]] = {}
_login_lock = asyncio.Lock()


async def check_login_rate_limit(ip: str) -> None:
    """Raise 429 if the IP has exceeded login failure threshold."""
    async with _login_lock:
        now = time.time()
        cutoff = now - _LOGIN_WINDOW_SEC
        attempts = _login_failures.get(ip)
        if attempts:
            # Trim old entries
            attempts[:] = [t for t in attempts if t > cutoff]
            if len(attempts) >= _LOGIN_MAX_FAILURES:
                raise HTTPException(
                    status_code=429,
                    detail="Too many login attempts. Please try again later.",
                )


async def record_login_failure(ip: str) -> None:
    """Record a failed login attempt for rate limiting."""
    async with _login_lock:
        now = time.time()
        if ip not in _login_failures:
            _login_failures[ip] = []
        _login_failures[ip].append(now)
        # Cap list size to avoid unbounded growth
        if len(_login_failures[ip]) > _LOGIN_MAX_FAILURES * 2:
            cutoff = now - _LOGIN_WINDOW_SEC
            _login_failures[ip] = [t for t in _login_failures[ip] if t > cutoff]


async def _load_legacy_api_keys() -> Set[str]:
    """
    Backward-compatible API keys loader.

    Older versions stored multiple API keys in `data/api_keys.json` with a shape like:
    [{"key": "...", "is_active": true, ...}, ...]
    """
    global _legacy_api_keys_cache, _legacy_api_keys_mtime, _legacy_keys_checked_at

    # Fast path: if cache is populated and within cooldown, skip all I/O
    now = time.monotonic()
    if _legacy_api_keys_cache is not None and now - _legacy_keys_checked_at < _LEGACY_KEYS_COOLDOWN:
        return _legacy_api_keys_cache

    if not LEGACY_API_KEYS_FILE.exists():
        _legacy_api_keys_cache = set()
        _legacy_api_keys_mtime = None
        _legacy_keys_checked_at = now
        return set()

    try:
        stat = LEGACY_API_KEYS_FILE.stat()
        mtime = stat.st_mtime
    except Exception:
        mtime = None

    if _legacy_api_keys_cache is not None and mtime is not None and _legacy_api_keys_mtime == mtime:
        _legacy_keys_checked_at = now
        return _legacy_api_keys_cache

    async with _legacy_api_keys_lock:
        # Re-check in lock
        if not LEGACY_API_KEYS_FILE.exists():
            _legacy_api_keys_cache = set()
            _legacy_api_keys_mtime = None
            _legacy_keys_checked_at = time.monotonic()
            return set()

        try:
            stat = LEGACY_API_KEYS_FILE.stat()
            mtime = stat.st_mtime
        except Exception:
            mtime = None

        if _legacy_api_keys_cache is not None and mtime is not None and _legacy_api_keys_mtime == mtime:
            _legacy_keys_checked_at = time.monotonic()
            return _legacy_api_keys_cache

        try:
            raw = await asyncio.to_thread(LEGACY_API_KEYS_FILE.read_text, "utf-8")
            data = json.loads(raw) if raw.strip() else []
        except Exception:
            data = []

        keys: Set[str] = set()
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                key = item.get("key")
                is_active = item.get("is_active", True)
                if isinstance(key, str) and key.strip() and is_active is not False:
                    keys.add(key.strip())

        _legacy_api_keys_cache = keys
        _legacy_api_keys_mtime = mtime
        _legacy_keys_checked_at = time.monotonic()
        return keys


async def verify_api_key(
    auth: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> Optional[str]:
    """
    验证 Bearer Token

    - 若 `app.api_key` 未配置且不存在 legacy keys，则跳过验证。
    - 若配置了 `app.api_key` 或存在 legacy keys，则必须提供 Authorization: Bearer <key>。
    """
    api_key = str(get_config("app.api_key", "") or "").strip()
    legacy_keys = await _load_legacy_api_keys()

    # 如果未配置 API Key 且没有 legacy keys，直接放行
    if not api_key and not legacy_keys:
        return None

    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth.credentials
    if (api_key and hmac.compare_digest(token, api_key)) or any(
        hmac.compare_digest(token, k) for k in legacy_keys
    ):
        return token

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def verify_app_key(
    auth: Optional[HTTPAuthorizationCredentials] = Security(security),
) -> Optional[str]:
    """
    验证后台登录密钥（app_key）。

    接受两种凭证:
    - 原始 app_key（适用于 API 自动化/脚本）
    - HMAC session token（由 /api/v1/admin/login 签发，不暴露原始密码）
    """
    app_key = str(get_config("app.app_key", "") or "").strip()

    if not app_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="App key is not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not auth:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    cred = auth.credentials
    # Accept raw app_key (for API automation) or a valid session token
    if hmac.compare_digest(cred, app_key) or verify_session_token(cred, app_key):
        return cred

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication token",
        headers={"WWW-Authenticate": "Bearer"},
    )


__all__ = [
    "verify_api_key",
    "verify_app_key",
    "create_session_token",
    "verify_session_token",
    "check_login_rate_limit",
    "record_login_failure",
]

