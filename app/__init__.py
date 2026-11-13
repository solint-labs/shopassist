"""ShopAssist: an LLM virtual agent for e-commerce customer support.

The package is organised by responsibility:

    app/channels   – Meta webhooks in, Graph API messages out (WhatsApp, Messenger, Instagram)
    app/llm        – provider-agnostic LLM clients (OpenAI-compatible, Anthropic, fake for tests)
    app/agent      – persona, prompt building, conversation memory and the turn orchestrator
    app/storage    – database engine and ORM models

Each part of the blog series adds packages next to these rather than rewriting them.
"""

__version__ = "0.2.0"
