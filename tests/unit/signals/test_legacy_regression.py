"""The legacy profiles decide bit for bit as before the pair-local families (D-044).

``tests/fixtures/legacy_snapshot.json`` was computed by ``tests.support.legacy_snapshot`` on
the base commit (d4a4b3c, before D-044): the features, scores, setups, stops, entry and exit
masks and last signal of ``swing_trend``, ``momentum_alpha`` and ``alt_atr`` on seven series
(with and without a benchmark), their protection policy and an entry plan.
"""

import json
from pathlib import Path

from tests.support.legacy_snapshot import snapshot

GOLDEN = Path(__file__).parents[2] / "fixtures" / "legacy_snapshot.json"


def test_the_legacy_profiles_decide_exactly_as_on_the_base_commit() -> None:
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    current = snapshot()
    assert current.keys() == expected.keys()
    changed = sorted(k for k in expected if current[k] != expected[k])
    assert changed == []
