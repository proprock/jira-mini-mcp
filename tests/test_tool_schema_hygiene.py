"""Deterministic half of the tool-selection eval.

README.md claims a small, clearly differentiated toolset makes an agent's
tool choice more reliable. The scenario half of that claim needs a model and
lives in `evals/`, outside the default suite. What can be checked offline is
the input the model actually sees: that every tool says what it does, that no
two say nearly the same thing, and that a parameter whose behavior cannot be
guessed from its name is explained in the description rather than left to the
schema. Those are the properties a wrong tool choice usually traces back to.
"""

from __future__ import annotations

import re

import pytest
from mcp import Client

from jira_mini_mcp.server import create_server

pytestmark = pytest.mark.anyio

# A parameter whose name fully explains it. Everything else must be named in
# its tool's description: an agent reads prose, and the schema alone does not
# say that `limit=0` means unbounded or that `fields` replaces a default.
_SELF_EXPLANATORY_PARAMS = frozenset({"issue_key", "attachment_id"})

# Measured headroom, not a guess: the closest real pair today is
# get_comments/get_changelog at 0.49, which share pagination vocabulary
# because they share pagination semantics. 0.60 leaves that pair alone while
# catching a genuine near-duplicate.
_MAX_DESCRIPTION_OVERLAP = 0.60

# Behavior an agent gets wrong when it is not spelled out, mapped to the
# wording that spells it out.
_MUST_EXPLAIN = {
    "search_issues": ["1..100", "invalid", "replaces"],
    "get_issue": ["replaces", "fields=[]"],
    "get_comments": ["limit=0", "newest", "since"],
    "get_changelog": ["limit=0", "newest"],
    "download_attachment": ["temporary", "shuts down"],
    "add_comment": ["Markdown", "cannot edit or delete"],
    "transition_issue": ["transition name", "lists every"],
    "update_issue": ["REPLACE", "transition_issue", "add_comment"],
}


def _significant_words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z_]+", text.lower()) if len(word) > 2}


def _overlap(first: str, second: str) -> float:
    left, right = _significant_words(first), _significant_words(second)
    return len(left & right) / len(left | right)


@pytest.fixture
async def tools(monkeypatch: pytest.MonkeyPatch) -> list:
    monkeypatch.setenv("JIRA_BASE_URL", "https://synthetic-tenant.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "agent@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "super-secret-token")
    monkeypatch.delenv("READ_ONLY_MODE", raising=False)
    monkeypatch.delenv("DISABLE_STRUCTURED_OUTPUT", raising=False)

    async with Client(create_server()) as client:
        return list((await client.list_tools()).tools)


class TestToolSchemaHygiene:
    async def test_every_tool_has_a_substantial_description(self, tools: list) -> None:
        for tool in tools:
            assert tool.description, f"{tool.name} has no description"
            assert len(tool.description.split()) >= 15, (
                f"{tool.name}'s description is too thin to choose on"
            )

    async def test_no_two_descriptions_are_near_duplicates(self, tools: list) -> None:
        worst = max(
            (
                (_overlap(a.description or "", b.description or ""), a.name, b.name)
                for index, a in enumerate(tools)
                for b in tools[index + 1 :]
            ),
            default=(0.0, "", ""),
        )
        score, first, second = worst
        assert score < _MAX_DESCRIPTION_OVERLAP, (
            f"{first} and {second} describe themselves too alike ({score:.2f}); "
            "an agent picking between them is guessing"
        )

    async def test_the_closest_pair_still_states_its_own_subject(self, tools: list) -> None:
        # get_comments and get_changelog are the pair most at risk, so each
        # has to name what it returns, not only how it paginates.
        described = {tool.name: (tool.description or "").lower() for tool in tools}
        assert "comment" in described["get_comments"]
        assert "field-change history" in described["get_changelog"]
        assert "comment" not in described["get_changelog"]

    async def test_non_obvious_parameters_are_named_in_the_description(self, tools: list) -> None:
        for tool in tools:
            description = (tool.description or "").lower()
            parameters = set(tool.input_schema.get("properties", {}))
            for name in sorted(parameters - _SELF_EXPLANATORY_PARAMS):
                assert name.lower() in description, (
                    f"{tool.name}'s description never mentions '{name}', so its "
                    "contract exists only in the schema"
                )

    @pytest.mark.parametrize(("tool_name", "phrases"), sorted(_MUST_EXPLAIN.items()))
    async def test_surprising_behavior_is_spelled_out(
        self, tools: list, tool_name: str, phrases: list[str]
    ) -> None:
        tool = next(item for item in tools if item.name == tool_name)
        description = tool.description or ""
        for phrase in phrases:
            assert phrase in description, f"{tool_name} does not tell the caller about '{phrase}'"

    async def test_every_tool_is_covered_by_the_expectations_above(self, tools: list) -> None:
        # A new tool must be considered here rather than slipping in
        # undescribed; get_attachments is listed as deliberately plain.
        considered = set(_MUST_EXPLAIN) | {"get_attachments"}
        assert {tool.name for tool in tools} == considered
