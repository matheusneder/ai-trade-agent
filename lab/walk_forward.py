"""Backtest *walk-forward* dos sinais do agente no Freqtrade (via Docker).

Gera a configuração e os parâmetros da estratégia a partir de ``config/profiles.yaml``
(fonte única da verdade), baixa os dados da Binance, executa um backtest por janela e
resume os resultados contra o *buy & hold* da cesta de pares e do BTC.

Com ``--optimize``, cada janela de validação é precedida de um *hyperopt* nos
``--train-months`` anteriores (fora da amostra): os parâmetros escolhidos no treino são
aplicados, sem ajuste, na janela seguinte. Os parâmetros que definem o risco do perfil
(stop máximo, risco por trade, tamanho máximo) nunca são otimizados.

Uso::

    uv run python -m lab.walk_forward --profile swing_trend --optimize --download
    uv run python -m lab.walk_forward --profile momentum_alpha --start 2024-01-01 \\
        --end 2026-09-01 --window-months 3
"""

import argparse
import dataclasses
import gzip
import json
import subprocess
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from trade_agent.execution.orders import StopMode
from trade_agent.market.universe import Tier
from trade_agent.strategy.profiles import ProfileConfig, StrategyConfig, load_strategy_config

ROOT = Path(__file__).resolve().parents[1]
LAB = ROOT / "lab" / "freqtrade"
USER_DATA = LAB / "user_data"
COMPOSE = LAB / "docker-compose.yml"
OUTPUT = ROOT / "var" / "lab"
STRATEGY = "TradeAgentStrategy"
# O Freqtrade lê os parâmetros do JSON com o nome do *arquivo* da estratégia.
PARAMS_FILE = "trade_agent_strategy.json"
BENCHMARK = "BTC/USDT"
DATA = USER_DATA / "data" / "binance"
OPTIMIZED_SPACES = ("buy", "sell")
# Definem o risco do perfil: nunca são otimizados (optimize=False na estratégia).
RISK_PARAMS = frozenset({"risk_per_trade", "max_position_pct", "stop_max_pct"})

