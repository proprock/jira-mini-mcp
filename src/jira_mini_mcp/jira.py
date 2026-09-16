"""Pure Jira Cloud REST API v3 client logic (no MCP dependencies).

`JiraClient` grows by one method per later phase. All HTTP calls route
through the private `_get` helper, which attaches auth, maps failures via
`errors.raise_for_response`, and returns parsed JSON.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO

import httpx2

from jira_mini_mcp import errors
from jira_mini_mcp.auth import BasicTokenAuth
from jira_mini_mcp.models import (
    Attachment,
    ChangelogEntry,
    Comment,
    DownloadResult,
    IncompleteNormalizationError,
    IssueDetail,
    IssueSummary,
    NormalizationProblem,
    Page,
    SearchPage,
    normalize_attachment,
    normalize_changelog_entry,
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

# Jira Cloud v3's changelog endpoint accepts startAt/maxResults but, unlike
# comments, has no orderBy: it is always oldest-first. Observed live on a
# Jira Cloud test site 2026-09-16: an `orderBy` param sent to this endpoint
# is silently ignored (still ascending); startAt/maxResults index that fixed
# ascending collection directly; and `total` is only trustworthy while the
# requested startAt does not exceed it -- once startAt overshoots the real
# total, Jira echoes back `total == startAt` instead of the real count. This
# constant is our own internal fetch chunk size, not a Jira-imposed limit;
# tests shrink it via monkeypatch to exercise multi-page fetches.
CHANGELOG_PAGE_SIZE = 100

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


def _changelog_sort_key(raw: Any) -> tuple[str, str]:
    """Best-effort `(created, id)` sort key for local tie-break resorting.

    An unparsable `created` or non-string `id` sorts as `""`: such an entry
    gets dropped by `normalize_changelog_entry` anyway, so its position here
    is moot.
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
    entry_id = raw.get("id")
    return (created_utc, entry_id if isinstance(entry_id, str) else "")


def _sanitize_attachment_filename(filename: str) -> str:
    """Reduce a Jira-provided filename to a safe basename for the cache.

    Splits on both `/` and `\\` -- the cache may run on POSIX or Windows --
    discards directory and `.`/`..` segments, and rejects an empty or hidden
    (dotfile) result. This intentionally loses any original subdirectory
    structure; only the final component is trusted.
    """
    normalized = filename.replace("\\", "/")
    segments = [s for s in normalized.split("/") if s not in ("", ".", "..")]
    if not segments:
        raise ValueError("filename has no usable basename after sanitization")
    candidate = segments[-1]
    if candidate.startswith("."):
        raise ValueError("filename sanitizes to a hidden file")
    return candidate


def _validate_attachment_id(attachment_id: str) -> None:
    """Reject an `attachment_id` that would escape its cache subdirectory.

    Unlike a Jira-provided filename, `attachment_id` is a caller-supplied
    public argument that becomes a path segment directly
    (`<cache>/<attachment_id>/...`); it gets a strict validation error
    instead of best-effort sanitization.
    """
    if (
        not attachment_id
        or "/" in attachment_id
        or "\\" in attachment_id
        or attachment_id in (".", "..")
    ):
        raise errors.JiraValidationError(
            "download_attachment 'attachment_id' must be a plain identifier "
            "with no path separators.",
            operation="download_attachment",
        )


def _open_binary_for_write(path: Path) -> BinaryIO:
    """Typed wrapper so `Path.open`'s overloaded return type stays unambiguous
    once passed through `asyncio.to_thread`'s generic signature."""
    return path.open("wb")


