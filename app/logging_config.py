"""Structured logging.

We use structlog so every log line is a set of key/value pairs (conversation_id,
channel, latency_ms...). In prod set LOG_JSON=true and ship stdout to your log stack.
"""

from __future__ import annotations

import logging
import sys

import structlog


def redact_pii(_logger, _method, event_dict: dict) -> dict:
    """Mask PII in every string field before it is written anywhere (Part 3).

    Logs are copied to many places (log stack, error trackers, support tickets) and
    usually kept longer than conversations, so they must never contain raw PII.
    """
    from app.privacy.pii import mask  # local import keeps logging free of import cycles

    for k, v in event_dict.items():
        if isinstance(v, str) and k not in {"timestamp", "level"}:
            event_dict[k] = mask(v)
        elif isinstance(v, dict):
            event_dict[k] = {ik: mask(iv) if isinstance(iv, str) else iv for ik, iv in v.items()}
    return event_dict


def configure_logging(level: str = "INFO", json: bool = False) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    processors: list = [
        structlog.contextvars.merge_contextvars,  # picks up bound request/conversation ids
        redact_pii,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    processors.append(structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer())
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )


log = structlog.get_logger("shopassist")
