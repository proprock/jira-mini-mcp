"""Tests for jira_mini_mcp.auth: config loading and Basic authentication."""

from __future__ import annotations

import base64

import httpx2
import pytest

from jira_mini_mcp import auth, errors

VALID_ENV = {
    "JIRA_BASE_URL": "https://example.atlassian.net",
    "JIRA_EMAIL": "developer@example.com",
    "JIRA_API_TOKEN": "super-secret-token",
}

VALID_TOOL_NAMES = frozenset(
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


class TestLoadConfigFromEnv:
    def test_loads_exact_three_variables(self) -> None:
        config = auth.load_config_from_env(env=VALID_ENV)
        assert config.base_url == "https://example.atlassian.net"
        assert config.email == "developer@example.com"
        assert config.api_token == "super-secret-token"

    def test_config_is_immutable(self) -> None:
        config = auth.load_config_from_env(env=VALID_ENV)
        with pytest.raises(Exception):  # noqa: B017 - dataclasses.FrozenInstanceError
            config.api_token = "changed"  # ty: ignore[invalid-assignment]

    @pytest.mark.parametrize("missing_var", ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
    def test_missing_variable_raises_actionable_error(self, missing_var: str) -> None:
        env = dict(VALID_ENV)
        del env[missing_var]
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_config_from_env(env=env)
        assert missing_var in str(exc_info.value)

    @pytest.mark.parametrize("empty_var", ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"])
    def test_empty_variable_raises_actionable_error(self, empty_var: str) -> None:
        env = dict(VALID_ENV)
        env[empty_var] = ""
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_config_from_env(env=env)
        assert empty_var in str(exc_info.value)

    def test_config_error_is_a_jira_mini_error(self) -> None:
        assert issubclass(auth.ConfigError, errors.JiraMiniError)

    def test_missing_value_error_never_echoes_configured_values(self) -> None:
        env = dict(VALID_ENV)
        del env["JIRA_API_TOKEN"]
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_config_from_env(env=env)
        message = str(exc_info.value)
        assert "super-secret-token" not in message
        assert "developer@example.com" not in message
        assert "example.atlassian.net" not in message

    def test_unrelated_env_variables_do_not_change_behavior(self) -> None:
        env = dict(VALID_ENV)
        env["UNRELATED_VAR"] = "whatever"
        env["JIRA_PAT"] = "should-be-ignored"
        config = auth.load_config_from_env(env=env)
        assert config.base_url == VALID_ENV["JIRA_BASE_URL"]
        assert config.email == VALID_ENV["JIRA_EMAIL"]
        assert config.api_token == VALID_ENV["JIRA_API_TOKEN"]


class TestBasicTokenAuth:
    def test_produces_correct_basic_encoding(self) -> None:
        basic_auth = auth.BasicTokenAuth("developer@example.com", "super-secret-token")
        request = httpx2.Request("GET", "https://example.atlassian.net/rest/api/3/myself")
        flow = basic_auth.auth_flow(request)
        authed_request = next(flow)

        expected = base64.b64encode(b"developer@example.com:super-secret-token").decode()
        assert authed_request.headers["Authorization"] == f"Basic {expected}"

    def test_repr_does_not_leak_credentials(self) -> None:
        basic_auth = auth.BasicTokenAuth("developer@example.com", "super-secret-token")
        text = repr(basic_auth)
        assert "super-secret-token" not in text
        assert "developer@example.com" not in text


class TestLoadReadOnlyMode:
    """READ_ONLY_MODE is MCP registration behavior, not a Jira credential:
    it is parsed beside JiraConfig, never inside it."""

    def test_absent_variable_disables_the_mode(self) -> None:
        assert auth.load_read_only_mode(env=VALID_ENV) is False

    @pytest.mark.parametrize("raw", ["true", "TRUE", "True", "1", "on", "ON", " true ", "\ton\n"])
    def test_recognized_true_spellings_enable_the_mode(self, raw: str) -> None:
        env = dict(VALID_ENV, READ_ONLY_MODE=raw)
        assert auth.load_read_only_mode(env=env) is True

    @pytest.mark.parametrize(
        "raw", ["false", "FALSE", "False", "0", "off", "OFF", "", "   ", " off "]
    )
    def test_recognized_false_spellings_and_empty_disable_the_mode(self, raw: str) -> None:
        env = dict(VALID_ENV, READ_ONLY_MODE=raw)
        assert auth.load_read_only_mode(env=env) is False

    @pytest.mark.parametrize("raw", ["yes", "no", "enabled", "read-only", "2", "tru e"])
    def test_unrecognized_value_raises_config_error_naming_it_and_the_spellings(
        self, raw: str
    ) -> None:
        env = dict(VALID_ENV, READ_ONLY_MODE=raw)
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_read_only_mode(env=env)

        message = str(exc_info.value)
        assert "READ_ONLY_MODE" in message
        assert raw in message
        for spelling in ("true", "1", "on", "false", "0", "off"):
            assert spelling in message

    def test_config_load_is_unaffected_by_the_variable(self) -> None:
        env = dict(VALID_ENV, READ_ONLY_MODE="true")
        config = auth.load_config_from_env(env=env)
        assert config.base_url == VALID_ENV["JIRA_BASE_URL"]
        assert not hasattr(config, "read_only_mode")

    def test_invalid_value_error_never_echoes_credentials(self) -> None:
        env = dict(VALID_ENV, READ_ONLY_MODE="maybe")
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_read_only_mode(env=env)

        message = str(exc_info.value)
        assert "super-secret-token" not in message
        assert "developer@example.com" not in message
        assert "example.atlassian.net" not in message


class TestLoadDisableStructuredOutput:
    """DISABLE_STRUCTURED_OUTPUT is MCP response-shape behavior, not a Jira
    credential: it is parsed beside JiraConfig, never inside it."""

    def test_absent_variable_disables_nothing(self) -> None:
        result = auth.load_disable_structured_output(VALID_TOOL_NAMES, env=VALID_ENV)
        assert result == frozenset()

    @pytest.mark.parametrize("raw", ["", "   "])
    def test_empty_variable_disables_nothing(self, raw: str) -> None:
        env = dict(VALID_ENV, DISABLE_STRUCTURED_OUTPUT=raw)
        result = auth.load_disable_structured_output(VALID_TOOL_NAMES, env=env)
        assert result == frozenset()

    def test_single_valid_name(self) -> None:
        env = dict(VALID_ENV, DISABLE_STRUCTURED_OUTPUT="add_comment")
        result = auth.load_disable_structured_output(VALID_TOOL_NAMES, env=env)
        assert result == frozenset({"add_comment"})

    def test_multiple_valid_names_with_surrounding_whitespace(self) -> None:
        env = dict(VALID_ENV, DISABLE_STRUCTURED_OUTPUT=" add_comment, transition_issue ")
        result = auth.load_disable_structured_output(VALID_TOOL_NAMES, env=env)
        assert result == frozenset({"add_comment", "transition_issue"})

    def test_unknown_name_raises_config_error_naming_it_and_the_valid_names(
        self,
    ) -> None:
        env = dict(VALID_ENV, DISABLE_STRUCTURED_OUTPUT="not_a_tool")
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_disable_structured_output(VALID_TOOL_NAMES, env=env)

        message = str(exc_info.value)
        assert "not_a_tool" in message
        for name in VALID_TOOL_NAMES:
            assert name in message

    def test_multiple_unknown_names_are_all_reported(self) -> None:
        env = dict(VALID_ENV, DISABLE_STRUCTURED_OUTPUT="not_a_tool,also_not_a_tool")
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_disable_structured_output(VALID_TOOL_NAMES, env=env)

        message = str(exc_info.value)
        assert "not_a_tool" in message
        assert "also_not_a_tool" in message

    def test_wrong_case_name_is_treated_as_unknown(self) -> None:
        env = dict(VALID_ENV, DISABLE_STRUCTURED_OUTPUT="Add_Comment")
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_disable_structured_output(VALID_TOOL_NAMES, env=env)
        assert "Add_Comment" in str(exc_info.value)

    def test_config_load_is_unaffected_by_the_variable(self) -> None:
        env = dict(VALID_ENV, DISABLE_STRUCTURED_OUTPUT="add_comment")
        config = auth.load_config_from_env(env=env)
        assert config.base_url == VALID_ENV["JIRA_BASE_URL"]
        assert not hasattr(config, "disable_structured_output")

    def test_invalid_value_error_never_echoes_credentials(self) -> None:
        env = dict(VALID_ENV, DISABLE_STRUCTURED_OUTPUT="not_a_tool")
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_disable_structured_output(VALID_TOOL_NAMES, env=env)

        message = str(exc_info.value)
        assert "super-secret-token" not in message
        assert "developer@example.com" not in message
        assert "example.atlassian.net" not in message
