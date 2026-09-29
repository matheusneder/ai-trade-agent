"""Configuração de logs estruturados (structlog).

Níveis: ``INFO`` (padrão) traz o ciclo de vida e as decisões; ``DEBUG``
(``TA_LOG_LEVEL=DEBUG``) acrescenta o detalhe de cada etapa: requisições à Binance (sem
*query string*), sincronizações de posição, leituras de risco, sinais por ativo, chamadas
ao LLM (tokens e custo, nunca o conteúdo), coleta de notícias, Telegram e duração das
tarefas de fundo.

Segredos nunca devem chegar aos logs; ``redact_secrets`` é a última barreira: mascara
campos com nomes sensíveis e padrões conhecidos de token em qualquer texto.
"""

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

from trade_agent.config.settings import LogFormat, LogLevel

REDACTED = "***"
# nome exato do campo (ou sufixo após "_"): "bot_token" é mascarado, "input_tokens" não
SENSITIVE_KEYS = re.compile(
    r"(^|_)(api_?key|secret|signature|token|password|passphrase|authorization|private_key)$",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    re.compile(r"(bot)\d+:[A-Za-z0-9_-]{20,}"),  # token de bot do Telegram na URL da API
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),  # chave da API Anthropic
    re.compile(r"(signature=)[^&\s]+"),  # assinatura de requisição da Binance
)
# Bibliotecas que registram URLs completas (podem conter tokens): nunca abaixo de WARNING.
QUIET_LIBRARIES = ("httpx", "httpcore", "httpx2", "httpcore2", "websockets", "anthropic")


def _scrub(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + REDACTED, text)
    return text


def redact_secrets(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Mascara campos sensíveis pelo nome e padrões de segredo em valores de texto."""
    for key, value in list(event_dict.items()):
        if SENSITIVE_KEYS.search(key):
            event_dict[key] = REDACTED
        elif isinstance(value, str):
            event_dict[key] = _scrub(value)
    return event_dict


def configure_logging(level: LogLevel = "INFO", fmt: LogFormat = LogFormat.CONSOLE) -> None:
    """Configura o structlog para saída em console (dev) ou JSON (produção)."""
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
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
