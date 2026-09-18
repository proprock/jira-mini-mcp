"""OAuth 2.0 (3LO) for Jira Cloud: token storage, refresh, and browser login.

Atlassian accepts only confidential 3LO clients, so every user registers an
app of their own and supplies its client ID and secret (see docs/oauth.md).
`login` runs the authorization-code flow once, with PKCE and a loopback
callback, and stores the tokens in a user-only file. The server then reads
that file through `OAuthSession`, refreshes the access token before it
expires, and persists each rotated refresh token.

Requests with an OAuth token go through Atlassian's API gateway,
`https://api.atlassian.com/ex/jira/{cloud_id}`, not through the site URL;
the cloud ID is resolved from JIRA_BASE_URL once, at login.

Tokens, the client secret, the cloud ID, and token-endpoint response bodies
never appear in an error, log line, or repr.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
import webbrowser
from collections.abc import AsyncGenerator, Callable, Generator, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2

from jira_mini_mcp import errors
from jira_mini_mcp.auth import OAuthConfig

AUTHORIZE_URL = "https://auth.atlassian.com/authorize"
TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ACCESSIBLE_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
API_GATEWAY = "https://api.atlassian.com/ex/jira"

REQUIRED_SCOPES = ("read:jira-work", "write:jira-work", "read:jira-user")
REQUESTED_SCOPES = (*REQUIRED_SCOPES, "offline_access")

DEFAULT_CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"
LOGIN_TIMEOUT_SECONDS = 300.0

# Refresh this long before the recorded expiry, so a token never expires
# between the check and Jira receiving the request.
_EXPIRY_MARGIN_SECONDS = 60.0

_STORE_VERSION = 1

_LOGIN_HINT = "Run `jira-mini-mcp login` with the same JIRA_BASE_URL and JIRA_OAUTH_CLIENT_ID."


class OAuthLoginError(errors.JiraMiniError):
    """The browser authorization could not be completed."""


@dataclass(frozen=True)
class StoredTokens:
    """What `login` saves and the server reads back."""

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    cloud_id: str = field(repr=False)
    scopes: tuple[str, ...] = ()


def config_dir(env: Mapping[str, str] = os.environ) -> Path:
    """The per-user directory holding token files."""
    if sys.platform == "win32" and env.get("APPDATA"):
        return Path(env["APPDATA"]) / "jira-mini-mcp"
    xdg = env.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "jira-mini-mcp"


def _normalized_site(base_url: str) -> str:
    parts = urlsplit(base_url.strip())
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}"


def token_store_path(config: OAuthConfig, directory: Path | None = None) -> Path:
    """One file per client ID and site, named by a hash so it reveals neither."""
    key = f"{config.client_id}|{_normalized_site(config.base_url)}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return (directory or config_dir()) / f"oauth-{digest}.json"


def read_tokens(path: Path) -> StoredTokens | None:
    """Load a token file; None when it is absent or unreadable.

    A corrupt file is treated as "not logged in" rather than surfaced,
    since its contents are secrets and the fix is the same either way.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != _STORE_VERSION:
        return None
    try:
        tokens = StoredTokens(
            access_token=raw["access_token"],
            refresh_token=raw["refresh_token"],
            expires_at=float(raw["expires_at"]),
            cloud_id=raw["cloud_id"],
            scopes=tuple(raw.get("scopes", ())),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if not all(
        isinstance(value, str) and value
        for value in (tokens.access_token, tokens.refresh_token, tokens.cloud_id)
    ):
        return None
    return tokens


def write_tokens(path: Path, tokens: StoredTokens) -> None:
    """Replace the token file atomically, readable by the current user only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {"version": _STORE_VERSION, **asdict(tokens), "scopes": list(tokens.scopes)}
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def delete_tokens(path: Path) -> bool:
    """Remove the token file; False when there was none."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _not_logged_in() -> errors.JiraAuthenticationError:
    return errors.JiraAuthenticationError(
        f"No OAuth authorization is stored for this Jira site. {_LOGIN_HINT}"
    )


def _token_request_error(response: httpx2.Response, operation: str) -> errors.JiraMiniError:
    """Map a failed token-endpoint call without echoing its body."""
    if response.status_code in (400, 401, 403):
        try:
            code = response.json().get("error")
        except (ValueError, AttributeError):
            code = None
        if code == "invalid_client" or response.status_code == 401:
            return errors.JiraAuthenticationError(
                "Atlassian rejected the OAuth client credentials. Check "
                "JIRA_OAUTH_CLIENT_ID and JIRA_OAUTH_CLIENT_SECRET.",
                operation=operation,
                status_code=response.status_code,
            )
        return errors.JiraAuthenticationError(
            f"The OAuth authorization has expired or was revoked. {_LOGIN_HINT}",
            operation=operation,
            status_code=response.status_code,
        )
    return errors.JiraServerError(
        f"Atlassian's OAuth token endpoint failed for operation '{operation}' "
        f"(HTTP {response.status_code}). Retry later.",
        operation=operation,
        status_code=response.status_code,
    )


async def _post_token(
    client: httpx2.AsyncClient, body: dict[str, str], operation: str
) -> dict[str, Any]:
    try:
        response = await client.post(TOKEN_URL, json=body)
    except httpx2.TimeoutException as exc:
        raise errors.JiraTimeoutError(
            f"The OAuth token request timed out for operation '{operation}'. "
            "Check network connectivity and retry.",
            operation=operation,
        ) from exc
    except httpx2.TransportError as exc:
        raise errors.JiraNetworkError(
            f"A network error occurred reaching Atlassian's OAuth token endpoint for "
            f"operation '{operation}'. Check connectivity and retry.",
            operation=operation,
        ) from exc
    if response.status_code >= 400:
        raise _token_request_error(response, operation)
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("access_token"), str)
        or not isinstance(payload.get("expires_in"), int | float)
    ):
        raise errors.JiraServerError(
            f"Atlassian's OAuth token endpoint returned an unexpected response for "
            f"operation '{operation}'.",
            operation=operation,
        )
    return payload


