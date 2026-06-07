import asyncio
from contextlib import asynccontextmanager

from app.api.v1.admin import tokens as admin_module


class _DummyStorage:
    def __init__(self, token_data):
        self._token_data = token_data

    async def load_tokens(self):
        return self._token_data

    async def save_tokens(self, data):
        self._token_data = data

    @asynccontextmanager
    async def acquire_lock(self, _name: str, timeout: int = 10):
        yield


class _DummyTokenManager:
    def __init__(self):
        self.reload_calls = 0
        self.cancel_pending_save_calls = 0

    async def reload(self):
        self.reload_calls += 1

    async def cancel_pending_save(self):
        self.cancel_pending_save_calls += 1


def test_update_tokens_api_does_not_trigger_background_for_new_tokens(monkeypatch):
    storage = _DummyStorage({"ssoBasic": [{"token": "token-a", "status": "active", "quota": 80}]})
    mgr = _DummyTokenManager()

    async def _fake_get_mgr():
        return mgr

    def _fail_refresh(*_args, **_kwargs):
        raise AssertionError("token update endpoint must not trigger account-settings refresh")

    monkeypatch.setattr(admin_module, "get_storage", lambda: storage)
    monkeypatch.setattr(admin_module, "get_token_manager", _fake_get_mgr)
    monkeypatch.setattr(admin_module, "_trigger_account_settings_refresh_background", _fail_refresh, raising=False)

    payload = {
        "ssoBasic": [
            {"token": "token-a", "status": "active", "quota": 80},
            {"token": "token-b", "status": "active", "quota": 80},
        ]
    }
    result = asyncio.run(admin_module.update_tokens_api(payload))

    assert result["status"] == "success"
    assert "nsfw_refresh" not in result
    assert mgr.cancel_pending_save_calls == 1
    assert mgr.reload_calls == 1


def test_update_tokens_api_does_not_trigger_when_no_new_tokens(monkeypatch):
    storage = _DummyStorage({"ssoBasic": [{"token": "token-a", "status": "active", "quota": 80}]})
    mgr = _DummyTokenManager()

    async def _fake_get_mgr():
        return mgr

    def _fail_refresh(*_args, **_kwargs):
        raise AssertionError("token update endpoint must not trigger account-settings refresh")

    monkeypatch.setattr(admin_module, "get_storage", lambda: storage)
    monkeypatch.setattr(admin_module, "get_token_manager", _fake_get_mgr)
    monkeypatch.setattr(admin_module, "_trigger_account_settings_refresh_background", _fail_refresh, raising=False)

    payload = {"ssoBasic": [{"token": "token-a", "status": "active", "quota": 70, "note": "edited"}]}
    result = asyncio.run(admin_module.update_tokens_api(payload))

    assert result["status"] == "success"
    assert "nsfw_refresh" not in result
    assert mgr.cancel_pending_save_calls == 1
    assert mgr.reload_calls == 1
