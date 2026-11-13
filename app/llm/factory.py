"""Build the configured LLM client from settings."""

from __future__ import annotations

from app.llm.base import LLMClient
from app.settings import Settings


def build_llm(s: Settings) -> LLMClient:
    key = s.llm_api_key.get_secret_value() if s.llm_api_key else None
    if s.llm_provider == "openai":
        from app.llm.openai_compat import OpenAICompatLLM

        return OpenAICompatLLM(s.llm_model, key, s.llm_base_url, s.llm_timeout_s, s.llm_temperature, s.llm_max_tokens)
    if s.llm_provider == "anthropic":
        from app.llm.anthropic_llm import AnthropicLLM

        return AnthropicLLM(s.llm_model, key, s.llm_timeout_s, s.llm_temperature, s.llm_max_tokens)
    from app.llm.fake import FakeLLM

    return FakeLLM()
