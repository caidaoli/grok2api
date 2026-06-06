import asyncio
from contextlib import asynccontextmanager

from app.api.v1.admin import tokens as admin_module
from app.services.token import manager as manager_module
from app.core.storage import SQLStorage


class _DummyStorage:
    def __init__(self):
        self.deleted_tokens: list[str] = []
        self.save_tokens_calls = 0

    async def delete_tokens(self, tokens):
        self.deleted_tokens.extend(tokens)
        return len(tokens)

    async def save_tokens(self, _data):
        self.save_tokens_calls += 1
        raise AssertionError("delete endpoint must not rewrite the full token set")

    @asynccontextmanager
    async def acquire_lock(self, _name: str, timeout: int = 10):
        yield


class _DummyTokenManager:
    def __init__(self):
        self.cancel_pending_save_calls = 0
        self.removed_tokens: list[str] = []

    async def cancel_pending_save(self):
        self.cancel_pending_save_calls += 1

    def remove_tokens(self, tokens):
        self.removed_tokens.extend(tokens)
        return len(tokens)


def test_delete_tokens_api_uses_targeted_storage_delete(monkeypatch):
    storage = _DummyStorage()
    mgr = _DummyTokenManager()

    async def _fake_get_mgr():
        return mgr

    monkeypatch.setattr(admin_module, "get_storage", lambda: storage)
    monkeypatch.setattr(admin_module, "get_token_manager", _fake_get_mgr)

    result = asyncio.run(
        admin_module.delete_tokens_api({"tokens": ["sso=token-a", "token-b", "token-a"]})
    )

    assert result == {"status": "success", "deleted": 2}
    assert storage.deleted_tokens == ["token-a", "token-b"]
    assert storage.save_tokens_calls == 0
    assert mgr.cancel_pending_save_calls == 1
    assert mgr.removed_tokens == ["token-a", "token-b"]


class _FakeExecuteResult:
    rowcount = 2


class _FakeSession:
    def __init__(self):
        self.executed: list[tuple[str, dict]] = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params or {}))
        return _FakeExecuteResult()

    async def commit(self):
        self.commits += 1


class _FakeSelectResult:
    def fetchall(self):
        return []


class _FakeSelectSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt, params=None):
        return _FakeSelectResult()


def test_sql_storage_delete_tokens_uses_single_targeted_delete(monkeypatch):
    session = _FakeSession()
    storage = SQLStorage.__new__(SQLStorage)
    storage.dialect = "mysql"
    storage.async_session = lambda: session

    async def _noop_schema():
        return None

    storage._ensure_schema = _noop_schema

    deleted = asyncio.run(storage.delete_tokens(["sso=token-a", "token-b", "token-a"]))

    assert deleted == 2
    assert session.commits == 1
    assert len(session.executed) == 1
    sql, params = session.executed[0]
    assert "DELETE FROM tokens WHERE token IN" in sql
    assert "INSERT INTO tokens" not in sql
    assert set(params.values()) == {"token-a", "token-b"}


def test_sql_storage_empty_tokens_table_is_valid_empty_state():
    storage = SQLStorage.__new__(SQLStorage)
    storage.dialect = "mysql"
    storage.async_session = lambda: _FakeSelectSession()

    async def _noop_schema():
        return None

    storage._ensure_schema = _noop_schema

    assert asyncio.run(storage.load_tokens()) == {}


class _EmptyRemoteStorage:
    def __init__(self):
        self.save_tokens_calls = 0

    async def load_tokens(self):
        return {}

    async def save_tokens(self, _data):
        self.save_tokens_calls += 1
        raise AssertionError("empty remote storage must not be repopulated from local tokens")


class _LocalStorageWithLegacyToken:
    async def load_tokens(self):
        return {"ssoBasic": [{"token": "legacy-local-token", "quota": 80}]}


def test_token_manager_keeps_explicit_empty_remote_storage_empty(monkeypatch):
    remote = _EmptyRemoteStorage()

    monkeypatch.setattr(manager_module, "get_storage", lambda: remote)
    monkeypatch.setattr("app.core.storage.LocalStorage", lambda: _LocalStorageWithLegacyToken())

    mgr = manager_module.TokenManager()
    asyncio.run(mgr._load())

    assert mgr.initialized is True
    assert sum(pool.count() for pool in mgr.pools.values()) == 0
    assert remote.save_tokens_calls == 0
