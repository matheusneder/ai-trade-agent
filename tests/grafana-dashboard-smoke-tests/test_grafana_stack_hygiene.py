"""Leftovers of killed runs are recognized without touching a live run (no Docker: always runs)."""

import os
import subprocess
import sys

from tests.support.grafana_stack import PROJECT, PROJECT_PREFIX, _process_alive


def test_project_names_carry_the_owner_pid() -> None:
    match = PROJECT.fullmatch(f"{PROJECT_PREFIX}{os.getpid()}-0a1b2c3d")
    assert match is not None and int(match["pid"]) == os.getpid()
    assert PROJECT.fullmatch("trade-agent") is None  # the real stack never matches
    assert PROJECT.fullmatch(f"{PROJECT_PREFIX}0a1b2c3d") is None


def test_a_finished_process_is_stale_and_a_running_one_is_not() -> None:
    assert _process_alive(os.getpid())
    done = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                          capture_output=True, text=True, check=True)  # fmt: skip
    assert not _process_alive(int(done.stdout))
