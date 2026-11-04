"""Token estimation and model context-window lookup.

Uses tiktoken (o200k_base) when installed; otherwise a ~4 chars/token heuristic.
Real usage numbers returned by Azure always take precedence over estimates.
"""
from __future__ import annotations

import json
import math
from functools import lru_cache
from typing import Any

try:  # pragma: no cover - depends on environment
    import tiktoken

    _ENC = tiktoken.get_encoding("o200k_base")
except Exception:  # noqa: BLE001
    _ENC = None

MESSAGE_OVERHEAD = 4  # role/separator tokens per chat message


def tokenizer_name() -> str:
    return "tiktoken/o200k_base" if _ENC is not None else "heuristic(chars/4)"


@lru_cache(maxsize=4096)
def _count_cached(text: str) -> int:
    if _ENC is not None:
        return len(_ENC.encode(text, disallowed_special=()))
    return math.ceil(len(text) / 4)


def count(text: str | None) -> int:
    if not text:
        return 0
    if len(text) > 20_000:  # don't pollute the cache with huge blobs
        return len(_ENC.encode(text, disallowed_special=())) if _ENC is not None else math.ceil(len(text) / 4)
    return _count_cached(text)


def count_json(obj: Any) -> int:
    if not obj:
        return 0
    return count(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


# Known context windows (total tokens). Matched by substring against the
# model name returned by Azure or the deployment name, longest key wins.
KNOWN_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5": 400_000,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4-32k": 32_768,
    "gpt-4": 8_192,
    "gpt-35-turbo-16k": 16_385,
    "gpt-35-turbo": 16_385,
    "gpt-3.5-turbo": 16_385,
    "o1-mini": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
    "o3": 200_000,
    "o4-mini": 200_000,
    "deepseek-r1": 128_000,
    "deepseek-v3": 128_000,
    "llama-3.3": 128_000,
    "llama-3.1": 128_000,
    "llama-4": 128_000,
    "mistral-large": 128_000,
    "mistral-small": 32_000,
    "phi-4": 16_384,
    "phi-3.5": 128_000,
    "grok-3": 131_072,
    "cohere-command-r": 128_000,
}


def context_window(model: str | None, overrides: dict[str, int], default: int) -> int:
    if not model:
        return default
    m = model.lower()
    if m in overrides:
        return overrides[m]
    for table in (overrides, KNOWN_CONTEXT_WINDOWS):
        best = None
        for key in table:
            if key in m and (best is None or len(key) > len(best)):
                best = key
        if best:
            return table[best]
    return default
