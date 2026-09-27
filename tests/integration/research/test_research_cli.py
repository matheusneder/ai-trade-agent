import io
import json
from pathlib import Path

import httpx
import pytest
import respx

from tests.support.claude import NOW, FakeClaude
from trade_agent.cli import main
from trade_agent.config.settings import load_settings
from trade_agent.persistence.db import Database
from trade_agent.research.commands import DEFAULT_DEPS, ResearchDeps
from trade_agent.research.config import load_research_config

CONFIG = """\
pricing:
  models:
    claude-opus-5: {input: 5, output: 25}
    claude-sonnet-5: {input: 2, output: 10}
sources:
  rss_feeds: {feed: "https://feed.example/rss"}
  fear_greed: false
  derivatives: false
asset_names: {SOL: [solana]}
"""
FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Solana upgrade</title><link>https://feed.example/1</link>
<pubDate>Sat, 26 Sep 2026 11:00:00 +0000</pubDate></item></channel></rss>"""
VIEW = {
    "market_regime": "neutral", "global_sentiment": 0.0, "exposure_multiplier": 1.0,
    "global_risk_flags": [], "assets": [],
}  # fmt: skip
TRIAGE = {
    "items": [{"id": 1, "relevance": 0.9, "category": "project", "severity": "low", "assets": []}]
}


@pytest.fixture
def env_file(tmp_path: Path, postgres_url: str) -> Path:
    config = tmp_path / "research.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    env = tmp_path / ".env"
    env.write_text(
        f"TA_DATABASE_URL={postgres_url}\nTA_RESEARCH_CONFIG={config}\nANTHROPIC_API_KEY=test-key\n",
        encoding="utf-8",
    )
    return env


def _run(args: list[str], env: Path, deps: ResearchDeps) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(["--env-file", str(env), "research", *args], out=out, err=err, research_deps=deps)
    return code, out.getvalue(), err.getvalue()


def _deps(fake: FakeClaude) -> ResearchDeps:
    return ResearchDeps(
        anthropic_factory=lambda _settings: fake.client(),
        http_factory=lambda _config: httpx.AsyncClient(),
        database_factory=DEFAULT_DEPS.database_factory,
        clock=lambda: NOW,
    )


def test_ingest_run_show(env_file: Path, db: Database) -> None:
    fake = FakeClaude()
    fake.reply_json(TRIAGE)
    fake.reply_json(VIEW)
    fake.reply([{"type": "text", "text": "fora do schema"}])
    with respx.mock() as router:
        router.get("https://feed.example/rss").respond(200, content=FEED)
        code, out, _ = _run(["ingest"], env_file, _deps(fake))
        assert code == 0
        assert json.loads(out) == {"coletadas": 1, "novas": 1, "erros": {}}

        code, out, _ = _run(["run", "--assets", "sol, btc,", "--no-web"], env_file, _deps(fake))
        assert code == 0
        report = json.loads(out)
        assert report["erro"] is None and report["leitura"]["exposure_multiplier"] == 1.0
        assert "<candidates>\nSOL (SOLUSDT" in fake.requests[1]["messages"][0]["content"]

        code, out, _ = _run(["run", "--assets", "SOL", "--no-web"], env_file, _deps(fake))
        assert code == 1 and json.loads(out)["leitura"] is None

    code, out, _ = _run(["show"], env_file, _deps(fake))
    assert code == 0 and json.loads(out)["market_regime"] == "neutral"


def test_show_without_reports(env_file: Path, db: Database) -> None:
    code, out, _ = _run(["show"], env_file, _deps(FakeClaude()))
    assert code == 0 and json.loads(out) is None


def test_eval_writes_report(env_file: Path, tmp_path: Path) -> None:
    fake = FakeClaude()
    fake.reply_json(
        {**VIEW, "assets": [{
            "asset": "ZEPH", "sentiment": -0.9, "confidence": 0.9, "horizon": "days",
            "catalysts": [], "risk_flags": ["delistagem"], "veto": True,
            "rationale": "Binance anunciou a delistagem", "sources": [],
        }]}
    )  # fmt: skip
    output = tmp_path / "eval"
    args = ["eval", "--case", "syn-delistagem", "--budget", "1", "--output", str(output)]
    code, out, _ = _run(args, env_file, _deps(fake))
    assert code == 0
    assert "| syn-delistagem | sim | ok | ✅ |" in out
    assert "atendido ✅" in out
    (written,) = output.glob("analyst-*.md")
    assert written.read_text(encoding="utf-8") == out
    assert fake.requests[0]["model"] == "claude-opus-5"
    assert "tools" not in fake.requests[0]  # avaliação sem busca web

    fake.reply_json(VIEW)  # não veta ZEPH: reprovado
    code, out, _ = _run(
        ["eval", "--case", "syn-delistagem", "--output", str(output)], env_file, _deps(fake)
    )
    assert code == 1 and "ZEPH: veto esperado" in out
    code, _, err = _run(["eval", "--case", "nao-existe"], env_file, _deps(fake))
    assert code == 2 and "nenhum caso" in err
    code, out, _ = _run(["eval", "--limit", "0"], env_file, _deps(fake))
    assert code == 2


def test_missing_key_or_database(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    code, _, err = _run(["eval"], env, DEFAULT_DEPS)
    assert code == 2 and "ANTHROPIC_API_KEY" in err
    code, _, err = _run(["run", "--assets", "BTC"], env, DEFAULT_DEPS)
    assert code == 2 and "ANTHROPIC_API_KEY" in err
    code, _, err = _run(["show"], env, DEFAULT_DEPS)
    assert code == 2 and "TA_DATABASE_URL" in err


async def test_default_factories(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "ANTHROPIC_API_KEY=sk-test\nTA_DATABASE_URL=postgresql+asyncpg://u@localhost:1/x\n",
        encoding="utf-8",
    )
    settings = load_settings(env)
    assert DEFAULT_DEPS.anthropic_factory(settings).api_key == "sk-test"
    database = DEFAULT_DEPS.database_factory(settings)
    await database.dispose()
    config = load_research_config(settings.research_config)
    async with DEFAULT_DEPS.http_factory(config) as http:
        assert http.timeout.read == config.sources.timeout_s
