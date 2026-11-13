"""Build the configured LLM client(s) from settings.

Part 4: the primary provider plus optional fallbacks are wrapped in `ResilientLLM`
(retries with backoff, per-provider circuit breakers, automatic failover).
"""

from __future__ import annotations

import json
import os

from app.llm.base import LLMClient
from app.settings import Settings


def _client(provider: str, model: str, api_key: str | None, base_url: str | None, s: Settings, name: str) -> LLMClient:
    if provider == "openai":
        from app.llm.openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(model, api_key, base_url, s.llm_timeout_s, s.llm_temperature, s.llm_max_tokens, name=name)
    if provider == "anthropic":
        from app.llm.anthropic_llm import AnthropicLLM

        return AnthropicLLM(model, api_key, s.llm_timeout_s, s.llm_temperature, s.llm_max_tokens, name=name)
    from app.llm.fake import FakeLLM

    return FakeLLM()


def build_llm(s: Settings) -> LLMClient:
    key = s.llm_api_key.get_secret_value() if s.llm_api_key else None
    primary = _client(s.llm_provider, s.llm_model, key, s.llm_base_url, s, s.llm_provider)
    fallbacks = [
        _client(f["provider"], f["model"], os.environ.get(f.get("api_key_env", "")) or None, f.get("base_url"), s,
                f.get("name") or f"{f['provider']}:{f['model']}")
        for f in json.loads(s.llm_fallbacks or "[]")
    ]
    if s.llm_provider == "fake" and not fallbacks:
        return primary
    from app.reliability.resilience import ResilientLLM

    return ResilientLLM([primary, *fallbacks], breaker_threshold=s.llm_breaker_threshold,
                        breaker_cooldown_s=s.llm_breaker_cooldown_s, timeout_s=s.llm_timeout_s + 15)
