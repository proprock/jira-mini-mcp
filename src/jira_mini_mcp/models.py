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
    """The full get_issue result.

    `field_names` maps a returned `customfield_*` id to its display name; it is
    filled only when the caller asked for such a field.
    """

    key: str
    fields: dict[str, Any]
    field_names: dict[str, str] = field(default_factory=dict)


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


@dataclass(frozen=True)
class Transition:
    """One workflow transition and the status it leads to.

    Jira names a transition independently of its target status -- a
    transition called "In Progress" can lead to a status called "In
    Development", and two differently named transitions can reach one
    status -- so both names have to survive normalization.
    """

    id: str
    name: str
    status: dict[str, Any]


@dataclass(frozen=True)
class TransitionResult:
    """Which transition ran, and where it left the issue."""

    key: str
    transition: Transition


@dataclass(frozen=True)
class UpdateResult:
    """Which fields `update_issue` sent, for the issue it sent them to."""

    key: str
    updated_fields: tuple[str, ...]


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


_FENCE_PATTERN = re.compile(r"^```(\w*)\s*$")
_HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_PATTERN = re.compile(r"^[-*]\s+(.*)$")
_ORDERED_PATTERN = re.compile(r"^\d+\.\s+(.*)$")

# One pass, no nesting: the leftmost delimiter wins and its content stays
# literal. A delimiter that is not closed, or that hugs whitespace, simply
# does not match and survives as text -- which is what keeps `2 * 3 * 4`
# and `next_page_token` out of the emphasis rules. An image keeps its
# whole source too: silently demoting it to a link would change what the
# author wrote, and ADF media cannot be built from an arbitrary URL.
_INLINE_PATTERN = re.compile(
    r"`(?P<code>[^`]+)`"
    r"|(?<!!)\[(?P<link_text>[^\]]*)\]\((?P<href>[^)\s]*)\)"
    r"|\*\*(?P<strong>\S(?:.*?\S)?)\*\*"
    r"|\*(?P<em>[^\s*](?:[^*]*?[^\s*])?)\*"
    r"|(?<![A-Za-z0-9_])_(?P<em_underscore>[^\s_](?:[^_]*?[^\s_])?)_(?![A-Za-z0-9_])"
)


def markdown_to_adf(markdown: str) -> dict[str, Any]:
    """Convert Markdown to a Jira Cloud Atlassian Document Format document.

    The inverse of `adf_to_markdown` over the same node set, so the pair
    round-trips the Markdown that function emits. Anything outside that
    vocabulary -- tables, images, raw HTML, reference links, an unclosed
    delimiter -- is kept as literal text rather than guessed at: Jira
    answers a malformed document with a 400 an agent cannot act on, so no
    input may produce one.
    """
    if not markdown.strip():
        raise ValueError(
            "The Markdown body is empty. Provide at least one non-whitespace character."
        )

    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return {"type": "doc", "version": 1, "content": _parse_blocks(lines)}


