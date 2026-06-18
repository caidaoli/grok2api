import tempfile
import uuid
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import response_middleware as response_middleware_mod


def _build_client(monkeypatch, enabled: bool, exempt_ips=None, ban_file=None) -> TestClient:
    exempt = list(exempt_ips or ["127.0.0.1", "::1"])
    if ban_file is None:
        ban_file = Path(tempfile.gettempdir()) / f"grok2api-banned-{uuid.uuid4().hex}.txt"

    def _fake_get_config(key, default=None):
        if key == "security.auto_ban_unknown_path":
            return enabled
        if key == "security.auto_ban_exempt_ips":
            return exempt
        return default

    monkeypatch.setattr(response_middleware_mod, "get_config", _fake_get_config)
    if ban_file is not None:
        monkeypatch.setattr(
            response_middleware_mod.ResponseLoggerMiddleware,
            "_ban_file_path",
            ban_file,
            raising=False,
        )

    cls = response_middleware_mod.ResponseLoggerMiddleware
    cls._banned_ips = frozenset()
    if hasattr(cls, "_banned_ips_loaded"):
        cls._banned_ips_loaded = False
    if hasattr(cls, "_banned_ips_file_mtime"):
        cls._banned_ips_file_mtime = None
    if hasattr(cls, "_banned_file_checked_at"):
        cls._banned_file_checked_at = 0.0

    app = FastAPI()
    app.add_middleware(response_middleware_mod.ResponseLoggerMiddleware)

    @app.get("/health")
    async def _health():
        return {"ok": True}

    return TestClient(app)


def test_unknown_path_auto_bans_ip_and_blocks_next_request(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "memory")
    client = _build_client(monkeypatch, enabled=True)

    first = client.get("/not-a-real-api")
    assert first.status_code == 403

    second = client.get("/health")
    assert second.status_code == 403


def test_unknown_path_returns_404_when_feature_disabled(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "memory")
    client = _build_client(monkeypatch, enabled=False)

    first = client.get("/not-a-real-api")
    assert first.status_code == 404

    second = client.get("/health")
    assert second.status_code == 200


def test_exempt_ip_will_not_be_banned(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "memory")
    client = _build_client(monkeypatch, enabled=True, exempt_ips=["testclient"])

    first = client.get("/not-a-real-api")
    assert first.status_code == 404

    second = client.get("/health")
    assert second.status_code == 200


def test_auto_ban_persists_ip_to_file(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_MODE", "file")
    ban_file = tmp_path / "banned_ips.txt"
    client = _build_client(monkeypatch, enabled=True, ban_file=ban_file)

    first = client.get("/not-a-real-api")
    assert first.status_code == 403
    assert ban_file.exists()
    assert "testclient" in ban_file.read_text(encoding="utf-8")


def test_auto_ban_loads_from_file_after_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_MODE", "file")
    ban_file = tmp_path / "banned_ips.txt"

    first_client = _build_client(monkeypatch, enabled=True, ban_file=ban_file)
    first = first_client.get("/not-a-real-api")
    assert first.status_code == 403

    # 模拟进程重启：清空内存封禁列表，仅保留文件。
    response_middleware_mod.ResponseLoggerMiddleware._banned_ips = frozenset()
    if hasattr(response_middleware_mod.ResponseLoggerMiddleware, "_banned_ips_loaded"):
        response_middleware_mod.ResponseLoggerMiddleware._banned_ips_loaded = False
    if hasattr(response_middleware_mod.ResponseLoggerMiddleware, "_banned_ips_file_mtime"):
        response_middleware_mod.ResponseLoggerMiddleware._banned_ips_file_mtime = None
    if hasattr(response_middleware_mod.ResponseLoggerMiddleware, "_banned_file_checked_at"):
        response_middleware_mod.ResponseLoggerMiddleware._banned_file_checked_at = 0.0

    second_client = _build_client(monkeypatch, enabled=True, ban_file=ban_file)
    second = second_client.get("/health")
    assert second.status_code == 403


def test_auto_ban_does_not_persist_when_storage_mode_not_file(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_MODE", "memory")
    ban_file = tmp_path / "banned_ips.txt"
    client = _build_client(monkeypatch, enabled=True, ban_file=ban_file)

    first = client.get("/not-a-real-api")
    assert first.status_code == 403
    assert not ban_file.exists()


def test_root_path_is_known_and_will_not_be_banned(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "memory")
    client = _build_client(monkeypatch, enabled=True)

    first = client.get("/")
    assert first.status_code == 404

    second = client.get("/health")
    assert second.status_code == 200


def test_favicon_path_is_known_and_will_not_be_banned(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "memory")
    client = _build_client(monkeypatch, enabled=True)

    first = client.get("/favicon.ico")
    assert first.status_code == 404

    second = client.get("/health")
    assert second.status_code == 200


def test_admin_prefix_is_known_and_will_not_be_banned(monkeypatch):
    monkeypatch.setenv("STORAGE_MODE", "memory")
    client = _build_client(monkeypatch, enabled=True)

    first = client.get("/admin/not-a-real-page")
    assert first.status_code == 404

    second = client.get("/health")
    assert second.status_code == 200
