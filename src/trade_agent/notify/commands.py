"""Operator commands over Telegram, authorized by ``chat_id``.

Queries (``/status``, ``/positions``, ``/pnl``, ``/report``, ``/config``) are injected by
the application. Control: ``/pause [scope]`` · ``/resume [scope]`` · ``/halt [scope]`` ·
``/flatten [scope]`` (asks for confirmation with a code, valid for 2 min) · ``/help``.
The scope is ``global`` (default) or a profile name. Messages from other chats are ignored
and recorded as an event.
"""

import asyncio
import contextlib
import secrets
from collections.abc import Awaitable, Callable, Collection, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from trade_agent import tracing
from trade_agent.notify.telegram import TelegramBot, TelegramError, Update
from trade_agent.persistence.store import Severity, Store
from trade_agent.risk.guard import RiskGuard
from trade_agent.risk.state import GLOBAL

log = structlog.get_logger(__name__)

OFFSET_KEY = "telegram.offset"
CONFIRMATION_TTL = timedelta(minutes=2)
CONTROL = "/pause [scope] · /resume [scope] · /halt [scope] · /flatten [scope]"

type Query = Callable[[list[str]], Awaitable[str]]


async def until_stopped[T](call: Coroutine[Any, Any, T], stop: asyncio.Event) -> T | None:
    """The result of ``call``, or ``None`` if ``stop`` comes first (``call`` is canceled)."""
    task = asyncio.ensure_future(call)
    waiter = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait((task, waiter), return_when=asyncio.FIRST_COMPLETED)
    finally:
        waiter.cancel()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return None if task.cancelled() else task.result()


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
        return f"Queries: {queries}. Control: {CONTROL}. Scope: global (default) or a profile name."

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
            return f"Invalid scope. Use: {', '.join(sorted(self._scopes))}."
        if command == "/pause":
            await self._guard.pause(scope, "/pause command")
            return f"{scope}: paused (no new entries; protections kept)."
        if command == "/halt":
            await self._guard.halt(scope, "/halt command")
            return f"{scope}: halted (protections kept on Binance). Resume with /resume."
        if command == "/resume":
            if not await self._guard.resume(scope):
                return f"{scope}: flatten in progress; wait for it to finish."
            active = "; ".join(record.reason for record in await self._guard.fired(scope))
            if not active:
                return f"{scope}: resumed."
            return (
                f"{scope}: resumed. Still holding, they fire again only if they worsen by another "
                f"full limit: {active}."
            )
        return await self._flatten(scope, args[1:])

    async def _flatten(self, scope: str, args: list[str]) -> str:
        now = self._clock()
        pending = self._pending
        if args and pending and pending.scope == scope and pending.expires > now:
            if args[0].upper() != pending.code:
                return "Wrong code. Send /flatten again to get a new one."
            self._pending = None
            closed = await self._guard.flatten(scope, "/flatten command")
            return f"{scope}: flatten done, positions closed: {closed}. State: halted."
        code = self._new_code()
        self._pending = _Pending(scope, code, now + CONFIRMATION_TTL)
        return (
            f"⚠️ This cancels the protections and SELLS the positions of '{scope}' at market. "
            f"Confirm within 2 min with: /flatten {scope} {code}"
        )

    @tracing.traced("telegram", "telegram.command")
    async def process(self, update: Update) -> None:
        command = update.text.split(maxsplit=1)[0] if update.text.strip() else ""
        # only the command word: the text may carry the /flatten confirmation code
        tracing.annotate(command=command, authorized=update.chat_id == self._chat_id)
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

    async def poll_once(self, *, timeout_s: int = 30, stop: asyncio.Event | None = None) -> int:
        """Fetches and processes a batch of messages; the offset is persisted.

        With ``stop``, the long wait (up to ``timeout_s``) ends as soon as the agent stops:
        Docker kills the process after a few seconds, and the agent would exit without an
        orderly shutdown. Messages already received are processed to the end.
        """
        saved = await self._store.get_checkpoint(OFFSET_KEY)
        offset = int(saved["offset"]) if saved else None
        fetch = self._bot.get_updates(offset, timeout_s=timeout_s)
        updates = await (until_stopped(fetch, stop) if stop is not None else fetch)
        if updates is None:
            return 0
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
                await self.poll_once(timeout_s=timeout_s, stop=stop)
            except TelegramError as exc:
                log.warning("telegram.poll_failed", error=str(exc))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff_s)
