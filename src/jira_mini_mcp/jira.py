"""Pure Jira Cloud REST API v3 client logic (no MCP dependencies).

`JiraClient` grows by one method per later phase. All HTTP calls route
through the private `_get` helper, which attaches auth, maps failures via
`errors.raise_for_response`, and returns parsed JSON.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import httpx2

from jira_mini_mcp import errors
from jira_mini_mcp.auth import BasicTokenAuth
from jira_mini_mcp.models import (
    IncompleteNormalizationError,
    IssueDetail,
    IssueSummary,
    SearchPage,
    normalize_issue_fields,
)

SEARCH_DEFAULT_FIELDS: tuple[str, ...] = (
    "summary",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "updated",
    "project",
)

ISSUE_DEFAULT_FIELDS: tuple[str, ...] = (
    "summary",
    "description",
    "issuetype",
    "status",
    "priority",
    "assignee",
    "reporter",
    "labels",
    "components",
    "created",
    "updated",
    "resolutiondate",
    "issuelinks",
    "project",
    "parent",
    "subtasks",
)

# Jira Cloud v3's single-issue GET treats fields="" and fields=-* as
# "unspecified" and returns a tenant- and permission-dependent full field set.
# Observed live on a Jira Cloud site 2026-09-16. The issue `id` lives outside
# the fields object, so using it as a sentinel yields an empty fields object and
# keeps explicit fields=[] from fetching and discarding the full field set.
_GET_ISSUE_EMPTY_FIELDS_SENTINEL = "id"


def _joined_fields(fields: list[str] | None, default: tuple[str, ...]) -> str:
    if fields is None:
        return ",".join(default)
    return ",".join(fields)


def _incomplete_response_error(
    *,
    operation: str,
    problems: list[str],
    partial_result: dict[str, Any],
    issue_key: str | None = None,
) -> errors.JiraIncompleteResponseError:
    details = "; ".join(problems)
    return errors.JiraIncompleteResponseError(
        f"Jira returned incomplete data for operation '{operation}': {details}",
        operation=operation,
        issue_key=issue_key,
        problems=tuple(problems),
        partial_result=partial_result,
    )


class JiraClient:
    """Pure Jira Cloud REST API v3 client. Holds no MCP dependency."""

    def __init__(self, client: httpx2.AsyncClient, auth: BasicTokenAuth, base_url: str) -> None:
        self._client = client
        self._auth = auth
        self._base_url = base_url.rstrip("/")

    async def _get(
        self,
        path: str,
        *,
        operation: str,
        issue_key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        try:
            response = await self._client.get(url, params=params, auth=self._auth)
        except httpx2.TimeoutException as exc:
            raise errors.JiraTimeoutError(
                f"Request timed out for operation '{operation}'. "
                "Check network connectivity and retry.",
                operation=operation,
                issue_key=issue_key,
            ) from exc
        except httpx2.TransportError as exc:
            raise errors.JiraNetworkError(
                f"A network error occurred for operation '{operation}'. "
                "Check connectivity and retry.",
                operation=operation,
                issue_key=issue_key,
            ) from exc

        errors.raise_for_response(response, operation=operation, issue_key=issue_key)

        try:
            return response.json()
        except ValueError as exc:
            raise errors.JiraServerError(
                f"Jira returned a response that could not be parsed for operation '{operation}'.",
                operation=operation,
                issue_key=issue_key,
            ) from exc

    async def search_issues(
        self,
        jql: str,
        page_token: str | None = None,
        limit: int = 20,
        fields: list[str] | None = None,
    ) -> SearchPage[IssueSummary]:
        if not 1 <= limit <= 100:
            raise errors.JiraValidationError(
                "search_issues 'limit' must be a positive value in 1..100.",
                operation="search_issues",
            )

        params: dict[str, Any] = {
            "jql": jql,
            "maxResults": limit,
            "fields": _joined_fields(fields, SEARCH_DEFAULT_FIELDS),
        }
        if page_token is not None:
            params["nextPageToken"] = page_token

        body = await self._get("/rest/api/3/search/jql", operation="search_issues", params=params)

        if not isinstance(body, dict):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'search_issues'.",
                operation="search_issues",
            )

        raw_issues = body.get("issues", [])
        next_page_token = body.get("nextPageToken")
        if not isinstance(raw_issues, list) or not (
            next_page_token is None or isinstance(next_page_token, str)
        ):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'search_issues'.",
                operation="search_issues",
            )

        items: list[IssueSummary] = []
        problems: list[str] = []
        for index, issue in enumerate(raw_issues):
            issue_path = f"$.issues[{index}]"
            if not isinstance(issue, dict):
                problems.append(f"{issue_path}: expected an object")
                continue
            key = issue.get("key")
            if not isinstance(key, str) or not key:
                problems.append(f"{issue_path}.key: expected a non-empty string")
                continue

            raw_fields = issue.get("fields", {})
            if raw_fields is None:
                raw_fields = {}
            if not isinstance(raw_fields, dict):
                problems.append(f"{issue_path}.fields: expected an object")
                normalized_fields: dict[str, Any] = {}
            else:
                try:
                    normalized_fields = normalize_issue_fields(
                        raw_fields, path=f"{issue_path}.fields"
                    )
                except IncompleteNormalizationError as exc:
                    normalized_fields = exc.partial_value
                    problems.extend(str(problem) for problem in exc.problems)
            items.append(IssueSummary(key=key, fields=normalized_fields))

        if problems:
            partial_result = {
                "items": [asdict(item) for item in items],
                "next_page_token": next_page_token,
            }
            raise _incomplete_response_error(
                operation="search_issues",
                problems=problems,
                partial_result=partial_result,
            )

        return SearchPage(items=items, next_page_token=next_page_token)

    async def get_issue(self, issue_key: str, fields: list[str] | None = None) -> IssueDetail:
        if fields is not None and len(fields) == 0:
            fields_param = _GET_ISSUE_EMPTY_FIELDS_SENTINEL
        else:
            fields_param = _joined_fields(fields, ISSUE_DEFAULT_FIELDS)

        body = await self._get(
            f"/rest/api/3/issue/{issue_key}",
            operation="get_issue",
            issue_key=issue_key,
            params={"fields": fields_param},
        )

        if not isinstance(body, dict):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'get_issue'.",
                operation="get_issue",
                issue_key=issue_key,
            )

        key = body.get("key")
        if not isinstance(key, str) or not key:
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'get_issue'.",
                operation="get_issue",
                issue_key=issue_key,
            )

        raw_fields = body.get("fields", {})
        problems: list[str] = []
        if raw_fields is None:
            raw_fields = {}
        if not isinstance(raw_fields, dict):
            normalized_fields: dict[str, Any] = {}
            problems.append("$.fields: expected an object")
        else:
            try:
                normalized_fields = normalize_issue_fields(raw_fields)
            except IncompleteNormalizationError as exc:
                normalized_fields = exc.partial_value
                problems.extend(str(problem) for problem in exc.problems)

        result = IssueDetail(key=key, fields=normalized_fields)
        if problems:
            raise _incomplete_response_error(
                operation="get_issue",
                issue_key=issue_key,
                problems=problems,
                partial_result=asdict(result),
            )

        return result
