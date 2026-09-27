import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garante que variáveis ``TA_*`` do ambiente do desenvolvedor não vazem para os testes."""
    for name in list(os.environ):
        if name.startswith(("TA_", "ANTHROPIC_")):
            monkeypatch.delenv(name)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Marca testes por diretório: ``tests/integration`` e ``tests/live``."""
    for item in items:
        parts = item.path.parts
        if "integration" in parts:
            item.add_marker(pytest.mark.integration)
        if "live" in parts:
            item.add_marker(pytest.mark.live)