def _parse_blocks(lines: list[str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue

        fence = _FENCE_PATTERN.match(stripped)
        if fence is not None:
            block, index = _parse_code_block(lines, index, fence.group(1))
        elif (heading := _HEADING_PATTERN.match(stripped)) is not None:
            block = {
                "type": "heading",
                "attrs": {"level": len(heading.group(1))},
                "content": _parse_inline(heading.group(2).strip()),
            }
            index += 1
        elif _BULLET_PATTERN.match(stripped) is not None:
            block, index = _parse_list(lines, index, _BULLET_PATTERN, "bulletList")
        elif _ORDERED_PATTERN.match(stripped) is not None:
            block, index = _parse_list(lines, index, _ORDERED_PATTERN, "orderedList")
        elif stripped.startswith(">"):
            block, index = _parse_blockquote(lines, index)
        else:
            block, index = _parse_paragraph(lines, index)
        blocks.append(block)
    return blocks


def _parse_code_block(lines: list[str], index: int, language: str) -> tuple[dict[str, Any], int]:
    """Collect a fenced block verbatim; an unclosed fence takes the rest."""
    body: list[str] = []
    index += 1
    while index < len(lines) and lines[index].strip() != "```":
        body.append(lines[index])
        index += 1

    block: dict[str, Any] = {"type": "codeBlock"}
    if language:
        block["attrs"] = {"language": language}
    text = "\n".join(body)
    block["content"] = [{"type": "text", "text": text}] if text else []
    return block, index + 1


def _parse_list(
    lines: list[str], index: int, pattern: re.Pattern[str], list_type: str
) -> tuple[dict[str, Any], int]:
    items: list[dict[str, Any]] = []
    while index < len(lines) and (match := pattern.match(lines[index].strip())) is not None:
        items.append(
            {
                "type": "listItem",
                "content": [
                    {"type": "paragraph", "content": _parse_inline(match.group(1).strip())}
                ],
            }
        )
        index += 1
    return {"type": list_type, "content": items}, index


def _parse_blockquote(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    quoted: list[str] = []
    while index < len(lines) and lines[index].strip().startswith(">"):
        quoted.append(lines[index].strip().removeprefix(">").lstrip())
        index += 1
    return {"type": "blockquote", "content": _parse_blocks(quoted)}, index


def _parse_paragraph(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    """Consecutive plain lines are one paragraph; the breaks between them
    are hardBreak nodes, which is how `adf_to_markdown` renders them back."""
    content: list[dict[str, Any]] = []
    while index < len(lines) and _starts_a_paragraph_line(lines[index]):
        if content:
            content.append({"type": "hardBreak"})
        content.extend(_parse_inline(lines[index].strip()))
        index += 1
    return {"type": "paragraph", "content": content}, index


def _starts_a_paragraph_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith(">"):
        return False
    return not any(
        pattern.match(stripped) is not None
        for pattern in (_FENCE_PATTERN, _HEADING_PATTERN, _BULLET_PATTERN, _ORDERED_PATTERN)
    )


def _parse_inline(text: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    position = 0
    # finditer yields non-overlapping matches left to right, so a match
    # never starts before the text already consumed.
    for match in _INLINE_PATTERN.finditer(text):
        marked = _marked_text(match)
        if marked is None:
            continue
        literal = text[position : match.start()]
        if literal:
            nodes.append({"type": "text", "text": literal})
        nodes.append(marked)
        position = match.end()

    tail = text[position:]
    if tail:
        nodes.append({"type": "text", "text": tail})
    return nodes


def _marked_text(match: re.Match[str]) -> dict[str, Any] | None:
    """The one marked text node for a delimiter match, or None to leave the
    matched source literal (an empty link label has no text to mark)."""
    if (code := match.group("code")) is not None:
        return {"type": "text", "text": code, "marks": [{"type": "code"}]}
    if (link_text := match.group("link_text")) is not None:
        if not link_text:
            return None
        return {
            "type": "text",
            "text": link_text,
            "marks": [{"type": "link", "attrs": {"href": match.group("href")}}],
        }
    if (strong := match.group("strong")) is not None:
        return {"type": "text", "text": strong, "marks": [{"type": "strong"}]}
    emphasis = match.group("em") or match.group("em_underscore")
    return {"type": "text", "text": emphasis, "marks": [{"type": "em"}]}


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
        "watches",
        "votes",
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


def _normalize_watches_stub(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> dict[str, Any] | None:
    """Normalize the `watches` reference stub embedded in an issue's `fields`.

    Jira's issue resource represents `watches` as `{self, watchCount,
    isWatching}` -- a REST link plus metadata, never the actual watcher list.
    This drops `self` and keeps the count/flag, which is already useful and
    safe without a follow-up request; `normalize_watchers` extends this same
    shape with the real `watchers` list for get_issue's explicit resolution.
    """
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None
    watch_count = raw.get("watchCount")
    is_watching = raw.get("isWatching")
    valid = True
    if not isinstance(watch_count, int) or isinstance(watch_count, bool) or watch_count < 0:
        _add_problem(problems, f"{path}.watchCount", "expected a non-negative integer")
        valid = False
    if not isinstance(is_watching, bool):
        _add_problem(problems, f"{path}.isWatching", "expected a boolean")
        valid = False
    if not valid:
        return None
    return {"watch_count": watch_count, "is_watching": is_watching}


def normalize_watchers(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> dict[str, Any] | None:
    """Normalize GET /issue/{key}/watchers into get_issue's resolved `watches` shape."""
    result = _normalize_watches_stub(raw, path, problems)
    if result is None:
        return None
    watchers = _normalize_resource_list(
        raw.get("watchers"), f"{path}.watchers", problems, _normalize_user
    )
    if watchers is None:
        return None
    result["watchers"] = watchers
    return result


def _normalize_votes_stub(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> dict[str, Any] | None:
    """Normalize the `votes` reference stub embedded in an issue's `fields`.

    Jira's issue resource represents `votes` as `{self, votes, hasVoted}` --
    a REST link plus metadata, never the actual voter list. The count field
    is renamed `vote_count` (Jira reuses "votes" for both the field key and
    the count) so it never collides with the outer `votes` field name.
    """
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None
    vote_count = raw.get("votes")
    has_voted = raw.get("hasVoted")
    valid = True
    if not isinstance(vote_count, int) or isinstance(vote_count, bool) or vote_count < 0:
        _add_problem(problems, f"{path}.votes", "expected a non-negative integer")
        valid = False
    if not isinstance(has_voted, bool):
        _add_problem(problems, f"{path}.hasVoted", "expected a boolean")
        valid = False
    if not valid:
        return None
    return {"vote_count": vote_count, "has_voted": has_voted}


def normalize_votes(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> dict[str, Any] | None:
    """Normalize GET /issue/{key}/votes into get_issue's resolved `votes` shape."""
    result = _normalize_votes_stub(raw, path, problems)
    if result is None:
        return None
    voters = _normalize_resource_list(
        raw.get("voters"), f"{path}.voters", problems, _normalize_user
    )
    if voters is None:
        return None
    result["voters"] = voters
    return result


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


def normalize_transition(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> Transition | None:
    """Normalize one entry of GET /issue/{key}/transitions."""
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None

    identifier = _required_string(raw, "id", path, problems)
    name = _required_string(raw, "name", path, problems)
    status = _normalize_status(raw.get("to"), f"{path}.to", problems)
    if identifier is None or name is None or status is None:
        return None
    return Transition(id=identifier, name=name, status=status)


def normalize_attachment(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> Attachment | None:
    """Normalize one raw Jira attachment; return None if a required part is unusable.

    Every field is required: a caller cannot usefully list or later download
    an attachment missing an id, filename, size, author, or created date.
    Jira's own two endpoints disagree on `id`'s JSON type (string when
    embedded in an issue's `fields.attachment`, integer from
    `GET /rest/api/3/attachment/{id}`), so both are accepted here.
    """
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None

    raw_id = raw.get("id")
    attachment_id: str | None = None
    if isinstance(raw_id, str) and raw_id:
        attachment_id = raw_id
    elif isinstance(raw_id, int) and not isinstance(raw_id, bool):
        attachment_id = str(raw_id)
    else:
        _add_problem(problems, f"{path}.id", "expected a non-empty string or integer")

    filename = _required_string(raw, "filename", path, problems)
    mime_type = _required_string(raw, "mimeType", path, problems)

    raw_size = raw.get("size")
    size: int | None = None
    if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0:
        size = raw_size
    else:
        _add_problem(problems, f"{path}.size", "expected a non-negative integer")

    author = _normalize_user(raw.get("author"), f"{path}.author", problems)

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

    if (
        attachment_id is None
        or filename is None
        or mime_type is None
        or size is None
        or author is None
        or created is None
    ):
        return None

    return Attachment(
        id=attachment_id,
        filename=filename,
        mime_type=mime_type,
        size=size,
        author=author,
        created=created,
    )


def normalize_changelog_change(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> ChangelogChange | None:
    """Normalize one raw Jira changelog history item.

    Jira's raw `from`/`to` carry internal option/user/version IDs -- often
    null even when a human-readable value exists (observed live 2026-09-16,
    e.g. a story-point change with `to: null, toString: "5"`) -- so this
    project only ever surfaces `fromString`/`toString`, never the internal
    IDs, and treats a missing or non-string one as "no value" rather than a
    normalization problem. `field_id` is surfaced only for a custom field
    (`fieldtype: "custom"`, `fieldId` like `customfield_10020`); a standard
    field's own `fieldId` (e.g. `status`) adds nothing `field` doesn't
    already say, and some standard-field items omit `fieldId` entirely.
    """
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None

    field_name = _required_string(raw, "field", path, problems)
    if field_name is None:
        return None

    from_raw = raw.get("fromString")
    from_value = from_raw if isinstance(from_raw, str) else None

    to_raw = raw.get("toString")
    to_value = to_raw if isinstance(to_raw, str) else None

    field_id: str | None = None
    if raw.get("fieldtype") == "custom":
        raw_field_id = raw.get("fieldId")
        if isinstance(raw_field_id, str) and raw_field_id:
            field_id = raw_field_id

    return ChangelogChange(field=field_name, from_=from_value, to=to_value, field_id=field_id)


def normalize_changelog_entry(
    raw: Any, path: str, problems: list[NormalizationProblem]
) -> ChangelogEntry | None:
    """Normalize one raw Jira changelog history entry; return None if a
    required part is unusable.

    `id`, `created`, and `author` are required, mirroring
    `normalize_comment`'s requirements. A malformed individual change item
    is dropped and reported on its own path; it never drops the whole entry,
    since sibling changes in the same history record remain valid.
    """
    if not isinstance(raw, dict):
        _add_problem(problems, path, "expected an object")
        return None

    entry_id = _required_string(raw, "id", path, problems)

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

    if entry_id is None or created is None or author is None:
        return None

    raw_items = raw.get("items", [])
    changes: list[ChangelogChange] = []
    if not isinstance(raw_items, list):
        _add_problem(problems, f"{path}.items", "expected a list")
    else:
        for index, raw_change in enumerate(raw_items):
            change = normalize_changelog_change(raw_change, f"{path}.items[{index}]", problems)
            if change is not None:
                changes.append(change)

    return ChangelogEntry(id=entry_id, author=author, created=created, changes=changes)


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
        if key == "watches":
            normalized = _normalize_watches_stub(value, resource_path, problems)
            if normalized is not None:
                result[key] = normalized
            continue
        if key == "votes":
            normalized = _normalize_votes_stub(value, resource_path, problems)
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
