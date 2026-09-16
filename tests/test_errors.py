"""Tests for the jira_mini_mcp.errors exception hierarchy and raise_for_response."""

from __future__ import annotations

import httpx2
import pytest

from jira_mini_mcp import errors

SECRET_URL = "https://hostile-tenant.atlassian.net/rest/api/3/issue/SECRET-1?token=leak-me"
SECRET_HEADER = "Basic dXNlckBleGFtcGxlLmNvbTpzdXBlci1zZWNyZXQtdG9rZW4="


def _response(
    status_code: int,
    *,
    json: object | None = None,
    text: str | None = None,
    headers: dict[str, str] | None = None,
    url: str = SECRET_URL,
) -> httpx2.Response:
    request = httpx2.Request("GET", url, headers={"Authorization": SECRET_HEADER})
    if text is not None:
        return httpx2.Response(status_code, text=text, headers=headers, request=request)
    return httpx2.Response(status_code, json=json, headers=headers, request=request)


ALL_EXCEPTION_CLASSES = [
    errors.JiraAuthenticationError,
    errors.JiraPermissionError,
    errors.JiraNotFoundError,
    errors.JiraRateLimitError,
    errors.JiraValidationError,
    errors.JiraTimeoutError,
    errors.JiraServerError,
    errors.JiraNetworkError,
]


class TestExceptionHierarchy:
    def test_base_class_is_exception_subclass(self) -> None:
        assert issubclass(errors.JiraMiniError, Exception)

    @pytest.mark.parametrize("exc_class", ALL_EXCEPTION_CLASSES)
    def test_all_subclass_jira_mini_error(self, exc_class: type) -> None:
        assert issubclass(exc_class, errors.JiraMiniError)

    def test_construct_carries_cause_and_correction(self) -> None:
        exc = errors.JiraAuthenticationError(
            "Jira rejected the request credentials. Check JIRA_EMAIL and JIRA_API_TOKEN.",
            operation="get_issue",
            status_code=401,
        )
        assert "rejected" in str(exc)
        assert "JIRA_EMAIL" in str(exc)
        assert exc.operation == "get_issue"
        assert exc.status_code == 401

    def test_rate_limit_carries_retry_after(self) -> None:
        exc = errors.JiraRateLimitError(
            "Jira rate-limited operation 'search_issues'. Retry after 30 seconds.",
            operation="search_issues",
            status_code=429,
            retry_after=30.0,
        )
        assert exc.retry_after == 30.0

    def test_rate_limit_retry_after_defaults_to_none(self) -> None:
        exc = errors.JiraRateLimitError("rate limited", operation="op", status_code=429)
        assert exc.retry_after is None

    @pytest.mark.parametrize("exc_class", ALL_EXCEPTION_CLASSES)
    def test_no_secret_in_message_when_only_message_given(self, exc_class: type) -> None:
        exc = exc_class("a safe actionable message", operation="op", issue_key="ABC-1")
        text = str(exc)
        assert SECRET_URL not in text
        assert SECRET_HEADER not in text

    def test_names_do_not_shadow_builtins(self) -> None:
        assert not issubclass(errors.JiraPermissionError, type(PermissionError()))
        assert not issubclass(errors.JiraTimeoutError, type(TimeoutError()))
        assert errors.JiraPermissionError.__name__ == "JiraPermissionError"
        assert errors.JiraTimeoutError.__name__ == "JiraTimeoutError"


