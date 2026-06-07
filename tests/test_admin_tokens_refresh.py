import asyncio
import json
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


class _StreamPool:
    def __init__(self, records):
        self.records = records

    def get(self, token):
        return self.records.get(token)


class _StreamManager:
    def __init__(self):
        self.calls = []
        self.invalidated = []
        self.save_scheduled = 0
        self.records = {
            "token-a": SimpleNamespace(
                token="token-a",
                status=TokenStatus.DISABLED,
                quota=7,
                heavy_quota=-1,
                note="",
                fail_count=0,
                use_count=0,
            ),
            "token-b": SimpleNamespace(
                token="token-b",
                status=TokenStatus.DISABLED,
                quota=0,
                heavy_quota=-1,
                note="",
                fail_count=0,
                use_count=0,
            ),
        }
        self.pools = {"ssoBasic": _StreamPool(self.records)}

    async def sync_usage(self, token_str, model_id, *, consume_on_fail, is_usage, retry):
        self.calls.append(token_str)
        return token_str == "token-a"

    def _find_token_info(self, token):
        record = self.records.get(token)
        return record, None

    async def set_token_invalid(self, token, reason="", save=True):
        self.invalidated.append({"token": token, "reason": reason, "save": save})
        record = self.records.get(token)
        if not record:
            return False
        record.status = TokenStatus.DISABLED
        record.last_fail_reason = reason
        return True

    def _schedule_save(self):
        self.save_scheduled += 1


def _collect_stream_body(response) -> str:
    async def _read():
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    return asyncio.run(_read())


def _parse_sse_events(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


def test_refresh_tokens_stream_api_emits_progress_for_each_token(monkeypatch):
    mgr = _StreamManager()

    async def _fake_get_token_manager():
        return mgr

    monkeypatch.setattr(admin_tokens_module, "get_token_manager", _fake_get_token_manager)

    response = asyncio.run(
        admin_tokens_module.refresh_tokens_stream_api(
            {"tokens": ["sso=token-a", "token-b", "token-a"]}
        )
    )
    body = _collect_stream_body(response)
    events = _parse_sse_events(body)

    assert response.media_type == "text/event-stream"
    assert [event["type"] for event in events] == ["progress", "progress", "complete"]
    assert events[0] == {
        "type": "progress",
        "token": "token-a",
        "ok": True,
        "current": 1,
        "total": 2,
        "success": 1,
        "failed": 0,
    } | {"record": events[0]["record"]}
    assert events[0]["record"]["token"] == "token-a"
    assert events[1]["token"] == "token-b"
    assert events[1]["ok"] is False
    assert events[1]["current"] == 2
    assert events[1]["failed"] == 1
    assert events[2]["type"] == "complete"
    assert events[2]["current"] == 2
    assert events[2]["success"] == 1
    assert events[2]["failed"] == 1
    assert mgr.calls == ["token-a", "token-b"]
    assert mgr.save_scheduled == 1


def test_refresh_tokens_stream_api_disables_failed_tokens_and_saves(monkeypatch):
    mgr = _StreamManager()
    mgr.records["token-a"].status = TokenStatus.ACTIVE
    mgr.records["token-b"].status = TokenStatus.ACTIVE

    async def _fake_get_token_manager():
        return mgr

    async def _fake_refresh_one_token_usage(_mgr, token):
        mgr.calls.append(token)
        return False

    monkeypatch.setattr(admin_tokens_module, "get_token_manager", _fake_get_token_manager)
    monkeypatch.setattr(admin_tokens_module, "_refresh_one_token_usage", _fake_refresh_one_token_usage)

    response = asyncio.run(
        admin_tokens_module.refresh_tokens_stream_api({"tokens": ["token-a", "token-b"]})
    )
    events = _parse_sse_events(_collect_stream_body(response))

    assert [event["ok"] for event in events if event["type"] == "progress"] == [False, False]
    assert mgr.invalidated == [
        {"token": "token-a", "reason": "manual_refresh_failed", "save": False},
        {"token": "token-b", "reason": "manual_refresh_failed", "save": False},
    ]
    assert mgr.records["token-a"].status == TokenStatus.DISABLED
    assert mgr.records["token-b"].status == TokenStatus.DISABLED
    assert mgr.save_scheduled == 1
