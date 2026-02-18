"""
Grok 用量服务
"""

import asyncio
import time
import uuid
from typing import Dict

import orjson
from curl_cffi.requests import AsyncSession

from app.core.logger import logger
from app.core.config import get_config
from app.core.exceptions import UpstreamException, AppException
from app.services.grok.statsig import StatsigService
from app.services.grok.retry import retry_on_status


LIMITS_API = "https://grok.com/rest/rate-limits"
BROWSER = "chrome136"
TIMEOUT = 10
DEFAULT_MAX_CONCURRENT = 25
DEFAULT_SYNC_BACKOFF_SECONDS = 600
_USAGE_SEMAPHORE = asyncio.Semaphore(DEFAULT_MAX_CONCURRENT)
_USAGE_SEM_VALUE = DEFAULT_MAX_CONCURRENT
_USAGE_SYNC_COOLDOWN_UNTIL = 0.0
_USAGE_SYNC_COOLDOWN_LOCK = asyncio.Lock()

def _get_usage_semaphore() -> asyncio.Semaphore:
    global _USAGE_SEMAPHORE, _USAGE_SEM_VALUE
    value = get_config("performance.usage_max_concurrent", DEFAULT_MAX_CONCURRENT)
    try:
        value = int(value)
    except Exception:
        value = DEFAULT_MAX_CONCURRENT
    value = max(1, value)
    if value != _USAGE_SEM_VALUE:
        _USAGE_SEM_VALUE = value
        _USAGE_SEMAPHORE = asyncio.Semaphore(value)
    return _USAGE_SEMAPHORE


def _get_sync_backoff_seconds() -> int:
    value = get_config("grok.usage_sync_backoff_seconds", DEFAULT_SYNC_BACKOFF_SECONDS)
    try:
        value = int(value)
    except Exception:
        value = DEFAULT_SYNC_BACKOFF_SECONDS
    return max(0, value)


def _get_sync_cooldown_remaining() -> float:
    return max(0.0, _USAGE_SYNC_COOLDOWN_UNTIL - time.time())


async def _arm_sync_cooldown(status_code: int) -> None:
    """针对稳定失败状态码做短期熔断，避免日志洪泛。"""
    if status_code != 404:
        return

    ttl = _get_sync_backoff_seconds()
    if ttl <= 0:
        return

    now = time.time()
    new_until = now + ttl

    global _USAGE_SYNC_COOLDOWN_UNTIL
    async with _USAGE_SYNC_COOLDOWN_LOCK:
        if new_until <= _USAGE_SYNC_COOLDOWN_UNTIL:
            return
        _USAGE_SYNC_COOLDOWN_UNTIL = new_until

    logger.warning(f"Usage sync disabled for {ttl}s due to status 404")


class UsageService:
    """用量查询服务"""
    
    def __init__(self, proxy: str = None):
        self.proxy = proxy or get_config("grok.base_proxy_url", "")
        self.timeout = get_config("grok.timeout", TIMEOUT)
    
    def _build_headers(self, token: str) -> dict:
        """构建请求头"""
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Baggage": "sentry-environment=production,sentry-release=d6add6fb0460641fd482d767a335ef72b9b6abb8,sentry-public_key=b311e0f2690c81f25e2c4cf6d4f7ce1c",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
            "Origin": "https://grok.com",
            "Pragma": "no-cache",
            "Priority": "u=1, i",
            "Referer": "https://grok.com/",
            "Sec-Ch-Ua": '"Google Chrome";v="136", "Chromium";v="136", "Not(A:Brand";v="24"',
            "Sec-Ch-Ua-Arch": "arm",
            "Sec-Ch-Ua-Bitness": "64",
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Model": "",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        }
        
        # Statsig ID
        headers["x-statsig-id"] = StatsigService.gen_id()
        headers["x-xai-request-id"] = str(uuid.uuid4())
        
        # Cookie
        token = token[4:] if token.startswith("sso=") else token
        cf = get_config("grok.cf_clearance", "")
        headers["Cookie"] = f"sso={token};cf_clearance={cf}" if cf else f"sso={token}"
        
        return headers
    
    def _build_proxies(self) -> dict:
        """构建代理配置"""
        return {"http": self.proxy, "https": self.proxy} if self.proxy else None
    
    async def get(self, token: str, model_name: str = "grok-4-1-thinking-1129") -> Dict:
        """
        获取速率限制信息
        
        Args:
            token: 认证 Token
            model_name: 模型名称
            
        Returns:
            响应数据
            
        Raises:
            UpstreamException: 当获取失败且重试耗尽时
        """
        async with _get_usage_semaphore():
            remaining = _get_sync_cooldown_remaining()
            if remaining > 0:
                logger.debug(f"Usage sync cooldown active, skip remote sync ({remaining:.0f}s remaining)")
                return {}

            # 定义状态码提取器
            def extract_status(e: Exception) -> int | None:
                if isinstance(e, UpstreamException) and e.details:
                    return e.details.get("status")
                return None
            
            # 定义实际的请求函数
            async def do_request():
                try:
                    headers = self._build_headers(token)
                    payload = {
                        "requestKind": "DEFAULT",
                        "modelName": model_name
                    }
                    
                    async with AsyncSession() as session:
                        response = await session.post(
                            LIMITS_API,
                            headers=headers,
                            json=payload,
                            impersonate=BROWSER,
                            timeout=self.timeout,
                            proxies=self._build_proxies()
                        )
                    
                    if response.status_code == 200:
                        data = response.json()
                        remaining = data.get('remainingTokens', 0)
                        logger.info(f"Usage: quota {remaining} remaining")
                        return data

                    if response.status_code == 404:
                        await _arm_sync_cooldown(response.status_code)
                        logger.debug("Usage failed: 404")
                    else:
                        logger.error(f"Usage failed: {response.status_code}")

                    raise UpstreamException(
                        message=f"Failed to get usage stats: {response.status_code}",
                        details={"status": response.status_code}
                    )
                    
                except Exception as e:
                    if isinstance(e, UpstreamException):
                        raise
                    logger.error(f"Usage error: {e}")
                    raise UpstreamException(
                        message=f"Usage service error: {str(e)}",
                        details={"error": str(e)}
                    )
            
            # 带重试的执行
            try:
                result = await retry_on_status(
                    do_request,
                    extract_status=extract_status
                )
                return result
                
            except Exception as e:
                # 最后一次失败已经被记录
                raise


__all__ = ["UsageService"]