async def _remove_part_file(part_path: Path) -> None:
    """Best-effort cleanup of a `.part` file after a failed or cancelled download."""

    def _unlink() -> None:
        part_path.unlink(missing_ok=True)

    await asyncio.to_thread(_unlink)


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

    def __init__(
        self,
        client: httpx2.AsyncClient,
        auth: BasicTokenAuth,
        base_url: str,
        cache_dir: Path,
    ) -> None:
        self._client = client
        self._auth = auth
        self._base_url = base_url.rstrip("/")
        # Must already exist (created once by the MCP lifespan); this client
        # only creates the per-attachment subdirectory beneath it.
        self._cache_dir = cache_dir

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

    async def get_attachments(self, issue_key: str) -> list[Attachment]:
        body = await self._get(
            f"/rest/api/3/issue/{issue_key}",
            operation="get_attachments",
            issue_key=issue_key,
            params={"fields": "attachment"},
        )
        if not isinstance(body, dict):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'get_attachments'.",
                operation="get_attachments",
                issue_key=issue_key,
            )

        raw_fields = body.get("fields")
        if not isinstance(raw_fields, dict):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'get_attachments'.",
                operation="get_attachments",
                issue_key=issue_key,
            )

        raw_attachments = raw_fields.get("attachment", [])
        if not isinstance(raw_attachments, list):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'get_attachments'.",
                operation="get_attachments",
                issue_key=issue_key,
            )

        items: list[Attachment] = []
        problems: list[str] = []
        for index, raw in enumerate(raw_attachments):
            model_problems: list[NormalizationProblem] = []
            attachment = normalize_attachment(raw, f"$.fields.attachment[{index}]", model_problems)
            problems.extend(str(problem) for problem in model_problems)
            if attachment is not None:
                items.append(attachment)

        if problems:
            # PROJECT-CONTRACTS.md only spells out partial shapes for
            # get_issue/search; get_attachments returns a plain list, so
            # {"items": [...]} is this project's own reasoned extension of
            # the same aggregate-problems convention used elsewhere.
            raise _incomplete_response_error(
                operation="get_attachments",
                issue_key=issue_key,
                problems=problems,
                partial_result={"items": [asdict(item) for item in items]},
            )

        return items

    async def download_attachment(self, attachment_id: str) -> DownloadResult:
        _validate_attachment_id(attachment_id)

        metadata = await self._get(
            f"/rest/api/3/attachment/{attachment_id}",
            operation="download_attachment",
        )
        if not isinstance(metadata, dict):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'download_attachment'.",
                operation="download_attachment",
            )

        # DownloadResult needs only filename/mime_type/size/content, not
        # author/created, so this checks a narrower set than
        # normalize_attachment (used by get_attachments) -- a malformed
        # author shouldn't block an otherwise-downloadable attachment.
        filename = metadata.get("filename")
        mime_type = metadata.get("mimeType")
        size = metadata.get("size")
        content_url = metadata.get("content")
        problems: list[str] = []
        if not isinstance(filename, str) or not filename:
            problems.append("$.filename: expected a non-empty string")
        if not isinstance(mime_type, str) or not mime_type:
            problems.append("$.mimeType: expected a non-empty string")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            problems.append("$.size: expected a non-negative integer")
        if not isinstance(content_url, str) or not content_url:
            problems.append("$.content: expected a non-empty string")
        if problems:
            raise errors.JiraServerError(
                "Jira returned incomplete attachment metadata for operation "
                f"'download_attachment': {'; '.join(problems)}",
                operation="download_attachment",
            )
        assert isinstance(filename, str)
        assert isinstance(mime_type, str)
        assert isinstance(size, int)
        assert isinstance(content_url, str)

        try:
            sanitized_filename = _sanitize_attachment_filename(filename)
        except ValueError as exc:
            raise errors.JiraValidationError(
                f"download_attachment could not derive a safe filename: {exc}",
                operation="download_attachment",
            ) from exc

        dest_dir = self._cache_dir / attachment_id
        final_path = dest_dir / sanitized_filename
        part_path = final_path.with_name(final_path.name + ".part")

        # Resolve before any write: this follows symlinks in every existing
        # ancestor (including one an attacker planted at dest_dir itself)
        # and rejects the download if the result would land outside the
        # cache, per PROJECT-CONTRACTS.md/PLAN.agents.md's symlink/
        # reparse-point-escape requirement.
        cache_root = self._cache_dir.resolve()
        resolved_final = final_path.resolve()
        if not resolved_final.is_relative_to(cache_root):
            raise errors.JiraValidationError(
                "download_attachment refused to write outside the attachment cache.",
                operation="download_attachment",
            )

        await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)

        try:
            async with self._client.stream(
                "GET", content_url, auth=self._auth, follow_redirects=True
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    errors.raise_for_response(response, operation="download_attachment")

                fh = await asyncio.to_thread(_open_binary_for_write, part_path)
                try:
                    async for chunk in response.aiter_bytes():
                        await asyncio.to_thread(fh.write, chunk)
                finally:
                    await asyncio.to_thread(fh.close)
        except httpx2.TimeoutException as exc:
            await _remove_part_file(part_path)
            raise errors.JiraTimeoutError(
                "Request timed out for operation 'download_attachment'. "
                "Check network connectivity and retry.",
                operation="download_attachment",
            ) from exc
        except httpx2.TransportError as exc:
            await _remove_part_file(part_path)
            raise errors.JiraNetworkError(
                "A network error occurred for operation 'download_attachment'. "
                "Check connectivity and retry.",
                operation="download_attachment",
            ) from exc
        except BaseException:
            await _remove_part_file(part_path)
            raise

        await asyncio.to_thread(part_path.replace, final_path)

        return DownloadResult(
            attachment_id=attachment_id,
            filename=sanitized_filename,
            mime_type=mime_type,
            size=size,
            local_path=str(final_path),
        )

    async def _fetch_changelog_page(
        self, issue_key: str, jira_start_at: int, jira_max_results: int
    ) -> tuple[list[Any], int]:
        body = await self._get(
            f"/rest/api/3/issue/{issue_key}/changelog",
            operation="get_changelog",
            issue_key=issue_key,
            params={"startAt": jira_start_at, "maxResults": jira_max_results},
        )
        raw_values = body.get("values") if isinstance(body, dict) else None
        total = body.get("total") if isinstance(body, dict) else None
        if (
            not isinstance(raw_values, list)
            or not isinstance(total, int)
            or isinstance(total, bool)
        ):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'get_changelog'.",
                operation="get_changelog",
                issue_key=issue_key,
            )
        return raw_values, total

    async def _fetch_changelog_window(
        self, issue_key: str, start_at: int, limit: int, order: str
    ) -> tuple[list[Any], int]:
        """Fetch the raw changelog entries for the logical `[start_at:start_at+limit]` window.

        Jira Cloud v3's changelog has no orderBy and is always oldest-first,
        so a descending request must be reconstructed locally: map the
        logical window onto the equivalent ascending upstream index range,
        fetch every page needed to cover that whole range, then resort it
        (which reverses it for "desc") -- never just reverse a single
        upstream page. `total` is only trustworthy for a startAt that does
        not exceed it, so a minimal discovery fetch at startAt=0 (always
        <= any real total) establishes the true total before it is used to
        compute that range.
        """
        _, total = await self._fetch_changelog_page(issue_key, 0, 1)

        if start_at >= total:
            return [], total

        remaining = limit if limit != 0 else total - start_at
        if order == "asc":
            low = start_at
            high = min(start_at + remaining, total)
        else:
            high = total - start_at
            low = max(high - remaining, 0)

        collected: list[Any] = []
        current = low
        while current < high:
            page_size = min(CHANGELOG_PAGE_SIZE, high - current)
            raw_values, _ = await self._fetch_changelog_page(issue_key, current, page_size)
            collected.extend(raw_values)
            current += len(raw_values)
            if len(raw_values) < page_size:
                break

        collected.sort(key=_changelog_sort_key, reverse=(order == "desc"))
        return collected, total

    async def get_changelog(
        self,
        issue_key: str,
        start_at: int = 0,
        limit: int = 20,
        order: str = "desc",
    ) -> Page[ChangelogEntry]:
        if start_at < 0:
            raise errors.JiraValidationError(
                "get_changelog 'start_at' must be a non-negative value.",
                operation="get_changelog",
                issue_key=issue_key,
            )
        if limit < 0:
            raise errors.JiraValidationError(
                "get_changelog 'limit' must be a non-negative value; use 0 for no limit.",
                operation="get_changelog",
                issue_key=issue_key,
            )
        if order not in ("asc", "desc"):
            raise errors.JiraValidationError(
                "get_changelog 'order' must be 'asc' or 'desc'.",
                operation="get_changelog",
                issue_key=issue_key,
            )

        raw_items, total = await self._fetch_changelog_window(issue_key, start_at, limit, order)

        items: list[ChangelogEntry] = []
        problems: list[str] = []
        for index, raw in enumerate(raw_items):
            model_problems: list[NormalizationProblem] = []
            entry = normalize_changelog_entry(raw, f"$.items[{index}]", model_problems)
            problems.extend(str(problem) for problem in model_problems)
            if entry is not None:
                items.append(entry)

        result = Page(start_at=start_at, total=total, items=items)
        if problems:
            raise _incomplete_response_error(
                operation="get_changelog",
                issue_key=issue_key,
                problems=problems,
                partial_result=asdict(result),
            )

        return result
