import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Garante que variáveis ``TA_*`` do ambiente do desenvolvedor não vazem para os testes."""
    for name in list(os.environ):
        if name.startswith("TA_"):
            monkeypatch.delenv(name)
