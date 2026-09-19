"""MCP server wiring: registers the Jira tools over stdio.

Registration goes through `_TOOL_SPECS` rather than a straight run of
`add_tool` calls so READ_ONLY_MODE can filter it on each tool's read-only
annotation -- never on tool names, so a future write tool only has to
annotate itself correctly to be gated.

Tool adapters are thin: validate/default through `JiraClient`'s own
signatures, call it, translate `JiraMiniError` into `ToolError` (so the
client sees the actionable message instead of a generic crash string), and
shape the dataclass result into the exact JSON contract from
PROJECT-CONTRACTS.md -- omitting genuinely-absent optional fields while
keeping semantically-nullable ones (`next_page_token`, a changelog change's
`from`/`to`) as explicit `null`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from jira_mini_mcp import errors, oauth
from jira_mini_mcp.auth import (
    BasicTokenAuth,
    ConfigError,
    JiraConfig,
    OAuthConfig,
    load_auth_method,
    load_config_from_env,
    load_disable_structured_output,
    load_oauth_config,
    load_read_only_mode,
)
from jira_mini_mcp.jira import (
    HTTP_TIMEOUT,
    ISSUE_DEFAULT_FIELDS,
    SEARCH_DEFAULT_FIELDS,
    JiraClient,
)
from jira_mini_mcp.models import (
    Attachment,
    ChangelogChange,
    ChangelogEntry,
    Comment,
    DownloadResult,
    IssueDetail,
    IssueSummary,
    Page,
    SearchPage,
    TransitionResult,
    UpdateResult,
    User,
)

_READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

# Write annotations are what READ_ONLY_MODE gates on, so they state what
# each tool really does: adding a comment only appends, setting fields
# overwrites but lands in the same state when repeated, and replaying a
# transition from the status it produced usually fails outright.
_WRITE_ADDITIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)
_WRITE_UPDATE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)
_WRITE_TRANSITION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)


@dataclass(frozen=True)
class _ToolSpec:
    """One registerable tool, as data, so the gate below can filter it."""

    fn: Callable[..., Any]
    description: str
    annotations: ToolAnnotations
    structured_output: bool | None = None


@dataclass
class AppContext:
    """The one shared resource bundle every tool reaches through `Context`."""

    jira_client: JiraClient


@asynccontextmanager
async def app_lifespan(server: MCPServer[AppContext]) -> AsyncIterator[AppContext]:
    """Validate configuration, then own the one HTTP client and attachment cache.

    Configuration failures raise before either resource is created, so
    nothing needs cleanup in that case. Both are closed/removed on normal
    shutdown; the cache removal is offloaded so it never blocks the loop.
    """
    config = load_config_from_env()
    cache_dir = Path(tempfile.mkdtemp(prefix="jira-mini-mcp-"))
    try:
        async with httpx2.AsyncClient(timeout=HTTP_TIMEOUT) as http_client:
            yield AppContext(jira_client=_build_jira_client(config, http_client, cache_dir))
    finally:
        await asyncio.to_thread(shutil.rmtree, cache_dir, ignore_errors=True)


def _build_jira_client(
    config: JiraConfig | OAuthConfig, http_client: httpx2.AsyncClient, cache_dir: Path
) -> JiraClient:
    """Pick the auth for the configured method.

    OAuth never fails here when nobody has logged in yet: the server still
    starts, and each tool call reports how to run `jira-mini-mcp login`.
    """
    if isinstance(config, OAuthConfig):
        session = oauth.OAuthSession(config, http_client)
        return JiraClient(
            http_client,
            oauth.OAuthBearerAuth(session),
            session.api_base_url,
            cache_dir,
            auth_hint=errors.OAUTH_AUTH_HINT,
        )
    return JiraClient(
        http_client, BasicTokenAuth(config.email, config.api_token), config.base_url, cache_dir
    )


def _jira_client(ctx: Context[AppContext]) -> JiraClient:
    return ctx.request_context.lifespan_context.jira_client


async def _call[T](awaitable: Awaitable[T]) -> T:
    """Run one `JiraClient` call, translating `JiraMiniError` into `ToolError`.

    A plain exception would reach the model as a generic "Error executing
    tool ..." with no detail, discarding the actionable, pre-sanitized
    message `errors.py` built -- `ToolError` is what keeps that text intact.
    """
    try:
        return await awaitable
    except errors.JiraIncompleteResponseError as exc:
        raise ToolError(_incomplete_message(exc)) from exc
    except errors.JiraMiniError as exc:
        raise ToolError(str(exc)) from exc


def _incomplete_message(exc: errors.JiraIncompleteResponseError) -> str:
    partial = _rename_from_key(exc.partial_result)
    partial_json = json.dumps(partial, separators=(",", ":"), sort_keys=True)
    return f"{exc}\nPartial result: {partial_json}"


def _rename_from_key(value: Any) -> Any:
    """`asdict()` keeps `ChangelogChange.from_`'s Python name; the public key is `from`."""
    if isinstance(value, dict):
        return {("from" if k == "from_" else k): _rename_from_key(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_rename_from_key(v) for v in value]
    return value


def _dump_user(user: User) -> dict[str, str]:
    return {"account_id": user.account_id, "display_name": user.display_name}


def _dump_field_value(value: Any) -> Any:
    """Recursively convert any `User` found inside a normalized `fields` object.

    Every other known-resource shape in `fields` is already a plain,
    contract-shaped dict (see `models.normalize_issue_fields`).
    """
    if isinstance(value, User):
        return _dump_user(value)
    if isinstance(value, dict):
        return {key: _dump_field_value(v) for key, v in value.items()}
    if isinstance(value, list):
        return [_dump_field_value(v) for v in value]
    return value


def _dump_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {key: _dump_field_value(value) for key, value in fields.items()}


def _dump_issue_summary(item: IssueSummary) -> dict[str, Any]:
    return {"key": item.key, "fields": _dump_fields(item.fields)}


def _dump_issue_detail(detail: IssueDetail) -> dict[str, Any]:
    return {"key": detail.key, "fields": _dump_fields(detail.fields)}


def _dump_search_page(page: SearchPage[IssueSummary]) -> dict[str, Any]:
    return {
        "items": [_dump_issue_summary(item) for item in page.items],
        "next_page_token": page.next_page_token,
    }


def _dump_comment(comment: Comment) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": comment.id,
        "author": _dump_user(comment.author),
        "body": comment.body,
        "created": comment.created,
    }
    if comment.updated is not None:
        result["updated"] = comment.updated
    if comment.updated_by is not None:
        result["updated_by"] = _dump_user(comment.updated_by)
    return result


