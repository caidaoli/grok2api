import asyncio
import time

import pytest

from app.core.exceptions import UpstreamException
from app.services.grok import usage as usage_mod


def test_usage_404_arms_sync_cooldown(monkeypatch):
    async def _run():
        monkeypatch.setattr(usage_mod, "_USAGE_SYNC_COOLDOWN_UNTIL", 0.0)
        monkeypatch.setattr(
            usage_mod,
            "get_config",
            lambda key, default=None: 60 if key == "grok.usage_sync_backoff_seconds" else default,
        )

        class _Resp:
            status_code = 404

            def json(self):
                return {}

        class _Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, *args, **kwargs):
                return _Resp()

        monkeypatch.setattr(usage_mod, "AsyncSession", _Session)

        svc = usage_mod.UsageService()
        with pytest.raises(UpstreamException):
            await svc.get("tok-demo", "grok-3")

        assert usage_mod._USAGE_SYNC_COOLDOWN_UNTIL > time.time()

    asyncio.run(_run())


def test_usage_sync_skips_remote_during_cooldown(monkeypatch):
    async def _run():
        monkeypatch.setattr(usage_mod, "_USAGE_SYNC_COOLDOWN_UNTIL", time.time() + 30)

        class _Session:
            def __init__(self, *args, **kwargs):
                raise AssertionError("AsyncSession should not be created during cooldown")

        monkeypatch.setattr(usage_mod, "AsyncSession", _Session)

        svc = usage_mod.UsageService()
        result = await svc.get("tok-demo", "grok-3")
        assert result == {}

    asyncio.run(_run())
