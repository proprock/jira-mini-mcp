"""Pure Jira Cloud REST API v3 client logic (no MCP dependencies).

`JiraClient` grows by one method per later phase. All HTTP calls route
through the private `_request` helper, which attaches auth, maps failures
via `errors.raise_for_response`, and returns parsed JSON; `_get` is the
read-only shorthand for it.
"""

from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, BinaryIO

import httpx2

from jira_mini_mcp import errors
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
    Transition,
    TransitionResult,
    UpdateResult,
    User,
    compact_user,
    markdown_to_adf,
    normalize_attachment,
    normalize_changelog_entry,
    normalize_comment,
    normalize_issue_fields,
    normalize_transition,
    normalize_votes,
    normalize_watchers,
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

# `watches`/`votes` are Extra 3's resolved reference fields: get_issue's raw
# response only ever holds a `{self, count, flag}` stub for these (never the
# actual watcher/voter list), so an explicit request for either name triggers
# exactly one follow-up GET, normalized by the paired function here. Every
# other Jira field ID passes straight through get_issue's `fields` argument.
_ReferenceNormalizer = Callable[[Any, str, list[NormalizationProblem]], dict[str, Any] | None]

_REFERENCE_FIELD_RESOLVERS: dict[str, tuple[str, _ReferenceNormalizer]] = {
    "watches": ("watchers", normalize_watchers),
    "votes": ("votes", normalize_votes),
}

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

# What an accountId looks like: the 24-character legacy id (observed hex, though
# older ids also carry other letters), `<digits>:<uuid>`, and the `qm:` form of
# some managed accounts. Such a value is used as is; anything else is looked up.
_ACCOUNT_ID_PATTERN = re.compile(
    r"[0-9a-z]{24}|[0-9]+:[0-9a-f-]{36}|qm:[A-Za-z0-9:_-]+", re.IGNORECASE
)

# How many users to ask Jira for, and how many to show when nobody matches.
_USER_SEARCH_LIMIT = 20
_MAX_LISTED_USERS = 10

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")

# Caller-supplied path segments. Anything outside these shapes (`/`, `?`, `#`,
# `..`) would let a crafted key steer the request, including a PUT, at another
# Jira endpoint once httpx normalizes the URL. Matched with `fullmatch`.
_ISSUE_KEY_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]*-[0-9]+|[0-9]+")
_ATTACHMENT_ID_PATTERN = re.compile(r"[0-9]+")

# One connect budget and a longer per-read budget: Jira is the slow part,
# but a stalled socket must not hang the agent for the library default.
HTTP_TIMEOUT = httpx2.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)

# Cap on one `_request` call, every attempt and wait included, so our
# actionable error arrives before the typical 60 s tool timeout of an MCP host.
# Without it three 30 s attempts plus backoff (or a long Retry-After) run ~90-150 s.
_REQUEST_BUDGET = 45.0

# An attachment is buffered to disk in full and handed to the agent as a local
# file, so a runaway one would fill the temp directory; refuse it up front and,
# in case Jira's reported size is wrong, while streaming.
_MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024

# Chunks from the network are small; coalescing them keeps the number of
# worker-thread hops per download low.
_WRITE_BUFFER_BYTES = 1024 * 1024

# Waits before the second and third attempt. A table rather than computed
# backoff: three attempts is the whole policy, and a table cannot grow a
# branch no test reaches. No jitter -- one process issues these
# sequentially, so there is nothing here to desynchronize.
_BACKOFF_DELAYS: tuple[float, ...] = (0.5, 1.0)
_MAX_ATTEMPTS = len(_BACKOFF_DELAYS) + 1

# Honour Retry-After only while the wait is short enough to be worth
# holding a tool call open; past that the caller is told to come back.
_RETRY_AFTER_CAP = 60.0

# A 429 is Jira refusing to process the request, so replaying it cannot
# duplicate a write. A 5xx or a dropped connection may mean the write
# landed, so only methods that converge on replay are retried -- never a
# POST, which would post a second comment or run a transition twice.
_REPLAYABLE_METHODS = frozenset({"GET", "PUT"})

