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
    Comment,
    IncompleteNormalizationError,
    IssueDetail,
    IssueSummary,
    NormalizationProblem,
    Page,
    SearchPage,
    normalize_comment,
    normalize_issue_fields,
    to_utc_iso,
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

# Jira Cloud v3's comment endpoint accepts arbitrary startAt/maxResults and an
# orderBy=created|-created parameter, unlike search's cursor-only pagination.
# Observed live on a Jira Cloud test site 2026-09-16: GET .../comment with no
# orderBy matched orderBy=created (ascending); orderBy=-created returned the
# newest comment first; startAt/maxResults indexed that same ascending
# collection directly; and startAt beyond total returned an empty list with
# the correct total, not an error. This constant is our own internal fetch
# chunk size, not a Jira-imposed limit; tests shrink it via monkeypatch to
# exercise multi-page fetches without giant fixtures.
COMMENTS_PAGE_SIZE = 100

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


def _comment_sort_key(raw: Any) -> tuple[str, str]:
    """Best-effort `(created, id)` sort key for local tie-break resorting.

    An unparsable `created` or non-string `id` sorts as `""`: such a comment
    gets dropped by `normalize_comment` anyway, so its position here is moot.
    """
    if not isinstance(raw, dict):
        return ("", "")
    created_raw = raw.get("created")
    created_utc = ""
    if isinstance(created_raw, str) and created_raw:
        try:
            created_utc = to_utc_iso(created_raw)
        except ValueError:
            created_utc = ""
    comment_id = raw.get("id")
    return (created_utc, comment_id if isinstance(comment_id, str) else "")


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

    async def _fetch_comments_page(
        self,
        issue_key: str,
        jira_start_at: int,
        jira_max_results: int,
        jira_order: str,
    ) -> tuple[list[Any], int]:
        body = await self._get(
            f"/rest/api/3/issue/{issue_key}/comment",
            operation="get_comments",
            issue_key=issue_key,
            params={
                "startAt": jira_start_at,
                "maxResults": jira_max_results,
                "orderBy": jira_order,
            },
        )
        raw_comments = body.get("comments") if isinstance(body, dict) else None
        total = body.get("total") if isinstance(body, dict) else None
        if (
            not isinstance(raw_comments, list)
            or not isinstance(total, int)
            or isinstance(total, bool)
        ):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'get_comments'.",
                operation="get_comments",
                issue_key=issue_key,
            )
        return raw_comments, total

    async def _fetch_comments_window(
        self, issue_key: str, start_at: int, limit: int, order: str
    ) -> tuple[list[Any], int]:
        """Fetch the raw comments for `[start_at:start_at+limit]` with no `since` filter.

        Jira's comment endpoint supports arbitrary startAt/maxResults plus
        orderBy=created|-created, so the public offset and order map directly
        onto Jira's own pagination -- no full-history scan is needed here.
        """
        jira_order = "created" if order == "asc" else "-created"
        collected: list[Any] = []
        total = 0
        current_start = start_at
        while limit == 0 or len(collected) < limit:
            remaining = None if limit == 0 else limit - len(collected)
            page_size = (
                COMMENTS_PAGE_SIZE if remaining is None else min(remaining, COMMENTS_PAGE_SIZE)
            )
            raw_comments, page_total = await self._fetch_comments_page(
                issue_key, current_start, page_size, jira_order
            )
            if current_start == start_at:
                total = page_total
            collected.extend(raw_comments)
            current_start += len(raw_comments)
            if len(raw_comments) < page_size:
                break

        if limit != 0:
            collected = collected[:limit]
        # Resort the fetched window locally: Jira's orderBy already matches the
        # requested direction, but its tie-break among comments that share the
        # same `created` instant is unspecified, so a fresh (created, id) sort
        # guarantees the deterministic tie-break the public contract requires.
        # This only fixes ties within the fetched window, not ones that
        # straddle its edge -- an accepted, documented limitation.
        collected.sort(key=_comment_sort_key, reverse=(order == "desc"))
        return collected, total

    async def _fetch_comments_since(
        self, issue_key: str, start_at: int, limit: int, order: str, since_utc: str
    ) -> tuple[list[Any], int]:
        """Fetch raw comments with `created >= since_utc`, applying `[start_at:limit]`.

        Always scans newest-first regardless of the requested public `order`:
        once an older comment is seen, Jira's descending order guarantees no
        later page can contain a match, so the scan stops there without
        fetching the rest of the history.
        """
        matched: list[Any] = []
        current_start = 0
        while True:
            raw_comments, _ = await self._fetch_comments_page(
                issue_key, current_start, COMMENTS_PAGE_SIZE, "-created"
            )
            if not raw_comments:
                break

            stop = False
            for raw in raw_comments:
                created_raw = raw.get("created") if isinstance(raw, dict) else None
                created_utc: str | None = None
                if isinstance(created_raw, str) and created_raw:
                    try:
                        created_utc = to_utc_iso(created_raw)
                    except ValueError:
                        created_utc = None
                if created_utc is None:
                    # Can't place this item in the scan order; it is dropped
                    # from the logical collection entirely (normalize_comment
                    # would drop it too), so total stays consistent.
                    continue
                if created_utc >= since_utc:
                    matched.append(raw)
                else:
                    stop = True
                    break
            if stop:
                break
            if len(raw_comments) < COMMENTS_PAGE_SIZE:
                break
            current_start += len(raw_comments)

        matched.sort(key=_comment_sort_key, reverse=(order == "desc"))
        total = len(matched)
        window = matched[start_at:] if limit == 0 else matched[start_at : start_at + limit]
        return window, total

    async def get_comments(
        self,
        issue_key: str,
        start_at: int = 0,
        limit: int = 20,
        order: str = "desc",
        since: str | None = None,
    ) -> Page[Comment]:
        if start_at < 0:
            raise errors.JiraValidationError(
                "get_comments 'start_at' must be a non-negative value.",
                operation="get_comments",
                issue_key=issue_key,
            )
        if limit < 0:
            raise errors.JiraValidationError(
                "get_comments 'limit' must be a non-negative value; use 0 for no limit.",
                operation="get_comments",
                issue_key=issue_key,
            )
        if order not in ("asc", "desc"):
            raise errors.JiraValidationError(
                "get_comments 'order' must be 'asc' or 'desc'.",
                operation="get_comments",
                issue_key=issue_key,
            )
        since_utc: str | None = None
        if since is not None:
            try:
                since_utc = to_utc_iso(since)
            except ValueError as exc:
                raise errors.JiraValidationError(
                    "get_comments 'since' must be an ISO-8601 timestamp with an "
                    "explicit offset (e.g. 2025-01-01T00:00:00Z).",
                    operation="get_comments",
                    issue_key=issue_key,
                ) from exc

        if since_utc is None:
            raw_items, total = await self._fetch_comments_window(issue_key, start_at, limit, order)
        else:
            raw_items, total = await self._fetch_comments_since(
                issue_key, start_at, limit, order, since_utc
            )

        items: list[Comment] = []
        problems: list[str] = []
        for index, raw in enumerate(raw_items):
            model_problems: list[NormalizationProblem] = []
            comment = normalize_comment(raw, f"$.items[{index}]", model_problems)
            problems.extend(str(problem) for problem in model_problems)
            if comment is not None:
                items.append(comment)

        result = Page(start_at=start_at, total=total, items=items)
        if problems:
            raise _incomplete_response_error(
                operation="get_comments",
                issue_key=issue_key,
                problems=problems,
                partial_result=asdict(result),
            )

        return result