class TestRaiseForResponse:
    def test_2xx_is_passthrough(self) -> None:
        response = _response(200, json={"key": "ABC-1"})
        errors.raise_for_response(response, operation="get_issue")

    def test_401_raises_authentication_error(self) -> None:
        response = _response(401, json={"errorMessages": ["You are not authenticated."]})
        with pytest.raises(errors.JiraAuthenticationError) as exc_info:
            errors.raise_for_response(response, operation="get_issue")
        assert exc_info.value.status_code == 401
        assert "JIRA_EMAIL" in str(exc_info.value) or "JIRA_API_TOKEN" in str(exc_info.value)

    def test_403_raises_permission_error(self) -> None:
        response = _response(403, json={"errorMessages": ["You do not have permission."]})
        with pytest.raises(errors.JiraPermissionError) as exc_info:
            errors.raise_for_response(response, operation="get_comments", issue_key="ABC-1")
        assert exc_info.value.status_code == 403
        assert exc_info.value.issue_key == "ABC-1"

    def test_404_raises_not_found_error(self) -> None:
        response = _response(404, json={"errorMessages": ["Issue does not exist"]})
        with pytest.raises(errors.JiraNotFoundError) as exc_info:
            errors.raise_for_response(response, operation="get_issue", issue_key="ABC-404")
        assert exc_info.value.status_code == 404
        assert "ABC-404" in str(exc_info.value)

    def test_429_raises_rate_limit_error_with_retry_after(self) -> None:
        response = _response(
            429,
            json={"errorMessages": ["Too many requests"]},
            headers={"Retry-After": "42"},
        )
        with pytest.raises(errors.JiraRateLimitError) as exc_info:
            errors.raise_for_response(response, operation="search_issues")
        assert exc_info.value.retry_after == 42.0
        assert exc_info.value.status_code == 429

    def test_429_without_retry_after_header(self) -> None:
        response = _response(429, json={"errorMessages": ["Too many requests"]})
        with pytest.raises(errors.JiraRateLimitError) as exc_info:
            errors.raise_for_response(response, operation="search_issues")
        assert exc_info.value.retry_after is None

    def test_other_4xx_raises_validation_error(self) -> None:
        response = _response(400, json={"errors": {"jql": "Field 'foo' does not exist."}})
        with pytest.raises(errors.JiraValidationError) as exc_info:
            errors.raise_for_response(response, operation="search_issues")
        assert exc_info.value.status_code == 400
        assert "foo" in str(exc_info.value)

    def test_5xx_raises_server_error(self) -> None:
        response = _response(503, text="Service Unavailable")
        with pytest.raises(errors.JiraServerError) as exc_info:
            errors.raise_for_response(response, operation="get_issue")
        assert exc_info.value.status_code == 503

    def test_malformed_json_body_does_not_crash(self) -> None:
        response = _response(500, text="<html>not json</html>")
        with pytest.raises(errors.JiraServerError):
            errors.raise_for_response(response, operation="get_issue")

    def test_hostile_headers_and_request_url_never_leak_into_message(self) -> None:
        # The response/request always carry the real Authorization header and
        # tenant URL (set by the _response helper); the JSON body is ordinary
        # Jira error text. Only the body text may appear in the message.
        response = _response(
            403,
            json={"errorMessages": ["Permission denied for this project."]},
            headers={"Set-Cookie": SECRET_HEADER, "X-Auth-Debug": SECRET_HEADER},
            url=SECRET_URL,
        )
        with pytest.raises(errors.JiraPermissionError) as exc_info:
            errors.raise_for_response(response, operation="get_issue")
        message = str(exc_info.value)
        assert SECRET_URL not in message
        assert SECRET_HEADER not in message

    def test_exception_repr_does_not_leak_request_url(self) -> None:
        response = _response(404, json={"errorMessages": ["not found"]})
        with pytest.raises(errors.JiraNotFoundError) as exc_info:
            errors.raise_for_response(response, operation="get_issue", issue_key="ABC-1")
        assert SECRET_URL not in repr(exc_info.value)
        assert SECRET_HEADER not in repr(exc_info.value)

    def test_non_dict_json_body_does_not_crash(self) -> None:
        response = _response(403, json=["unexpected", "array", "body"])
        with pytest.raises(errors.JiraPermissionError) as exc_info:
            errors.raise_for_response(response, operation="get_issue")
        assert exc_info.value.status_code == 403

    def test_non_numeric_retry_after_header_is_ignored(self) -> None:
        response = _response(
            429,
            json={"errorMessages": ["Too many requests"]},
            headers={"Retry-After": "not-a-number"},
        )
        with pytest.raises(errors.JiraRateLimitError) as exc_info:
            errors.raise_for_response(response, operation="search_issues")
        assert exc_info.value.retry_after is None

    def test_403_without_body_detail(self) -> None:
        response = _response(403, json={})
        with pytest.raises(errors.JiraPermissionError) as exc_info:
            errors.raise_for_response(response, operation="get_issue")
        assert "Permission denied" in str(exc_info.value)

    def test_other_4xx_without_body_detail(self) -> None:
        response = _response(409, json={})
        with pytest.raises(errors.JiraValidationError) as exc_info:
            errors.raise_for_response(response, operation="get_issue")
        assert exc_info.value.status_code == 409
