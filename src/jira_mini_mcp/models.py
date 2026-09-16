"""Typed response structures shared by jira.py and server.py.

Value normalization (ADF-to-Markdown, UTC timestamp conversion, compact
users, and known-vs-custom field handling) lives here, next to the shapes
it produces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class User:
    """A compact Jira user: no email, avatar, or `self` URL."""

    account_id: str
    display_name: str


@dataclass(frozen=True)
class NormalizationProblem:
    """One sanitized problem found while normalizing a Jira response."""

    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


class IncompleteNormalizationError(ValueError):
    """Known Jira resources were malformed, but a clean partial value exists."""

    def __init__(
        self,
        partial_value: dict[str, Any],
        problems: list[NormalizationProblem],
    ) -> None:
        self.partial_value = partial_value
        self.problems = tuple(problems)
        super().__init__("; ".join(str(problem) for problem in problems))


@dataclass(frozen=True)
class Page[T]:
    """A logical, offset-paginated collection (comments, changelog)."""

    start_at: int
    total: int
    items: list[T]


@dataclass(frozen=True)
class SearchPage[T]:
    """A cursor-paginated collection (search_issues)."""

    items: list[T]
    next_page_token: str | None


@dataclass(frozen=True)
class IssueSummary:
    """One search_issues result row."""

    key: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class IssueDetail:
    """The full get_issue result."""

    key: str
    fields: dict[str, Any]


@dataclass(frozen=True)
class Comment:
    """A single issue comment."""

    id: str
    author: User
    body: str
    created: str
    updated: str | None = None
    updated_by: User | None = None


@dataclass(frozen=True)
class Attachment:
    """Attachment metadata only -- never bytes or a Jira download URL."""

    id: str
    filename: str
    mime_type: str
    size: int
    author: User
    created: str


@dataclass(frozen=True)
class DownloadResult:
    """The result of downloading one attachment to the local cache."""

    attachment_id: str
    filename: str
    mime_type: str
    size: int
    local_path: str


@dataclass(frozen=True)
class ChangelogChange:
    """One field change within a changelog entry.

    `from_` because `from` is a Python keyword; the response-shaping layer
    is responsible for emitting the JSON key `from`, not this dataclass.
    """

    field: str
    from_: str | None
    to: str | None
    field_id: str | None = None


@dataclass(frozen=True)
class ChangelogEntry:
    """One changelog entry (a single Jira history record)."""

    id: str
    author: User
    created: str
    changes: list[ChangelogChange] = field(default_factory=list)


# --- Normalization -----------------------------------------------------

_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})$"
)


def compact_user(raw: dict[str, Any]) -> User:
    """Reduce a raw Jira user object to `account_id` and `display_name`."""
    return User(account_id=raw["accountId"], display_name=raw["displayName"])


def to_utc_iso(raw: str) -> str:
    """Normalize an explicit-offset ISO-8601 timestamp to UTC with a Z suffix."""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp is missing an explicit UTC offset: {raw!r}")
    utc = parsed.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _looks_like_iso_timestamp(value: str) -> bool:
    return bool(_TIMESTAMP_PATTERN.match(value))


def _render_text(node: dict[str, Any]) -> str:
    text = node.get("text", "")
    for mark in node.get("marks", []):
        mark_type = mark.get("type")
        if mark_type == "strong":
            text = f"**{text}**"
        elif mark_type == "em":
            text = f"*{text}*"
        elif mark_type == "code":
            text = f"`{text}`"
        elif mark_type == "link":
            href = mark.get("attrs", {}).get("href", "")
            text = f"[{text}]({href})"
    return text


def _render_node(node: dict[str, Any]) -> str:
    node_type = node.get("type")
    content = node.get("content", [])

    if node_type == "doc":
        return "\n\n".join(_render_node(c) for c in content)
    if node_type == "paragraph":
        return "".join(_render_node(c) for c in content)
    if node_type == "heading":
        level = node.get("attrs", {}).get("level", 1)
        text = "".join(_render_node(c) for c in content)
        return f"{'#' * level} {text}"
    if node_type == "text":
        return _render_text(node)
    if node_type == "hardBreak":
        return "\n"
    if node_type == "bulletList":
        return "\n".join(f"- {_render_node(c)}" for c in content)
    if node_type == "orderedList":
        return "\n".join(f"{i}. {_render_node(c)}" for i, c in enumerate(content, start=1))
    if node_type == "listItem":
        return "".join(_render_node(c) for c in content)
    if node_type == "blockquote":
        text = "\n".join(_render_node(c) for c in content)
        return "\n".join(f"> {line}" for line in text.splitlines())
    if node_type == "codeBlock":
        text = "".join(_render_node(c) for c in content)
        language = node.get("attrs", {}).get("language", "")
        return f"```{language}\n{text}\n```"
    if node_type == "mention":
        attrs = node.get("attrs", {})
        text = attrs.get("text") or attrs.get("displayName") or ""
        if text and not text.startswith("@"):
            text = f"@{text}"
        return text or "@mention"

    # Unsupported node: retain recursively extractable text instead of
    # dropping content or failing the whole conversion.
    if content:
        return "".join(_render_node(c) for c in content)
    return node.get("text") or node.get("attrs", {}).get("text", "")


def adf_to_markdown(adf: dict[str, Any]) -> str:
    """Convert a Jira Cloud Atlassian Document Format node to Markdown."""
    return _render_node(adf).strip()


def _normalize_unknown_value(value: Any) -> Any:
    """Recursively normalize embedded ADF docs and timestamps in custom JSON."""
    if isinstance(value, dict):
        if value.get("type") == "doc" and "content" in value:
            return adf_to_markdown(value)
        return {k: _normalize_unknown_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_unknown_value(v) for v in value]
    if isinstance(value, str) and _looks_like_iso_timestamp(value):
        try:
            return to_utc_iso(value)
        except ValueError:
            return value
    return value


_USER_FIELDS = frozenset({"assignee", "reporter"})
_DATE_FIELDS = frozenset({"created", "updated", "resolutiondate"})
_ADF_FIELDS = frozenset({"description"})

KNOWN_FIELDS = frozenset(
    {
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
    }
)


def _add_problem(problems: list[NormalizationProblem], path: str, reason: str) -> None:
    problems.append(NormalizationProblem(path=path, reason=reason))


def _required_string(
    raw: dict[str, Any],
    key: str,
    path: str,
    problems: list[NormalizationProblem],
) -> str | None:
    value = raw.get(key)
    if isinstance(value, str) and value:
        return value
    _add_problem(problems, f"{path}.{key}", "expected a non-empty string")
    return None


def _normalize_named_resource(
    raw: Any,
    path: str,
    problems: list[NormalizationProblem],
    *,
    require_key: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None

    resource_id = _required_string(raw, "id", path, problems)
    name = _required_string(raw, "name", path, problems)
    key = _required_string(raw, "key", path, problems) if require_key else None
    if resource_id is None or name is None or (require_key and key is None):
        return None

    result = {"id": resource_id}
    if require_key:
        result["key"] = key
    result["name"] = name
    return result


def _normalize_issuetype(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> dict[str, Any] | None:
    result = _normalize_named_resource(raw, path, problems)
    if result is None:
        return None

    hierarchy_level = raw.get("hierarchyLevel")
    if hierarchy_level is None:
        return result
    if isinstance(hierarchy_level, int) and not isinstance(hierarchy_level, bool):
        result["hierarchy_level"] = hierarchy_level
    else:
        _add_problem(problems, f"{path}.hierarchyLevel", "expected an integer")
    return result


def _normalize_status(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> dict[str, Any] | None:
    result = _normalize_named_resource(raw, path, problems)
    if result is None:
        return None

    status_category = raw.get("statusCategory")
    if status_category is None:
        return result
    if not isinstance(status_category, dict):
        _add_problem(problems, f"{path}.statusCategory", "expected an object")
        return result

    category = status_category.get("key")
    if category is None:
        return result
    if isinstance(category, str) and category:
        result["category"] = category
    else:
        _add_problem(
            problems,
            f"{path}.statusCategory.key",
            "expected a non-empty string",
        )
    return result


def _normalize_user(raw: Any, path: str, problems: list[NormalizationProblem]) -> User | None:
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None
    account_id = _required_string(raw, "accountId", path, problems)
    display_name = _required_string(raw, "displayName", path, problems)
    if account_id is None or display_name is None:
        return None
    return User(account_id=account_id, display_name=display_name)


def _normalize_issue_reference(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None
    key = _required_string(raw, "key", path, problems)
    if key is None:
        return None

    result: dict[str, Any] = {"key": key}
    raw_fields = raw.get("fields")
    if raw_fields is None:
        return result
    if not isinstance(raw_fields, dict):
        _add_problem(problems, f"{path}.fields", "expected an object")
        return result

    summary = raw_fields.get("summary")
    if summary is not None:
        if isinstance(summary, str):
            result["summary"] = summary
        else:
            _add_problem(problems, f"{path}.fields.summary", "expected a string")

    status = raw_fields.get("status")
    if status is not None:
        normalized_status = _normalize_status(status, f"{path}.fields.status", problems)
        if normalized_status is not None:
            result["status"] = normalized_status

    issue_type = raw_fields.get("issuetype")
    if issue_type is not None:
        normalized_type = _normalize_issuetype(issue_type, f"{path}.fields.issuetype", problems)
        if normalized_type is not None:
            result["issuetype"] = normalized_type
    return result


def _normalize_resource_list(
    raw: Any,
    path: str,
    problems: list[NormalizationProblem],
    normalizer: Any,
) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        _add_problem(problems, path, "expected an array")
        return None
    result: list[dict[str, Any]] = []
    for index, value in enumerate(raw):
        normalized = normalizer(value, f"{path}[{index}]", problems)
        if normalized is not None:
            result.append(normalized)
    return result


def _normalize_issue_link(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None

    directions = [name for name in ("inwardIssue", "outwardIssue") if name in raw]
    if len(directions) != 1:
        _add_problem(
            problems,
            path,
            "expected exactly one of inwardIssue or outwardIssue",
        )
        return None

    direction = directions[0]
    relationship_key = "inward" if direction == "inwardIssue" else "outward"
    link_type = raw.get("type")
    if not isinstance(link_type, dict):
        _add_problem(problems, f"{path}.type", "expected an object")
        return None
    relationship = _required_string(link_type, relationship_key, f"{path}.type", problems)
    issue = _normalize_issue_reference(raw.get(direction), f"{path}.{direction}", problems)
    if relationship is None or issue is None:
        return None
    return {"relationship": relationship, "issue": issue}


def normalize_comment(raw: Any, path: str, problems: list[NormalizationProblem]) -> Comment | None:
    """Normalize one raw Jira comment; return None if a required part is unusable.

    `id`, `created`, `author`, and `body` are required for a comment to be
    minimally useful, matching how a search item is dropped when its `key` is
    unusable. `updated`/`updated_by` are optional and are simply omitted, not
    dropped, when malformed.
    """
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None

    comment_id = _required_string(raw, "id", path, problems)

    created_raw = raw.get("created")
    created: str | None = None
    if isinstance(created_raw, str) and created_raw:
        try:
            created = to_utc_iso(created_raw)
        except ValueError:
            _add_problem(
                problems,
                f"{path}.created",
                "expected an ISO-8601 timestamp with an explicit offset",
            )
    else:
        _add_problem(problems, f"{path}.created", "expected a non-empty string")

    author = _normalize_user(raw.get("author"), f"{path}.author", problems)

    body_raw = raw.get("body")
    body: str | None = None
    if isinstance(body_raw, dict):
        body = adf_to_markdown(body_raw)
    else:
        _add_problem(problems, f"{path}.body", "expected an ADF document object")

    if comment_id is None or created is None or author is None or body is None:
        return None

    updated: str | None = None
    updated_by: User | None = None
    updated_raw = raw.get("updated")
    if isinstance(updated_raw, str) and updated_raw:
        try:
            updated_norm = to_utc_iso(updated_raw)
        except ValueError:
            _add_problem(
                problems,
                f"{path}.updated",
                "expected an ISO-8601 timestamp with an explicit offset",
            )
        else:
            if updated_norm != created:
                updated = updated_norm
                update_author_raw = raw.get("updateAuthor")
                if update_author_raw is not None:
                    update_author = _normalize_user(
                        update_author_raw, f"{path}.updateAuthor", problems
                    )
                    if update_author is not None and update_author.account_id != author.account_id:
                        updated_by = update_author
    elif updated_raw is not None:
        _add_problem(problems, f"{path}.updated", "expected a string")

    return Comment(
        id=comment_id,
        author=author,
        body=body,
        created=created,
        updated=updated,
        updated_by=updated_by,
    )


def normalize_issue_fields(raw_fields: dict[str, Any], *, path: str = "$.fields") -> dict[str, Any]:
    """Normalize a raw Jira `fields` object into its compact response form.

    Absent or null values are omitted rather than emitted as `null`. Known
    fields get their specific normalization (users, UTC dates, ADF); values
    outside KNOWN_FIELDS -- including all `customfield_*` -- pass through as
    Jira JSON, except that embedded ADF documents and explicit-offset
    timestamps are still normalized recursively.
    """
    result: dict[str, Any] = {}
    problems: list[NormalizationProblem] = []
    for key, value in raw_fields.items():
        if value is None:
            continue

        if key in _USER_FIELDS:
            user = _normalize_user(value, f"{path}.{key}", problems)
            if user is not None:
                result[key] = user
            continue

        if key in _DATE_FIELDS:
            if isinstance(value, str):
                try:
                    result[key] = to_utc_iso(value)
                except ValueError:
                    _add_problem(
                        problems,
                        f"{path}.{key}",
                        "expected an ISO-8601 timestamp with an explicit offset",
                    )
            else:
                _add_problem(problems, f"{path}.{key}", "expected a string")
            continue

        if key in _ADF_FIELDS:
            result[key] = adf_to_markdown(value) if isinstance(value, dict) else value
            continue

        resource_path = f"{path}.{key}"
        if key == "issuetype":
            normalized = _normalize_issuetype(value, resource_path, problems)
            if normalized is not None:
                result[key] = normalized
            continue
        if key == "status":
            normalized = _normalize_status(value, resource_path, problems)
            if normalized is not None:
                result[key] = normalized
            continue
        if key == "priority":
            normalized = _normalize_named_resource(value, resource_path, problems)
            if normalized is not None:
                result[key] = normalized
            continue
        if key == "project":
            normalized = _normalize_named_resource(value, resource_path, problems, require_key=True)
            if normalized is not None:
                result[key] = normalized
            continue
        if key == "components":
            normalized = _normalize_resource_list(
                value, resource_path, problems, _normalize_named_resource
            )
            if normalized is not None:
                result[key] = normalized
            continue
        if key == "parent":
            normalized = _normalize_issue_reference(value, resource_path, problems)
            if normalized is not None:
                result[key] = normalized
            continue
        if key == "subtasks":
            normalized = _normalize_resource_list(
                value, resource_path, problems, _normalize_issue_reference
            )
            if normalized is not None:
                result[key] = normalized
            continue
        if key == "issuelinks":
            normalized = _normalize_resource_list(
                value, resource_path, problems, _normalize_issue_link
            )
            if normalized is not None:
                result[key] = normalized
            continue

        if key in KNOWN_FIELDS:
            result[key] = value
            continue

        result[key] = _normalize_unknown_value(value)

    if problems:
        raise IncompleteNormalizationError(result, problems)
    return result
