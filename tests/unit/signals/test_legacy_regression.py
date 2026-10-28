"""The legacy profiles decide bit for bit as before the pair-local families (D-044).

``tests.support.legacy_snapshot`` digests the features, scores, setups, stops, entry and exit
masks and last signal of ``swing_trend``, ``momentum_alpha`` and ``alt_atr`` on seven series
(with and without a benchmark), their protection policy and an entry plan. The test computes
it twice on this machine: with the code and ``config/profiles.yaml`` of the base commit
(``BASE``, before D-044, read with ``git archive``) in a subprocess, and with this tree's.

Both run on the same libraries: the digests hash the floats' bytes, and TA-Lib, libm and
numpy differ in the last bit between Windows and Linux, so a snapshot stored on one platform
does not reproduce on another. Needs the base commit in the repository (CI checks out the
whole history).
"""

import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

from tests.support.legacy_snapshot import CONFIG_ENV, snapshot

ROOT = Path(__file__).resolve().parents[3]
BASE = "d4a4b3c"
SCRIPT = (
    "import json, trade_agent; from tests.support.legacy_snapshot import snapshot; "
    "print(json.dumps({'package': trade_agent.__file__, 'snapshot': snapshot()}))"
)


def _base_snapshot(tmp_path: Path) -> dict[str, str]:
    git = shutil.which("git")
    assert git is not None
    archive = tmp_path / "base.tar"
    subprocess.run(  # noqa: S603 - fixed arguments
        [git, "archive", "--format=tar", f"--output={archive}", BASE, "src", "config"],
        cwd=ROOT,
        check=True,
    )
    with tarfile.open(archive) as tar:
        tar.extractall(tmp_path, filter="data")
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(tmp_path / "src"), str(ROOT))),
        CONFIG_ENV: str(tmp_path / "config" / "profiles.yaml"),
    }
    result = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
        [sys.executable, "-c", SCRIPT],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    output = json.loads(result.stdout.strip().splitlines()[-1])
    # not vacuous: the subprocess ran the base commit's package, not this tree's
    assert Path(output["package"]).resolve().is_relative_to(tmp_path.resolve())
    base: dict[str, str] = output["snapshot"]
    return base


def test_the_legacy_profiles_decide_exactly_as_on_the_base_commit(tmp_path: Path) -> None:
    expected = _base_snapshot(tmp_path)
    current = snapshot()
    assert current.keys() == expected.keys()
    changed = sorted(k for k in expected if current[k] != expected[k])
    assert changed == []