def _dump_comments_page(page: Page[Comment]) -> dict[str, Any]:
    return {
        "start_at": page.start_at,
        "total": page.total,
        "items": [_dump_comment(item) for item in page.items],
    }


def _dump_attachment(attachment: Attachment) -> dict[str, Any]:
    return {
        "id": attachment.id,
        "filename": attachment.filename,
        "mime_type": attachment.mime_type,
        "size": attachment.size,
        "author": _dump_user(attachment.author),
        "created": attachment.created,
    }


def _dump_download_result(result: DownloadResult) -> dict[str, Any]:
    return {
        "attachment_id": result.attachment_id,
        "filename": result.filename,
        "mime_type": result.mime_type,
        "size": result.size,
        "local_path": result.local_path,
    }


def _dump_changelog_change(change: ChangelogChange) -> dict[str, Any]:
    result: dict[str, Any] = {"field": change.field, "from": change.from_, "to": change.to}
    if change.field_id is not None:
        result["field_id"] = change.field_id
    return result


def _dump_changelog_entry(entry: ChangelogEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "author": _dump_user(entry.author),
        "created": entry.created,
        "changes": [_dump_changelog_change(change) for change in entry.changes],
    }


def _dump_changelog_page(page: Page[ChangelogEntry]) -> dict[str, Any]:
    return {
        "start_at": page.start_at,
        "total": page.total,
        "items": [_dump_changelog_entry(item) for item in page.items],
    }


def _dump_transition_result(result: TransitionResult) -> dict[str, Any]:
    """Flat on purpose: the transition's own name and the status it produced
    are different things, and an agent that conflates them picks wrong."""
    return {
        "key": result.key,
        "transition": {"id": result.transition.id, "name": result.transition.name},
        "status": result.transition.status,
    }


def _dump_update_result(result: UpdateResult) -> dict[str, Any]:
    return {"key": result.key, "updated_fields": list(result.updated_fields)}


