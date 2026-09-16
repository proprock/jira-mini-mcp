"""Automated comparison: the six tools, and their default fields, as actually
registered in server.py/jira.py must match PROJECT-CONTRACTS.md and README.md.

This does not replace human review of prose -- it catches the concrete,
regression-prone drift: a tool renamed/added/removed, or a default field list
edited in code without updating the two documents that promise it publicly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mcp import Client

from jira_mini_mcp.jira import ISSUE_DEFAULT_FIELDS, SEARCH_DEFAULT_FIELDS
from jira_mini_mcp.server import create_server

pytestmark = pytest.mark.anyio

ROOT = Path(__file__).resolve().parent.parent
PROJECT_CONTRACTS = (ROOT / "PROJECT-CONTRACTS.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")

TOOL_NAMES = frozenset(
    {
        "search_issues",
        "get_issue",
        "get_comments",
        "get_attachments",
        "download_attachment",
        "get_changelog",
    }
)


def _fenced_block_after(text: str, anchor: str, occurrence: int = 1) -> str:
    """The contents of the ```-fenced block after the Nth occurrence of `anchor`."""
    pos = 0
    for _ in range(occurrence):
        pos = text.index(anchor, pos) + len(anchor)
    match = re.search(r"```(?:text|json)?\n(.*?)\n```", text[pos:], re.DOTALL)
    assert match is not None, f"no fenced block found after occurrence {occurrence} of {anchor!r}"
    return match.group(1)


def _csv_fields(block: str) -> tuple[str, ...]:
    return tuple(field.strip() for field in block.replace("\n", " ").split(",") if field.strip())


async def test_registered_tool_names_match_project_contracts_tool_list() -> None:
    documented = set(_fenced_block_after(PROJECT_CONTRACTS, "exactly these tools:").split("\n"))
    assert documented == TOOL_NAMES


async def test_registered_tool_names_match_readme_tool_table() -> None:
    referenced_in_readme = {name for name in TOOL_NAMES if f"`{name}`" in README}
    assert referenced_in_readme == TOOL_NAMES


async def test_registered_tool_names_match_actual_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIRA_BASE_URL", "https://synthetic-tenant.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "agent@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "super-secret-token")

    server = create_server()
    async with Client(server) as client:
        actual = {tool.name for tool in (await client.list_tools()).tools}
    assert actual == TOOL_NAMES


def test_search_default_fields_match_project_contracts() -> None:
    block = _fenced_block_after(PROJECT_CONTRACTS, "When `fields` is omitted, use exactly:")
    assert _csv_fields(block) == SEARCH_DEFAULT_FIELDS


def test_issue_default_fields_match_project_contracts() -> None:
    block = _fenced_block_after(
        PROJECT_CONTRACTS, "When `fields` is omitted, use exactly:", occurrence=2
    )
    assert _csv_fields(block) == ISSUE_DEFAULT_FIELDS


def test_search_default_fields_match_readme() -> None:
    block = _fenced_block_after(README, "defaults to these seven fields:")
    assert _csv_fields(block) == SEARCH_DEFAULT_FIELDS


def test_issue_default_fields_match_readme() -> None:
    block = _fenced_block_after(README, "defaults to these sixteen fields:")
    assert _csv_fields(block) == ISSUE_DEFAULT_FIELDS
