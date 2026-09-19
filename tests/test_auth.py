"""Tests for jira_mini_mcp.auth: config loading and Basic authentication."""

from __future__ import annotations

import base64

import httpx2
import pytest

from jira_mini_mcp import auth

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
        assert isinstance(config, auth.JiraConfig)
        assert config.base_url == "https://example.atlassian.net"
        assert config.email == "developer@example.com"
        assert config.api_token == "super-secret-token"

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
        assert isinstance(config, auth.JiraConfig)
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

    def test_invalid_value_error_never_echoes_credentials(self) -> None:
        env = dict(VALID_ENV, READ_ONLY_MODE="maybe")
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_read_only_mode(env=env)

        message = str(exc_info.value)
        assert "super-secret-token" not in message
        assert "developer@example.com" not in message
        assert "example.atlassian.net" not in message


class TestLoadStructuredOutput:
    """STRUCTURED_OUTPUT is MCP response-shape behavior, off unless asked for."""

    def test_absent_variable_leaves_it_off(self) -> None:
        assert auth.load_structured_output(env=VALID_ENV) is False

    @pytest.mark.parametrize("raw", ["true", "TRUE", "1", "on", " On "])
    def test_recognized_true_spellings_turn_it_on(self, raw: str) -> None:
        assert auth.load_structured_output(env=dict(VALID_ENV, STRUCTURED_OUTPUT=raw)) is True

    @pytest.mark.parametrize("raw", ["false", "False", "0", "off", "OFF", "", "   "])
    def test_recognized_false_spellings_and_empty_leave_it_off(self, raw: str) -> None:
        assert auth.load_structured_output(env=dict(VALID_ENV, STRUCTURED_OUTPUT=raw)) is False

    @pytest.mark.parametrize("raw", ["yes", "no", "enabled", "2", "tru e"])
    def test_unrecognized_value_names_itself_the_setting_and_the_spellings(self, raw: str) -> None:
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_structured_output(env=dict(VALID_ENV, STRUCTURED_OUTPUT=raw))

        message = str(exc_info.value)
        assert "STRUCTURED_OUTPUT" in message
        assert raw in message
        for spelling in ("true", "1", "on", "false", "0", "off"):
            assert spelling in message


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

    def test_invalid_value_error_never_echoes_credentials(self) -> None:
        env = dict(VALID_ENV, DISABLE_STRUCTURED_OUTPUT="not_a_tool")
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_disable_structured_output(VALID_TOOL_NAMES, env=env)

        message = str(exc_info.value)
        assert "super-secret-token" not in message
        assert "developer@example.com" not in message
        assert "example.atlassian.net" not in message


OAUTH_ENV = {
    "JIRA_AUTH_METHOD": "oauth",
    "JIRA_BASE_URL": "https://example.atlassian.net",
    "JIRA_OAUTH_CLIENT_ID": "synthetic-client-id",
    "JIRA_OAUTH_CLIENT_SECRET": "synthetic-client-secret",
}


class TestAuthMethod:
    @pytest.mark.parametrize("raw", [None, "", "  ", "api_token", "API_TOKEN", " Api_Token "])
    def test_api_token_is_the_default(self, raw: str | None) -> None:
        env = {} if raw is None else {"JIRA_AUTH_METHOD": raw}
        assert auth.load_auth_method(env) == "api_token"

    @pytest.mark.parametrize("raw", ["oauth", "OAuth", " OAUTH "])
    def test_oauth_spellings(self, raw: str) -> None:
        assert auth.load_auth_method({"JIRA_AUTH_METHOD": raw}) == "oauth"

    def test_unknown_value_is_a_config_error(self) -> None:
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_auth_method({"JIRA_AUTH_METHOD": "oauth2"})
        message = str(exc_info.value)
        assert "'oauth2'" in message
        assert "api_token" in message
        assert "oauth" in message

    def test_oauth_config_needs_no_email_or_token(self) -> None:
        config = auth.load_config_from_env(env=OAUTH_ENV)
        assert config == auth.OAuthConfig(
            base_url="https://example.atlassian.net",
            client_id="synthetic-client-id",
            client_secret="synthetic-client-secret",
        )

    @pytest.mark.parametrize(
        "missing_var", ["JIRA_BASE_URL", "JIRA_OAUTH_CLIENT_ID", "JIRA_OAUTH_CLIENT_SECRET"]
    )
    def test_oauth_missing_variable_is_named(self, missing_var: str) -> None:
        env = dict(OAUTH_ENV)
        del env[missing_var]
        with pytest.raises(auth.ConfigError) as exc_info:
            auth.load_config_from_env(env=env)
        message = str(exc_info.value)
        assert missing_var in message
        assert "synthetic-client" not in message

    def test_configs_hide_credentials_in_repr(self) -> None:
        assert "super-secret-token" not in repr(auth.load_config_from_env(env=VALID_ENV))
        assert "developer@example.com" not in repr(auth.load_config_from_env(env=VALID_ENV))
        assert "synthetic-client" not in repr(auth.load_config_from_env(env=OAUTH_ENV))
