import asyncio
from types import SimpleNamespace

from app.api.v1 import admin as admin_module


def test_admin_login_prefers_global_api_key(monkeypatch):
    async def _fake_load_legacy():
        return {"sk-legacy-a", "sk-legacy-b"}

    def _fake_get_config(path: str, default=None):
        values = {
            "app.admin_username": "admin",
            "app.app_key": "admin",
            "app.api_key": "sk-global",
        }
        return values.get(path, default)

    monkeypatch.setattr(admin_module, "_load_legacy_api_keys", _fake_load_legacy)
    monkeypatch.setattr(admin_module, "get_config", _fake_get_config)

    result = asyncio.run(
        admin_module.admin_login_api(
            SimpleNamespace(headers={}),
            admin_module.AdminLoginBody(username="admin", password="admin"),
        )
    )

    assert result["status"] == "success"
    assert result["api_key"] == "sk-global"


def test_admin_login_falls_back_to_legacy_api_key(monkeypatch):
    async def _fake_load_legacy():
        return {"sk-legacy-z", "sk-legacy-a"}

    def _fake_get_config(path: str, default=None):
        values = {
            "app.admin_username": "admin",
            "app.app_key": "admin",
            "app.api_key": "",
        }
        return values.get(path, default)

    monkeypatch.setattr(admin_module, "_load_legacy_api_keys", _fake_load_legacy)
    monkeypatch.setattr(admin_module, "get_config", _fake_get_config)

    result = asyncio.run(
        admin_module.admin_login_api(
            SimpleNamespace(headers={}),
            admin_module.AdminLoginBody(username="admin", password="admin"),
        )
    )

    assert result["status"] == "success"
    assert result["api_key"] == "sk-legacy-a"
