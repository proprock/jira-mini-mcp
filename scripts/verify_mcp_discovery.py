"""Verify that a runnable jira-mini-mcp command exposes exactly the published tools.

Used by CI's packaging job to check a clean-environment wheel/sdist install,
and for manual verification of `uvx --from git+https://github.com/proprock/jira-mini-mcp`
after a branch or tag reaches GitHub (see PLAN.agents.md Phase 9). Never
contacts Jira: tool discovery only needs the MCP lifespan to start, not a
live request, so synthetic configuration values are sufficient.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import Client, StdioServerParameters

EXPECTED_TOOLS = frozenset(
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

_SYNTHETIC_ENV = {
    "JIRA_BASE_URL": "https://synthetic-tenant.atlassian.net",
    "JIRA_EMAIL": "agent@example.com",
    "JIRA_API_TOKEN": "super-secret-token",
}


async def _verify(command: str, args: list[str]) -> None:
    params = StdioServerParameters(command=command, args=args, env=_SYNTHETIC_ENV)
    async with Client(params) as client:
        tools = {tool.name for tool in (await client.list_tools()).tools}
    if tools != EXPECTED_TOOLS:
        raise SystemExit(f"tool discovery mismatch: got {sorted(tools)}")
    invocation = " ".join([command, *args])
    print(f"OK: `{invocation}` exposes exactly the {len(EXPECTED_TOOLS)} expected tools")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(f"usage: {sys.argv[0]} <command> [args...]")
    asyncio.run(_verify(sys.argv[1], sys.argv[2:]))


if __name__ == "__main__":
    main()
