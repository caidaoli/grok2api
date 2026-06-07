import asyncio

from app.services.token.manager import TokenManager
from app.services.token.models import TokenInfo, TokenStatus
from app.services.token.pool import TokenPool
from app.services.grok import usage as usage_mod


def test_sync_usage_consumes_locally_without_remote_sync(monkeypatch):
    async def _run():
        mgr = TokenManager()
        token_info = TokenInfo(token="tok-1", quota=10, status=TokenStatus.ACTIVE)
        pool = TokenPool("ssoBasic")
        pool.add(token_info)
        mgr.pools = {"ssoBasic": pool}

        # Avoid touching storage in unit test.
        monkeypatch.setattr(mgr, "_schedule_save", lambda: None)

        calls = {"count": 0}

        class _FakeUsageService:
            async def get(self, _token: str, model_name: str = "grok-3", retry: bool = True):
                calls["count"] += 1
                return {"remainingTokens": 7}

        monkeypatch.setattr(usage_mod, "UsageService", _FakeUsageService)

        ok = await mgr.sync_usage("tok-1", "grok-3", consume_on_fail=True, is_usage=True)
        assert ok is True
        assert token_info.quota == 9
        assert token_info.use_count == 1

        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert calls["count"] == 0
        assert token_info.quota == 9
        assert token_info.use_count == 1

    asyncio.run(_run())


def test_sync_usage_manual_refresh_still_reads_remote_quota(monkeypatch):
    async def _run():
        mgr = TokenManager()
        token_info = TokenInfo(token="tok-1", quota=10, status=TokenStatus.ACTIVE)
        pool = TokenPool("ssoBasic")
        pool.add(token_info)
        mgr.pools = {"ssoBasic": pool}

        monkeypatch.setattr(mgr, "_schedule_save", lambda: None)

        class _FakeUsageService:
            async def get(self, _token: str, model_name: str = "grok-3", retry: bool = True):
                return {"remainingTokens": 7}

        monkeypatch.setattr(usage_mod, "UsageService", _FakeUsageService)

        ok = await mgr.sync_usage("tok-1", "grok-3", consume_on_fail=False, is_usage=False)
        assert ok is True
        assert token_info.quota == 7
        assert token_info.use_count == 0

    asyncio.run(_run())
