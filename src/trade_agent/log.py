"""Configuração de logs estruturados (structlog)."""

import logging

import structlog

from trade_agent.config.settings import LogFormat, LogLevel


def configure_logging(level: LogLevel = "INFO", fmt: LogFormat = LogFormat.CONSOLE) -> None:
    """Configura o structlog para saída em console (dev) ou JSON (produção)."""
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
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