# Pares com histórico contínuo desde 2023 por tier (aproximação estática do universo).
PAIRS_BY_TIER: dict[Tier, tuple[str, ...]] = {
    Tier.CORE: ("BTC/USDT", "ETH/USDT"),
    Tier.LARGE: (
        "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT", "ADA/USDT",
        "LINK/USDT", "AVAX/USDT", "TRX/USDT", "LTC/USDT", "DOT/USDT",
    ),
    Tier.MID: ("ATOM/USDT", "NEAR/USDT", "UNI/USDT", "FIL/USDT", "APT/USDT", "OP/USDT"),
    Tier.SMALL: ("GALA/USDT", "SAND/USDT", "MANA/USDT", "CHZ/USDT"),
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class WindowResult:
    start: date
    end: date
    trades: int
    profit_pct: float
    winrate_pct: float
    max_drawdown_pct: float
    market_change_pct: float
    """*Buy & hold* da cesta de pares (média simples, calculada pelo Freqtrade)."""
    btc_pct: float | None = None
    """*Buy & hold* do BTC na janela (``None`` sem os dados locais)."""
    params: dict[str, float] = field(default_factory=dict)
    """Parâmetros escolhidos no treino (apenas com ``--optimize``)."""

    @property
    def excess_pct(self) -> float:
        return self.profit_pct - self.market_change_pct


# ============================================================================ funções puras
def add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    return date(day.year + month_index // 12, month_index % 12 + 1, 1)


def windows(start: date, end: date, months: int) -> list[tuple[date, date]]:
    """Janelas consecutivas de ``months`` meses (a última pode ser mais curta)."""
    if months < 1 or start >= end:
        raise ValueError("janela inválida")
    result: list[tuple[date, date]] = []
    cursor = start
    while cursor < end:
        nxt = min(add_months(cursor.replace(day=1), months), end)
        result.append((cursor, nxt))
        cursor = nxt
    return result


def timerange(start: date, end: date) -> str:
    return f"{start:%Y%m%d}-{end:%Y%m%d}"


def train_range(start: date, months: int) -> tuple[date, date]:
    """Janela de treino imediatamente anterior à janela de validação."""
    if months < 1:
        raise ValueError("treino inválido")
    return add_months(start.replace(day=1), -months), start


def profile_pairs(profile: ProfileConfig) -> list[str]:
    pairs: list[str] = []
    for tier in Tier:
        if profile.tier_limit(tier) > 0:
            pairs.extend(PAIRS_BY_TIER[tier])
    return pairs


def lab_config(base: dict[str, Any], profile: ProfileConfig, capital: Decimal) -> dict[str, Any]:
    """Configuração do Freqtrade para o perfil (timeframe, vagas, reserva, pares, carteira)."""
    config: dict[str, Any] = json.loads(json.dumps(base))
    config.pop("$comment", None)
    config["timeframe"] = profile.timeframe
    config["max_open_trades"] = profile.allocation.max_open_positions
    config["tradable_balance_ratio"] = float(1 - profile.allocation.cash_reserve_pct)
    config["dry_run_wallet"] = float(capital)
    config["exchange"]["pair_whitelist"] = profile_pairs(profile)
    return config


def strategy_params(profile: ProfileConfig) -> dict[str, Any]:
    """Conteúdo de ``trade_agent_strategy.json`` (formato de parâmetros do Freqtrade)."""
    signals = profile.signal_params()
    protection = profile.protection
    stop = protection.stop
    if stop.mode is StopMode.FIXED:  # já refletidos em signal_params()
        stop_max, atr_mult = signals.max_stop_pct, signals.atr_stop_mult
    else:  # aproximação: stop trailing modelado como stop fixo na distância do trailing
        stop_max = (stop.trailing_delta_bps or 0) / 10_000
        atr_mult = 100.0
    return {
        "strategy_name": STRATEGY,
        "params": {
            "buy": {
                "min_score": profile.entry.min_score,
                "adx_min": signals.adx_min,
                "rsi_pullback_max": signals.rsi_pullback_max,
                "vol_rel_min": signals.vol_rel_min,
                "risk_per_trade": float(profile.allocation.risk_per_trade_pct) / 100,
                "max_position_pct": float(profile.allocation.max_position_pct),
            },
            "sell": {
                "atr_stop_mult": atr_mult,
                "stop_max_pct": stop_max,
                "tp_activation": float(protection.take_profit.activation_pct) / 100,
                "tp_trailing": (protection.take_profit.trailing_delta_bps or 0) / 10_000,
                "exit_score": profile.exits.exit_score,
            },
            "stoploss": {"stoploss": -0.25},
            "roi": {"0": 100},
            "trailing": {
                "trailing_stop": False,
                "trailing_stop_positive": None,
                "trailing_stop_positive_offset": 0.0,
                "trailing_only_offset_is_reached": False,
            },
        },
        "ft_stratparam_v": 1,
        "export_time": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00:00"),
    }


def chosen_params(exported: dict[str, Any]) -> dict[str, float]:
    """Parâmetros escolhidos pelo *hyperopt*, sem os de risco do perfil."""
    return {
        name: float(value)
        for space in OPTIMIZED_SPACES
        for name, value in exported["params"].get(space, {}).items()
        if name not in RISK_PARAMS
    }


def merge_optimized(base: dict[str, Any], exported: dict[str, Any]) -> dict[str, Any]:
    """Aplica os parâmetros escolhidos pelo *hyperopt* sobre os do perfil. Os de risco
    (``RISK_PARAMS``) sempre vêm do perfil, mesmo que o arquivo exportado os traga."""
    merged: dict[str, Any] = json.loads(json.dumps(base))
    chosen = chosen_params(exported)
    for space in OPTIMIZED_SPACES:
        for name in merged["params"][space]:
            if name in chosen:
                merged["params"][space][name] = chosen[name]
    return merged


def buy_and_hold_pct(data_file: Path, start: date, end: date) -> float | None:
    """Variação % entre a abertura do primeiro candle da janela e o fechamento do último
    (arquivo ``jsongz`` do Freqtrade: ``[[ms, open, high, low, close, volume], ...]``)."""
    if not data_file.exists():
        return None
    start_ms = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1000)
    end_ms = int(datetime(end.year, end.month, end.day, tzinfo=UTC).timestamp() * 1000)
    candles = [
        c for c in json.loads(gzip.decompress(data_file.read_bytes())) if start_ms <= c[0] < end_ms
    ]
    if not candles:
        return None
    return (float(candles[-1][4]) / float(candles[0][1]) - 1) * 100


def parse_result(archive: Path, start: date, end: date) -> WindowResult:
    """Lê o resultado (.zip ou .json) de um backtest do Freqtrade."""
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zipped:
            name = next(
                n
                for n in zipped.namelist()
                if n.endswith(".json") and "_config" not in n and "_market" not in n
            )
            data = json.loads(zipped.read(name))
    else:
        data = json.loads(archive.read_text(encoding="utf-8"))
    stats = data["strategy"][STRATEGY]
    return WindowResult(
        start=start,
        end=end,
        trades=int(stats.get("total_trades", 0)),
        profit_pct=float(stats.get("profit_total", 0.0)) * 100,
        winrate_pct=float(stats.get("winrate", 0.0) or 0.0) * 100,
        max_drawdown_pct=float(stats.get("max_drawdown_account", 0.0)) * 100,
        market_change_pct=float(stats.get("market_change", 0.0)) * 100,
    )


def latest_result(directory: Path) -> Path:
    pointer = json.loads((directory / ".last_result.json").read_text(encoding="utf-8"))
    return directory / str(pointer["latest_backtest"])


def _compounded(values: Sequence[float]) -> float:
    total = 1.0
    for value in values:
        total *= 1 + value / 100
    return (total - 1) * 100


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def summarize(profile_name: str, results: Sequence[WindowResult], subtitle: str = "") -> str:
    """Relatório em Markdown com uma linha por janela, o agregado e, com otimização, os
    parâmetros escolhidos em cada treino."""
    lines = [
        f"# Walk-forward — perfil `{profile_name}`",
        "",
        *([subtitle, ""] if subtitle else []),
        "| Janela | Trades | Resultado % | Cesta B&H % | BTC B&H % | Excesso % | Acerto % "
        "| DD máx % |",
        "|--------|-------:|------------:|------------:|----------:|----------:|---------:"
        "|---------:|",
    ]
    for r in results:
        lines.append(
            f"| {r.start:%Y-%m-%d} → {r.end:%Y-%m-%d} | {r.trades} | {r.profit_pct:.2f} | "
            f"{r.market_change_pct:.2f} | {_pct(r.btc_pct)} | {r.excess_pct:.2f} | "
            f"{r.winrate_pct:.1f} | {r.max_drawdown_pct:.2f} |"
        )
    if results:
        positive = sum(1 for r in results if r.profit_pct > 0)
        beat = sum(1 for r in results if r.excess_pct > 0)
        with_btc = [r for r in results if r.btc_pct is not None]
        beat_btc = sum(1 for r in with_btc if r.profit_pct > (r.btc_pct or 0.0))
        lines += [
            "",
            f"- Janelas com resultado positivo: **{positive}/{len(results)}**",
            f"- Janelas acima do buy & hold da cesta: **{beat}/{len(results)}**",
            f"- Janelas acima do buy & hold do BTC: **{beat_btc}/{len(with_btc)}**",
            f"- Resultado composto: **{_compounded([r.profit_pct for r in results]):.2f}%**",
            f"- Buy & hold composto — cesta: "
            f"**{_compounded([r.market_change_pct for r in results]):.2f}%**; BTC: "
            f"**{_pct(_compounded([r.btc_pct or 0.0 for r in with_btc]) if with_btc else None)}%**",
            f"- Trades: **{sum(r.trades for r in results)}**",
            f"- Pior drawdown de janela: **{max(r.max_drawdown_pct for r in results):.2f}%**",
        ]
    names = sorted({name for r in results for name in r.params})
    if names:
        lines += [
            "",
            "## Parâmetros escolhidos no treino",
            "",
            "| Janela | " + " | ".join(names) + " |",
            "|--------|" + "|".join("---:" for _ in names) + "|",
        ]
        lines += [
            f"| {r.start:%Y-%m-%d} | "
            + " | ".join(f"{r.params[n]:g}" if n in r.params else "—" for n in names)
            + " |"
            for r in results
        ]
    return "\n".join(lines) + "\n"


# ============================================================================ execução (Docker)
def _freqtrade(*args: str, attempts: int = 3) -> None:
    """Executa um comando do Freqtrade no contêiner, com retentativa (a carga de mercados
    da Binance a cada execução pode sofrer *timeout* de rede)."""
    command = ["docker", "compose", "-f", str(COMPOSE), "run", "--rm", "freqtrade", *args]
    for attempt in range(1, attempts + 1):
        try:
            subprocess.run(command, check=True)  # noqa: S603 - comando montado localmente
            return
        except subprocess.CalledProcessError:
            if attempt == attempts:
                raise


def _params_file() -> Path:
    return USER_DATA / "strategies" / PARAMS_FILE


def _write_params(params: dict[str, Any]) -> None:
    _params_file().write_text(json.dumps(params, indent=2), encoding="utf-8")


def prepare(config: StrategyConfig, profile_name: str) -> str:
    """Gera ``config.<perfil>.json`` e ``trade_agent_strategy.json``; retorna o caminho relativo."""
    profile = config.profiles[profile_name]
    base = json.loads((USER_DATA / "config.base.json").read_text(encoding="utf-8"))
    generated = lab_config(base, profile, config.profile_capital(profile_name))
    config_file = USER_DATA / f"config.{profile_name}.json"
    config_file.write_text(json.dumps(generated, indent=2), encoding="utf-8")
    _write_params(strategy_params(profile))
    return f"user_data/{config_file.name}"


def optimize(
    profile: ProfileConfig, config_path: str, train: tuple[date, date], args: argparse.Namespace
) -> dict[str, float]:
    """*Hyperopt* na janela de treino; grava os parâmetros escolhidos (sobre os do perfil)."""
    base = strategy_params(profile)
    _write_params(base)
    _freqtrade(
        "hyperopt", "--config", config_path, "--strategy", STRATEGY,
        "--timerange", timerange(*train), "--spaces", *OPTIMIZED_SPACES,
        "--hyperopt-loss", args.loss, "--epochs", str(args.epochs),
        "--random-state", str(args.seed), "--min-trades", str(args.min_trades),
    )  # fmt: skip
    exported = json.loads(_params_file().read_text(encoding="utf-8"))
    _write_params(merge_optimized(base, exported))
    return chosen_params(exported)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--profile", required=True, help="perfil de config/profiles.yaml")
    parser.add_argument("--profiles-file", type=Path, default=ROOT / "config" / "profiles.yaml")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2023, 1, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2026, 9, 1))
    parser.add_argument("--window-months", type=int, default=3)
    parser.add_argument("--download", action="store_true", help="baixa/atualiza os candles")
    parser.add_argument("--optimize", action="store_true", help="hyperopt no treino anterior")
    parser.add_argument("--train-months", type=int, default=12)
    parser.add_argument("--epochs", type=int, default=150)
    parser.add_argument("--loss", default="SharpeHyperOptLossDaily")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--min-trades", type=int, default=20)
    args = parser.parse_args(argv)

    strategy_config = load_strategy_config(args.profiles_file)
    profile = strategy_config.profiles[args.profile]
    config_path = prepare(strategy_config, args.profile)
    periods = windows(args.start, args.end, args.window_months)
    if args.download:
        history = 3 + (args.train_months if args.optimize else 0)
        warmup_start = add_months(args.start.replace(day=1), -history)
        _freqtrade(
            "download-data", "--config", config_path, "--timerange",
            f"{warmup_start:%Y%m%d}-{args.end:%Y%m%d}", "-t", profile.timeframe,
            "--pairs", *dict.fromkeys([*profile_pairs(profile), BENCHMARK]),
        )  # fmt: skip
    mode = "otimizado" if args.optimize else "fixo"
    btc_file = DATA / f"{BENCHMARK.replace('/', '_')}-{profile.timeframe}.json.gz"
    results: list[WindowResult] = []
    for index, (start, end) in enumerate(periods):
        chosen: dict[str, float] = {}
        if args.optimize:
            chosen = optimize(profile, config_path, train_range(start, args.train_months), args)
        directory = f"user_data/backtest_results/wf-{args.profile}-{mode}-{index:02d}"
        (LAB / directory).mkdir(parents=True, exist_ok=True)
        _freqtrade(
            "backtesting", "--config", config_path, "--strategy", STRATEGY,
            "--timerange", timerange(start, end), "--export", "trades",
            "--backtest-directory", directory, "--cache", "none",
        )  # fmt: skip
        result = parse_result(latest_result(LAB / directory), start, end)
        btc = buy_and_hold_pct(btc_file, start, end)
        results.append(dataclasses.replace(result, btc_pct=btc, params=chosen))
    subtitle = (
        f"Parâmetros otimizados ({args.loss}, {args.epochs} épocas) nos {args.train_months} "
        "meses anteriores a cada janela e aplicados sem ajuste na janela."
        if args.optimize
        else "Parâmetros fixos do perfil (`config/profiles.yaml`)."
    )
    report = summarize(args.profile, results, subtitle)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    name = f"walk-forward-{args.profile}-{mode}-{stamp}.md"
    (OUTPUT / name).write_text(report, encoding="utf-8")
    sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
