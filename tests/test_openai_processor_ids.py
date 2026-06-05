import asyncio
from typing import Any, AsyncGenerator, Iterable

import orjson
import pytest

import app.services.grok.processor as processor_mod


async def _ndjson_stream(items: Iterable[dict[str, Any]]) -> AsyncGenerator[bytes, None]:
    for item in items:
        yield orjson.dumps(item)


def _parse_sse_data(chunk: str) -> Any:
    assert chunk.startswith("data: ")
    payload = chunk[len("data: ") :].strip()
    if payload == "[DONE]":
        return "[DONE]"
    return orjson.loads(payload)


def _fake_get_config(key: str, default=None):
    if key == "app.app_url":
        return ""
    if key == "grok.filter_tags":
        return []
    if key == "grok.thinking":
        return False
    if key == "app.image_format":
        return "url"
    if key == "grok.video_poster_preview":
        return False
    return default


def test_stream_processor_emits_stable_chatcmpl_id(monkeypatch):
    monkeypatch.setattr(processor_mod, "get_config", _fake_get_config)

    proc = processor_mod.StreamProcessor(
        model="grok-4-mini-thinking-tahoe",
        token="test-token",
        think=False,
        prompt_tokens=7,
    )

    upstream = [
        {"result": {"response": {"token": "Hello"}}},
        {"result": {"response": {"token": " world"}}},
    ]

    async def _run():
        out: list[str] = []
        async for chunk in proc.process(_ndjson_stream(upstream)):
            out.append(chunk)
        return out

    chunks = asyncio.run(_run())
    parsed = [_parse_sse_data(c) for c in chunks]

    assert parsed[-1] == "[DONE]"
    objs = [x for x in parsed if isinstance(x, dict)]
    ids = {x.get("id") for x in objs}
    assert len(ids) == 1
    only_id = next(iter(ids))
    assert isinstance(only_id, str) and only_id.startswith("chatcmpl-")


def test_collect_processor_returns_non_empty_chatcmpl_id(monkeypatch):
    monkeypatch.setattr(processor_mod, "get_config", _fake_get_config)

    proc = processor_mod.CollectProcessor(
        model="grok-4-mini-thinking-tahoe",
        token="test-token",
        prompt_tokens=0,
    )

    upstream = [
        {"result": {"response": {"modelResponse": {"message": "hi", "generatedImageUrls": []}}}},
    ]

    result = asyncio.run(proc.process(_ndjson_stream(upstream)))

    assert isinstance(result.get("id"), str)
    assert result["id"].startswith("chatcmpl-")


def test_collect_processor_reraises_midstream_errors(monkeypatch):
    monkeypatch.setattr(processor_mod, "get_config", _fake_get_config)

    proc = processor_mod.CollectProcessor(
        model="grok-4-mini-thinking-tahoe",
        token="test-token",
        prompt_tokens=0,
    )
    closed = False

    async def _close():
        nonlocal closed
        closed = True

    async def _broken_stream():
        yield orjson.dumps(
            {"result": {"response": {"modelResponse": {"message": "partial", "generatedImageUrls": []}}}}
        )
        raise RuntimeError("upstream broke")

    monkeypatch.setattr(proc, "close", _close)

    with pytest.raises(RuntimeError, match="upstream broke"):
        asyncio.run(proc.process(_broken_stream()))

    assert closed is True


def test_stream_processor_first_chunk_contains_content_from_upstream(monkeypatch):
    """
    不要为了“协议样子”先发一个空 role chunk。
    首包应该至少包含一个来自上游的可见内容 token，避免首字耗时统计失真。
    """
    monkeypatch.setattr(processor_mod, "get_config", _fake_get_config)

    proc = processor_mod.StreamProcessor(
        model="grok-4-mini-thinking-tahoe",
        token="test-token",
        think=False,
        prompt_tokens=0,
    )

    upstream = [
        {"result": {"response": {"llmInfo": {"modelHash": "abc"}}}},  # 元数据：不应触发下游输出
        {"result": {"response": {"token": "Hello"}}},
        {"result": {"response": {"token": " world"}}},
    ]

    async def _run():
        out: list[str] = []
        async for chunk in proc.process(_ndjson_stream(upstream)):
            out.append(chunk)
        return out

    chunks = asyncio.run(_run())
    parsed = [_parse_sse_data(c) for c in chunks]

    first = next(x for x in parsed if isinstance(x, dict) and x.get("choices"))
    delta = first["choices"][0]["delta"]
    assert delta.get("content") == "Hello"


def test_stream_processor_does_not_emit_thinking_tokens_when_disabled(monkeypatch):
    monkeypatch.setattr(processor_mod, "get_config", _fake_get_config)

    proc = processor_mod.StreamProcessor(
        model="grok-4-mini-thinking-tahoe",
        token="test-token",
        think=False,
        prompt_tokens=0,
    )

    upstream = [
        {"result": {"response": {"token": "Thinking about your request", "isThinking": True}}},
        {"result": {"response": {"token": "Checking review duration", "isThinking": True}}},
        {"result": {"response": {"token": "Final answer", "isThinking": False}}},
    ]

    async def _run():
        out: list[str] = []
        async for chunk in proc.process(_ndjson_stream(upstream)):
            out.append(chunk)
        return out

    chunks = asyncio.run(_run())
    parsed = [_parse_sse_data(c) for c in chunks]
    content = "".join(
        choice["delta"].get("content", "")
        for obj in parsed
        if isinstance(obj, dict)
        for choice in obj.get("choices", [])
        if choice.get("delta")
    )

    assert content == "Final answer"


def test_stream_processor_defaults_to_hiding_thinking_for_openai_compat(monkeypatch):
    def _config_with_global_thinking_enabled(key: str, default=None):
        if key == "grok.thinking":
            return True
        return _fake_get_config(key, default)

    monkeypatch.setattr(processor_mod, "get_config", _config_with_global_thinking_enabled)

    proc = processor_mod.StreamProcessor(
        model="grok-4-mini-thinking-tahoe",
        token="test-token",
        think=None,
        prompt_tokens=0,
    )

    upstream = [
        {"result": {"response": {"token": "Thinking about your request", "isThinking": True}}},
        {"result": {"response": {"token": "Final answer", "isThinking": False}}},
    ]

    async def _run():
        out: list[str] = []
        async for chunk in proc.process(_ndjson_stream(upstream)):
            out.append(chunk)
        return out

    chunks = asyncio.run(_run())
    parsed = [_parse_sse_data(c) for c in chunks]
    content = "".join(
        choice["delta"].get("content", "")
        for obj in parsed
        if isinstance(obj, dict)
        for choice in obj.get("choices", [])
        if choice.get("delta")
    )

    assert content == "Final answer"
