import os
from collections.abc import Iterator

import pytest
import structlog

from tests.support.tracing import Recorded, recording
from trade_agent import tracing


@pytest.fixture
def spans() -> Iterator[Recorded]:
    """Spans de todos os componentes, gravados em memória durante o teste."""
    with recording() as recorded:
        yield recorded


@pytest.fixture(autouse=True)
def _no_tracing_leak() -> Iterator[None]:
    """Nenhum teste herda (nem deixa) um rastreamento instalado."""
    tracing.install(None)
    yield
    tracing.install(None)


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garante que variáveis ``TA_*`` do ambiente do desenvolvedor não vazem para os testes."""
    for name in list(os.environ):
        if name.startswith(("TA_", "ANTHROPIC_")):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Cada teste começa com o structlog padrão (sem filtro de nível): testes da CLI chamam
    ``configure_logging`` e a configuração global não pode vazar para os demais."""
    structlog.reset_defaults()
    yield
    structlog.reset_defaults()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Marca testes por diretório: ``tests/integration`` e ``tests/live``."""
    for item in items:
        parts = item.path.parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
        if "live" in parts:
            item.add_marker(pytest.mark.live)
