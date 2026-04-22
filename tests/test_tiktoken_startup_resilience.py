import asyncio
import importlib
import sys

import tiktoken


def _drop_module(name: str) -> None:
    sys.modules.pop(name, None)


def test_processor_module_import_should_not_initialize_tiktoken(monkeypatch):
    calls: list[str] = []

    def _boom(name: str):
        calls.append(name)
        raise RuntimeError("network is unavailable")

    monkeypatch.setattr(tiktoken, "get_encoding", _boom)
    _drop_module("app.services.grok.processor")

    module = importlib.import_module("app.services.grok.processor")

    assert module.__name__ == "app.services.grok.processor"
    assert calls == []


def test_chat_module_import_should_not_initialize_tiktoken(monkeypatch):
    calls: list[str] = []

    def _boom(name: str):
        calls.append(name)
        raise RuntimeError("network is unavailable")

    monkeypatch.setattr(tiktoken, "get_encoding", _boom)
    _drop_module("app.services.grok.chat")
    _drop_module("app.services.grok.processor")

    module = importlib.import_module("app.services.grok.chat")

    assert module.__name__ == "app.services.grok.chat"
    assert calls == []


def test_count_prompt_tokens_should_fallback_when_tiktoken_unavailable(monkeypatch):
    chat_mod = importlib.import_module("app.services.grok.chat")
    tokenizer_mod = importlib.import_module("app.services.grok.tokenizer")

    monkeypatch.setattr(tokenizer_mod, "_encoder", None)
    monkeypatch.setattr(tokenizer_mod, "_encoder_failed", False)
    monkeypatch.setattr(
        tokenizer_mod.tiktoken,
        "get_encoding",
        lambda _name: (_ for _ in ()).throw(RuntimeError("network is unavailable")),
    )

    async def _run():
        total = await chat_mod._count_prompt_tokens(
            [
                {"role": "user", "content": "hello world"},
                {"role": "assistant", "content": [{"type": "text", "text": "second message"}]},
            ]
        )
        assert total > 0

    asyncio.run(_run())


def test_count_tokens_async_should_fallback_when_tiktoken_unavailable(monkeypatch):
    processor_mod = importlib.import_module("app.services.grok.processor")
    tokenizer_mod = importlib.import_module("app.services.grok.tokenizer")

    monkeypatch.setattr(tokenizer_mod, "_encoder", None)
    monkeypatch.setattr(tokenizer_mod, "_encoder_failed", False)
    monkeypatch.setattr(
        tokenizer_mod.tiktoken,
        "get_encoding",
        lambda _name: (_ for _ in ()).throw(RuntimeError("network is unavailable")),
    )

    async def _run():
        total = await processor_mod._count_tokens_async("hello world")
        assert total > 0

    asyncio.run(_run())
