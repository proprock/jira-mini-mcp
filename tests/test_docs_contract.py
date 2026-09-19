"""Automated comparison: the six tools, and their default fields, as actually
registered in server.py/jira.py must match PROJECT-CONTRACTS.md and docs/tools.md,
and every setting the server reads must be documented.

This does not replace human review of prose -- it catches the concrete,
regression-prone drift: a tool renamed/added/removed, or a default field list
edited in code without updating the two documents that promise it publicly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jira_mini_mcp.jira import ISSUE_DEFAULT_FIELDS, SEARCH_DEFAULT_FIELDS

pytestmark = [pytest.mark.anyio, pytest.mark.hygiene]

ROOT = Path(__file__).resolve().parent.parent
PROJECT_CONTRACTS = (ROOT / "PROJECT-CONTRACTS.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
TOOLS_DOC = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
CONFIGURATION_DOC = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
SERVER_JSON = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

SETTINGS = (
    "JIRA_BASE_URL",
    "JIRA_AUTH_METHOD",
    "JIRA_EMAIL",
    "JIRA_API_TOKEN",
    "JIRA_OAUTH_CLIENT_ID",
    "JIRA_OAUTH_CLIENT_SECRET",
    "READ_ONLY_MODE",
    "STRUCTURED_OUTPUT",
    "DISABLE_STRUCTURED_OUTPUT",
)

TOOL_NAMES = frozenset(
    {
        "search_issues",
        "get_issue",
        "get_comments",
        "get_attachments",
        "download_attachment",
        "get_changelog",
        "add_comment",
        "transition_issue",
        "update_issue",
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


def test_search_default_fields_match_project_contracts() -> None:
    block = _fenced_block_after(PROJECT_CONTRACTS, "When `fields` is omitted, use exactly:")
    assert _csv_fields(block) == SEARCH_DEFAULT_FIELDS


def test_issue_default_fields_match_project_contracts() -> None:
    block = _fenced_block_after(
        PROJECT_CONTRACTS, "When `fields` is omitted, use exactly:", occurrence=2
    )
    assert _csv_fields(block) == ISSUE_DEFAULT_FIELDS


def test_search_default_fields_match_tools_doc() -> None:
    block = _fenced_block_after(TOOLS_DOC, "defaults to these seven fields:")
    assert _csv_fields(block) == SEARCH_DEFAULT_FIELDS


def test_issue_default_fields_match_tools_doc() -> None:
    block = _fenced_block_after(TOOLS_DOC, "defaults to these sixteen fields:")
    assert _csv_fields(block) == ISSUE_DEFAULT_FIELDS


def test_settings_read_by_the_server_are_the_documented_ones() -> None:
    source = (ROOT / "src" / "jira_mini_mcp" / "auth.py").read_text(encoding="utf-8")
    read = set(re.findall(r'"((?:JIRA|READ|STRUCTURED|DISABLE)_[A-Z_]+)"', source))
    assert read == set(SETTINGS)


@pytest.mark.parametrize("setting", SETTINGS)
def test_every_setting_is_in_the_configuration_doc_and_server_json(setting: str) -> None:
    assert f"| `{setting}` |" in CONFIGURATION_DOC
    registry_names = {
        variable["name"]
        for package in SERVER_JSON["packages"]
        for variable in package.get("environmentVariables", [])
    }
    assert setting in registry_names


def test_readme_links_into_docs_resolve() -> None:
    prefix = "https://github.com/proprock/jira-mini-mcp/blob/master/"
    targets = set(re.findall(re.escape(prefix) + r"([\w./-]+?)(?:#[\w-]+)?\)", README))
    assert {"docs/configuration.md", "docs/oauth.md", "docs/tools.md"} <= targets
    for target in targets:
        assert (ROOT / target).exists(), target
