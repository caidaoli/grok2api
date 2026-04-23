import asyncio
from types import SimpleNamespace

from app.api.v1.admin import tokens as admin_tokens_module
from app.services.token.models import TokenStatus


class _DummyManager:
    def __init__(self):
        self.calls = []
        self.save_scheduled = 0
        self.token_info = SimpleNamespace(status=TokenStatus.DISABLED)

    async def sync_usage(self, token_str, model_id, *, consume_on_fail, is_usage, retry):
        self.calls.append(
            {
                "token": token_str,
                "model": model_id,
                "consume_on_fail": consume_on_fail,
                "is_usage": is_usage,
                "retry": retry,
            }
        )
        return True

    def _find_token_info(self, _token: str):
        return self.token_info, None

    def _schedule_save(self):
        self.save_scheduled += 1


def test_refresh_tokens_api_disables_retry_for_manual_token_check(monkeypatch):
    mgr = _DummyManager()

    async def _fake_get_token_manager():
        return mgr

    monkeypatch.setattr(admin_tokens_module, "get_token_manager", _fake_get_token_manager)

    result = asyncio.run(admin_tokens_module.refresh_tokens_api({"token": "token-a"}))

    assert result["status"] == "success"
    assert result["results"] == {"token-a": True}
    assert mgr.calls == [
        {
            "token": "token-a",
            "model": "grok-3",
            "consume_on_fail": False,
            "is_usage": False,
            "retry": False,
        }
    ]
    assert mgr.token_info.status == TokenStatus.ACTIVE
    assert mgr.save_scheduled == 1
