"""Comandos do operador pelo Telegram, com autorização por ``chat_id``.

Consultas (``/status``, ``/positions``, ``/pnl``, ``/report``, ``/config``) são injetadas
pela aplicação. Controle: ``/pause [escopo]`` · ``/resume [escopo]`` · ``/halt [escopo]`` ·
``/flatten [escopo]`` (pede confirmação com código, válido por 2 min) · ``/help``.
O escopo é ``global`` (padrão) ou o nome de um perfil. Mensagens de outros chats são
ignoradas e registradas como evento.
"""

import asyncio
import contextlib
import secrets
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog

from trade_agent.notify.telegram import TelegramBot, TelegramError, Update
from trade_agent.persistence.store import Severity, Store
from trade_agent.risk.guard import RiskGuard
from trade_agent.risk.state import GLOBAL

log = structlog.get_logger(__name__)

OFFSET_KEY = "telegram.offset"
CONFIRMATION_TTL = timedelta(minutes=2)
CONTROL = "/pause [escopo] · /resume [escopo] · /halt [escopo] · /flatten [escopo]"

type Query = Callable[[list[str]], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class _Pending:
    scope: str
    code: str
    expires: datetime


class CommandCenter:
    def __init__(
        self,
        *,
        bot: TelegramBot,
        chat_id: int,
        guard: RiskGuard,
        store: Store,
        scopes: Collection[str],
        queries: Mapping[str, Query],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_code: Callable[[], str] = lambda: secrets.token_hex(3).upper(),
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._guard = guard
        self._store = store
        self._scopes = {GLOBAL, *scopes}
        self._queries = dict(queries)
        self._clock = clock
        self._new_code = new_code
        self._pending: _Pending | None = None

    @property
    def help(self) -> str:
        queries = " · ".join(sorted(self._queries))
        return (
            f"Consultas: {queries}. Controle: {CONTROL}. "
            "Escopo: global (padrão) ou o nome de um perfil."
        )

    def _scope(self, args: list[str]) -> str | None:
        scope = args[0].lower() if args else GLOBAL
        return scope if scope in self._scopes else None

    async def handle(self, text: str) -> str:
        parts = text.strip().split()
        if not parts or not parts[0].startswith("/"):
            return self.help
        command, args = parts[0].split("@")[0].lower(), parts[1:]
        if command in self._queries:
            return await self._queries[command](args)
        if command not in {"/pause", "/resume", "/halt", "/flatten"}:
            return self.help
        scope = self._scope(args)
        if scope is None:
            return f"Escopo inválido. Use: {', '.join(sorted(self._scopes))}."
        if command == "/pause":
            await self._guard.pause(scope, "comando /pause")
            return f"{scope}: pausado (sem novas entradas; proteções mantidas)."
        if command == "/halt":
            await self._guard.halt(scope, "comando /halt")
            return f"{scope}: parado (proteções mantidas na Binance). Retome com /resume."
        if command == "/resume":
            if await self._guard.resume(scope):
                return f"{scope}: retomado."
            return f"{scope}: flatten em andamento; aguarde o término."
        return await self._flatten(scope, args[1:])

    async def _flatten(self, scope: str, args: list[str]) -> str:
        now = self._clock()
        pending = self._pending
        if args and pending and pending.scope == scope and pending.expires > now:
            if args[0].upper() != pending.code:
                return "Código incorreto. Envie /flatten novamente para gerar outro."
            self._pending = None
            closed = await self._guard.flatten(scope, "comando /flatten")
            return f"{scope}: flatten concluído ({closed} posições). Estado: halted."
        code = self._new_code()
        self._pending = _Pending(scope, code, now + CONFIRMATION_TTL)
        return (
            f"⚠️ Isto cancela as proteções e VENDE a mercado as posições de '{scope}'. "
            f"Confirme em 2 min com: /flatten {scope} {code}"
        )

    async def process(self, update: Update) -> None:
        command = update.text.split(maxsplit=1)[0] if update.text.strip() else ""
        log.debug(
            "telegram.update",
            update_id=update.update_id,
            authorized=update.chat_id == self._chat_id,
            command=command,
        )
        if update.chat_id != self._chat_id:
            await self._store.record_event(
                "telegram.unauthorized", Severity.HIGH, {"chat_id": update.chat_id}
            )
            return
        reply = await self.handle(update.text)
        await self._store.record_event("telegram.command", Severity.INFO, {"text": update.text})
        await self._bot.send_message(self._chat_id, reply)

    async def poll_once(self, *, timeout_s: int = 30) -> int:
        """Busca e processa um lote de mensagens; o offset fica persistido."""
        saved = await self._store.get_checkpoint(OFFSET_KEY)
        offset = int(saved["offset"]) if saved else None
        updates = await self._bot.get_updates(offset, timeout_s=timeout_s)
        log.debug("telegram.poll", offset=offset, updates=len(updates))
        for update in updates:
            await self._store.set_checkpoint(OFFSET_KEY, {"offset": update.update_id + 1})
            await self.process(update)
        return len(updates)

    async def run(
        self, stop: asyncio.Event, *, backoff_s: float = 5.0, timeout_s: int = 30
    ) -> None:
        while not stop.is_set():
            try:
                await self.poll_once(timeout_s=timeout_s)
            except TelegramError as exc:
                log.warning("telegram.poll_failed", error=str(exc))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff_s)
