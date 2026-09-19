"""Exception hierarchy for Jira and MCP tool error mapping.

`raise_for_response` is the only place HTTP status codes are mapped to
exception types; `jira.py` calls it instead of reimplementing the mapping.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx2

API_TOKEN_AUTH_HINT = "Check JIRA_EMAIL and JIRA_API_TOKEN."
OAUTH_AUTH_HINT = "Run `jira-mini-mcp login` to authorize again."

_URL_PATTERN = re.compile(r"(?:https?|ftp)://\S+", re.IGNORECASE)


class JiraMiniError(Exception):
    """Base class for all jira-mini-mcp errors.

    Never carries a URL, credential, header value, or raw response body.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        issue_key: str | None = None,
        status_code: int | None = None,
        jira_error_code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.operation = operation
        self.issue_key = issue_key
        self.status_code = status_code
        self.jira_error_code = jira_error_code

    def __str__(self) -> str:
        return self.message


class JiraAuthenticationError(JiraMiniError):
    """Jira rejected the configured credentials (HTTP 401)."""


class JiraPermissionError(JiraMiniError):
    """The authenticated user lacks permission for the operation (HTTP 403)."""


class JiraNotFoundError(JiraMiniError):
    """The requested Jira resource does not exist or is not visible (HTTP 404)."""


class JiraRateLimitError(JiraMiniError):
    """Jira is rate-limiting requests (HTTP 429)."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        issue_key: str | None = None,
        status_code: int | None = None,
        jira_error_code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(
            message,
            operation=operation,
            issue_key=issue_key,
            status_code=status_code,
            jira_error_code=jira_error_code,
        )
        self.retry_after = retry_after


class JiraValidationError(JiraMiniError):
    """The request was invalid, locally or as reported by Jira (other 4xx)."""


class JiraTimeoutError(JiraMiniError):
    """The request to Jira timed out."""


class JiraServerError(JiraMiniError):
    """Jira reported an internal server error (5xx)."""


class JiraNetworkError(JiraMiniError):
    """A network/transport failure occurred while contacting Jira."""


class JiraIncompleteResponseError(JiraMiniError):
    """Jira returned malformed known resources alongside useful clean data."""

    def __init__(
        self,
        message: str,
        *,
        problems: tuple[str, ...] = (),
        partial_result: dict[str, Any] | None = None,
        operation: str | None = None,
        issue_key: str | None = None,
    ) -> None:
        super().__init__(message, operation=operation, issue_key=issue_key)
        self.problems = problems
        self.partial_result = partial_result or {}


def _extract_jira_detail(response: httpx2.Response) -> str | None:
    """Pull human-readable text out of Jira's error JSON body, if any.

    Only extracted text strings are used; the raw body is never surfaced,
    and any URL inside those strings is replaced. Jira's own error text can
    carry links, and a link carries the tenant's host -- which this
    project never puts in front of a model.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None

    parts: list[str] = []
    error_messages = body.get("errorMessages")
    if isinstance(error_messages, list):
        parts.extend(str(m) for m in error_messages if isinstance(m, str))
    field_errors = body.get("errors")
    if isinstance(field_errors, dict):
        parts.extend(
            f"{field}: {text}" for field, text in field_errors.items() if isinstance(text, str)
        )
    if not parts:
        return None
    return _URL_PATTERN.sub("[link removed]", "; ".join(parts))


def retry_after_seconds(response: httpx2.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def raise_for_response(
    response: httpx2.Response,
    *,
    operation: str,
    issue_key: str | None = None,
    auth_hint: str = API_TOKEN_AUTH_HINT,
) -> None:
    """Raise the JiraMiniError subclass matching `response`'s status code.

    Does nothing for a successful (< 400) response. `auth_hint` completes
    the 401 message with how to fix the credentials of the active auth
    method.
    """
    status = response.status_code
    if status < 400:
        return

    detail = _extract_jira_detail(response)

    if status == 401:
        message = f"Jira rejected the request credentials. {auth_hint}"
        raise JiraAuthenticationError(
            message, operation=operation, issue_key=issue_key, status_code=status
        )

    if status == 403:
        message = f"Permission denied for operation '{operation}'."
        if detail:
            message = f"{message} {detail}"
        raise JiraPermissionError(
            message, operation=operation, issue_key=issue_key, status_code=status
        )

    if status == 404:
        target = f" '{issue_key}'" if issue_key else ""
        message = f"Jira resource{target} was not found for operation '{operation}'."
        raise JiraNotFoundError(
            message, operation=operation, issue_key=issue_key, status_code=status
        )

    if status == 429:
        retry_after = retry_after_seconds(response)
        message = f"Jira rate-limited operation '{operation}'."
        message = (
            f"{message} Retry after {retry_after:.0f} seconds."
            if retry_after is not None
            else f"{message} Retry after a short delay."
        )
        raise JiraRateLimitError(
            message,
            operation=operation,
            issue_key=issue_key,
            status_code=status,
            retry_after=retry_after,
        )

    if status >= 500:
        message = f"Jira is currently unavailable for operation '{operation}'. Retry later."
        raise JiraServerError(message, operation=operation, issue_key=issue_key, status_code=status)

    message = f"Jira rejected operation '{operation}' as invalid."
    if detail:
        message = f"{message} {detail}"
    raise JiraValidationError(message, operation=operation, issue_key=issue_key, status_code=status)
