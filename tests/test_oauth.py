"""Tests for jira_mini_mcp.oauth: token store, refresh, Bearer auth, and login."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from jira_mini_mcp import errors, oauth
from jira_mini_mcp.auth import OAuthConfig

pytestmark = pytest.mark.anyio

CONFIG = OAuthConfig(
    base_url="https://synthetic-tenant.atlassian.net",
    client_id="synthetic-client-id",
    client_secret="synthetic-client-secret",
)
CLOUD_ID = "00000000-0000-4000-8000-000000000001"
NOW = 1_800_000_000.0

Handler = Callable[[httpx2.Request], httpx2.Response]


def _tokens(**overrides: Any) -> oauth.StoredTokens:
    values: dict[str, Any] = {
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expires_at": NOW + 3600,
        "cloud_id": CLOUD_ID,
        "scopes": oauth.REQUIRED_SCOPES,
    }
    values.update(overrides)
    return oauth.StoredTokens(**values)


def _client(handler: Handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _session(
    handler: Handler, path: Path, clock: Callable[[], float] = lambda: NOW
) -> oauth.OAuthSession:
    return oauth.OAuthSession(CONFIG, _client(handler), store_path=path, clock=clock)


def _token_endpoint(responses: list[httpx2.Response], seen: list[dict[str, Any]]) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == oauth.TOKEN_URL
        seen.append(json.loads(request.content))
        return responses.pop(0)

    return handler


def _refreshed(access: str = "access-2", refresh: str | None = "refresh-2") -> httpx2.Response:
    body: dict[str, Any] = {"access_token": access, "expires_in": 3600, "scope": "x"}
    if refresh is not None:
        body["refresh_token"] = refresh
    return httpx2.Response(200, json=body)


class TestTokenStore:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "store" / "oauth.json"
        oauth.write_tokens(path, _tokens())
        assert oauth.read_tokens(path) == _tokens()

    def test_leaves_no_temporary_file(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        oauth.write_tokens(path, _tokens(access_token="access-2"))
        assert [p.name for p in tmp_path.iterdir()] == ["oauth.json"]

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_file_is_readable_by_the_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        assert path.stat().st_mode & 0o777 == 0o600

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            "[]",
            json.dumps({"version": 99}),
            json.dumps({"version": 1, "access_token": "a"}),
            json.dumps(
                {
                    "version": 1,
                    "access_token": "",
                    "refresh_token": "r",
                    "expires_at": 1,
                    "cloud_id": "c",
                }
            ),
            json.dumps(
                {
                    "version": 1,
                    "access_token": "a",
                    "refresh_token": "r",
                    "expires_at": "soon",
                    "cloud_id": "c",
                }
            ),
        ],
    )
    def test_unusable_file_reads_as_absent(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / "oauth.json"
        path.write_text(content, encoding="utf-8")
        assert oauth.read_tokens(path) is None

    def test_failed_write_keeps_the_old_file_and_no_temporary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())

        def failing_replace(src: Any, dst: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(oauth.os, "replace", failing_replace)
        with pytest.raises(OSError):
            oauth.write_tokens(path, _tokens(access_token="access-2"))

        assert [p.name for p in tmp_path.iterdir()] == ["oauth.json"]
        assert oauth.read_tokens(path) == _tokens()

    def test_missing_file_reads_as_absent(self, tmp_path: Path) -> None:
        assert oauth.read_tokens(tmp_path / "absent.json") is None

    def test_delete_reports_whether_a_file_existed(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        assert oauth.delete_tokens(path) is True
        assert oauth.delete_tokens(path) is False

    def test_path_is_per_client_and_site_and_reveals_neither(self, tmp_path: Path) -> None:
        path = oauth.token_store_path(CONFIG, tmp_path)
        assert path.parent == tmp_path
        assert "synthetic" not in path.name
        same_site = OAuthConfig(
            "HTTPS://Synthetic-Tenant.atlassian.net/", CONFIG.client_id, "other-secret"
        )
        assert oauth.token_store_path(same_site, tmp_path) == path
        other_client = OAuthConfig(CONFIG.base_url, "other-client", CONFIG.client_secret)
        assert oauth.token_store_path(other_client, tmp_path) != path

    def test_repr_hides_secrets(self) -> None:
        text = repr(_tokens())
        assert "access-1" not in text
        assert "refresh-1" not in text
        assert CLOUD_ID not in text
        assert "synthetic-client-secret" not in repr(CONFIG)


class TestConfigDir:
    def test_windows_uses_appdata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "win32")
        assert oauth.config_dir({"APPDATA": "C:/AppData"}) == Path("C:/AppData/jira-mini-mcp")

    def test_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert oauth.config_dir({"XDG_CONFIG_HOME": "/xdg"}) == Path("/xdg/jira-mini-mcp")

    def test_falls_back_to_dot_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        assert oauth.config_dir({}) == Path.home() / ".config" / "jira-mini-mcp"


class TestSession:
    async def test_not_logged_in_is_an_actionable_auth_error(self, tmp_path: Path) -> None:
        session = _session(_token_endpoint([], []), tmp_path / "absent.json")
        with pytest.raises(errors.JiraAuthenticationError, match="jira-mini-mcp login"):
            await session.access_token()
        with pytest.raises(errors.JiraAuthenticationError, match="jira-mini-mcp login"):
            await session.api_base_url()

    async def test_api_base_is_the_gateway_for_the_stored_site(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        session = _session(_token_endpoint([], []), path)
        assert await session.api_base_url() == f"https://api.atlassian.com/ex/jira/{CLOUD_ID}"

    async def test_fresh_token_is_used_without_refresh(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        seen: list[dict[str, Any]] = []
        session = _session(_token_endpoint([], seen), path)
        assert await session.access_token() == "access-1"
        assert seen == []

    async def test_expiring_token_is_refreshed_and_rotation_persisted(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens(expires_at=NOW + 30))
        seen: list[dict[str, Any]] = []
        session = _session(_token_endpoint([_refreshed()], seen), path)

        assert await session.access_token() == "access-2"

        assert seen == [
            {
                "grant_type": "refresh_token",
                "client_id": "synthetic-client-id",
                "client_secret": "synthetic-client-secret",
                "refresh_token": "refresh-1",
            }
        ]
        stored = oauth.read_tokens(path)
        assert stored is not None
        assert stored.access_token == "access-2"
        assert stored.refresh_token == "refresh-2"
        assert stored.expires_at == NOW + 3600
        assert stored.cloud_id == CLOUD_ID

    async def test_keeps_refresh_token_when_none_is_rotated_in(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens(expires_at=NOW))
        session = _session(_token_endpoint([_refreshed(refresh=None)], []), path)
        await session.access_token()
        stored = oauth.read_tokens(path)
        assert stored is not None
        assert stored.refresh_token == "refresh-1"

    async def test_concurrent_callers_share_one_refresh(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens(expires_at=NOW))
        seen: list[dict[str, Any]] = []
        session = _session(_token_endpoint([_refreshed()], seen), path)

        results = await asyncio.gather(*(session.access_token() for _ in range(5)))

        assert results == ["access-2"] * 5
        assert len(seen) == 1

    async def test_rejected_token_forces_refresh(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        seen: list[dict[str, Any]] = []
        session = _session(_token_endpoint([_refreshed()], seen), path)
        assert await session.access_token() == "access-1"
        assert await session.access_token(rejected="access-1") == "access-2"
        assert len(seen) == 1

    async def test_rejecting_a_replaced_token_does_not_refresh_again(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        seen: list[dict[str, Any]] = []
        session = _session(_token_endpoint([_refreshed()], seen), path)
        await session.access_token(rejected="access-1")
        assert await session.access_token(rejected="access-1") == "access-2"
        assert len(seen) == 1

    async def test_adopts_tokens_another_process_rotated(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens(expires_at=NOW + 30))
        seen: list[dict[str, Any]] = []
        session = _session(_token_endpoint([], seen), path)
        await session.api_base_url()  # caches the expiring tokens
        oauth.write_tokens(path, _tokens(access_token="other", refresh_token="other-r"))

        assert await session.access_token() == "other"
        assert seen == []

    async def test_file_removed_while_running_reports_login(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens(expires_at=NOW))
        session = _session(_token_endpoint([], []), path)
        await session.api_base_url()
        path.unlink()
        with pytest.raises(errors.JiraAuthenticationError, match="jira-mini-mcp login"):
            await session.access_token()

    @pytest.mark.parametrize(
        ("response", "error_type", "fragment"),
        [
            (
                httpx2.Response(403, json={"error": "invalid_grant"}),
                errors.JiraAuthenticationError,
                "jira-mini-mcp login",
            ),
            (
                httpx2.Response(401, json={"error": "invalid_client"}),
                errors.JiraAuthenticationError,
                "JIRA_OAUTH_CLIENT_SECRET",
            ),
            (
                httpx2.Response(400, json={"error": "invalid_client"}),
                errors.JiraAuthenticationError,
                "JIRA_OAUTH_CLIENT_ID",
            ),
            (httpx2.Response(400, text="nope"), errors.JiraAuthenticationError, "login"),
            (httpx2.Response(503, text="down"), errors.JiraServerError, "HTTP 503"),
            (httpx2.Response(200, text="not json"), errors.JiraServerError, "unexpected"),
            (httpx2.Response(200, json={"token": "x"}), errors.JiraServerError, "unexpected"),
        ],
    )
    async def test_refresh_failures_are_mapped_without_echoing_bodies(
        self,
        tmp_path: Path,
        response: httpx2.Response,
        error_type: type[errors.JiraMiniError],
        fragment: str,
    ) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens(expires_at=NOW))
        session = _session(_token_endpoint([response], []), path)
        with pytest.raises(error_type) as exc_info:
            await session.access_token()
        message = str(exc_info.value)
        assert fragment in message
        for secret in ("refresh-1", "synthetic-client-secret", CLOUD_ID, "invalid_grant"):
            assert secret not in message

    @pytest.mark.parametrize(
        ("exc", "error_type"),
        [
            (httpx2.ConnectTimeout("slow"), errors.JiraTimeoutError),
            (httpx2.ConnectError("refused"), errors.JiraNetworkError),
        ],
    )
    async def test_refresh_transport_failures(
        self, tmp_path: Path, exc: Exception, error_type: type[errors.JiraMiniError]
    ) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens(expires_at=NOW))

        def handler(request: httpx2.Request) -> httpx2.Response:
            raise exc

        with pytest.raises(error_type):
            await _session(handler, path).access_token()


class TestBearerAuth:
    async def test_success_sends_one_request(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        calls: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request.headers["Authorization"])
            return httpx2.Response(200, json={})

        http_client = _client(handler)
        session = oauth.OAuthSession(CONFIG, http_client, store_path=path, clock=lambda: NOW)
        await http_client.get("https://api.atlassian.com/x", auth=oauth.OAuthBearerAuth(session))
        assert calls == ["Bearer access-1"]

    async def test_attaches_bearer_and_replays_once_after_401(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        jira_seen: list[tuple[str, bytes]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            if str(request.url) == oauth.TOKEN_URL:
                return _refreshed()
            jira_seen.append((request.headers["Authorization"], request.content))
            if request.headers["Authorization"] == "Bearer access-1":
                return httpx2.Response(401, json={})
            return httpx2.Response(200, json={"ok": True})

        http_client = _client(handler)
        session = oauth.OAuthSession(CONFIG, http_client, store_path=path, clock=lambda: NOW)
        response = await http_client.post(
            "https://api.atlassian.com/ex/jira/x/rest/api/3/thing",
            json={"body": 1},
            auth=oauth.OAuthBearerAuth(session),
        )

        assert response.status_code == 200
        assert jira_seen == [
            ("Bearer access-1", b'{"body":1}'),
            ("Bearer access-2", b'{"body":1}'),
        ]

    async def test_second_401_is_returned_not_looped(self, tmp_path: Path) -> None:
        path = tmp_path / "oauth.json"
        oauth.write_tokens(path, _tokens())
        calls: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            if str(request.url) == oauth.TOKEN_URL:
                return _refreshed()
            calls.append(request.headers["Authorization"])
            return httpx2.Response(401, json={})

        http_client = _client(handler)
        session = oauth.OAuthSession(CONFIG, http_client, store_path=path, clock=lambda: NOW)
        response = await http_client.get(
            "https://api.atlassian.com/x", auth=oauth.OAuthBearerAuth(session)
        )
        assert response.status_code == 401
        assert len(calls) == 2

    def test_sync_client_is_refused(self, tmp_path: Path) -> None:
        session = oauth.OAuthSession(CONFIG, _client(lambda r: httpx2.Response(200)))
        auth = oauth.OAuthBearerAuth(session)
        with pytest.raises(RuntimeError, match="AsyncClient"):
            next(auth.sync_auth_flow(httpx2.Request("GET", "https://api.atlassian.com/x")))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _resources(url: str = CONFIG.base_url, scopes: list[str] | None = None) -> list[Any]:
    return [
        {"id": "00000000-0000-4000-8000-000000000099", "url": "https://other.atlassian.net"},
        "not-a-resource",
        {"url": url, "name": "no id"},
        {
            "id": CLOUD_ID,
            "url": url,
            "name": "synthetic",
            "scopes": list(oauth.REQUESTED_SCOPES) if scopes is None else scopes,
        },
    ]


def _atlassian(
    *,
    token_response: httpx2.Response | None = None,
    resources: httpx2.Response | None = None,
    seen: list[httpx2.Request] | None = None,
) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        if str(request.url) == oauth.TOKEN_URL:
            return token_response or httpx2.Response(
                200,
                json={"access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600},
            )
        assert str(request.url) == oauth.ACCESSIBLE_RESOURCES_URL
        return resources or httpx2.Response(200, json=_resources())

    return handler


async def _run_login(
    tmp_path: Path,
    handler: Handler,
    callback: Callable[[dict[str, str]], str] = lambda q: f"code=auth-code&state={q['state']}",
    *,
    timeout: float = 10.0,
) -> tuple[Path, list[str], list[str]]:
    """Run login; the fake browser follows `callback(query)` to the loopback server."""
    port = _free_port()
    opened: list[str] = []
    notices: list[str] = []
    pending: list[asyncio.Task[Any]] = []

    async def browser(url: str) -> None:
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        callback_query = callback(query)
        async with httpx2.AsyncClient() as real:
            await real.get(f"http://127.0.0.1:{port}/favicon.ico")
            await real.get(f"http://127.0.0.1:{port}/callback?{callback_query}")

    def open_browser(url: str) -> None:
        opened.append(url)
        pending.append(asyncio.get_running_loop().create_task(browser(url)))

    try:
        path = await oauth.login(
            CONFIG,
            _client(handler),
            port=port,
            store_path=tmp_path / "oauth.json",
            open_browser=open_browser,
            notify=notices.append,
            clock=lambda: NOW,
            timeout=timeout,
        )
    finally:
        await asyncio.gather(*pending, return_exceptions=True)
    return path, opened, notices


class TestLogin:
    async def test_success_stores_tokens_for_the_matching_site(self, tmp_path: Path) -> None:
        seen: list[httpx2.Request] = []
        path, opened, notices = await _run_login(tmp_path, _atlassian(seen=seen))

        stored = oauth.read_tokens(path)
        assert stored == oauth.StoredTokens(
            access_token="access-1",
            refresh_token="refresh-1",
            expires_at=NOW + 3600,
            cloud_id=CLOUD_ID,
            scopes=oauth.REQUESTED_SCOPES,
        )

        query = {k: v[0] for k, v in parse_qs(urlsplit(opened[0]).query).items()}
        assert opened[0].startswith(oauth.AUTHORIZE_URL + "?")
        assert query["audience"] == "api.atlassian.com"
        assert query["client_id"] == "synthetic-client-id"
        assert query["scope"] == "read:jira-work write:jira-work read:jira-user offline_access"
        assert query["redirect_uri"].startswith("http://localhost:")
        assert query["redirect_uri"].endswith("/callback")
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        assert "synthetic-client-secret" not in opened[0]
        assert opened[0] in notices[0]

        exchange = json.loads(seen[0].content)
        assert exchange["grant_type"] == "authorization_code"
        assert exchange["code"] == "auth-code"
        assert exchange["client_secret"] == "synthetic-client-secret"
        assert exchange["redirect_uri"] == query["redirect_uri"]
        digest = hashlib.sha256(exchange["code_verifier"].encode()).digest()
        assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"]
        assert seen[1].headers["Authorization"] == "Bearer access-1"

    async def test_browser_failure_still_leaves_the_printed_url(self, tmp_path: Path) -> None:
        port = _free_port()

        def broken_browser(url: str) -> None:
            raise OSError("no browser")

        with pytest.raises(oauth.OAuthLoginError, match="within 0 seconds"):
            await oauth.login(
                CONFIG,
                _client(_atlassian()),
                port=port,
                store_path=tmp_path / "oauth.json",
                open_browser=broken_browser,
                notify=lambda message: None,
                timeout=0.2,
            )

    @pytest.mark.parametrize(
        ("callback", "fragment"),
        [
            (lambda q: "code=auth-code&state=forged", "state mismatch"),
            (lambda q: f"error=access_denied&state={q['state']}", "access_denied"),
            (lambda q: f"state={q['state']}", "without an authorization code"),
        ],
    )
    async def test_bad_callbacks_fail_actionably(
        self, tmp_path: Path, callback: Callable[[dict[str, str]], str], fragment: str
    ) -> None:
        with pytest.raises(oauth.OAuthLoginError, match=fragment):
            await _run_login(tmp_path, _atlassian(), callback)
        assert not (tmp_path / "oauth.json").exists()

    async def test_busy_port_is_reported(self, tmp_path: Path) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            with pytest.raises(oauth.OAuthLoginError, match="--port"):
                await oauth.login(
                    CONFIG,
                    _client(_atlassian()),
                    port=port,
                    store_path=tmp_path / "oauth.json",
                    open_browser=lambda url: None,
                    notify=lambda message: None,
                )

    async def test_unmatched_site_names_no_url(self, tmp_path: Path) -> None:
        handler = _atlassian(
            resources=httpx2.Response(200, json=_resources(url="https://elsewhere.atlassian.net"))
        )
        with pytest.raises(oauth.OAuthLoginError, match="4 site") as exc_info:
            await _run_login(tmp_path, handler)
        assert "elsewhere" not in str(exc_info.value)
        assert "other.atlassian" not in str(exc_info.value)

    async def test_missing_scopes_are_named(self, tmp_path: Path) -> None:
        handler = _atlassian(
            resources=httpx2.Response(200, json=_resources(scopes=["read:jira-work"]))
        )
        with pytest.raises(oauth.OAuthLoginError, match="write:jira-work, read:jira-user"):
            await _run_login(tmp_path, handler)

    @pytest.mark.parametrize(
        "resources",
        [httpx2.Response(500, text="boom"), httpx2.Response(200, text="not json")],
    )
    async def test_unusable_resource_list(self, tmp_path: Path, resources: httpx2.Response) -> None:
        with pytest.raises(oauth.OAuthLoginError, match="unexpected list"):
            await _run_login(tmp_path, _atlassian(resources=resources))

    async def test_resource_list_network_failure(self, tmp_path: Path) -> None:
        base = _atlassian()

        def handler(request: httpx2.Request) -> httpx2.Response:
            if str(request.url) == oauth.ACCESSIBLE_RESOURCES_URL:
                raise httpx2.ConnectError("refused")
            return base(request)

        with pytest.raises(oauth.OAuthLoginError, match="Check connectivity"):
            await _run_login(tmp_path, handler)

    async def test_missing_refresh_token(self, tmp_path: Path) -> None:
        handler = _atlassian(
            token_response=httpx2.Response(200, json={"access_token": "a", "expires_in": 3600})
        )
        with pytest.raises(oauth.OAuthLoginError, match="offline access"):
            await _run_login(tmp_path, handler)

    async def test_rejected_client_credentials(self, tmp_path: Path) -> None:
        handler = _atlassian(token_response=httpx2.Response(401, json={"error": "access_denied"}))
        with pytest.raises(errors.JiraAuthenticationError, match="JIRA_OAUTH_CLIENT_SECRET"):
            await _run_login(tmp_path, handler)
