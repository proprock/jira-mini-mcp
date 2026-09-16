"""Startup configuration loading and Jira Cloud Basic authentication.

Exactly three settings are supported: JIRA_BASE_URL, JIRA_EMAIL, and
JIRA_API_TOKEN. Credential values never leave this module in an error,
log message, or repr.
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


class BasicTokenAuth(httpx2.BasicAuth):
    """Jira Cloud Basic authentication from an email and an API token."""

    def __init__(self, email: str, api_token: str) -> None:
        super().__init__(email, api_token)
