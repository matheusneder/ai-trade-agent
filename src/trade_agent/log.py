"""Structured logging configuration (structlog).

Levels: ``INFO`` (default) carries the lifecycle and the decisions; ``DEBUG``
(``TA_LOG_LEVEL=DEBUG``) adds the detail of each step: Binance requests (without the
*query string*), position syncs, risk readings, per-asset signals, LLM calls (tokens and
cost, never the content), news collection, Telegram and the duration of background tasks.

Secrets must never reach the logs; ``redact_secrets`` is the last barrier: it masks fields
with sensitive names and known token patterns in any text.

Inside a span (OpenTelemetry), every log carries ``trace_id`` and ``span_id``: in Grafana,
the log opens the trace in Jaeger and the trace lists its logs.
"""

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

from trade_agent.config.settings import LogFormat, LogLevel
from trade_agent.tracing import add_trace_ids

REDACTED = "***"
# exact field name (or suffix after "_"): "bot_token" is masked, "input_tokens" is not
SENSITIVE_KEYS = re.compile(
    r"(^|_)(api_?key|secret|signature|token|password|passphrase|authorization|private_key)$",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    re.compile(r"(bot)\d+:[A-Za-z0-9_-]{20,}"),  # Telegram bot token in the API URL
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),  # Anthropic API key
    re.compile(r"(signature=)[^&\s]+"),  # Binance request signature
)
# Libraries that log full URLs (which may contain tokens): never below WARNING.
QUIET_LIBRARIES = ("httpx", "httpcore", "httpx2", "httpcore2", "websockets", "anthropic")
# The trace exporter warns on every retry while Jaeger is down: only final errors.
TRACE_EXPORTER_LOGGER = "opentelemetry"


def _scrub(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, text)
    return text


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Masks sensitive fields by name and secret patterns in text values."""
    for key, value in list(event_dict.items()):
        if SENSITIVE_KEYS.search(key):
            event_dict[key] = REDACTED
        elif isinstance(value, str):
            event_dict[key] = _scrub(value)
    return event_dict


def configure_logging(level: LogLevel = "INFO", fmt: LogFormat = LogFormat.CONSOLE) -> None:
    """Configures structlog for console output (dev) or JSON (production)."""
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        add_trace_ids,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        redact_secrets,
    ]
    if fmt is LogFormat.JSON:
        processors += [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        cache_logger_on_first_use=False,
    )
    for name in QUIET_LIBRARIES:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger(TRACE_EXPORTER_LOGGER).setLevel(logging.ERROR)
