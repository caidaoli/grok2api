import asyncio

from app.services.grok import chat as chat_mod
from app.services.grok import tokenizer as tokenizer_mod


def test_count_prompt_tokens_should_use_batch_encoding_for_large_fragmented_input(monkeypatch):
    class _FakeEncoding:
        def __init__(self):
            self.batch_called = 0
            self.encode_called = 0
            self.batch_inputs = []

        def encode_batch(self, texts, **_kwargs):
            self.batch_called += 1
            self.batch_inputs.append(list(texts))
            return [[1] * len(text) for text in texts]

        def encode(self, text):
            self.encode_called += 1
            return [1] * len(text)

    async def _run():
        fake = _FakeEncoding()
        monkeypatch.setattr(tokenizer_mod, "_encoder", fake)
        monkeypatch.setattr(tokenizer_mod, "_encoder_failed", False)

        messages = [
            {"role": "user", "content": "u" * 5000},
            {"role": "assistant", "content": "a" * 5000},
            {"role": "user", "content": [{"type": "text", "text": "z" * 500}] * 20},
            {"role": "assistant", "content": [{"type": "text", "text": "y" * 500}] * 20},
        ]

        total = await chat_mod._count_prompt_tokens(messages)

        # base overhead 3 + per-message overhead 4 * 4 + text lengths (5000 + 5000 + 500*20 + 500*20)
        assert total == 30019
        assert fake.batch_called == 1
        assert fake.encode_called == 0
        assert len(fake.batch_inputs[0]) == 42

    asyncio.run(_run())


def test_count_prompt_tokens_should_use_encode_for_small_input(monkeypatch):
    class _FakeEncoding:
        def __init__(self):
            self.batch_called = 0
            self.encode_called = 0
            self.batch_inputs = []

        def encode_batch(self, texts, **_kwargs):
            self.batch_called += 1
            self.batch_inputs.append(list(texts))
            return [[1] * len(text) for text in texts]

        def encode(self, text):
            self.encode_called += 1
            return [1] * len(text)

    async def _run():
        fake = _FakeEncoding()
        monkeypatch.setattr(tokenizer_mod, "_encoder", fake)
        monkeypatch.setattr(tokenizer_mod, "_encoder_failed", False)

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]

        total = await chat_mod._count_prompt_tokens(messages)

        assert total == 15
        assert fake.batch_called == 0
        assert fake.encode_called == 2

    asyncio.run(_run())


def test_count_prompt_tokens_should_respect_config_thresholds(monkeypatch):
    class _FakeEncoding:
        def __init__(self):
            self.batch_called = 0
            self.encode_called = 0

        def encode_batch(self, texts, **_kwargs):
            self.batch_called += 1
            return [[1] * len(text) for text in texts]

        def encode(self, text):
            self.encode_called += 1
            return [1] * len(text)

    async def _run():
        fake = _FakeEncoding()
        monkeypatch.setattr(tokenizer_mod, "_encoder", fake)
        monkeypatch.setattr(tokenizer_mod, "_encoder_failed", False)

        config_map = {
            "performance.prompt_token_batch_min_parts": 1,
            "performance.prompt_token_batch_min_total_chars": 1,
            "performance.prompt_token_batch_min_avg_chars": 1,
            "performance.prompt_token_batch_threads": 2,
        }
        monkeypatch.setattr(
            chat_mod,
            "get_config",
            lambda key, default=None: config_map.get(key, default),
        )

        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]

        total = await chat_mod._count_prompt_tokens(messages)

        assert total == 15
        assert fake.batch_called == 1
        assert fake.encode_called == 0

    asyncio.run(_run())
