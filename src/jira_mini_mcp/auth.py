"""Startup configuration loading and Jira Cloud Basic authentication.

Exactly three settings are required: JIRA_BASE_URL, JIRA_EMAIL, and
JIRA_API_TOKEN. Credential values never leave this module in an error,
log message, or repr.

READ_ONLY_MODE and DISABLE_STRUCTURED_OUTPUT are optional settings, parsed
here but deliberately kept out of JiraConfig: they select which MCP tools
get registered and how they report their results, which is server behavior
rather than a Jira credential, and JiraClient must stay unaware of both.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

import httpx2

from jira_mini_mcp.errors import JiraMiniError

_REQUIRED_VARS: dict[str, str] = {
    "JIRA_BASE_URL": "your Jira Cloud site URL, e.g. https://your-domain.atlassian.net",
    "JIRA_EMAIL": "the email address associated with your Jira API token",
    "JIRA_API_TOKEN": (
        "a Jira Cloud API token (create one at "
        "https://id.atlassian.com/manage-profile/security/api-tokens)"
    ),
}

_READ_ONLY_MODE_VAR = "READ_ONLY_MODE"
_READ_ONLY_MODE_TRUE = frozenset({"true", "1", "on"})
_READ_ONLY_MODE_FALSE = frozenset({"false", "0", "off", ""})

_DISABLE_STRUCTURED_OUTPUT_VAR = "DISABLE_STRUCTURED_OUTPUT"


class ConfigError(JiraMiniError):
    """Required startup configuration is missing or empty."""


@dataclass(frozen=True)
class JiraConfig:
    """The three settings needed to talk to a Jira Cloud site."""

    base_url: str
    email: str
    api_token: str


def load_config_from_env(env: Mapping[str, str] = os.environ) -> JiraConfig:
    """Validate and load JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN.

    Raises ConfigError naming the first missing or empty variable and how
    to provide it; never echoes any configured value.
    """
    for var_name, guidance in _REQUIRED_VARS.items():
        value = env.get(var_name)
        if not value:
            raise ConfigError(f"{var_name} is not set. Set it to {guidance}.")

    return JiraConfig(
        base_url=env["JIRA_BASE_URL"],
        email=env["JIRA_EMAIL"],
        api_token=env["JIRA_API_TOKEN"],
    )


def load_read_only_mode(env: Mapping[str, str] = os.environ) -> bool:
    """Parse the optional READ_ONLY_MODE switch.

    Absent, empty, and the false spellings all disable it; an unrecognized
    value is a startup error rather than a silent fallback, because
    silently ignoring a typo here would register write tools an operator
    believed they had turned off.
    """
    raw = env.get(_READ_ONLY_MODE_VAR)
    if raw is None:
        return False

    value = raw.strip().lower()
    if value in _READ_ONLY_MODE_TRUE:
        return True
    if value in _READ_ONLY_MODE_FALSE:
        return False

    raise ConfigError(
        f"{_READ_ONLY_MODE_VAR} is set to {raw!r}, which is not a recognized value. "
        "Set it to true, 1, or on to register read-only tools only, or to "
        "false, 0, or off to register every tool (case-insensitive); "
        "leaving it unset also registers every tool."
    )


def load_disable_structured_output(
    valid_tool_names: frozenset[str],
    env: Mapping[str, str] = os.environ,
) -> frozenset[str]:
    """Parse the optional DISABLE_STRUCTURED_OUTPUT comma-separated list.

    Absent or empty disables nothing. Each entry is stripped of surrounding
    whitespace and matched case-sensitively against valid_tool_names; a name
    outside that set is a startup error naming every bad value and every
    valid tool name, never a silently ignored typo.
    """
    raw = env.get(_DISABLE_STRUCTURED_OUTPUT_VAR)
    if not raw or not raw.strip():
        return frozenset()

    names = frozenset(name.strip() for name in raw.split(",") if name.strip())
    unknown = names - valid_tool_names
    if unknown:
        raise ConfigError(
            f"{_DISABLE_STRUCTURED_OUTPUT_VAR} names {sorted(unknown)!r}, which "
            f"{'is' if len(unknown) == 1 else 'are'} not a registered tool "
            f"name. Valid tool names: {', '.join(sorted(valid_tool_names))}."
        )
    return names


class BasicTokenAuth(httpx2.BasicAuth):
    """Jira Cloud Basic authentication from an email and an API token."""

    def __init__(self, email: str, api_token: str) -> None:
        super().__init__(email, api_token)
