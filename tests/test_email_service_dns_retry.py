from __future__ import annotations

import requests

from app.services.register.services.email_service import EmailService
import app.services.register.services.email_service as email_module


def test_create_email_fallbacks_to_doh_when_dns_resolution_fails(monkeypatch):
    called = {"count": 0}

    def _fake_post(*_args, **_kwargs):
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='mail-back.caidao.workers.dev', port=443): "
            "Max retries exceeded with url: /admin/new_address "
            "(Caused by NameResolutionError(\"Failed to resolve host\"))"
        )

    def _fake_doh_fallback(self, path: str, payload: dict, headers: dict):
        called["count"] += 1
        assert path == "/admin/new_address"
        assert payload["domain"] == "5179.nyc.mn"
        assert headers["x-admin-auth"] == "x"
        return "jwt-token", "test@5179.nyc.mn"

    monkeypatch.setattr(email_module.requests, "post", _fake_post)
    monkeypatch.setattr(email_module.random, "choices", lambda seq, k: [seq[0]] * k)
    monkeypatch.setattr(email_module.random, "randint", lambda a, _b: a)
    monkeypatch.setattr(EmailService, "_create_email_via_doh", _fake_doh_fallback, raising=False)

    svc = EmailService(
        worker_domain="mail-back.caidao.workers.dev",
        email_domain="5179.nyc.mn",
        admin_password="x",
    )

    jwt, address = svc.create_email()

    assert called["count"] == 1
    assert jwt == "jwt-token"
    assert address == "test@5179.nyc.mn"


def test_create_email_does_not_fallback_on_non_dns_errors(monkeypatch):
    called = {"count": 0}

    def _fake_post(*_args, **_kwargs):
        raise requests.exceptions.ConnectTimeout("connect timeout")

    def _fake_doh_fallback(self, path: str, payload: dict, headers: dict):
        called["count"] += 1
        return "jwt-token", "test@5179.nyc.mn"

    monkeypatch.setattr(email_module.requests, "post", _fake_post)
    monkeypatch.setattr(email_module.random, "choices", lambda seq, k: [seq[0]] * k)
    monkeypatch.setattr(email_module.random, "randint", lambda a, _b: a)
    monkeypatch.setattr(EmailService, "_create_email_via_doh", _fake_doh_fallback, raising=False)

    svc = EmailService(
        worker_domain="mail-back.caidao.workers.dev",
        email_domain="5179.nyc.mn",
        admin_password="x",
    )

    jwt, address = svc.create_email()

    assert called["count"] == 0
    assert jwt is None
    assert address is None