class OAuthSession:
    """The server's view of one stored authorization: a fresh token and the API base."""

    def __init__(
        self,
        config: OAuthConfig,
        http_client: httpx2.AsyncClient,
        store_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._http_client = http_client
        self._path = store_path or token_store_path(config)
        self._clock = clock
        self._tokens: StoredTokens | None = None
        self._lock = asyncio.Lock()

    async def _load(self) -> StoredTokens | None:
        return await asyncio.to_thread(read_tokens, self._path)

    async def _current(self) -> StoredTokens:
        if self._tokens is None:
            self._tokens = await self._load()
        if self._tokens is None:
            raise _not_logged_in()
        return self._tokens

    async def api_base_url(self) -> str:
        tokens = await self._current()
        return f"{API_GATEWAY}/{tokens.cloud_id}"

    def _fresh(self, tokens: StoredTokens) -> bool:
        return tokens.expires_at - _EXPIRY_MARGIN_SECONDS > self._clock()

    async def access_token(self, *, rejected: str | None = None) -> str:
        """A token Jira should accept; refreshes when expiring or `rejected`.

        `rejected` is the token Jira just answered 401 to: if another request
        already replaced it, the replacement is returned without a second
        refresh.
        """
        async with self._lock:
            tokens = await self._current()
            if tokens.access_token != rejected and self._fresh(tokens):
                return tokens.access_token

            # Another process sharing the file may have rotated the tokens;
            # its refresh token is the only one still valid then.
            on_disk = await self._load()
            if on_disk is None:
                self._tokens = None
                raise _not_logged_in()
            if on_disk.access_token not in (tokens.access_token, rejected) and self._fresh(on_disk):
                self._tokens = on_disk
                return on_disk.access_token

            self._tokens = await self._refresh(on_disk)
            return self._tokens.access_token

    async def _refresh(self, tokens: StoredTokens) -> StoredTokens:
        payload = await _post_token(
            self._http_client,
            {
                "grant_type": "refresh_token",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "refresh_token": tokens.refresh_token,
            },
            operation="oauth_refresh",
        )
        refresh_token = payload.get("refresh_token")
        refreshed = replace(
            tokens,
            access_token=payload["access_token"],
            # Rotation hands out a new refresh token; keep the old one only
            # if the app has rotation turned off and none came back.
            refresh_token=refresh_token if isinstance(refresh_token, str) else tokens.refresh_token,
            expires_at=self._clock() + float(payload["expires_in"]),
        )
        await asyncio.to_thread(write_tokens, self._path, refreshed)
        return refreshed


class OAuthBearerAuth(httpx2.Auth):
    """Attach a fresh Bearer token; on a 401, refresh once and replay."""

    # The body must be buffered for the replay after a 401.
    requires_request_body = True

    def __init__(self, session: OAuthSession) -> None:
        self._session = session

    def sync_auth_flow(self, request: httpx2.Request) -> Generator[httpx2.Request, Any, None]:
        raise RuntimeError("OAuthBearerAuth supports only httpx2.AsyncClient.")

    async def async_auth_flow(
        self, request: httpx2.Request
    ) -> AsyncGenerator[httpx2.Request, httpx2.Response]:
        token = await self._session.access_token()
        request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code == 401:
            token = await self._session.access_token(rejected=token)
            request.headers["Authorization"] = f"Bearer {token}"
            yield request


# ---------------------------------------------------------------------------
# Browser login


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def redirect_uri(port: int) -> str:
    return f"http://localhost:{port}{CALLBACK_PATH}"


def authorization_url(config: OAuthConfig, *, port: int, state: str, challenge: str) -> str:
    query = urlencode(
        {
            "audience": "api.atlassian.com",
            "client_id": config.client_id,
            "scope": " ".join(REQUESTED_SCOPES),
            "redirect_uri": redirect_uri(port),
            "state": state,
            "response_type": "code",
            "prompt": "consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


_PAGE = (
    "<!doctype html><meta charset=utf-8><title>jira-mini-mcp</title>"
    "<body style='font-family:sans-serif;margin:3em'><h1>{title}</h1><p>{text}</p></body>"
)


async def _serve_one_callback(
    port: int, state: str, ready: Callable[[], None], timeout: float
) -> str:
    """Accept the browser redirect on the loopback port; return the code."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await reader.readline()).decode("latin-1")
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            parts = request_line.split(" ")
            target = urlsplit(parts[1]) if len(parts) >= 2 else urlsplit("/")
            if target.path != CALLBACK_PATH or result.done():
                status, title, text = "404 Not Found", "Not found", ""
            else:
                params = parse_qs(target.query)
                if params.get("state", [""])[0] != state:
                    status, title = "400 Bad Request", "Authorization failed"
                    text = "The response did not match this login attempt. Run login again."
                    result.set_exception(
                        OAuthLoginError(
                            "The authorization response did not match this login attempt "
                            "(state mismatch). Run `jira-mini-mcp login` again."
                        )
                    )
                elif "error" in params:
                    status, title = "400 Bad Request", "Authorization failed"
                    text = "Atlassian did not grant access. You can close this tab."
                    error_code = params["error"][0][:64]
                    result.set_exception(
                        OAuthLoginError(
                            f"Atlassian did not grant access ({error_code}). Run "
                            "`jira-mini-mcp login` again and approve the consent screen."
                        )
                    )
                elif params.get("code", [""])[0]:
                    status, title = "200 OK", "Authorized"
                    text = "jira-mini-mcp is authorized. You can close this tab."
                    result.set_result(params["code"][0])
                else:
                    status, title = "400 Bad Request", "Authorization failed"
                    text = "The response carried no authorization code."
                    result.set_exception(
                        OAuthLoginError(
                            "Atlassian redirected back without an authorization code. "
                            "Run `jira-mini-mcp login` again."
                        )
                    )
            body = _PAGE.format(title=title, text=text).encode("utf-8")
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                + body
            )
            await writer.drain()
        finally:
            writer.close()

    try:
        server = await asyncio.start_server(handle, "127.0.0.1", port)
    except OSError as exc:
        raise OAuthLoginError(
            f"Could not listen on localhost port {port} for the OAuth callback. Free the "
            "port or pass --port with another one registered as the app's callback URL."
        ) from exc
    async with server:
        ready()
        try:
            return await asyncio.wait_for(result, timeout)
        except TimeoutError as exc:
            raise OAuthLoginError(
                f"No authorization arrived within {int(timeout)} seconds. "
                "Run `jira-mini-mcp login` again."
            ) from exc


async def _accessible_cloud_id(
    client: httpx2.AsyncClient, access_token: str, base_url: str
) -> tuple[str, tuple[str, ...]]:
    try:
        response = await client.get(
            ACCESSIBLE_RESOURCES_URL, headers={"Authorization": f"Bearer {access_token}"}
        )
    except httpx2.HTTPError as exc:
        raise OAuthLoginError(
            "Could not list the Jira sites this authorization grants. Check connectivity "
            "and run `jira-mini-mcp login` again."
        ) from exc
    try:
        resources = response.json() if response.status_code < 400 else None
    except ValueError:
        resources = None
    if not isinstance(resources, list):
        raise OAuthLoginError(
            "Atlassian returned an unexpected list of accessible sites "
            f"(HTTP {response.status_code}). Run `jira-mini-mcp login` again."
        )

    wanted = _normalized_site(base_url)
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        url, cloud_id = resource.get("url"), resource.get("id")
        if isinstance(url, str) and isinstance(cloud_id, str) and cloud_id:
            if _normalized_site(url) == wanted:
                scopes = resource.get("scopes")
                granted = tuple(s for s in scopes if isinstance(s, str)) if scopes else ()
                return cloud_id, granted

    raise OAuthLoginError(
        f"The authorization grants {len(resources)} site(s), none of them JIRA_BASE_URL. "
        "Run `jira-mini-mcp login` again and pick that site on the consent screen, "
        "or correct JIRA_BASE_URL."
    )


async def login(
    config: OAuthConfig,
    http_client: httpx2.AsyncClient,
    *,
    port: int = DEFAULT_CALLBACK_PORT,
    store_path: Path | None = None,
    open_browser: Callable[[str], object] = webbrowser.open,
    notify: Callable[[str], None] = lambda message: print(message, file=sys.stderr),
    clock: Callable[[], float] = time.time,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> Path:
    """Run the browser authorization and save the tokens; return the file path."""
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    url = authorization_url(config, port=port, state=state, challenge=challenge)

    def ready() -> None:
        notify(
            "Opening your browser to authorize jira-mini-mcp. If it does not open, "
            f"visit this URL:\n\n{url}\n"
        )
        try:
            open_browser(url)
        except Exception:  # noqa: BLE001 -- the printed URL is the fallback
            pass

    code = await _serve_one_callback(port, state, ready, timeout)

    payload = await _post_token(
        http_client,
        {
            "grant_type": "authorization_code",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "redirect_uri": redirect_uri(port),
            "code_verifier": verifier,
        },
        operation="oauth_login",
    )
    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OAuthLoginError(
            "Atlassian returned no refresh token, so the authorization would lapse within "
            "an hour. Run `jira-mini-mcp login` again and approve offline access."
        )

    cloud_id, granted = await _accessible_cloud_id(
        http_client, payload["access_token"], config.base_url
    )
    missing = [scope for scope in REQUIRED_SCOPES if scope not in granted]
    if missing:
        raise OAuthLoginError(
            f"The authorization lacks the scope(s) {', '.join(missing)}. Add them to the "
            "app's Jira API permissions in the developer console, then run login again."
        )

    path = store_path or token_store_path(config)
    tokens = StoredTokens(
        access_token=payload["access_token"],
        refresh_token=refresh_token,
        expires_at=clock() + float(payload["expires_in"]),
        cloud_id=cloud_id,
        scopes=granted,
    )
    await asyncio.to_thread(write_tokens, path, tokens)
    return path
