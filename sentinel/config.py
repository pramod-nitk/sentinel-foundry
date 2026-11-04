"""Runtime configuration, read from environment variables (optionally a .env file)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    v = os.environ.get(name)
    return int(v) if v not in (None, "") else default


def _float(name: str, default: float) -> float:
    v = os.environ.get(name)
    return float(v) if v not in (None, "") else default


@dataclass
class Settings:
    # Where real traffic goes, e.g. https://my-res.openai.azure.com
    # or https://my-res.services.ai.azure.com
    upstream_url: str = ""
    db_path: str = "sentinel.db"
    store_bodies: bool = True
    max_stored_body_chars: int = 200_000
    app_header: str = "x-sentinel-app"
    admin_token: str | None = None
    # If set, Sentinel injects this key into every upstream call, so client
    # services never need the real Foundry key.
    upstream_api_key: str | None = None
    # Optional keys clients must present (api-key header or Bearer) to use the proxy.
    client_keys: list[str] = field(default_factory=list)
    upstream_timeout_s: float = 600.0

    # --- rule thresholds ---
    default_context_window: int = 128_000
    context_windows: dict[str, int] = field(default_factory=dict)
    context_warn_ratio: float = 0.60
    context_crit_ratio: float = 0.85
    system_prompt_warn_tokens: int = 3_000
    system_prompt_crit_tokens: int = 8_000
    tool_output_warn_tokens: int = 4_000
    tool_share_warn_ratio: float = 0.50
    history_turns_warn: int = 30
    history_share_warn_ratio: float = 0.60
    max_tools_warn: int = 20
    tool_schema_warn_tokens: int = 4_000
    duplicate_min_chars: int = 200
    latency_warn_ms: int = 30_000
    cache_min_prompt_tokens: int = 1_024

    # --- LLM judge ---
    judge_enabled: bool = False
    judge_endpoint: str | None = None  # full chat-completions URL incl. api-version
    judge_api_key: str | None = None
    judge_bearer_token: str | None = None
    judge_model: str | None = None  # only needed for /openai/v1 or /models endpoints
    judge_mode: str = "on_findings"  # on_findings | sample | always
    judge_min_severity: str = "warning"
    judge_sample_rate: float = 0.10
    judge_max_input_chars: int = 60_000
    judge_timeout_s: float = 60.0

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(os.environ.get("SENTINEL_ENV_FILE", ".env"))
        cw_raw = _env("SENTINEL_CONTEXT_WINDOWS")
        return cls(
            upstream_url=(_env("SENTINEL_UPSTREAM_URL", "") or "").rstrip("/"),
            db_path=_env("SENTINEL_DB_PATH", "sentinel.db") or "sentinel.db",
            store_bodies=_bool("SENTINEL_STORE_BODIES", True),
            max_stored_body_chars=_int("SENTINEL_MAX_STORED_BODY_CHARS", 200_000),
            app_header=(_env("SENTINEL_APP_HEADER", "x-sentinel-app") or "").lower(),
            admin_token=_env("SENTINEL_ADMIN_TOKEN") or None,
            upstream_api_key=_env("SENTINEL_UPSTREAM_API_KEY") or None,
            client_keys=[k.strip() for k in (_env("SENTINEL_CLIENT_KEYS", "") or "").split(",") if k.strip()],
            upstream_timeout_s=_float("SENTINEL_UPSTREAM_TIMEOUT_S", 600.0),
            default_context_window=_int("SENTINEL_DEFAULT_CONTEXT_WINDOW", 128_000),
            context_windows={k.lower(): int(v) for k, v in json.loads(cw_raw).items()} if cw_raw else {},
            context_warn_ratio=_float("SENTINEL_CONTEXT_WARN_RATIO", 0.60),
            context_crit_ratio=_float("SENTINEL_CONTEXT_CRIT_RATIO", 0.85),
            system_prompt_warn_tokens=_int("SENTINEL_SYSTEM_PROMPT_WARN_TOKENS", 3_000),
            system_prompt_crit_tokens=_int("SENTINEL_SYSTEM_PROMPT_CRIT_TOKENS", 8_000),
            tool_output_warn_tokens=_int("SENTINEL_TOOL_OUTPUT_WARN_TOKENS", 4_000),
            tool_share_warn_ratio=_float("SENTINEL_TOOL_SHARE_WARN_RATIO", 0.50),
            history_turns_warn=_int("SENTINEL_HISTORY_TURNS_WARN", 30),
            history_share_warn_ratio=_float("SENTINEL_HISTORY_SHARE_WARN_RATIO", 0.60),
            max_tools_warn=_int("SENTINEL_MAX_TOOLS_WARN", 20),
            tool_schema_warn_tokens=_int("SENTINEL_TOOL_SCHEMA_WARN_TOKENS", 4_000),
            duplicate_min_chars=_int("SENTINEL_DUPLICATE_MIN_CHARS", 200),
            latency_warn_ms=_int("SENTINEL_LATENCY_WARN_MS", 30_000),
            cache_min_prompt_tokens=_int("SENTINEL_CACHE_MIN_PROMPT_TOKENS", 1_024),
            judge_enabled=_bool("SENTINEL_JUDGE_ENABLED", False),
            judge_endpoint=_env("SENTINEL_JUDGE_ENDPOINT") or None,
            judge_api_key=_env("SENTINEL_JUDGE_API_KEY") or None,
            judge_bearer_token=_env("SENTINEL_JUDGE_BEARER_TOKEN") or None,
            judge_model=_env("SENTINEL_JUDGE_MODEL") or None,
            judge_mode=_env("SENTINEL_JUDGE_MODE", "on_findings") or "on_findings",
            judge_min_severity=_env("SENTINEL_JUDGE_MIN_SEVERITY", "warning") or "warning",
            judge_sample_rate=_float("SENTINEL_JUDGE_SAMPLE_RATE", 0.10),
            judge_max_input_chars=_int("SENTINEL_JUDGE_MAX_INPUT_CHARS", 60_000),
            judge_timeout_s=_float("SENTINEL_JUDGE_TIMEOUT_S", 60.0),
        )
