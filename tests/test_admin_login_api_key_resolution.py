"""Tests for admin login API.

The login endpoint was refactored: it no longer returns raw API keys
or performs legacy key resolution. It now validates username/password
and returns a session token.
"""

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.admin import auth as auth_module
from app.api.v1.admin.auth import router


def _build_login_client(monkeypatch, username="admin", password="admin"):
    monkeypatch.setattr(
        auth_module,
        "get_config",
        lambda key, default=None: {
            "app.admin_username": username,
            "app.app_key": password,
        }.get(key, default),
    )
    monkeypatch.setattr(auth_module, "check_login_rate_limit", AsyncMock())
    monkeypatch.setattr(auth_module, "record_login_failure", AsyncMock())

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_admin_login_success_returns_session_token(monkeypatch):
    client = _build_login_client(monkeypatch)
    resp = client.post("/api/v1/admin/login", json={"username": "admin", "password": "admin"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["api_key"]  # session token returned


def test_admin_login_default_admin_password_works_for_first_deploy(monkeypatch):
    monkeypatch.setattr(auth_module, "get_config", lambda key, default=None: default)
    monkeypatch.setattr(auth_module, "check_login_rate_limit", AsyncMock())
    monkeypatch.setattr(auth_module, "record_login_failure", AsyncMock())

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.post("/api/v1/admin/login", json={"username": "admin", "password": "admin"})

    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_admin_login_wrong_password_rejected(monkeypatch):
    client = _build_login_client(monkeypatch, password="secret")
    resp = client.post("/api/v1/admin/login", json={"username": "admin", "password": "wrong"})

    assert resp.status_code == 401


def test_admin_login_rejects_when_app_key_is_empty(monkeypatch):
    client = _build_login_client(monkeypatch, password="")
    resp = client.post("/api/v1/admin/login", json={"username": "admin", "password": "admin"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "App key is not configured"