async def search_issues(
    jql: str,
    ctx: Context[AppContext],
    page_token: str | None = None,
    limit: int = 20,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    page = await _call(
        _jira_client(ctx).search_issues(jql, page_token=page_token, limit=limit, fields=fields)
    )
    return _dump_search_page(page)


async def get_issue(
    issue_key: str,
    ctx: Context[AppContext],
    fields: list[str] | None = None,
) -> dict[str, Any]:
    detail = await _call(_jira_client(ctx).get_issue(issue_key, fields=fields))
    return _dump_issue_detail(detail)


async def get_comments(
    issue_key: str,
    ctx: Context[AppContext],
    start_at: int = 0,
    limit: int = 20,
    order: str = "desc",
    since: str | None = None,
) -> dict[str, Any]:
    page = await _call(
        _jira_client(ctx).get_comments(
            issue_key, start_at=start_at, limit=limit, order=order, since=since
        )
    )
    return _dump_comments_page(page)


async def get_attachments(issue_key: str, ctx: Context[AppContext]) -> dict[str, Any]:
    """Returns `{items}` rather than a bare list.

    A bare list can't be a `structuredContent` object per the MCP spec, and
    this SDK version also splits an unwrapped list return into one content
    block per item instead of one array block. Wrapping under `items` -- the
    same convention search_issues/get_comments/get_changelog already use --
    gives one coherent JSON block on both channels; a deliberate, documented
    departure from PROJECT-CONTRACTS.md's original bare-list wording.
    """
    attachments = await _call(_jira_client(ctx).get_attachments(issue_key))
    return {"items": [_dump_attachment(item) for item in attachments]}


async def download_attachment(attachment_id: str, ctx: Context[AppContext]) -> dict[str, Any]:
    result = await _call(_jira_client(ctx).download_attachment(attachment_id))
    return _dump_download_result(result)


async def get_changelog(
    issue_key: str,
    ctx: Context[AppContext],
    start_at: int = 0,
    limit: int = 20,
    order: str = "desc",
) -> dict[str, Any]:
    page = await _call(
        _jira_client(ctx).get_changelog(issue_key, start_at=start_at, limit=limit, order=order)
    )
    return _dump_changelog_page(page)


async def add_comment(issue_key: str, body: str, ctx: Context[AppContext]) -> dict[str, Any]:
    comment = await _call(_jira_client(ctx).add_comment(issue_key, body))
    return _dump_comment(comment)


async def transition_issue(
    issue_key: str,
    to: str,
    ctx: Context[AppContext],
    comment: str | None = None,
) -> dict[str, Any]:
    result = await _call(_jira_client(ctx).transition_issue(issue_key, to, comment=comment))
    return _dump_transition_result(result)


async def update_issue(
    issue_key: str, fields: dict[str, Any], ctx: Context[AppContext]
) -> dict[str, Any]:
    result = await _call(_jira_client(ctx).update_issue(issue_key, fields))
    return _dump_update_result(result)


_TOOL_SPECS: tuple[_ToolSpec, ...] = (
    _ToolSpec(
        search_issues,
        (
            "Search Jira Cloud issues with JQL. Returns items and an opaque "
            "next_page_token (null on the last page); pass it back as page_token "
            "for the next page. limit is 1..100 (default 20); 0 is invalid. fields "
            "replaces the default fields entirely -- default: "
            f"{', '.join(SEARCH_DEFAULT_FIELDS)}. fields=[] returns only the issue key."
        ),
        _READ_ONLY,
    ),
    _ToolSpec(
        get_issue,
        (
            "Fetch one issue by key. fields replaces the default fields entirely -- "
            f"default: {', '.join(ISSUE_DEFAULT_FIELDS)}. fields=[] returns the key "
            "with no fields. Requesting 'watches' or 'votes' resolves the real "
            "watcher/voter list (watch_count/is_watching/watchers, "
            "vote_count/has_voted/voters) via one extra request per field, instead "
            "of Jira's own link-only stub. Does not include comments, attachments, "
            "or changelog history; use the dedicated tools for those."
        ),
        _READ_ONLY,
    ),
    _ToolSpec(
        get_comments,
        (
            "List an issue's comments. order='desc' (default) returns newest "
            "first; 'asc' returns oldest first. start_at/limit paginate the "
            "logical, filtered, sorted collection (ties broken by comment id); "
            "limit=0 returns every remaining comment from start_at with no "
            "100-comment cap. since (ISO-8601 with an explicit offset) keeps only "
            "comments created at or after that instant, and total reflects the "
            "filtered collection."
        ),
        _READ_ONLY,
    ),
    _ToolSpec(
        get_attachments,
        (
            "List an issue's attachment metadata (id, filename, mime_type, size, "
            "author, created) without downloading content. Use download_attachment "
            "to fetch a file's bytes."
        ),
        _READ_ONLY,
    ),
    _ToolSpec(
        download_attachment,
        (
            "Download one attachment by id into a process-scoped temporary cache "
            "and return its local_path. The cache is removed when the server "
            "shuts down."
        ),
        _READ_ONLY,
    ),
    _ToolSpec(
        get_changelog,
        (
            "List an issue's field-change history. order='desc' (default) returns "
            "newest first; 'asc' returns oldest first. start_at/limit paginate the "
            "logical, sorted collection (ties broken by entry id); limit=0 returns "
            "every remaining entry from start_at with no cap. Each entry lists "
            "human-readable field changes."
        ),
        _READ_ONLY,
    ),
    _ToolSpec(
        add_comment,
        (
            "Add one comment to an issue. body is Markdown -- headings, lists, "
            "fenced code blocks, bold/italic, inline code, links -- converted to "
            "Jira's rich text; anything outside that set stays literal. Returns "
            "the created comment in the same shape get_comments returns. This "
            "server cannot edit or delete a comment afterwards."
        ),
        _WRITE_ADDITIVE,
    ),
    _ToolSpec(
        transition_issue,
        (
            "Move an issue through its workflow. to is a transition name or the "
            "name of the status to reach, matched ignoring case and surrounding "
            "space. A transition's name often differs from the status it leads "
            "to (a transition called 'In Progress' can produce status 'In "
            "Development'), and two transitions can reach one status, so prefer "
            "the transition name; if nothing matches, the error lists every "
            "available transition and its resulting status. comment is Markdown "
            "and is posted in the same call as the move. Changes issue state."
        ),
        _WRITE_TRANSITION,
    ),
    _ToolSpec(
        update_issue,
        (
            "Set issue fields, taking the same values get_issue returns: summary "
            "as text, description as Markdown, assignee as an account id, an email, "
            'a display name, or the literal "me" (an ambiguous name is an error '
            "listing the candidates), labels as a list, components and priority by name, "
            "duedate as YYYY-MM-DD, parent as an issue key, and any "
            "customfield_* or unknown field as raw Jira JSON. null clears "
            "assignee, description, priority, parent, or duedate. labels and "
            "components REPLACE the whole list, so read the issue first if you "
            "mean to add one. Cannot change status (use transition_issue) or add "
            "a comment (use add_comment)."
        ),
        _WRITE_UPDATE,
    ),
)


def _tool_name(spec: _ToolSpec) -> str:
    # spec.fn is always a plain `async def` function defined in this module,
    # but Callable itself has no __name__ in typeshed, so ty needs getattr.
    return getattr(spec.fn, "__name__")  # noqa: B009


def _registered_tools(specs: Sequence[_ToolSpec], *, read_only_mode: bool) -> tuple[_ToolSpec, ...]:
    """Select the tools to register, gating on the read-only annotation.

    Deliberately not a name allowlist: a future write tool becomes
    gate-aware purely by annotating itself, with no edit here.
    """
    if not read_only_mode:
        return tuple(specs)
    return tuple(spec for spec in specs if spec.annotations.read_only_hint)


def create_server(
    *,
    lifespan: Callable[
        [MCPServer[AppContext]], AbstractAsyncContextManager[AppContext]
    ] = app_lifespan,
    read_only_mode: bool | None = None,
    disable_structured_output: frozenset[str] | None = None,
) -> MCPServer[AppContext]:
    """Build the server with its tools registered but not yet running.

    `read_only_mode` defaults to the READ_ONLY_MODE environment value, and
    `disable_structured_output` defaults to the DISABLE_STRUCTURED_OUTPUT
    environment value, so an unrecognized setting stops startup here --
    before any tool is registered -- rather than at the first call.
    """
    if read_only_mode is None:
        read_only_mode = load_read_only_mode()
    if disable_structured_output is None:
        disable_structured_output = load_disable_structured_output(
            valid_tool_names=frozenset(_tool_name(spec) for spec in _TOOL_SPECS)
        )

    server: MCPServer[AppContext] = MCPServer(name="jira-mini-mcp", lifespan=lifespan)
    for spec in _registered_tools(_TOOL_SPECS, read_only_mode=read_only_mode):
        structured_output = spec.structured_output
        if _tool_name(spec) in disable_structured_output:
            structured_output = False
        server.add_tool(
            spec.fn,
            description=spec.description,
            annotations=spec.annotations,
            structured_output=structured_output,
        )

    return server


def _silence_request_logging() -> None:
    """Keep the tenant URL out of whatever log the host is running.

    httpx2 logs every request line at INFO, which includes the configured
    Jira host and the full query string -- a JQL query among it. A host
    that turns on INFO logging would collect exactly what AGENTS.md says
    never to log. Done here rather than at import time: a process's logging
    configuration belongs to whoever owns the process, and this function
    owns only the shipped stdio entry point.
    """
    logging.getLogger("httpx2").setLevel(logging.WARNING)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="jira-mini-mcp",
        description="Minimal Jira Cloud MCP server. Without a command, serves MCP over stdio.",
    )
    commands = parser.add_subparsers(dest="command")
    login = commands.add_parser(
        "login", help="authorize through the browser (JIRA_AUTH_METHOD=oauth)"
    )
    login.add_argument(
        "--port",
        type=int,
        default=oauth.DEFAULT_CALLBACK_PORT,
        help=(
            "localhost port for the OAuth callback; the app's callback URL must be "
            f"http://localhost:<port>{oauth.CALLBACK_PATH} (default: %(default)s)"
        ),
    )
    commands.add_parser("logout", help="delete the stored OAuth authorization")
    return parser.parse_args(argv)


