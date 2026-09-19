"""Startup configuration loading and Jira Cloud Basic authentication.

JIRA_AUTH_METHOD selects the credentials. `api_token` (the default) needs
exactly JIRA_BASE_URL, JIRA_EMAIL, and JIRA_API_TOKEN; `oauth` needs
JIRA_BASE_URL, JIRA_OAUTH_CLIENT_ID, and JIRA_OAUTH_CLIENT_SECRET, and the
OAuth flow itself lives in `oauth.py`. Credential values never leave this
module in an error, log message, or repr.

READ_ONLY_MODE and DISABLE_STRUCTURED_OUTPUT are optional settings, parsed
here but deliberately kept out of JiraConfig: they select which MCP tools
get registered and how they report their results, which is server behavior
rather than a Jira credential, and JiraClient must stay unaware of both.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

import httpx2

from jira_mini_mcp.errors import JiraMiniError

_BASE_URL_GUIDANCE = "your Jira Cloud site URL, e.g. https://your-domain.atlassian.net"

_REQUIRED_VARS: dict[str, str] = {
    "JIRA_BASE_URL": _BASE_URL_GUIDANCE,
    "JIRA_EMAIL": "the email address associated with your Jira API token",
    "JIRA_API_TOKEN": (
        "a Jira Cloud API token (create one at "
        "https://id.atlassian.com/manage-profile/security/api-tokens)"
    ),
}

_OAUTH_APP_GUIDANCE = (
    "of your OAuth 2.0 (3LO) app (create one at https://developer.atlassian.com/console/myapps/; "
    "see docs/oauth.md)"
)

_OAUTH_REQUIRED_VARS: dict[str, str] = {
    "JIRA_BASE_URL": _BASE_URL_GUIDANCE,
    "JIRA_OAUTH_CLIENT_ID": f"the Client ID {_OAUTH_APP_GUIDANCE}",
    "JIRA_OAUTH_CLIENT_SECRET": f"the Secret {_OAUTH_APP_GUIDANCE}",
}

AuthMethod = Literal["api_token", "oauth"]

_AUTH_METHOD_VAR = "JIRA_AUTH_METHOD"
_AUTH_METHODS: tuple[AuthMethod, ...] = ("api_token", "oauth")

_READ_ONLY_MODE_VAR = "READ_ONLY_MODE"
_READ_ONLY_MODE_TRUE = frozenset({"true", "1", "on"})
_READ_ONLY_MODE_FALSE = frozenset({"false", "0", "off", ""})

_DISABLE_STRUCTURED_OUTPUT_VAR = "DISABLE_STRUCTURED_OUTPUT"


class ConfigError(JiraMiniError):
    """Required startup configuration is missing or empty."""


@dataclass(frozen=True)
class JiraConfig:
    """The three settings needed to talk to a Jira Cloud site with an API token."""

    base_url: str
    email: str = field(repr=False)
    api_token: str = field(repr=False)


@dataclass(frozen=True)
class OAuthConfig:
    """The three settings needed to talk to a Jira Cloud site through OAuth 2.0 (3LO)."""

    base_url: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


def load_auth_method(env: Mapping[str, str] = os.environ) -> AuthMethod:
    """Parse the optional JIRA_AUTH_METHOD switch.

    Absent or empty means `api_token`, so an existing configuration keeps
    working unchanged. Anything else outside the accepted values is a
    startup error rather than a silent fallback to the other method.
    """
    raw = env.get(_AUTH_METHOD_VAR)
    if raw is None or not raw.strip():
        return "api_token"

    value = raw.strip().lower()
    for method in _AUTH_METHODS:
        if value == method:
            return method

    raise ConfigError(
        f"{_AUTH_METHOD_VAR} is set to {raw!r}, which is not a recognized value. "
        "Set it to api_token to authenticate with JIRA_EMAIL and JIRA_API_TOKEN, "
        "or to oauth to authenticate through an OAuth 2.0 (3LO) app "
        "(case-insensitive); leaving it unset means api_token."
    )


def _require(env: Mapping[str, str], required: Mapping[str, str]) -> None:
    for var_name, guidance in required.items():
        if not env.get(var_name):
            raise ConfigError(f"{var_name} is not set. Set it to {guidance}.")


def load_config_from_env(env: Mapping[str, str] = os.environ) -> JiraConfig | OAuthConfig:
    """Validate and load the credentials JIRA_AUTH_METHOD selects.

    Raises ConfigError naming the first missing or empty variable and how
    to provide it; never echoes any configured value.
    """
    if load_auth_method(env) == "oauth":
        return load_oauth_config(env)

    _require(env, _REQUIRED_VARS)
    return JiraConfig(
        base_url=env["JIRA_BASE_URL"],
        email=env["JIRA_EMAIL"],
        api_token=env["JIRA_API_TOKEN"],
    )


def load_oauth_config(env: Mapping[str, str] = os.environ) -> OAuthConfig:
    """Validate and load JIRA_BASE_URL, JIRA_OAUTH_CLIENT_ID, and JIRA_OAUTH_CLIENT_SECRET."""
    _require(env, _OAUTH_REQUIRED_VARS)
    return OAuthConfig(
        base_url=env["JIRA_BASE_URL"],
        client_id=env["JIRA_OAUTH_CLIENT_ID"],
        client_secret=env["JIRA_OAUTH_CLIENT_SECRET"],
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
