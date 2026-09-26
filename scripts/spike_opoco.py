"""Spike da Fase 0: valida OPOCO/OCO com trailing no Spot Testnet ou no Demo Mode.

Cenários (ver doc/04-plano-de-construcao.md, Fase 0):

A. OPOCO: compra LIMIT FOK "marketable" + OCO de venda com TAKE_PROFIT (ativação + trailing)
   acima e STOP_LOSS fixo abaixo. Inclui reenvio do mesmo listClientOrderId com a lista aberta.
B. OPOCO: compra FOK + LIMIT_MAKER acima e STOP_LOSS somente com trailingDelta abaixo.
C. OPOCO com compra FOK não executável: a lista deve expirar sem armar as pendentes.
   Em seguida, reutiliza o mesmo listClientOrderId (lista já encerrada).
D. Compra a mercado + OCO avulso (orderList/oco) com trailing TP, consulta e cancelamento.

Durante todos os cenários, assina o User Data Stream via WebSocket API
(``userDataStream.subscribe.signature``) e registra os eventos.

Uso:  uv run python scripts/spike_opoco.py --env-file .env [--symbol BTCUSDT]

O script RECUSA o ambiente ``prod``. Ordens são enviadas apenas no Testnet/Demo.
"""

import argparse
import asyncio
import contextlib
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

import websockets

from trade_agent.config.settings import Settings, load_settings
from trade_agent.exchange.environments import BinanceEnvironment, endpoints_for
from trade_agent.exchange.errors import BinanceAPIError
from trade_agent.exchange.factory import build_rest_client, build_signer
from trade_agent.exchange.rest import BinanceRestClient
from trade_agent.exchange.serialization import ws_signature_payload

OUT_DIR = Path("var/spike")


