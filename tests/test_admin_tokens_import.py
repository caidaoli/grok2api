import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

from app.api.v1.admin import tokens as admin_module
from app.core.storage import SQLStorage


class _DummyStorage:
    def __init__(self, existing=None):
        self.existing = set(existing or [])
        self.add_calls = []
        self.save_tokens_calls = 0

    async def add_tokens(self, pool, records):
        self.add_calls.append((pool, records))
        added = []
        for record in records:
            token = record["token"]
            if token in self.existing:
                continue
            self.existing.add(token)
            added.append(token)
        return added

    async def save_tokens(self, _data):
        self.save_tokens_calls += 1
        raise AssertionError("import endpoint must not rewrite the full token set")

    @asynccontextmanager
    async def acquire_lock(self, _name: str, timeout: int = 10):
        yield


class _DummyPool:
    def __init__(self):
        self.items = {}

    def get(self, token):
        return self.items.get(token)


class _DummyManager:
    def __init__(self):
        self.cancel_pending_save_calls = 0
        self.add_token_record_calls = []
        self.reload_calls = 0
        self.pools = {"ssoBasic": _DummyPool()}

    async def cancel_pending_save(self):
        self.cancel_pending_save_calls += 1

    async def reload(self):
        self.reload_calls += 1
        raise AssertionError("import endpoint must not reload the full token set")

    def add_token_records(self, pool, records):
        self.add_token_record_calls.append((pool, records))
        target = self.pools.setdefault(pool, _DummyPool())
        for record in records:
            target.items[record["token"]] = SimpleNamespace(**record)
        return [record["token"] for record in records]


def test_import_tokens_api_uses_targeted_add_without_full_save_or_reload(monkeypatch):
    storage = _DummyStorage(existing={"token-a"})
    mgr = _DummyManager()
    captured = {}

    async def _fake_get_mgr():
        return mgr

    def _fake_trigger(tokens, concurrency, retries):
        captured["tokens"] = tokens
        captured["concurrency"] = concurrency
        captured["retries"] = retries

    monkeypatch.setattr(admin_module, "get_storage", lambda: storage)
    monkeypatch.setattr(admin_module, "get_token_manager", _fake_get_mgr)
    monkeypatch.setattr(admin_module, "_trigger_account_settings_refresh_background", _fake_trigger)
    monkeypatch.setattr(admin_module, "_resolve_nsfw_refresh_concurrency", lambda override=None: 10)
    monkeypatch.setattr(admin_module, "_resolve_nsfw_refresh_retries", lambda override=None: 3)

    result = asyncio.run(admin_module.import_tokens_api({
        "pool": "ssoBasic",
        "tokens": [
            {"token": "sso=token-a", "quota": 80},
            {"token": "token-b", "quota": 70, "note": "new"},
            "token-b",
            "token-c",
        ],
    }))

    assert result["status"] == "success"
    assert result["added"] == 2
    assert result["skipped"] == 1
    assert [item["token"] for item in result["tokens"]] == ["token-b", "token-c"]
    assert captured["tokens"] == ["token-b", "token-c"]
    assert captured["concurrency"] == 10
    assert captured["retries"] == 3
    assert storage.save_tokens_calls == 0
    assert storage.add_calls[0][0] == "ssoBasic"
    assert [record["token"] for record in storage.add_calls[0][1]] == ["token-a", "token-b", "token-c"]
    assert mgr.cancel_pending_save_calls == 1
    assert mgr.reload_calls == 0
    assert [record["token"] for _, records in mgr.add_token_record_calls for record in records] == ["token-b", "token-c"]


class _SelectExistingResult:
    def fetchall(self):
        return [("token-a",)]


class _InsertResult:
    def fetchall(self):
        return []


class _FakeSqlSession:
    def __init__(self):
        self.executed = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((sql, params))
        if sql.startswith("SELECT token FROM tokens"):
            return _SelectExistingResult()
        return _InsertResult()

    async def commit(self):
        self.commits += 1


def test_sql_storage_add_tokens_uses_targeted_insert(monkeypatch):
    session = _FakeSqlSession()
    storage = SQLStorage.__new__(SQLStorage)
    storage.dialect = "mysql"
    storage.async_session = lambda: session

    async def _noop_schema():
        return None

    storage._ensure_schema = _noop_schema

    added = asyncio.run(storage.add_tokens("ssoBasic", [
        {"token": "sso=token-a", "quota": 80},
        {"token": "token-b", "quota": 70},
        "token-c",
    ]))

    assert added == ["token-b", "token-c"]
    assert session.commits == 1
    assert len(session.executed) == 2
    select_sql, select_params = session.executed[0]
    insert_sql, insert_params = session.executed[1]
    assert "SELECT token FROM tokens WHERE token IN" in select_sql
    assert set(select_params.values()) == {"token-a", "token-b", "token-c"}
    assert "INSERT IGNORE INTO tokens" in insert_sql
    assert "DELETE FROM tokens" not in insert_sql
    assert [row["token"] for row in insert_params] == ["token-b", "token-c"]
