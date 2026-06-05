import asyncio
import base64
import socket

import pytest

import app.services.grok.assets as assets_mod
from app.core.exceptions import ValidationException


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, content: bytes = b"ok", content_type: str = "image/png"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


class _NoNetworkSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        raise AssertionError("network should not be called for rejected URLs")


class _FakeSession:
    def __init__(self, resp: _FakeResponse):
        self._resp = resp
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url: str, *args, **kwargs):
        self.calls.append(url)
        return self._resp


def test_fetch_rejects_loopback_ip(monkeypatch):
    monkeypatch.setattr(assets_mod, "AsyncSession", _NoNetworkSession)

    with pytest.raises(ValidationException):
        asyncio.run(assets_mod.BaseService.fetch("http://127.0.0.1/secret"))


def test_fetch_rejects_domain_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(assets_mod, "AsyncSession", _NoNetworkSession)

    def _fake_getaddrinfo(host, *args, **kwargs):
        assert host == "example.internal"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.10", 80)),
        ]

    monkeypatch.setattr(assets_mod.socket, "getaddrinfo", _fake_getaddrinfo, raising=False)

    with pytest.raises(ValidationException):
        asyncio.run(assets_mod.BaseService.fetch("http://example.internal/a.png"))


def test_fetch_rejects_domain_when_dns_resolution_times_out(monkeypatch):
    monkeypatch.setattr(assets_mod, "AsyncSession", _NoNetworkSession)

    def _fake_getaddrinfo(host, *args, **kwargs):
        assert host == "slow.example"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 80)),
        ]

    async def _fake_wait_for(awaitable, timeout):
        awaitable.close()
        assert timeout == 5
        raise TimeoutError

    monkeypatch.setattr(assets_mod.socket, "getaddrinfo", _fake_getaddrinfo, raising=False)
    monkeypatch.setattr(assets_mod.asyncio, "wait_for", _fake_wait_for)

    with pytest.raises(ValidationException) as exc_info:
        asyncio.run(assets_mod.BaseService.fetch("http://slow.example/a.png"))

    assert "unable to resolve host" in str(exc_info.value)


def test_fetch_allows_public_ip_and_returns_base64(monkeypatch):
    resp = _FakeResponse(status_code=200, content=b"hello", content_type="text/plain")
    session = _FakeSession(resp)
    monkeypatch.setattr(assets_mod, "AsyncSession", lambda: session)

    filename, b64, content_type = asyncio.run(assets_mod.BaseService.fetch("https://1.1.1.1/file.txt"))

    assert filename == "file.txt"
    assert b64 == base64.b64encode(b"hello").decode()
    assert content_type == "text/plain"
    assert session.calls == ["https://1.1.1.1/file.txt"]