# Fields update_issue refuses, each pointing at the tool that does the job
# or saying plainly that this server does not do it.
_UPDATE_REJECTED_FIELDS: dict[str, str] = {
    "status": "Use transition_issue to move an issue through its workflow.",
    "comment": "Use add_comment to comment on an issue.",
    "attachment": "This server does not upload attachments.",
    "issuelinks": (
        "This server does not create or remove issue links. Put the link, or a "
        "request to add it, in a comment with add_comment."
    ),
    "worklog": "This server does not log work.",
    "project": "This server does not move issues between projects.",
    "issuetype": "This server does not change an issue's type.",
    "key": "Jira assigns the issue key; it cannot be set.",
    "id": "Jira assigns the issue id; it cannot be set.",
    "created": "Jira maintains 'created'; it cannot be set.",
    "updated": "Jira maintains 'updated'; it cannot be set.",
    "resolutiondate": "Jira maintains 'resolutiondate'; it cannot be set.",
}


def _joined_fields(fields: list[str] | None, default: tuple[str, ...]) -> str:
    if fields is None:
        return ",".join(default)
    return ",".join(fields)


def _created_id_sort_key(raw: Any) -> tuple[str, str]:
    """Best-effort `(created, id)` sort key for local tie-break resorting.

    An unparsable `created` or non-string `id` sorts as `""`: such a comment
    or changelog entry gets dropped by its normalizer anyway, so its position
    here is moot.
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
    item_id = raw.get("id")
    return (created_utc, item_id if isinstance(item_id, str) else "")


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
    """Reject an `attachment_id` that is not a numeric Jira attachment id.

    Unlike a Jira-provided filename, `attachment_id` is a caller-supplied
    public argument that becomes a URL path and a cache path segment
    (`<cache>/<attachment_id>/...`); it gets a strict validation error
    instead of best-effort sanitization.
    """
    if (
        not isinstance(attachment_id, str)
        or _ATTACHMENT_ID_PATTERN.fullmatch(attachment_id) is None
    ):
        raise errors.JiraValidationError(
            "download_attachment 'attachment_id' must be a numeric attachment id, "
            "as get_attachments returns it.",
            operation="download_attachment",
        )


def _attachment_too_large(size: int) -> errors.JiraValidationError:
    return errors.JiraValidationError(
        f"download_attachment refuses attachments over 100 MB; this one is {size} bytes. "
        "Do not retry: tell the user this attachment is too large for this server to "
        "fetch, so they can download it from Jira themselves and share it.",
        operation="download_attachment",
    )


def _issue_segment(issue_key: str, *, operation: str) -> str:
    """Validate `issue_key` and return it as one safe URL path segment.

    The rejected value is left out of the message: it is the caller's raw
    input, and echoing a crafted path back adds nothing.
    """
    if not isinstance(issue_key, str) or _ISSUE_KEY_PATTERN.fullmatch(issue_key) is None:
        raise errors.JiraValidationError(
            f"{operation} needs issue_key as an issue key like PROJ-123 or a numeric issue id.",
            operation=operation,
        )
    return urllib.parse.quote(issue_key, safe="")


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


async def _sleep(seconds: float) -> None:
    """Indirection so tests can drive backoff without real waiting."""
    await asyncio.sleep(seconds)


def _monotonic() -> float:
    """Indirection so tests can drive the request budget without real waiting."""
    return time.monotonic()


def _attempt_timeout(remaining: float) -> httpx2.Timeout:
    """`HTTP_TIMEOUT`, with no phase allowed to outlast the budget that is left."""
    return httpx2.Timeout(
        connect=min(10.0, remaining),
        read=min(30.0, remaining),
        write=min(30.0, remaining),
        pool=min(10.0, remaining),
    )


def _fits(delay: float, deadline: float) -> bool:
    """Whether waiting `delay` still leaves room for another attempt."""
    return _monotonic() + delay < deadline


def _retry_delay(response: httpx2.Response, attempt: int) -> float | None:
    """How long to wait before retrying, or None to stop retrying now."""
    if response.status_code == 429:
        retry_after = errors.retry_after_seconds(response)
        if retry_after is not None:
            if retry_after > _RETRY_AFTER_CAP:
                return None
            return retry_after
    return _BACKOFF_DELAYS[attempt - 1]


def _adf_or_validation_error(
    markdown: str, *, operation: str, issue_key: str | None
) -> dict[str, Any]:
    """Convert a Markdown body, reporting an unusable one to the caller."""
    try:
        return markdown_to_adf(markdown)
    except ValueError as exc:
        raise errors.JiraValidationError(
            str(exc), operation=operation, issue_key=issue_key
        ) from exc


def _update_type_error(name: str, expected: str, issue_key: str) -> errors.JiraValidationError:
    return errors.JiraValidationError(
        f"update_issue needs '{name}' as {expected}.",
        operation="update_issue",
        issue_key=issue_key,
    )


def _required_update_string(name: str, value: Any, issue_key: str) -> str:
    if isinstance(value, str) and value.strip():
        return value
    raise _update_type_error(name, "a non-empty string", issue_key)


def _required_update_string_list(name: str, value: Any, issue_key: str) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        return list(value)
    raise _update_type_error(name, "a list of non-empty strings", issue_key)


def _resolve_transition(transitions: list[Transition], to: str, *, issue_key: str) -> Transition:
    """Pick the transition a caller meant, or say why none could be picked.

    Transition name wins over target status name: in a real workflow two
    transitions can lead to one status, and only the transition name tells
    them apart.
    """
    wanted = to.strip().casefold()

    by_transition_name = [item for item in transitions if item.name.casefold() == wanted]
    if len(by_transition_name) == 1:
        return by_transition_name[0]
    if len(by_transition_name) > 1:
        raise _ambiguous_transition_error(by_transition_name, to, issue_key)

    by_status_name = [
        item for item in transitions if str(item.status.get("name", "")).casefold() == wanted
    ]
    if len(by_status_name) == 1:
        return by_status_name[0]
    if len(by_status_name) > 1:
        raise _ambiguous_transition_error(by_status_name, to, issue_key)

    if not transitions:
        raise errors.JiraValidationError(
            f"No transition is available on {issue_key} for the configured account, "
            "so its status cannot be changed.",
            operation="transition_issue",
            issue_key=issue_key,
        )
    raise errors.JiraValidationError(
        f"No transition on {issue_key} matches '{to}'. Available transitions "
        f"(transition -> resulting status): {_transition_list(transitions)}.",
        operation="transition_issue",
        issue_key=issue_key,
    )


def _ambiguous_transition_error(
    candidates: list[Transition], to: str, issue_key: str
) -> errors.JiraValidationError:
    return errors.JiraValidationError(
        f"'{to}' matches more than one transition on {issue_key}: "
        f"{_transition_list(candidates)}. Name the transition itself.",
        operation="transition_issue",
        issue_key=issue_key,
    )


def _custom_field_names(body: dict[str, Any], fields: dict[str, Any]) -> dict[str, str]:
    """Display names for the `customfield_*` ids present in the normalized `fields`.

    The names are auxiliary: an absent or malformed `names` yields no mapping
    rather than failing an issue whose data is fine.
    """
    names = body.get("names")
    if not isinstance(names, dict):
        return {}
    mapping: dict[str, str] = {}
    for field_id in fields:
        name = names.get(field_id)
        if field_id.startswith("customfield_") and isinstance(name, str) and name:
            mapping[field_id] = name
    return mapping


def _user_list(users: list[User]) -> str:
    return "; ".join(f"{user.display_name} ({user.account_id})" for user in users)


def _transition_list(transitions: list[Transition]) -> str:
    return "; ".join(f"{item.name} -> {item.status.get('name', '?')}" for item in transitions)


class JiraClient:
    """Pure Jira Cloud REST API v3 client. Holds no MCP dependency."""

    def __init__(
        self,
        client: httpx2.AsyncClient,
        auth: httpx2.Auth,
        base_url: str | Callable[[], Awaitable[str]],
        cache_dir: Path,
        *,
        auth_hint: str = errors.API_TOKEN_AUTH_HINT,
    ) -> None:
        """`base_url` is the site URL for API-token auth, or, for OAuth, an
        async provider of the API-gateway URL -- resolved per request so a
        server started before `jira-mini-mcp login` picks the login up.
        `auth_hint` completes every 401 message.
        """
        self._client = client
        self._auth = auth
        self._base_url = base_url
        self._auth_hint = auth_hint
        # Must already exist (created once by the MCP lifespan); this client
        # only creates the per-attachment subdirectory beneath it.
        self._cache_dir = cache_dir
        # Resolved on first use by assignee="me"; one account per process.
        self._account_id: str | None = None

    async def _api_base(self) -> str:
        if isinstance(self._base_url, str):
            return self._base_url.rstrip("/")
        return (await self._base_url()).rstrip("/")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        issue_key: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        expect_json: bool = True,
    ) -> Any:
        """Issue one Jira request: attach auth, map failures, parse the body.

        Jira answers a successful write with 204 and no body, so
        `expect_json=False` returns None instead of treating an empty
        response as a parse failure.
        """
        url = f"{await self._api_base()}{path}"
        replayable = method.upper() in _REPLAYABLE_METHODS
        deadline = _monotonic() + _REQUEST_BUDGET

        attempt = 0
        while True:
            # A while loop rather than a range: every path out of the body
            # returns or raises, and there is no loop exit to leave dead.
            attempt += 1
            final = attempt == _MAX_ATTEMPTS
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    auth=self._auth,
                    timeout=_attempt_timeout(deadline - _monotonic()),
                )
            except httpx2.TimeoutException as exc:
                if final or not replayable or not _fits(_BACKOFF_DELAYS[attempt - 1], deadline):
                    raise errors.JiraTimeoutError(
                        f"Request timed out for operation '{operation}'. "
                        "Check network connectivity and retry.",
                        operation=operation,
                        issue_key=issue_key,
                    ) from exc
                await _sleep(_BACKOFF_DELAYS[attempt - 1])
                continue
            except httpx2.TransportError as exc:
                if final or not replayable or not _fits(_BACKOFF_DELAYS[attempt - 1], deadline):
                    raise errors.JiraNetworkError(
                        f"A network error occurred for operation '{operation}'. "
                        "Check connectivity and retry.",
                        operation=operation,
                        issue_key=issue_key,
                    ) from exc
                await _sleep(_BACKOFF_DELAYS[attempt - 1])
                continue

            retriable = response.status_code == 429 or (response.status_code >= 500 and replayable)
            if retriable and not final:
                delay = _retry_delay(response, attempt)
                if delay is not None and _fits(delay, deadline):
                    await _sleep(delay)
                    continue

            # Out of attempts, or not worth another: report the real failure.
            errors.raise_for_response(
                response, operation=operation, issue_key=issue_key, auth_hint=self._auth_hint
            )

            if not expect_json:
                return None

            try:
                return response.json()
            except ValueError as exc:
                raise errors.JiraServerError(
                    "Jira returned a response that could not be parsed for operation "
                    f"'{operation}'.",
                    operation=operation,
                    issue_key=issue_key,
                ) from exc

    async def _get(
        self,
        path: str,
        *,
        operation: str,
        issue_key: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._request(
            "GET", path, operation=operation, issue_key=issue_key, params=params
        )

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
        segment = _issue_segment(issue_key, operation="get_issue")
        if fields is not None and len(fields) == 0:
            fields_param = _GET_ISSUE_EMPTY_FIELDS_SENTINEL
        else:
            fields_param = _joined_fields(fields, ISSUE_DEFAULT_FIELDS)

        params = {"fields": fields_param}
        wants_names = fields is not None and any(name.startswith("customfield_") for name in fields)
        if wants_names:
            # A customfield_* id means nothing to a reader; Jira supplies the
            # display names in the same response.
            params["expand"] = "names"

        body = await self._get(
            f"/rest/api/3/issue/{segment}",
            operation="get_issue",
            issue_key=issue_key,
            params=params,
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

        if not problems and fields is not None:
            for field_name, (endpoint, normalizer) in _REFERENCE_FIELD_RESOLVERS.items():
                if field_name in fields:
                    normalized_fields[field_name] = await self._resolve_reference_field(
                        segment, field_name, endpoint, normalizer
                    )

        field_names = _custom_field_names(body, normalized_fields) if wants_names else {}
        result = IssueDetail(key=key, fields=normalized_fields, field_names=field_names)
        if problems:
            partial = asdict(result)
            if not partial["field_names"]:
                del partial["field_names"]
            raise _incomplete_response_error(
                operation="get_issue",
                issue_key=issue_key,
                problems=problems,
                partial_result=partial,
            )

        return result

    async def _resolve_reference_field(
        self,
        issue_key: str,
        field_name: str,
        endpoint: str,
        normalizer: _ReferenceNormalizer,
    ) -> dict[str, Any]:
        """Follow `fields.{field_name}`'s `self` link and return real data.

        Only called for a field explicitly named in `get_issue`'s `fields`
        argument (see `_REFERENCE_FIELD_RESOLVERS`), never as part of the
        default field set. HTTP failures propagate as-is from `_get` (already
        a well-typed, actionable `JiraMiniError`); `operation` names the field
        being resolved so the message points at the actual cause instead of a
        generic get_issue failure.
        """
        body = await self._get(
            f"/rest/api/3/issue/{issue_key}/{endpoint}",
            operation=f"get_issue (resolving '{field_name}')",
            issue_key=issue_key,
        )
        problems: list[NormalizationProblem] = []
        resolved = normalizer(body, f"$.{field_name}", problems)
        if resolved is None or problems:
            detail = "; ".join(str(problem) for problem in problems) or "expected an object"
            raise errors.JiraServerError(
                f"Jira returned an unusable '{field_name}' response for operation "
                f"'get_issue': {detail}",
                operation="get_issue",
                issue_key=issue_key,
            )
        return resolved

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
        collected.sort(key=_created_id_sort_key, reverse=(order == "desc"))
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

        matched.sort(key=_created_id_sort_key, reverse=(order == "desc"))
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
        segment = _issue_segment(issue_key, operation="get_comments")
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
            raw_items, total = await self._fetch_comments_window(segment, start_at, limit, order)
        else:
            raw_items, total = await self._fetch_comments_since(
                segment, start_at, limit, order, since_utc
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
        segment = _issue_segment(issue_key, operation="get_attachments")
        body = await self._get(
            f"/rest/api/3/issue/{segment}",
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

        if size > _MAX_ATTACHMENT_BYTES:
            raise _attachment_too_large(size)

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

        if not isinstance(self._base_url, str):
            # `content` points at the site host, which does not accept an
            # OAuth Bearer token; the gateway serves the same bytes.
            content_url = f"{await self._api_base()}/rest/api/3/attachment/content/{attachment_id}"

        await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)

        try:
            async with self._client.stream(
                "GET", content_url, auth=self._auth, follow_redirects=True
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    errors.raise_for_response(
                        response, operation="download_attachment", auth_hint=self._auth_hint
                    )

                written = 0
                buffer = bytearray()
                fh = await asyncio.to_thread(_open_binary_for_write, part_path)
                try:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > _MAX_ATTACHMENT_BYTES:
                            raise _attachment_too_large(written)
                        buffer += chunk
                        if len(buffer) >= _WRITE_BUFFER_BYTES:
                            await asyncio.to_thread(fh.write, bytes(buffer))
                            buffer.clear()
                    if buffer:
                        await asyncio.to_thread(fh.write, bytes(buffer))
                finally:
                    await asyncio.to_thread(fh.close)

                if written != size:
                    raise errors.JiraServerError(
                        f"Jira sent {written} bytes for an attachment it reported as "
                        f"{size} bytes; retry the download.",
                        operation="download_attachment",
                    )
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
        collected: list[Any] = []
        if order == "asc" and start_at == 0:
            # startAt=0 never exceeds the real total, so the first working page
            # doubles as the discovery fetch.
            first_size = min(limit or CHANGELOG_PAGE_SIZE, CHANGELOG_PAGE_SIZE)
            collected, total = await self._fetch_changelog_page(issue_key, 0, first_size)
            if len(collected) < first_size:
                collected.sort(key=_created_id_sort_key)
                return collected, total
            current = len(collected)
        else:
            _, total = await self._fetch_changelog_page(issue_key, 0, 1)
            if start_at >= total:
                return [], total
            current = -1  # set from the range below

        remaining = limit if limit != 0 else total - start_at
        if order == "asc":
            low = start_at
            high = min(start_at + remaining, total)
        else:
            high = total - start_at
            low = max(high - remaining, 0)
        if current < 0:
            current = low

        while current < high:
            page_size = min(CHANGELOG_PAGE_SIZE, high - current)
            raw_values, _ = await self._fetch_changelog_page(issue_key, current, page_size)
            collected.extend(raw_values)
            current += len(raw_values)
            if len(raw_values) < page_size:
                break

        collected.sort(key=_created_id_sort_key, reverse=(order == "desc"))
        return collected, total

    async def get_changelog(
        self,
        issue_key: str,
        start_at: int = 0,
        limit: int = 20,
        order: str = "desc",
    ) -> Page[ChangelogEntry]:
        segment = _issue_segment(issue_key, operation="get_changelog")
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

        raw_items, total = await self._fetch_changelog_window(segment, start_at, limit, order)

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

    async def add_comment(self, issue_key: str, body: str) -> Comment:
        """Post one comment, written in Markdown, and return it normalized.

        The result is the same `Comment` shape `get_comments` publishes:
        Jira answers the POST with the created comment object, identical to
        an item of the comment collection.
        """
        segment = _issue_segment(issue_key, operation="add_comment")
        if not isinstance(body, str):
            raise errors.JiraValidationError(
                "add_comment needs the comment body as Markdown text.",
                operation="add_comment",
                issue_key=issue_key,
            )
        document = _adf_or_validation_error(body, operation="add_comment", issue_key=issue_key)

        raw = await self._request(
            "POST",
            f"/rest/api/3/issue/{segment}/comment",
            operation="add_comment",
            issue_key=issue_key,
            json_body={"body": document},
        )

        problems: list[NormalizationProblem] = []
        comment = normalize_comment(raw, "$", problems)
        if comment is None or problems:
            raise _incomplete_response_error(
                operation="add_comment",
                issue_key=issue_key,
                problems=[str(problem) for problem in problems] or ["$: expected a comment object"],
                partial_result=asdict(comment) if comment is not None else {},
            )
        return comment

    async def transition_issue(
        self, issue_key: str, to: str, comment: str | None = None
    ) -> TransitionResult:
        """Move an issue through its workflow, optionally commenting.

        Jira accepts only a workflow-specific transition id, so `to` is
        resolved against the issue's available transitions: by transition
        name first, then by target status name. Those differ in real
        workflows -- a transition named "In Progress" can lead to a status
        named "In Development" -- and two transitions can reach one status,
        so an ambiguous match is reported rather than guessed.
        """
        segment = _issue_segment(issue_key, operation="transition_issue")
        if not isinstance(to, str) or not to.strip():
            raise errors.JiraValidationError(
                "transition_issue needs a target. Pass `to` as a transition name or "
                "the name of the status to reach.",
                operation="transition_issue",
                issue_key=issue_key,
            )

        body = await self._get(
            f"/rest/api/3/issue/{segment}/transitions",
            operation="transition_issue",
            issue_key=issue_key,
        )
        transitions = self._normalized_transitions(body, issue_key)
        chosen = _resolve_transition(transitions, to, issue_key=issue_key)

        payload: dict[str, Any] = {"transition": {"id": chosen.id}}
        if comment is not None:
            document = _adf_or_validation_error(
                comment, operation="transition_issue", issue_key=issue_key
            )
            # One call, so the move and the note cannot land apart.
            payload["update"] = {"comment": [{"add": {"body": document}}]}

        await self._request(
            "POST",
            f"/rest/api/3/issue/{segment}/transitions",
            operation="transition_issue",
            issue_key=issue_key,
            json_body=payload,
            expect_json=False,
        )
        return TransitionResult(key=issue_key, transition=chosen)

    def _normalized_transitions(self, body: Any, issue_key: str) -> list[Transition]:
        if not isinstance(body, dict) or not isinstance(body.get("transitions"), list):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for operation 'transition_issue'.",
                operation="transition_issue",
                issue_key=issue_key,
            )

        problems: list[NormalizationProblem] = []
        transitions: list[Transition] = []
        for index, raw in enumerate(body["transitions"]):
            transition = normalize_transition(raw, f"$.transitions[{index}]", problems)
            if transition is not None:
                transitions.append(transition)

        if problems:
            # Resolving against a partial list could report "no match" for a
            # transition that exists, so a malformed entry stops the move.
            raise _incomplete_response_error(
                operation="transition_issue",
                issue_key=issue_key,
                problems=[str(problem) for problem in problems],
                partial_result={
                    "key": issue_key,
                    "transitions": [asdict(transition) for transition in transitions],
                },
            )
        return transitions

    async def update_issue(self, issue_key: str, fields: dict[str, Any]) -> UpdateResult:
        """Set issue fields, taking the same values `get_issue` returns.

        Known fields accept friendly values; unknown and `customfield_*`
        values pass through as raw Jira JSON, mirroring the read side. The
        issue is not re-fetched: a caller wanting confirmation calls
        `get_issue`.
        """
        segment = _issue_segment(issue_key, operation="update_issue")
        if not isinstance(fields, dict) or not fields:
            raise errors.JiraValidationError(
                "update_issue needs at least one field to change, for example "
                '{"labels": ["triage"]}.',
                operation="update_issue",
                issue_key=issue_key,
            )

        payload: dict[str, Any] = {}
        for name, value in fields.items():
            guidance = _UPDATE_REJECTED_FIELDS.get(name)
            if guidance is not None:
                raise errors.JiraValidationError(
                    f"update_issue cannot set '{name}'. {guidance}",
                    operation="update_issue",
                    issue_key=issue_key,
                )
            payload[name] = await self._coerced_field(name, value, issue_key)

        await self._request(
            "PUT",
            f"/rest/api/3/issue/{segment}",
            operation="update_issue",
            issue_key=issue_key,
            json_body={"fields": payload},
            expect_json=False,
        )
        return UpdateResult(key=issue_key, updated_fields=tuple(sorted(fields)))

    async def _coerced_field(self, name: str, value: Any, issue_key: str) -> Any:
        """Translate one friendly field value into Jira's own shape."""
        if name == "assignee":
            if value is None:
                return None
            return {"accountId": await self._resolve_assignee(value, issue_key)}

        if name == "description":
            if value is None:
                return None
            if not isinstance(value, str) or not value.strip():
                raise _update_type_error(
                    name, "non-empty Markdown text, or null to clear it", issue_key
                )
            return _adf_or_validation_error(value, operation="update_issue", issue_key=issue_key)

        if name == "summary":
            return _required_update_string(name, value, issue_key)

        if name == "labels":
            return _required_update_string_list(name, value, issue_key)

        if name == "components":
            return [{"name": item} for item in _required_update_string_list(name, value, issue_key)]

        if name == "priority":
            if value is None:
                return None
            return {"name": _required_update_string(name, value, issue_key)}

        if name == "parent":
            if value is None:
                return None
            return {"key": _required_update_string(name, value, issue_key)}

        if name == "duedate":
            if value is None:
                return None
            due = _required_update_string(name, value, issue_key)
            if _DATE_PATTERN.fullmatch(due) is None:
                raise errors.JiraValidationError(
                    "update_issue needs 'duedate' as a YYYY-MM-DD date, or null to clear it.",
                    operation="update_issue",
                    issue_key=issue_key,
                )
            return due

        # Unknown and customfield_* values are the caller's own Jira JSON,
        # exactly as the read side keeps them.
        return value

    async def _resolve_assignee(self, value: Any, issue_key: str) -> str:
        """Turn an assignee value into an account id.

        `"me"` is the configured account, an id-shaped value is used as is,
        and anything else is an email or display name looked up among active
        Atlassian accounts. A lookup that is not conclusive lists the
        candidates rather than guessing; the email itself is matched against
        but never shown.
        """
        text = _required_update_string("assignee", value, issue_key)
        if text.strip().casefold() == "me":
            return await self._current_account_id(issue_key)
        if _ACCOUNT_ID_PATTERN.fullmatch(text):
            return text

        query = text.strip()
        body = await self._get(
            "/rest/api/3/user/search",
            operation="update_issue (resolving 'assignee')",
            issue_key=issue_key,
            params={"query": query, "maxResults": _USER_SEARCH_LIMIT},
        )
        if not isinstance(body, list):
            raise errors.JiraServerError(
                "Jira returned an unexpected response shape for the user search behind "
                "update_issue 'assignee'.",
                operation="update_issue",
                issue_key=issue_key,
            )

        # Apps and deactivated accounts cannot hold an assignment, so they are
        # not candidates however well their name matches.
        candidates: list[tuple[User, str | None]] = []
        for raw in body:
            if (
                isinstance(raw, dict)
                and raw.get("accountType") == "atlassian"
                and raw.get("active") is True
                and isinstance(raw.get("accountId"), str)
                and isinstance(raw.get("displayName"), str)
            ):
                email = raw.get("emailAddress")
                candidates.append((compact_user(raw), email if isinstance(email, str) else None))

        # An email typed by the caller is as private as one Jira returns, so it
        # is never echoed back into a message.
        shown = "that email" if "@" in query else f"'{query}'"
        wanted = query.casefold()
        exact = [
            user
            for user, email in candidates
            if user.display_name.casefold() == wanted
            or (email is not None and email.casefold() == wanted)
        ]
        if len(exact) == 1:
            return exact[0].account_id
        if len(exact) > 1:
            raise errors.JiraValidationError(
                f"{shown[0].upper()}{shown[1:]} matches more than one user: "
                f"{_user_list(exact)}. Pass the account id.",
                operation="update_issue",
                issue_key=issue_key,
            )
        # Jira hides many accounts' emails yet still finds them by it, so a
        # lone hit for an email-shaped query is that person.
        if "@" in query and len(candidates) == 1:
            return candidates[0][0].account_id

        message = f"No active user matches {shown}."
        if candidates:
            listed = [user for user, _ in candidates[:_MAX_LISTED_USERS]]
            message += f" Closest candidates: {_user_list(listed)}. Pass the account id."
        raise errors.JiraValidationError(message, operation="update_issue", issue_key=issue_key)

    async def _current_account_id(self, issue_key: str) -> str:
        """The configured account's own id, fetched once per process.

        Jira assigns by account id, and an agent asked to "assign it to me"
        has no way to know its own.
        """
        if self._account_id is not None:
            return self._account_id

        body = await self._get("/rest/api/3/myself", operation="update_issue")
        account_id = body.get("accountId") if isinstance(body, dict) else None
        if not isinstance(account_id, str) or not account_id:
            raise errors.JiraServerError(
                "Jira did not report an account id for the configured credentials, "
                "so assignee='me' cannot be resolved. Pass an explicit account id.",
                operation="update_issue",
                issue_key=issue_key,
            )
        self._account_id = account_id
        return account_id
