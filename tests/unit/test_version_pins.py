"""Every pinned version agrees with its native source, and nothing floats.

Each version is written where the tool that uses it reads it (``tests/support/versions.py``
says where); a copy elsewhere (the Dockerfile, the docs, a regeneration command) must match it,
so an upgrade that changes only one place fails here instead of diverging between production,
tests, the lab and CI.
"""

import re
from pathlib import Path

import pytest
import yaml

from tests.support.versions import (
    CASTING,
    DEPLOY,
    ROOT,
    SIGNOZ,
    casting_specs,
    compose_images,
    foundry_image,
    python_version,
    text,
    uv_version,
)

DOCKERFILE = ROOT / "Dockerfile"
CI = ROOT / ".github" / "workflows" / "ci.yml"
STACK = DEPLOY / "stack.yml"
ARCHITECTURE = ROOT / "doc" / "03-architecture.md"
RUNBOOK = ROOT / "doc" / "runbook.md"
# a tag with an exact version (e.g. 18.7, v1.22.0, 2026.10, 3.15.2-slim), optionally a digest
PINNED = re.compile(r"[\w./-]+:v?\d+(\.\d+)+(-[a-z0-9]+)?(@sha256:[0-9a-f]{64})?")


def _ci_steps() -> list[dict[str, object]]:
    steps: list[dict[str, object]] = yaml.safe_load(text(CI))["jobs"]["checks"]["steps"]
    return steps


def test_the_image_runs_the_development_python() -> None:
    assert re.search(rf"^FROM python:{re.escape(python_version())}-slim\b", text(DOCKERFILE), re.M)


def test_ci_reads_python_from_python_version_file() -> None:
    uses = "actions/setup-python@"
    setup = next(s for s in _ci_steps() if str(s.get("uses", "")).startswith(uses))
    assert setup["with"] == {"python-version-file": ".python-version"}


def test_the_image_copies_the_pinned_uv() -> None:
    copy = rf"^COPY --from=ghcr\.io/astral-sh/uv:{re.escape(uv_version())} "
    assert re.search(copy, text(DOCKERFILE), re.M)


def test_ci_installs_uv_from_required_version_without_its_own_pin() -> None:
    commands = " ".join(str(s.get("run", "")) for s in _ci_steps())
    assert "required-version" in commands
    assert not re.search(r"uv==\d", commands)


def test_every_foundryctl_mention_names_the_casting_version() -> None:
    expected = foundry_image()
    sources = [RUNBOOK, CASTING, *SIGNOZ.glob("*.md"), *(ROOT / "doc").glob("*.md")]
    found = {
        path.relative_to(ROOT).as_posix(): mention
        for path in sources
        for mention in re.findall(r"signoz/foundryctl:v[\d.]+\d", text(path))
    }
    assert found, "the runbook and the casting name the foundryctl image"
    assert {path: mention for path, mention in found.items() if mention != expected} == {}


@pytest.mark.parametrize("component", sorted(casting_specs()))
def test_casting_version_matches_its_image_tag(component: str) -> None:
    # Foundry writes `version` but does not apply it to the image: both are pinned, and agree
    spec = casting_specs()[component]
    tag = spec["image"].rsplit(":", 1)[1]
    assert str(spec["version"]) == tag


def test_architecture_table_lists_the_stack_images() -> None:
    rows = dict(re.findall(r"^\| `([\w-]+)` \| `([^`]+:[^`]+)` \|", text(ARCHITECTURE), re.M))
    assert rows == compose_images(STACK)


@pytest.mark.parametrize(
    "path",
    [
        STACK,
        ROOT / "lab" / "freqtrade" / "docker-compose.yml",
        SIGNOZ / "pours" / "deployment" / "compose.yaml",
    ],
    ids=lambda path: path.relative_to(ROOT).as_posix(),
)
def test_compose_images_are_pinned(path: Path) -> None:
    images = compose_images(path)
    assert images
    assert {name: ref for name, ref in images.items() if not PINNED.fullmatch(ref)} == {}


def test_signoz_lock_images_are_pinned() -> None:
    images = re.findall(r"^\s+image: (\S+)$", text(SIGNOZ / "casting.yaml.lock"), re.M)
    assert images
    assert [ref for ref in images if not PINNED.fullmatch(ref)] == []


def test_dockerfile_references_are_pinned() -> None:
    dockerfile = text(DOCKERFILE)
    refs = re.findall(r"^# syntax=(\S+)", dockerfile, re.M)
    refs += re.findall(r"^FROM (\S+)", dockerfile, re.M)
    refs += re.findall(r"^COPY --from=(\S+)", dockerfile, re.M)
    assert len(refs) >= 3
    assert [ref for ref in refs if not PINNED.fullmatch(ref)] == []


def test_actions_are_pinned_by_commit_with_their_version() -> None:
    uses = re.findall(r"uses: (\S+)(.*)$", text(CI), re.M)
    assert uses
    loose = [ref for ref, comment in uses if not re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref)]
    unnamed = [ref for ref, comment in uses if not re.fullmatch(r"\s+# v\d+(\.\d+)+", comment)]
    assert (loose, unnamed) == ([], [])
