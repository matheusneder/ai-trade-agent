from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.support.claude import research_config
from trade_agent.research.config import ModelsConfig, load_research_config
from trade_agent.research.tagging import AssetTagger

REPOSITORY_FILE = Path(__file__).parents[3] / "config" / "research.yaml"


def test_repository_research_config_is_valid() -> None:
    config = load_research_config(REPOSITORY_FILE)
    assert config.models.analyst in config.pricing.models
    assert config.models.triage in config.pricing.models
    assert config.budget.daily_usd > 0
    assert config.sources.rss_feeds
    assert all(names for names in config.asset_names.values())


def test_models_without_price_are_rejected() -> None:
    with pytest.raises(ValidationError, match="sem preço"):
        research_config(models=ModelsConfig(analyst="claude-desconhecido"))


def test_tagger_codes_marked_bare_and_names() -> None:
    tagger = AssetTagger(["SOL", "OP", "NEAR", "BTC"], {"BTC": ["bitcoin"], "OP": ["optimism"]})
    assert tagger.tag("Solana (SOL) rallies; $OP too") == ("OP", "SOL")
    assert tagger.tag("SOL and BTC rise") == ("BTC", "SOL")
    assert tagger.tag("OP ed: markets") == ()  # código curto só com $ ou parênteses
    assert tagger.tag("Bitcoin and Optimism news") == ("BTC", "OP")
    assert tagger.tag("near-term outlook, SOLANA, solid, XSOL, SOL2") == ()
    assert tagger.tag("NEAR breaks out") == ("NEAR",)


def test_tagger_without_assets_matches_nothing() -> None:
    assert AssetTagger([], {}).tag("BTC $SOL (OP)") == ()