# ----------------------------------------------------------------------------- utilidades
def quantize(value: Decimal, step: Decimal, *, up: bool) -> Decimal:
    units = (value / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return (units * step).normalize()


@dataclass
class SymbolRules:
    tick: Decimal
    step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    trailing: dict[str, int]
    flags: dict[str, bool]

    @classmethod
    def parse(cls, info: dict[str, Any]) -> "SymbolRules":
        filters = {f["filterType"]: f for f in info["filters"]}
        return cls(
            tick=Decimal(filters["PRICE_FILTER"]["tickSize"]),
            step=Decimal(filters["LOT_SIZE"]["stepSize"]),
            min_qty=Decimal(filters["LOT_SIZE"]["minQty"]),
            min_notional=Decimal(filters["NOTIONAL"]["minNotional"]),
            trailing={
                k: v for k, v in filters.get("TRAILING_DELTA", {}).items() if k != "filterType"
            },
            flags={
                k: bool(info.get(k))
                for k in ("ocoAllowed", "otoAllowed", "opoAllowed", "allowTrailingStop")
            },
        )


@dataclass
class Report:
    env: str
    symbol: str
    started_at: float = field(default_factory=time.time)
    checks: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def check(self, scenario: str, name: str, ok: bool, detail: Any = None) -> None:
        self.checks.append({"scenario": scenario, "check": name, "ok": ok, "detail": detail})
        mark = "PASS" if ok else "FAIL"
        print(f"  [{mark}] {scenario}: {name}" + (f" -> {detail}" if detail is not None else ""))

    def save(self) -> Path:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"spike-{self.env}-{int(self.started_at)}.json"
        path.write_text(json.dumps(self.__dict__, indent=2, default=str), encoding="utf-8")
        return path


class Spike:
    def __init__(self, client: BinanceRestClient, report: Report, rules: SymbolRules) -> None:
        self.c = client
        self.r = report
        self.rules = rules
        self.symbol = report.symbol
        self.run = uuid.uuid4().hex[:6]

    async def call(
        self, label: str, method: str, path: str, params: dict[str, Any], **kw: Any
    ) -> Any:
        entry: dict[str, Any] = {"label": label, "method": method, "path": path, "params": params}
        try:
            result = await self.c.signed(method, path, params, **kw)  # type: ignore[arg-type]
            entry["response"] = result
            return result
        except BinanceAPIError as exc:
            entry["error"] = {"status": exc.status, "code": exc.code, "msg": exc.message}
            raise
        finally:
            self.r.calls.append(entry)

    def cid(self, scenario: str, leg: str) -> str:
        return f"spk-{self.run}-{scenario}-{leg}"

    async def book(self) -> tuple[Decimal, Decimal]:
        data = await self.c.public("GET", "/api/v3/ticker/bookTicker", {"symbol": self.symbol})
        return Decimal(data["bidPrice"]), Decimal(data["askPrice"])

    async def free(self, asset: str) -> Decimal:
        account = await self.c.signed("GET", "/api/v3/account", {"omitZeroBalances": "true"})
        for balance in account["balances"]:
            if balance["asset"] == asset:
                return Decimal(balance["free"])
        return Decimal(0)

    def qty_for(self, price: Decimal) -> Decimal:
        notional = max(self.rules.min_notional * 2, Decimal(15))
        return max(quantize(notional / price, self.rules.step, up=True), self.rules.min_qty)

    def px(self, value: Decimal, *, up: bool) -> Decimal:
        return quantize(value, self.rules.tick, up=up)

    async def sell_back(self, base: str, before: Decimal, label: str) -> None:
        delta = await self.free(base) - before
        qty = quantize(delta, self.rules.step, up=False)
        if qty < self.rules.min_qty:
            return
        _, ask = await self.book()
        if qty * ask < self.rules.min_notional:
            return
        await self.call(
            f"{label}:sell_back",
            "POST",
            "/api/v3/order",
            {"symbol": self.symbol, "side": "SELL", "type": "MARKET", "quantity": qty},
            trading=True,
        )

    async def list_status(self, list_cid: str) -> dict[str, Any]:
        result: dict[str, Any] = await self.call(
            "query_list", "GET", "/api/v3/orderList", {"origClientOrderId": list_cid}
        )
        return result

    async def orders_of(self, status: dict[str, Any]) -> list[dict[str, Any]]:
        result = []
        for order in status.get("orders", []):
            result.append(
                await self.call(
                    "query_order",
                    "GET",
                    "/api/v3/order",
                    {"symbol": self.symbol, "orderId": order["orderId"]},
                )
            )
        return result

    # ------------------------------------------------------------------------- cenários
    async def scenario_a(self, base: str) -> None:
        print("Cenário A — OPOCO FOK + TAKE_PROFIT(ativação+trailing) / STOP_LOSS fixo")
        before = await self.free(base)
        _, ask = await self.book()
        list_cid = self.cid("A", "L")
        params = {
            "symbol": self.symbol,
            "listClientOrderId": list_cid,
            "workingType": "LIMIT",
            "workingSide": "BUY",
            "workingPrice": self.px(ask * Decimal("1.003"), up=True),
            "workingQuantity": self.qty_for(ask),
            "workingTimeInForce": "FOK",
            "workingClientOrderId": self.cid("A", "E"),
            "pendingSide": "SELL",
            "pendingAboveType": "TAKE_PROFIT",
            "pendingAboveStopPrice": self.px(ask * Decimal("1.03"), up=True),
            "pendingAboveTrailingDelta": 100,
            "pendingAboveClientOrderId": self.cid("A", "TP"),
            "pendingBelowType": "STOP_LOSS",
            "pendingBelowStopPrice": self.px(ask * Decimal("0.96"), up=False),
            "pendingBelowClientOrderId": self.cid("A", "SL"),
            "newOrderRespType": "FULL",
        }
        try:
            response = await self.call(
                "A:opoco", "POST", "/api/v3/orderList/opoco", params, trading=True
            )
        except BinanceAPIError as exc:
            self.r.check("A", "OPOCO aceito", False, f"{exc.code} {exc.message}")
            return
        self.r.check("A", "OPOCO aceito", True, response.get("contingencyType"))
        await asyncio.sleep(2)

        status = await self.list_status(list_cid)
        orders = await self.orders_of(status)
        by_cid = {o["clientOrderId"]: o for o in orders}
        working = by_cid.get(self.cid("A", "E"), {})
        tp = by_cid.get(self.cid("A", "TP"), {})
        sl = by_cid.get(self.cid("A", "SL"), {})
        self.r.check(
            "A", "compra FOK executada", working.get("status") == "FILLED", working.get("status")
        )
        self.r.check("A", "TP armado (NEW)", tp.get("status") == "NEW", tp.get("status"))
        self.r.check("A", "TP é TAKE_PROFIT", tp.get("type") == "TAKE_PROFIT", tp.get("type"))
        self.r.check(
            "A",
            "TP com trailingDelta=100",
            str(tp.get("trailingDelta")) == "100",
            tp.get("trailingDelta"),
        )
        self.r.check("A", "SL armado (NEW)", sl.get("status") == "NEW", sl.get("status"))
        received = await self.free(base) - before
        self.r.check(
            "A",
            "qtd pendente = qtd recebida (OPO)",
            Decimal(tp.get("origQty", "0")) <= received + self.rules.step,
            {"pending": tp.get("origQty"), "received": str(received)},
        )

        # Idempotência: mesmo listClientOrderId com a lista ainda aberta.
        try:
            await self.call("A:duplicate", "POST", "/api/v3/orderList/opoco", params, trading=True)
            self.r.check("A", "listClientOrderId duplicado (aberta) rejeitado", False, "aceito!")
        except BinanceAPIError as exc:
            self.r.check(
                "A",
                "listClientOrderId duplicado (aberta) rejeitado",
                True,
                f"{exc.code} {exc.message}",
            )

        await self.call(
            "A:cancel",
            "DELETE",
            "/api/v3/orderList",
            {"symbol": self.symbol, "listClientOrderId": list_cid},
            trading=True,
        )
        await self.sell_back(base, before, "A")

    async def scenario_b(self, base: str) -> None:
        print("Cenário B — OPOCO FOK + LIMIT_MAKER acima / STOP_LOSS só com trailingDelta")
        before = await self.free(base)
        _, ask = await self.book()
        list_cid = self.cid("B", "L")
        params = {
            "symbol": self.symbol,
            "listClientOrderId": list_cid,
            "workingType": "LIMIT",
            "workingSide": "BUY",
            "workingPrice": self.px(ask * Decimal("1.003"), up=True),
            "workingQuantity": self.qty_for(ask),
            "workingTimeInForce": "FOK",
            "pendingSide": "SELL",
            "pendingAboveType": "LIMIT_MAKER",
            "pendingAbovePrice": self.px(ask * Decimal("1.05"), up=True),
            "pendingBelowType": "STOP_LOSS",
            "pendingBelowTrailingDelta": 300,
            "pendingBelowClientOrderId": self.cid("B", "SL"),
        }
        try:
            await self.call("B:opoco", "POST", "/api/v3/orderList/opoco", params, trading=True)
        except BinanceAPIError as exc:
            self.r.check("B", "OPOCO com trailing stop aceito", False, f"{exc.code} {exc.message}")
            return
        await asyncio.sleep(2)
        orders = await self.orders_of(await self.list_status(list_cid))
        sl = next((o for o in orders if o["clientOrderId"] == self.cid("B", "SL")), {})
        self.r.check("B", "OPOCO com trailing stop aceito", True)
        self.r.check("B", "SL trailing armado (NEW)", sl.get("status") == "NEW", sl.get("status"))
        self.r.check(
            "B",
            "SL com trailingDelta=300",
            str(sl.get("trailingDelta")) == "300",
            sl.get("trailingDelta"),
        )
        await self.call(
            "B:cancel",
            "DELETE",
            "/api/v3/orderList",
            {"symbol": self.symbol, "listClientOrderId": list_cid},
            trading=True,
        )
        await self.sell_back(base, before, "B")

    async def scenario_c(self) -> None:
        print("Cenário C — OPOCO com compra FOK não executável + reuso do listClientOrderId")
        bid, _ = await self.book()
        list_cid = self.cid("C", "L")
        params = {
            "symbol": self.symbol,
            "listClientOrderId": list_cid,
            "workingType": "LIMIT",
            "workingSide": "BUY",
            "workingPrice": self.px(bid * Decimal("0.95"), up=False),
            "workingQuantity": self.qty_for(bid),
            "workingTimeInForce": "FOK",
            "workingClientOrderId": self.cid("C", "E"),
            "pendingSide": "SELL",
            "pendingAboveType": "TAKE_PROFIT",
            "pendingAboveStopPrice": self.px(bid * Decimal("1.03"), up=True),
            "pendingAboveTrailingDelta": 100,
            "pendingBelowType": "STOP_LOSS",
            "pendingBelowStopPrice": self.px(bid * Decimal("0.90"), up=False),
        }
        response = await self.call(
            "C:opoco", "POST", "/api/v3/orderList/opoco", params, trading=True
        )
        await asyncio.sleep(1)
        status = await self.list_status(list_cid)
        orders = await self.orders_of(status)
        working = next((o for o in orders if o["clientOrderId"] == self.cid("C", "E")), {})
        self.r.check(
            "C", "compra FOK expirou", working.get("status") == "EXPIRED", working.get("status")
        )
        self.r.check(
            "C",
            "lista encerrada (ALL_DONE)",
            status.get("listOrderStatus") == "ALL_DONE",
            status.get("listOrderStatus"),
        )
        self.r.check("C", "resposta inicial", True, response.get("listOrderStatus"))
        try:
            await self.call("C:reuse", "POST", "/api/v3/orderList/opoco", params, trading=True)
            self.r.check(
                "C",
                "reuso de listClientOrderId após encerrada",
                True,
                "ACEITO (atenção à idempotência)",
            )
        except BinanceAPIError as exc:
            self.r.check(
                "C",
                "reuso de listClientOrderId após encerrada",
                True,
                f"rejeitado: {exc.code} {exc.message}",
            )

    async def scenario_d(self, base: str) -> None:
        print("Cenário D — compra a mercado + OCO avulso com trailing TP")
        before = await self.free(base)
        _, ask = await self.book()
        await self.call(
            "D:buy",
            "POST",
            "/api/v3/order",
            {"symbol": self.symbol, "side": "BUY", "type": "MARKET", "quantity": self.qty_for(ask)},
            trading=True,
        )
        received = quantize(await self.free(base) - before, self.rules.step, up=False)
        list_cid = self.cid("D", "L")
        try:
            await self.call(
                "D:oco",
                "POST",
                "/api/v3/orderList/oco",
                {
                    "symbol": self.symbol,
                    "listClientOrderId": list_cid,
                    "side": "SELL",
                    "quantity": received,
                    "aboveType": "TAKE_PROFIT",
                    "aboveStopPrice": self.px(ask * Decimal("1.03"), up=True),
                    "aboveTrailingDelta": 100,
                    "belowType": "STOP_LOSS",
                    "belowStopPrice": self.px(ask * Decimal("0.96"), up=False),
                },
                trading=True,
            )
            self.r.check("D", "OCO avulso com trailing TP aceito", True)
            status = await self.list_status(list_cid)
            self.r.check(
                "D", "consulta por listClientOrderId", status.get("listClientOrderId") == list_cid
            )
            await self.call(
                "D:cancel",
                "DELETE",
                "/api/v3/orderList",
                {"symbol": self.symbol, "listClientOrderId": list_cid},
                trading=True,
            )
        except BinanceAPIError as exc:
            self.r.check(
                "D", "OCO avulso com trailing TP aceito", False, f"{exc.code} {exc.message}"
            )
        await self.sell_back(base, before, "D")


async def user_stream(
    settings: Settings, report: Report, stop: asyncio.Event, timestamp_ms: int
) -> None:
    signer = build_signer(settings)
    if signer is None or settings.binance_api_key is None:
        raise RuntimeError("credenciais ausentes para o user data stream")
    params: dict[str, Any] = {
        "apiKey": settings.binance_api_key.get_secret_value(),
        "timestamp": timestamp_ms,
    }
    params["signature"] = signer.sign(ws_signature_payload(params))
    url = endpoints_for(settings.binance_env).ws_api
    async with websockets.connect(url) as ws:
        await ws.send(
            json.dumps(
                {"id": "sub", "method": "userDataStream.subscribe.signature", "params": params}
            )
        )
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except TimeoutError:
                continue
            report.events.append(json.loads(raw))


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--skip", default="", help="cenários a pular, ex.: 'B,D'")
    args = parser.parse_args()

    settings = load_settings(env_file=args.env_file)
    if settings.binance_env is BinanceEnvironment.PROD:
        print("Recusado: o spike não roda em produção.")
        return 2
    if not settings.has_credentials:
        print("Sem credenciais: preencha TA_BINANCE_API_KEY e a chave privada no .env.")
        return 2
    settings = settings.model_copy(update={"trading_enabled": True})

    report = Report(env=settings.binance_env.value, symbol=args.symbol)
    stop = asyncio.Event()
    async with build_rest_client(settings) as client:
        offset = await client.sync_time()
        info = await client.public("GET", "/api/v3/exchangeInfo", {"symbol": args.symbol})
        rules = SymbolRules.parse(info["symbols"][0])
        base = info["symbols"][0]["baseAsset"]
        print(f"Ambiente={report.env} símbolo={args.symbol} offset_relógio={offset}ms")
        print(f"Flags={rules.flags} TRAILING_DELTA={rules.trailing}")
        report.check("0", "flags OCO/OTO/OPO/trailing", all(rules.flags.values()), rules.flags)

        stream = asyncio.create_task(user_stream(settings, report, stop, client.now_ms()))
        spike = Spike(client, report, rules)
        skip = {s.strip().upper() for s in args.skip.split(",") if s.strip()}
        try:
            if "A" not in skip:
                await spike.scenario_a(base)
            if "B" not in skip:
                await spike.scenario_b(base)
            if "C" not in skip:
                await spike.scenario_c()
            if "D" not in skip:
                await spike.scenario_d(base)
        finally:
            await asyncio.sleep(2)
            stop.set()
            with contextlib.suppress(Exception):
                await stream
        types = sorted(
            {
                e.get("event", {}).get("e", e.get("id", "?"))
                for e in report.events
                if isinstance(e, dict)
            }
        )
        report.check("WS", "eventos do user data stream recebidos", len(report.events) > 1, types)

    path = report.save()
    failed = [c for c in report.checks if not c["ok"]]
    passed = len(report.checks) - len(failed)
    print(f"\nResultado: {passed}/{len(report.checks)} verificações OK. Log: {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
