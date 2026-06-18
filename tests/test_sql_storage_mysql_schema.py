import asyncio
import re

from app.core.storage import SQLStorage


class _FakeConn:
    def __init__(self):
        self.sqls: list[str] = []

    async def execute(self, stmt):
        self.sqls.append(str(stmt))
        return None


class _FakeBeginCtx:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeEngine:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def begin(self):
        return _FakeBeginCtx(self._conn)


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip().lower()


def _find_tokens_ddl(sqls: list[str]) -> str:
    for sql in sqls:
        if "create table if not exists tokens" in sql.lower():
            return sql
    raise AssertionError("tokens DDL was not executed")


def _run_ensure_schema(dialect: str) -> str:
    conn = _FakeConn()
    storage = SQLStorage.__new__(SQLStorage)
    storage.dialect = dialect
    storage.engine = _FakeEngine(conn)
    storage._initialized = False

    asyncio.run(storage._ensure_schema())

    return _normalize_sql(_find_tokens_ddl(conn.sqls))


def test_mysql_schema_should_use_191_char_primary_key_for_utf8mb4_limits():
    ddl = _run_ensure_schema("mysql")
    assert "token varchar(191) primary key" in ddl
