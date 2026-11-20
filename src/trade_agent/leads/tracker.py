"""Keeps the lead traders a profile follows, and their trades, up to date (D-052).

One periodic task per ``lead_consensus`` profile (:meth:`LeadTracker.refresh`, every
``leads.poll_interval``):

1. when the ranking is due (``ranking_interval``), reads its first pages, keeps the
   portfolios whose figures pass, reads the PnL curve and the trades of each of those and
   follows the ``max_leads`` best scores among the ones that pass on both
   (:mod:`trade_agent.leads.ranking`). Ranked by activity (D-054), it reads them in the
   ranking's order until ``shortlist`` are eligible, the trades first and the curve only of
   an active one, and follows the ones that trade most often. Who is followed, each one's
   score and how many portfolios each criterion refused go to a ``leads.selected`` event,
   and a change of the followed traders to Telegram;
2. reads the most recent trades of each followed trader.

The decision cycle only reads memory (:meth:`LeadTracker.view`): it never waits for the
source. The source is undocumented and may fail or change at any time, so every failure is
contained here and ends in fewer entries, never in a wrong one:

* a failed ranking keeps the traders already followed, up to ``max_ranking_age``, and one
  where no candidate could be read is a failed one;
* a failure is never a reason to stop following a trader: one the ranking cannot read stays
  followed, in one of the ``max_leads`` slots, and only the ranking's own data takes a
  trader out (no longer listed, failing the figures, outranked, or failing its curve or its
  trading rhythm). A new candidate that cannot be read is left out of that ranking: it never
  had a vote;
* a trader whose trades could not be read stops counting after ``max_data_age``, and its
  vote is then unknown: a possible seller against a buy (:class:`LeadView`);
* a refusal (HTTP 403, 418, 429 or 451) stops every request for ``backoff``, and the
  readings keep ageing meanwhile (the gauges too);
* each trader's reading carries the time its trades were received, also in a long ranking;
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
from trade_agent.leads.ranking import Assessment, activity_reasons, assess, best, candidates
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
                "trades_per_day": round(assessment.trades_per_day, 2),
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
            self._publish(self.view(now), errors=0)  # no request: the readings only age
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
        """One request, counted, after the wait that spaces it from the previous one: the
        clock right after it is when its answer was received."""
        await self._sleep(self._config.request_delay_s)
        result = await request
        outcome.ok += 1
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
        """Chooses the followed traders again; returns the ones whose trades it already asked
        for. A failure keeps the previous ones, and the ranking is tried again at the next
        reading; a followed trader it cannot read keeps its place, its vote unknown."""
        selection = self._config.selection
        assessed: list[Assessment] = []
        trades: dict[str, tuple[datetime, tuple[LeadTrade, ...]]] = {}
        unread: list[str] = []
        try:
            pool: list[LeadStats] = []
            for page in range(1, selection.pool_pages + 1):
                rows = await self._request(self._source.ranking(page), outcome)
                pool += rows
                if len(rows) < PAGE_SIZE:
                    break
            examined, rejected = candidates(pool, selection)
            by_activity = selection.rank_by == "activity"
            tried = found = 0
            for lead in examined:
                if by_activity and found >= selection.shortlist:
                    break  # D-054: enough eligible candidates, in the ranking's order
                tried += 1
                try:
                    assessment, received, history = await self._assess(lead, now, outcome)
                except LeadSourceRefusedError:
                    raise
                except LeadSourceError as exc:  # e.g. a portfolio closed since the ranking
                    outcome.failed += 1
                    followed = lead.portfolio_id in self._followed  # its vote must not vanish
                    if followed:
                        unread.append(lead.portfolio_id)
                    else:
                        rejected[UNREADABLE] += 1
                    log.warning(
                        "leads.candidate_failed",
                        profile=self._profile,
                        lead=lead.portfolio_id,
                        error=str(exc),
                        kept=followed,
                    )
                    continue
                rejected.update(assessment.reasons)
                assessed.append(assessment)
                found += assessment.eligible
                trades[lead.portfolio_id] = (received, history)
            if examined and not assessed:  # an outage is no reason to stop following anybody
                raise LeadSourceError("nenhum candidato pôde ser lido")
        except LeadSourceRefusedError:
            raise
        except LeadSourceError as exc:
            outcome.failed += 1
            log.warning("leads.ranking_failed", profile=self._profile, error=str(exc))
            return set()
        previous = self._followed
        kept = {lead: previous[lead] for lead in unread}
        ranked = best(assessed, selection)[: max(selection.max_leads - len(kept), 0)]
        chosen = {a.lead.portfolio_id: Followed.chosen(a) for a in ranked}
        self._followed, self._ranked_at = chosen | kept, now
        self._readings = {lead: trades[lead] for lead in chosen} | {
            lead: reading for lead, reading in self._readings.items() if lead in kept
        }  # a kept trader's last reading ages, then its vote is unknown
        await self._selected(
            previous,
            now,
            pool=len(pool),
            examined=tried,
            rejected=rejected,
            unread=sorted(kept),
        )
        return set(self._followed)

    async def _assess(
        self, lead: LeadStats, now: datetime, outcome: _Outcome
    ) -> tuple[Assessment, datetime, tuple[LeadTrade, ...]]:
        """A candidate's assessment, when its trades arrived, and the trades. Ranked by
        activity (D-054), the trades come first and an idle candidate's PnL curve is not read:
        it cannot be eligible, so the search goes on through the pool at one request each."""
        selection, portfolio = self._config.selection, lead.portfolio_id
        if selection.rank_by == "activity":
            history = await self._request(self._source.trades(portfolio), outcome)
            received = self._clock()
            if idle := activity_reasons(history, now, selection):
                return Assessment(lead, 0.0, None, None, idle), received, tuple(history)
            curve = await self._request(self._source.pnl_curve(portfolio), outcome)
        else:
            curve = await self._request(self._source.pnl_curve(portfolio), outcome)
            history = await self._request(self._source.trades(portfolio), outcome)
            received = self._clock()  # a long ranking ages its first readings
        return assess(lead, curve, history, now, selection), received, tuple(history)

    async def _selected(
        self,
        previous: Mapping[str, Followed],
        ranked_at: datetime,
        *,
        pool: int,
        examined: int,
        rejected: Counter[str],
        unread: list[str],
    ) -> None:
        """Persists and records the followed traders, and announces a change of them.
        ``unread``: the followed traders this ranking could not read, kept."""
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
            "unread": unread,
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

    def _publish(self, view: LeadView, *, errors: int) -> list[str]:
        """The gauges of ``view`` and the failed requests; returns the assets with a
        consensus."""
        config = self._config.consensus
        consensus = sorted(s for s in view.backings if view.consensus(s, config))
        bought = [s for s in consensus if view.consensus(s, config) == BUY]
        metrics.observe_leads(
            profile=self._profile,
            selected=len(self._followed),
            fresh=view.fresh,
            consensus=len(bought),
            errors=errors,
        )
        return consensus

    async def _report(self, now: datetime, outcome: _Outcome) -> None:
        """Metrics and log of the reading, and the source going down or coming back."""
        view = self.view(now)
        consensus = self._publish(view, errors=outcome.failed)
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
        (log.warning if down else log.info)(kind, profile=self._profile)
        if self._notify is not None:
            text = (
                "fonte de copy trading indisponível: sem novas entradas até voltar"
                if down
                else "fonte de copy trading voltou a responder"
            )
            await self._notify(severity, f"{self._profile}: {text}")
