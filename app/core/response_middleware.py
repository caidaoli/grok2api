"""
响应中间件
Response Middleware

用于记录请求日志、生成 TraceID 和计算请求耗时
"""

import asyncio
import os
import time
import uuid
import ipaddress
from pathlib import Path
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match

from app.core.config import get_config
from app.core.logger import logger

class ResponseLoggerMiddleware(BaseHTTPMiddleware):
    """
    请求日志/响应追踪中间件
    Request Logging and Response Tracking Middleware
    """

    _banned_ips: set[str] = set()
    _banned_lock = asyncio.Lock()
    _ban_file_path: Path = Path(__file__).parent.parent.parent / "data" / "banned_ips.txt"
    _banned_ips_loaded: bool = False
    _banned_ips_file_mtime: float | None = None

    @staticmethod
    def _file_persistence_enabled() -> bool:
        """
        仅当 STORAGE_MODE=file 时启用封禁文件持久化。
        其他模式（含未设置）只走内存封禁。
        """
        return (os.getenv("STORAGE_MODE", "") or "").strip().lower() == "file"

    @staticmethod
    def _parse_ip(value: str | None) -> str | None:
        if not value:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            return str(ipaddress.ip_address(raw))
        except ValueError:
            return None

    @staticmethod
    def _trust_proxy_headers() -> bool:
        return bool(get_config("security.trust_proxy_headers", True))

    @staticmethod
    def _trusted_proxy_rules() -> tuple[set[str], list[ipaddress._BaseNetwork]]:
        raw = get_config("security.trusted_proxy_ips", ["127.0.0.1", "::1"])
        if isinstance(raw, str):
            items = [p.strip() for p in raw.split(",")]
        elif isinstance(raw, (list, tuple, set)):
            items = [str(p).strip() for p in raw]
        else:
            items = []

        exact: set[str] = set()
        nets: list[ipaddress._BaseNetwork] = []

        for item in items:
            if not item:
                continue

            if "/" in item:
                try:
                    nets.append(ipaddress.ip_network(item, strict=False))
                    continue
                except ValueError:
                    pass

            ip = ResponseLoggerMiddleware._parse_ip(item)
            if ip:
                exact.add(ip)
            else:
                # Keep non-IP tokens as-is for tests or custom environments.
                exact.add(item)

        return exact, nets

    @classmethod
    def _is_trusted_proxy_peer(cls, peer: str) -> bool:
        exact, nets = cls._trusted_proxy_rules()
        if not exact and not nets:
            return False

        raw = str(peer or "").strip()
        if not raw:
            return False

        if raw in exact:
            return True

        ip_str = cls._parse_ip(raw)
        if not ip_str:
            return False

        ip = ipaddress.ip_address(ip_str)
        return any(ip in net for net in nets)

    @classmethod
    def _get_client_ip(cls, request: Request) -> str:
        client = request.client
        direct_ip = client.host if client and client.host else ""

        if not cls._trust_proxy_headers():
            return direct_ip

        if not cls._is_trusted_proxy_peer(direct_ip):
            return direct_ip

        # Prefer Cloudflare header if present (common in fronted deployments).
        cf_ip = cls._parse_ip(request.headers.get("CF-Connecting-IP"))
        if cf_ip:
            return cf_ip

        xff = request.headers.get("X-Forwarded-For")
        if xff:
            for part in xff.split(","):
                ip = cls._parse_ip(part)
                if ip:
                    return ip

        real_ip = cls._parse_ip(request.headers.get("X-Real-IP"))
        if real_ip:
            return real_ip

        return direct_ip

    @staticmethod
    def _is_known_route(request: Request) -> bool:
        # Explicit allowlist: do NOT auto-ban for these paths, even when the route
        # isn't registered (common browser probes / admin prefixes).
        path = (request.url.path or "").strip() or "/"

        def _is_prefix_or_exact(prefix: str) -> bool:
            return path == prefix or path.startswith(f"{prefix}/")

        if path in {"/", "/favicon.ico"}:
            return True
        if _is_prefix_or_exact("/admin"):
            return True

        scope = request.scope
        routes = getattr(getattr(request.app, "router", None), "routes", [])
        for route in routes:
            try:
                match, _ = route.matches(scope)
            except Exception:
                continue
            # PARTIAL 表示路径存在但 method 不匹配（405 场景），不应视为非法路径。
            if match is not Match.NONE:
                return True
        return False

    @staticmethod
    def _auto_ban_enabled() -> bool:
        return bool(get_config("security.auto_ban_unknown_path", True))

    @staticmethod
    def _is_exempt_ip(client_ip: str) -> bool:
        exempt_ips = get_config("security.auto_ban_exempt_ips", ["127.0.0.1", "::1"])
        if isinstance(exempt_ips, str):
            exempt_ips = [part.strip() for part in exempt_ips.split(",") if part.strip()]
        if not isinstance(exempt_ips, (list, tuple, set)):
            return False
        return client_ip in {str(ip).strip() for ip in exempt_ips if str(ip).strip()}

    @classmethod
    async def _refresh_banned_ips_from_file_locked(cls):
        path = cls._ban_file_path
        try:
            mtime = path.stat().st_mtime if path.exists() else None
        except Exception:
            mtime = None

        if cls._banned_ips_loaded and cls._banned_ips_file_mtime == mtime:
            return

        loaded: set[str] = set()
        if path.exists():
            try:
                raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
                for line in raw.splitlines():
                    ip = line.strip()
                    if not ip or ip.startswith("#"):
                        continue
                    loaded.add(ip)
            except Exception as e:
                logger.warning(f"Failed to load banned IP file {path}: {e}")

        cls._banned_ips = loaded
        cls._banned_ips_loaded = True
        cls._banned_ips_file_mtime = mtime

    @classmethod
    async def _persist_banned_ips_to_file_locked(cls):
        path = cls._ban_file_path
        try:
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            content = "\n".join(sorted(cls._banned_ips))
            if content:
                content += "\n"
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            await asyncio.to_thread(tmp_path.write_text, content, "utf-8")
            await asyncio.to_thread(os.replace, tmp_path, path)
            cls._banned_ips_file_mtime = path.stat().st_mtime
            cls._banned_ips_loaded = True
        except Exception as e:
            logger.warning(f"Failed to persist banned IP file {path}: {e}")

    @classmethod
    async def _ban_ip(cls, client_ip: str):
        if not client_ip:
            return
        async with cls._banned_lock:
            if cls._file_persistence_enabled():
                await cls._refresh_banned_ips_from_file_locked()
            if client_ip in cls._banned_ips:
                return
            cls._banned_ips.add(client_ip)
            if cls._file_persistence_enabled():
                await cls._persist_banned_ips_to_file_locked()

    @classmethod
    async def _is_ip_banned(cls, client_ip: str) -> bool:
        if not client_ip:
            return False
        async with cls._banned_lock:
            if cls._file_persistence_enabled():
                await cls._refresh_banned_ips_from_file_locked()
            return client_ip in cls._banned_ips
    
    async def dispatch(self, request: Request, call_next):
        # 生成请求 ID
        trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id
        client_ip = self._get_client_ip(request)
        
        start_time = time.time()
        
        # 记录请求信息
        logger.info(
            f"Request: {request.method} {request.url.path}",
            extra={
                "traceID": trace_id,
                "method": request.method,
                "path": request.url.path,
                "client_ip": client_ip,
            }
        )
        
        try:
            response = None
            if await self._is_ip_banned(client_ip):
                response = JSONResponse(
                    status_code=403,
                    content={"error": "forbidden", "message": "IP blocked"},
                )
                logger.warning(
                    f"Blocked banned IP: {client_ip} {request.method} {request.url.path}",
                    extra={
                        "traceID": trace_id,
                        "method": request.method,
                        "path": request.url.path,
                        "client_ip": client_ip,
                        "event": "ip_block_hit",
                    },
                )
            elif (
                self._auto_ban_enabled()
                and (not self._is_exempt_ip(client_ip))
                and (not self._is_known_route(request))
            ):
                await self._ban_ip(client_ip)
                response = JSONResponse(
                    status_code=403,
                    content={"error": "forbidden", "message": "IP blocked due invalid path"},
                )
                logger.warning(
                    f"Auto banned IP {client_ip} for unknown path {request.method} {request.url.path}",
                    extra={
                        "traceID": trace_id,
                        "method": request.method,
                        "path": request.url.path,
                        "client_ip": client_ip,
                        "event": "ip_auto_ban_unknown_path",
                    },
                )
            else:
                response = await call_next(request)
            
            # 计算耗时
            duration = (time.time() - start_time) * 1000
            
            # 记录响应信息
            logger.info(
                f"Response: {request.method} {request.url.path} - {response.status_code} ({duration:.2f}ms)",
                extra={
                    "traceID": trace_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(duration, 2),
                    "client_ip": client_ip,
                }
            )
            
            return response
            
        except Exception as e:
            duration = (time.time() - start_time) * 1000
            logger.error(
                f"Response Error: {request.method} {request.url.path} - {str(e)} ({duration:.2f}ms)",
                extra={
                    "traceID": trace_id,
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round(duration, 2),
                    "error": str(e),
                    "client_ip": client_ip,
                }
            )
            raise