def _oauth_command(command: str, port: int) -> int:
    """Run `login` or `logout`; return the process exit code.

    Status goes to stderr like everything else this program prints, and
    names no configured value.
    """
    try:
        if load_auth_method() != "oauth":
            raise ConfigError(
                f"`jira-mini-mcp {command}` needs JIRA_AUTH_METHOD=oauth; with an API "
                "token there is nothing to log in to."
            )
        config = load_oauth_config()
        if command == "logout":
            removed = oauth.delete_tokens(oauth.token_store_path(config))
            print(
                "jira-mini-mcp: OAuth authorization removed."
                if removed
                else "jira-mini-mcp: no OAuth authorization was stored.",
                file=sys.stderr,
            )
            return 0

        async def run_login() -> Path:
            async with httpx2.AsyncClient(timeout=HTTP_TIMEOUT) as http_client:
                return await oauth.login(config, http_client, port=port)

        path = asyncio.run(run_login())
    except errors.JiraMiniError as exc:
        print(f"jira-mini-mcp: {exc}", file=sys.stderr)
        return 1
    print(f"jira-mini-mcp: authorized. Tokens saved to {path}", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.command is not None:
        sys.exit(_oauth_command(args.command, getattr(args, "port", 0)))

    _silence_request_logging()
    if load_auth_method() == "oauth":
        # Like READ_ONLY_MODE below: name the non-default mode, never a value.
        print("jira-mini-mcp: JIRA_AUTH_METHOD=oauth.", file=sys.stderr)
    read_only_mode = load_read_only_mode()
    if read_only_mode:
        # Announce only the non-default mode: the default is evident from
        # the advertised tool list, while a restricted surface would
        # otherwise leave an operator guessing why a tool vanished. stdout
        # carries the MCP protocol, so this goes to stderr, and it names
        # the mode without echoing any configured value.
        print(
            "jira-mini-mcp: READ_ONLY_MODE enabled; registering read-only tools only.",
            file=sys.stderr,
        )
    disable_structured_output = load_disable_structured_output(
        valid_tool_names=frozenset(_tool_name(spec) for spec in _TOOL_SPECS)
    )
    create_server(
        read_only_mode=read_only_mode,
        disable_structured_output=disable_structured_output,
    ).run()


if __name__ == "__main__":
    main()
