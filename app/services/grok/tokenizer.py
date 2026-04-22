"""
Token counting helpers.

Do not initialize tiktoken at import time. Startup must not depend on network.
"""

import math

import tiktoken

from app.core.logger import logger


_ENCODING_NAME = "o200k_base"
_FALLBACK_BYTES_PER_TOKEN = 4

_encoder = None
_encoder_failed = False
_encoder_warned = False
_runtime_warned = False


def _estimate_tokens(text: str) -> int:
    """Approximate tokens when tiktoken is unavailable."""
    if not text:
        return 0
    return max(1, math.ceil(len(text.encode("utf-8")) / _FALLBACK_BYTES_PER_TOKEN))


def get_encoder():
    """Lazily initialize the shared encoder."""
    global _encoder, _encoder_failed, _encoder_warned

    if _encoder is not None:
        return _encoder
    if _encoder_failed:
        return None

    try:
        _encoder = tiktoken.get_encoding(_ENCODING_NAME)
    except Exception as exc:
        _encoder_failed = True
        if not _encoder_warned:
            logger.warning(
                "Failed to initialize tiktoken encoding {}, falling back to approximate token counts: {}",
                _ENCODING_NAME,
                exc,
            )
            _encoder_warned = True
        return None

    return _encoder


def count_text_tokens(text: str) -> int:
    """Count tokens using tiktoken when available, otherwise use a cheap estimate."""
    global _runtime_warned

    if not text:
        return 0

    enc = get_encoder()
    if enc is None:
        return _estimate_tokens(text)

    try:
        return len(enc.encode(text))
    except Exception as exc:
        if not _runtime_warned:
            logger.warning(
                "tiktoken encode failed, falling back to approximate token counts: {}",
                exc,
            )
            _runtime_warned = True
        return _estimate_tokens(text)


__all__ = ["count_text_tokens", "get_encoder"]
