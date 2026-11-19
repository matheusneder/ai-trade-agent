"""Keeps the lead traders a profile follows, and their trades, up to date (D-052).

One periodic task per ``lead_consensus`` profile (:meth:`LeadTracker.refresh`, every
``leads.poll_interval``):

1. when the ranking is due (``ranking_interval``), reads its first pages, keeps the
   portfolios whose figures pass, reads the PnL curve and the trades of each of those and
   follows the ``max_leads`` best scores among the ones that pass on both
   (:mod:`trade_agent.leads.ranking`). Who is followed, each one's score and how many
   portfolios each criterion refused go to a ``leads.selected`` event, and a change of the
   followed traders to Telegram;
2. reads the most recent trades of each followed trader.

The decision cycle only reads memory (:meth:`LeadTracker.view`): it never waits for the
source. The source is undocumented and may fail or change at any time, so every failure is
contained here and ends in fewer entries, never in a wrong one:

* a failed ranking keeps the traders already followed, up to ``max_ranking_age``; a
  candidate that cannot be read is left out of that ranking, and one where no candidate
  could be read is a failed one;
* a trader whose trades could not be read stops counting after ``max_data_age``;
* a refusal (HTTP 403, 418, 429 or 451) stops every request for ``backoff``;
* a reading with nothing but failures is a ``leads.source_unavailable`` event and alert,
  once per outage.

The followed traders survive a restart in the ``leads.<profile>`` checkpoint; their trades
are read again at startup.
"""

import asyncio
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog

from trade_agent import metrics, tracing
from trade_agent.leads.consensus import (
    BUY,
    NO_DATA,
    NO_LEADS,
    STALE_RANKING,
    LeadReading,
    LeadView,
    backings,
)
from trade_agent.leads.ranking import Assessment, assess, best, candidates
from trade_agent.leads.source import (
    PAGE_SIZE,
    LeadSource,
    LeadSourceError,
    LeadSourceRefusedError,
    LeadStats,
    LeadTrade,
)
from trade_agent.persistence.store import Severity, Store
from trade_agent.strategy.profiles import LeadsConfig

log = structlog.get_logger(__name__)

CHECKPOINT = "leads.{profile}"
SELECTED_EVENT = "leads.selected"
UNAVAILABLE_EVENT = "leads.source_unavailable"
RECOVERED_EVENT = "leads.source_recovered"
UNREADABLE = "leitura falhou"
"""Why a candidate was left out of a ranking: its PnL curve or its trades could not be read."""


