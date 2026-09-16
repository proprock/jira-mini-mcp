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


def normalize_issue_fields(raw_fields: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw Jira `fields` object into its compact response form.

    Absent or null values are omitted rather than emitted as `null`. Known
    fields get their specific normalization (users, UTC dates, ADF); values
    outside KNOWN_FIELDS -- including all `customfield_*` -- pass through as
    Jira JSON, except that embedded ADF documents and explicit-offset
    timestamps are still normalized recursively.
    """
    result: dict[str, Any] = {}
    for key, value in raw_fields.items():
        if value is None:
            continue

        if key in _USER_FIELDS:
            if isinstance(value, dict):
                result[key] = compact_user(value)
            continue

        if key in _DATE_FIELDS:
            if isinstance(value, str):
                result[key] = to_utc_iso(value)
            continue

        if key in _ADF_FIELDS:
            result[key] = adf_to_markdown(value) if isinstance(value, dict) else value
            continue

        if key in KNOWN_FIELDS:
            result[key] = value
            continue

        result[key] = _normalize_unknown_value(value)

    return result
