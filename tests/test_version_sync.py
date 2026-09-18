"""Every place that records jira-mini-mcp's current released version --
`pyproject.toml`, both `version` fields in `server.json`, `uv.lock`'s own
package entry, and CHANGELOG.md's latest release heading and compare link --
must agree, so a version bump can't update one and silently miss another.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
SERVER_JSON = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
UV_LOCK = (ROOT / "uv.lock").read_text(encoding="utf-8")

PYPROJECT_VERSION = PYPROJECT["project"]["version"]


def _changelog_latest_released_version() -> str:
    match = re.search(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}", CHANGELOG, re.MULTILINE)
    assert match is not None, "CHANGELOG.md has no released `## [X.Y.Z] - date` heading"
    return match.group(1)


def _changelog_unreleased_compare_base() -> str:
    match = re.search(r"^\[Unreleased\]: .*/compare/v([\d.]+)\.\.\.HEAD$", CHANGELOG, re.MULTILINE)
    assert match is not None, "CHANGELOG.md is missing its `[Unreleased]` compare link"
    return match.group(1)


def _uv_lock_project_version() -> str:
    match = re.search(r'name = "jira-mini-mcp"\nversion = "([\d.]+)"', UV_LOCK)
    assert match is not None, "uv.lock has no jira-mini-mcp package entry"
    return match.group(1)


def test_server_json_top_level_version_matches_pyproject() -> None:
    assert SERVER_JSON["version"] == PYPROJECT_VERSION


def test_server_json_pypi_package_version_matches_pyproject() -> None:
    pypi_packages = [p for p in SERVER_JSON["packages"] if p["registryType"] == "pypi"]
    assert pypi_packages, "server.json has no pypi package entry"
    for package in pypi_packages:
        assert package["version"] == PYPROJECT_VERSION


def test_changelog_latest_release_matches_pyproject() -> None:
    assert _changelog_latest_released_version() == PYPROJECT_VERSION


def test_changelog_unreleased_link_compares_from_latest_release() -> None:
    assert _changelog_unreleased_compare_base() == PYPROJECT_VERSION


def test_uv_lock_version_matches_pyproject() -> None:
    assert _uv_lock_project_version() == PYPROJECT_VERSION