@dataclass(frozen=True, slots=True)
class Followed:
    """A lead trader the profile follows, with the figures it was chosen on."""

    portfolio_id: str
    nickname: str
    balance: Decimal
    score: float
    figures: Mapping[str, Any]

    @classmethod
    def chosen(cls, assessment: Assessment) -> "Followed":
        lead = assessment.lead
        return cls(
            portfolio_id=lead.portfolio_id,
            nickname=lead.nickname,
            balance=lead.balance,
            score=assessment.score,
            figures={
                "roi_pct": lead.roi_pct,
                "mdd_pct": lead.mdd_pct,
                "sharpe": lead.sharpe,
                "positive_weeks": assessment.consistency,
                "recent_gain_pct": assessment.recent_gain_pct,
                "trading_days": lead.trading_days,
                "copier_pnl": f"{lead.copier_pnl:f}",
            },
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.portfolio_id,
            "nickname": self.nickname,
            "balance": f"{self.balance:f}",
            "score": self.score,
            **self.figures,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Followed":
        figures = {k: v for k, v in data.items() if k not in {"id", "nickname", "balance", "score"}}
        return cls(
            portfolio_id=str(data["id"]),
            nickname=str(data["nickname"]),
            balance=Decimal(str(data["balance"])),
            score=float(data["score"]),
            figures=figures,
        )


@dataclass(slots=True)
class _Outcome:
    """The requests of one reading."""

    ok: int = 0
    failed: int = 0


class LeadTracker:
    def __init__(
        self,
        *,
        profile: str,
        config: LeadsConfig,
        source: LeadSource,
        store: Store,
        quote_asset: str,
        notify: Callable[[Severity, str], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._profile = profile
        self._config = config
        self._source = source
        self._store = store
        self._quote_asset = quote_asset
        self._notify = notify
        self._clock = clock
        self._sleep = sleep
        self._followed: dict[str, Followed] = {}
        self._ranked_at: datetime | None = None
        self._readings: dict[str, tuple[datetime, tuple[LeadTrade, ...]]] = {}
        self._blocked_until: datetime | None = None
        self._loaded = False
        self._down = False

    @property
    def interval_s(self) -> float:
        return self._config.poll_interval.total_seconds()

    # ------------------------------------------------------------------ what the cycle reads
    def view(self, now: datetime | None = None) -> LeadView:
        """The followed traders' consensus from the readings that still count, in memory."""
        now = now or self._clock()
        config = self._config
        if self._ranked_at is None or now - self._ranked_at > config.max_ranking_age:
            return LeadView(STALE_RANKING)
        if not self._followed:
            return LeadView(NO_LEADS)
        fresh = {
            lead: self._readings[lead]
            for lead in self._followed
            if lead in self._readings and now - self._readings[lead][0] <= config.max_data_age
        }
        if not fresh:
            return LeadView(NO_DATA, selected=len(self._followed))
        readings = {
            lead: LeadReading(self._followed[lead].balance, trades)
            for lead, (_, trades) in fresh.items()
        }
        return LeadView(
            selected=len(self._followed),
            fresh=len(fresh),
            as_of=min(at for at, _ in fresh.values()),
            backings=backings(
                readings, now=now, config=config.consensus, quote_asset=self._quote_asset
            ),
        )

    # ------------------------------------------------------------------ the periodic reading
    @tracing.traced("leads", "leads.refresh")
    async def refresh(self) -> None:
        """One reading: the ranking when it is due, then the followed traders' trades."""
        now = self._clock()
        if self._blocked_until is not None and now < self._blocked_until:
            log.debug("leads.backing_off", profile=self._profile, until=self._blocked_until)
            return
        if not self._loaded:
            await self._load()
        outcome = _Outcome()
        try:
            read: set[str] = set()
            if self._ranked_at is None or now - self._ranked_at >= self._config.ranking_interval:
                read = await self._rank(now, outcome)
            await self._read(read, outcome)
        except LeadSourceRefusedError as exc:
            outcome.failed += 1
            self._blocked_until = now + self._config.backoff
            log.warning(
                "leads.source_refused",
                profile=self._profile,
                error=str(exc),
                until=self._blocked_until.isoformat(),
            )
        await self._report(now, outcome)

    async def _request[T](self, request: Awaitable[T], outcome: _Outcome) -> T:
        """One request, counted, with the wait that spaces it from the next one."""
        result = await request
        outcome.ok += 1
        await self._sleep(self._config.request_delay_s)
        return result

    async def _load(self) -> None:
        """The followed traders of before the restart, when the checkpoint is still readable."""
        self._loaded = True
        saved = await self._store.get_checkpoint(CHECKPOINT.format(profile=self._profile))
        if saved is None:
            return
        try:
            followed = [Followed.from_json(lead) for lead in saved["leads"]]
            ranked_at = datetime.fromisoformat(saved["ranked_at"])
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            log.warning("leads.checkpoint_unreadable", profile=self._profile, error=repr(exc))
            return
        self._followed = {lead.portfolio_id: lead for lead in followed}
        self._ranked_at = ranked_at

    async def _rank(self, now: datetime, outcome: _Outcome) -> set[str]:
        """Chooses the followed traders again; returns the ones whose trades it just read.
        A failure keeps the previous ones, and the ranking is tried again at the next reading."""
        selection = self._config.selection
        assessed: list[Assessment] = []
        trades: dict[str, tuple[LeadTrade, ...]] = {}
        try:
            pool: list[LeadStats] = []
            for page in range(1, selection.pool_pages + 1):
                rows = await self._request(self._source.ranking(page), outcome)
                pool += rows
                if len(rows) < PAGE_SIZE:
                    break
            examined, rejected = candidates(pool, selection)
            for lead in examined:
                try:
                    curve = await self._request(self._source.pnl_curve(lead.portfolio_id), outcome)
                    history = await self._request(self._source.trades(lead.portfolio_id), outcome)
                except LeadSourceRefusedError:
                    raise
                except LeadSourceError as exc:  # e.g. a portfolio closed since the ranking
                    outcome.failed += 1
                    rejected[UNREADABLE] += 1
                    log.warning(
                        "leads.candidate_failed",
                        profile=self._profile,
                        lead=lead.portfolio_id,
                        error=str(exc),
                    )
                    continue
                assessment = assess(lead, curve, history, now, selection)
                rejected.update(assessment.reasons)
                assessed.append(assessment)
                trades[lead.portfolio_id] = tuple(history)
            if examined and not assessed:  # an outage is no reason to stop following anybody
                raise LeadSourceError("nenhum candidato pôde ser lido")
        except LeadSourceRefusedError:
            raise
        except LeadSourceError as exc:
            outcome.failed += 1
            log.warning("leads.ranking_failed", profile=self._profile, error=str(exc))
            return set()
        previous = self._followed
        chosen = {a.lead.portfolio_id: Followed.chosen(a) for a in best(assessed, selection)}
        self._followed, self._ranked_at = chosen, now
        read_at = self._clock()
        self._readings = {lead: (read_at, trades[lead]) for lead in chosen}
        await self._selected(
            previous, now, pool=len(pool), examined=len(examined), rejected=rejected
        )
        return set(chosen)

    async def _selected(
        self,
        previous: Mapping[str, Followed],
        ranked_at: datetime,
        *,
        pool: int,
        examined: int,
        rejected: Counter[str],
    ) -> None:
        """Persists and records the followed traders, and announces a change of them."""
        followed = [lead.to_json() for lead in self._followed.values()]
        await self._store.set_checkpoint(
            CHECKPOINT.format(profile=self._profile),
            {"ranked_at": ranked_at.isoformat(), "leads": followed},
        )
        payload = {
            "profile": self._profile,
            "pool": pool,
            "examined": examined,
            "selected": followed,
            "rejected": dict(rejected.most_common()),
        }
        await self._store.record_event(SELECTED_EVENT, Severity.INFO, payload)
        log.info(SELECTED_EVENT, **payload)
        joined, left = self._followed.keys() - previous.keys(), previous.keys() - self._followed
        if (joined or left) and self._notify is not None:
            names = [f"{lead.nickname} ({lead.score:.2f})" for lead in self._followed.values()]
            gone = [previous[lead].nickname for lead in sorted(left)]
            text = f"{self._profile}: traders acompanhados: {', '.join(names) or 'nenhum'}"
            if gone:
                text += f"; saíram: {', '.join(gone)}"
            await self._notify(Severity.INFO, text)

    async def _read(self, skip: set[str], outcome: _Outcome) -> None:
        """The most recent trades of each followed trader not in ``skip``."""
        for lead in self._followed:
            if lead in skip:
                continue
            try:
                history = await self._request(self._source.trades(lead), outcome)
            except LeadSourceRefusedError:
                raise
            except LeadSourceError as exc:
                outcome.failed += 1
                log.warning("leads.trades_failed", profile=self._profile, lead=lead, error=str(exc))
                continue
            self._readings[lead] = (self._clock(), tuple(history))

    async def _report(self, now: datetime, outcome: _Outcome) -> None:
        """Metrics and log of the reading, and the source going down or coming back."""
        view = self.view(now)
        config = self._config.consensus
        consensus = sorted(s for s, b in view.backings.items() if b.consensus(config.min_backers))
        bought = [s for s in consensus if view.backings[s].consensus(config.min_backers) == BUY]
        metrics.observe_leads(
            profile=self._profile,
            selected=len(self._followed),
            fresh=view.fresh,
            consensus=len(bought),
            errors=outcome.failed,
        )
        tracing.annotate(
            profile=self._profile,
            status=view.status,
            selected=len(self._followed),
            fresh=view.fresh,
            consensus=consensus,
            requests=outcome.ok,
            failed=outcome.failed,
        )
        log.debug(
            "leads.refreshed",
            profile=self._profile,
            status=view.status,
            selected=len(self._followed),
            fresh=view.fresh,
            consensus=consensus,
            requests=outcome.ok,
            failed=outcome.failed,
        )
        down = outcome.ok == 0 and outcome.failed > 0
        if down == self._down or (outcome.ok == 0 and not down):
            return  # no change, or a reading that asked nothing
        self._down = down
        kind, severity = (
            (UNAVAILABLE_EVENT, Severity.HIGH) if down else (RECOVERED_EVENT, Severity.INFO)
        )
        await self._store.record_event(kind, severity, {"profile": self._profile})
        if self._notify is not None:
            text = (
                "fonte de copy trading indisponível: sem novas entradas até voltar"
                if down
                else "fonte de copy trading voltou a responder"
            )
            await self._notify(severity, f"{self._profile}: {text}")
