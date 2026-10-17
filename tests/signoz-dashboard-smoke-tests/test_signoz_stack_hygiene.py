"""Each run has its own stack, and only leftovers of finished runs are removed (no Docker)."""

import os

from tests.support.signoz_smoke import environment


def test_every_run_has_its_own_project_named_after_its_pid() -> None:
    first, second = environment.Stack(), environment.Stack()
    assert first.project != second.project
    match = environment.PROJECT.fullmatch(first.project)
    assert match is not None and int(match["pid"]) == os.getpid()


def test_the_real_stack_and_the_manual_stack_are_never_stale_leftovers() -> None:
    for project in ("trade-agent", "signoz", environment.MANUAL_PROJECT):
        assert environment.PROJECT.fullmatch(project) is None


def test_the_isolation_guard_wants_the_project_own_network() -> None:
    project = environment.Stack().project
    shared = {"name": project, "networks": {"default": {"name": "ta-signoz-smoke-network"}}}
    assert environment.problems(shared, project) == [
        "network 'ta-signoz-smoke-network' is not the project's own"
    ]
    own = {"name": project, "networks": {"default": {"name": f"{project}_signoz-network"}}}
    assert environment.problems(own, project) == []
