from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import chat as chat_api
from app.core.exceptions import UpstreamException, register_exception_handlers


def _build_client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(chat_api.router, prefix="/v1")
    app.dependency_overrides[chat_api.verify_api_key] = lambda: "test-key"
    return TestClient(app)


def test_chat_completions_default_stream_should_be_false(monkeypatch):
    observed: dict[str, object] = {}

    monkeypatch.setattr(chat_api.ModelService, "valid", staticmethod(lambda _m: True))
    monkeypatch.setattr(
        chat_api.ModelService,
        "get",
        staticmethod(lambda _m: SimpleNamespace(is_video=False)),
    )

    async def _fake_quota(_api_key, _model):
        return None

    async def _fake_completions(*, model, messages, stream=None, thinking=None):
        observed["stream"] = stream
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok", "refusal": None, "annotations": []},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(chat_api, "enforce_daily_quota", _fake_quota)
    monkeypatch.setattr(chat_api.ChatService, "completions", staticmethod(_fake_completions))

    client = _build_client()
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "grok-4.2-fast",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    assert resp.status_code == 200
    assert resp.json()["object"] == "chat.completion"
    assert observed["stream"] is False


def test_chat_completions_stream_error_before_first_chunk_returns_json(monkeypatch):
    monkeypatch.setattr(chat_api.ModelService, "valid", staticmethod(lambda _m: True))
    monkeypatch.setattr(
        chat_api.ModelService,
        "get",
        staticmethod(lambda _m: SimpleNamespace(is_video=False)),
    )

    async def _fake_quota(_api_key, _model):
        return None

    async def _fake_completions(*, model, messages, stream=None, thinking=None):
        async def _stream():
            raise UpstreamException("Grok API request failed: 403", details={"status": 403})
            yield ""

        return _stream()

    monkeypatch.setattr(chat_api, "enforce_daily_quota", _fake_quota)
    monkeypatch.setattr(chat_api.ChatService, "completions", staticmethod(_fake_completions))

    client = _build_client()
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "grok-4.2-fast",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "upstream_error"


@pytest.mark.parametrize(
    ("thinking_value", "expected"),
    [
        (True, "enabled"),
        (False, "disabled"),
        ("true", "enabled"),
        ("0", "disabled"),
        ("yes", "enabled"),
        ("off", "disabled"),
    ],
)
def test_chat_completions_normalizes_common_thinking_bool_values(monkeypatch, thinking_value, expected):
    observed: dict[str, object] = {}

    monkeypatch.setattr(chat_api.ModelService, "valid", staticmethod(lambda _m: True))
    monkeypatch.setattr(
        chat_api.ModelService,
        "get",
        staticmethod(lambda _m: SimpleNamespace(is_video=False)),
    )

    async def _fake_quota(_api_key, _model):
        return None

    async def _fake_completions(*, model, messages, stream=None, thinking=None):
        observed["thinking"] = thinking
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok", "refusal": None, "annotations": []},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    monkeypatch.setattr(chat_api, "enforce_daily_quota", _fake_quota)
    monkeypatch.setattr(chat_api.ChatService, "completions", staticmethod(_fake_completions))

    client = _build_client()
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "grok-4.2-fast",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": thinking_value,
        },
    )

    assert resp.status_code == 200
    assert observed["thinking"] == expected


def test_chat_completions_rejects_invalid_thinking_value(monkeypatch):
    monkeypatch.setattr(chat_api.ModelService, "valid", staticmethod(lambda _m: True))
    monkeypatch.setattr(
        chat_api.ModelService,
        "get",
        staticmethod(lambda _m: SimpleNamespace(is_video=False)),
    )

    async def _fake_quota(_api_key, _model):
        return None

    monkeypatch.setattr(chat_api, "enforce_daily_quota", _fake_quota)

    client = _build_client()
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "grok-4.2-fast",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": "maybe",
        },
    )

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_thinking"
