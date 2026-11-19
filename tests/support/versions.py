"""Where each pinned version lives, read from the file the tool itself uses.

There is no central versions file: each pin stays in its native source (the one Docker,
Compose, uv, Foundry or GitHub Actions reads), and ``tests/unit/test_version_pins.py`` fails
when a copy elsewhere disagrees with it.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
SIGNOZ = DEPLOY / "signoz"
CASTING = SIGNOZ / "casting.yaml"


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def python_version() -> str:
    """``.python-version``: what uv uses in development and CI reads (``python-version-file``)."""
    return text(ROOT / ".python-version").strip()


def uv_version() -> str:
    """``[tool.uv] required-version`` in ``pyproject.toml``, an exact ``==`` pin."""
    pyproject = tomllib.loads(text(ROOT / "pyproject.toml"))
    required: str = pyproject["tool"]["uv"]["required-version"]
    match = re.fullmatch(r"==(\d+\.\d+\.\d+)", required)
    assert match, f"required-version must be an exact pin, not {required!r}"
    return match.group(1)


def foundry_image() -> str:
    """The foundryctl image of the regeneration command in ``casting.yaml``'s header."""
    match = re.search(r"\bsignoz/foundryctl:v\d+\.\d+\.\d+\b", text(CASTING))
    assert match, "casting.yaml must name the foundryctl image that generates pours/"
    return match.group(0)


def compose_images(path: Path) -> dict[str, str]:
    """``service -> image`` of a compose file, for the services that name an image."""
    services: dict[str, Any] = yaml.safe_load(text(path))["services"]
    return {name: spec["image"] for name, spec in services.items() if "image" in spec}


def casting_specs() -> dict[str, dict[str, Any]]:
    """The components of ``casting.yaml`` that pin a version and an image."""
    spec: dict[str, Any] = yaml.safe_load(text(CASTING))["spec"]
    return {
        name: component["spec"]
        for name, component in spec.items()
        if isinstance(component, dict) and "image" in component.get("spec", {})
    }
