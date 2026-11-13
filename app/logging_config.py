"""Structured logging.

We use structlog so every log line is a set of key/value pairs (conversation_id,
channel, latency_ms...). In prod set LOG_JSON=true and ship stdout to your log stack.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", json: bool = False) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    processors: list = [
        structlog.contextvars.merge_contextvars,  # picks up bound request/conversation ids
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
