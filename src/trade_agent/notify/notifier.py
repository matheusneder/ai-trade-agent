"""Sends alerts to the operator. Sending failures never interrupt the agent."""

from typing import Protocol

import structlog

from trade_agent import tracing
from trade_agent.notify.telegram import TelegramBot, TelegramError
from trade_agent.persistence.store import Severity

log = structlog.get_logger(__name__)

_LABEL = {Severity.INFO: "ℹ️", Severity.HIGH: "🔴", Severity.CRITICAL: "🚨"}


class Notifier(Protocol):
    async def notify(self, severity: Severity, text: str) -> None: ...


class LogNotifier:
    """No Telegram configured: the alert only goes to the log."""

    async def notify(self, severity: Severity, text: str) -> None:
        log.warning("alert", severity=severity.value, text=text)


class TelegramNotifier:
    def __init__(self, bot: TelegramBot, chat_id: int, *, min_severity: Severity) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._min_rank = list(Severity).index(min_severity)

    async def notify(self, severity: Severity, text: str) -> None:
        if list(Severity).index(severity) < self._min_rank:
            return
        with tracing.span("telegram", "alert.send") as span:
            tracing.annotate(severity=severity, chars=len(text))
            try:
                await self._bot.send_message(self._chat_id, f"{_LABEL[severity]} {text}")
            except TelegramError as exc:
                tracing.fail(span, exc)
                log.error("alert.telegram_failed", error=str(exc), text=text)
            else:
                log.debug("alert.sent", severity=severity.value, chars=len(text))
