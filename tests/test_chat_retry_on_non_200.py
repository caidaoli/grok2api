import asyncio

import pytest

from app.core.exceptions import UpstreamException
from app.core.logger import logger
from app.services.grok import chat as chat_mod


class _DummyTokenManager:
    def __init__(self):
        self.select_calls = []
        self.fail_calls = []

    async def reload_if_stale(self):
        return None

    def get_token_for_model(self, model, exclude=None):
        excluded = tuple(sorted(exclude or set()))
        self.select_calls.append((model, excluded))
        for token in ("token-a-long-1234567890", "token-b-long-0987654321"):
            if token not in set(excluded):
                return token
        return None

    async def record_fail(self, token, status, reason):
        self.fail_calls.append((token, status, reason))
        return True

    async def sync_usage(self, *args, **kwargs):
        return True


def test_non_200_should_switch_token_even_if_not_in_retry_codes(monkeypatch):
    async def _run():
        mgr = _DummyTokenManager()
        sleep_calls = []

        async def _fake_get_token_manager():
            return mgr

        async def _fake_chat_openai(self, token, request):
            raise UpstreamException("upstream-500", details={"status": 500})

        async def _fake_sleep(delay):
            sleep_calls.append(delay)

        monkeypatch.setattr(chat_mod, "get_token_manager", _fake_get_token_manager)
        monkeypatch.setattr(chat_mod.RetryConfig, "get_max_retry", staticmethod(lambda: 1))
        # 故意不包含 500，验证“非 200 一律重试换 key”
        monkeypatch.setattr(chat_mod.RetryConfig, "get_retry_codes", staticmethod(lambda: [401, 429, 403]))
        monkeypatch.setattr(chat_mod.GrokChatService, "chat_openai", _fake_chat_openai)
        monkeypatch.setattr(chat_mod.asyncio, "sleep", _fake_sleep)

        warning_messages = []
        sink_id = logger.add(lambda message: warning_messages.append(message.record["message"]), level="WARNING")
        try:
            with pytest.raises(UpstreamException):
                await chat_mod.ChatService.completions(
                    model="grok-3",
                    messages=[{"role": "user", "content": "hello"}],
                    stream=False,
                )
        finally:
            logger.remove(sink_id)

        assert mgr.select_calls == [
            ("grok-3", ()),
            ("grok-3", ("token-a-long-1234567890",)),
        ]
        assert [item[:2] for item in mgr.fail_calls] == [
            ("token-a-long-1234567890", 500),
            ("token-b-long-0987654321", 500),
        ]
        assert sleep_calls == [0.5]
        assert any("token-a-long-1234567890" in msg for msg in warning_messages)

    asyncio.run(_run())
