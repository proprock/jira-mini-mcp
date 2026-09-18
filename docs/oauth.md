# OAuth setup

`jira-mini-mcp` can authorize through your browser with OAuth 2.0 (3LO)
instead of an API token. You do it once per machine; after that the server
refreshes its access token by itself.

Setup takes three parts: register an OAuth app, configure the MCP host, and log
in. The [README](../README.md) covers the API-token setup, which is shorter.

## Why you register your own app

The official Atlassian MCP server runs on Atlassian's infrastructure, so
Atlassian owns its OAuth client. A server running on your machine has to bring
its own client, and Atlassian accepts only clients that hold a secret (PKCE is
supported, but only in addition to the secret). A secret shipped in an
open-source package would be public, so every user registers a private app
instead. The app is free, and nothing needs Atlassian's review while you use it
yourself.

## 1. Register an OAuth 2.0 (3LO) app

1. Open the [Atlassian developer console](https://developer.atlassian.com/console/myapps/)
   and sign in with the account you use for Jira.
2. **Create** → **OAuth 2.0 integration**. Name it, for example
   `jira-mini-mcp`, accept the terms, and create it.
3. **Permissions** → **Jira API** → **Add**, then **Configure** → **Edit
   Scopes**. Under the classic scopes select:

   | Scope | Used for |
   |---|---|
   | `read:jira-work` | Every read tool |
   | `write:jira-work` | `add_comment`, `transition_issue`, `update_issue` |
   | `read:jira-user` | Users in results, and `assignee="me"` |

   Save. `offline_access`, which allows refreshing, is requested at login and
   needs no setting here.
4. **Authorization** → **OAuth 2.0 (3LO)** → **Configure**. Set the callback
   URL to exactly:

   ```text
   http://localhost:8765/callback
   ```

   To use another port, register `http://localhost:<port>/callback` and pass
   `--port <port>` to `login`.
5. **Settings**: copy the **Client ID** and the **Secret**.

While the app is in development, only its owner can authorize it. For
teammates, have each register their own app, or enable sharing under
**Distribution**.

## 2. Configure the MCP host

Set `JIRA_AUTH_METHOD=oauth`, your site URL, and the app's credentials.
`JIRA_EMAIL` and `JIRA_API_TOKEN` are not needed.

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add --env JIRA_AUTH_METHOD=oauth --env JIRA_BASE_URL=https://example.atlassian.net --env JIRA_OAUTH_CLIENT_ID=your-client-id --env JIRA_OAUTH_CLIENT_SECRET=your-secret --transport stdio jira-mini -- uvx jira-mini-mcp
```

</details>

<details>
<summary><b>Claude Desktop</b></summary>

In `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "jira-mini": {
      "command": "uvx",
      "args": ["jira-mini-mcp"],
      "env": {
        "JIRA_AUTH_METHOD": "oauth",
        "JIRA_BASE_URL": "https://example.atlassian.net",
        "JIRA_OAUTH_CLIENT_ID": "your-client-id",
        "JIRA_OAUTH_CLIENT_SECRET": "your-secret"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Codex CLI</b></summary>

```bash
codex mcp add jira-mini --env JIRA_AUTH_METHOD=oauth --env JIRA_BASE_URL=https://example.atlassian.net --env JIRA_OAUTH_CLIENT_ID=your-client-id --env JIRA_OAUTH_CLIENT_SECRET=your-secret -- uvx jira-mini-mcp
```

</details>

## 3. Log in

Run `login` in a terminal with the same four variables set:

```bash
JIRA_AUTH_METHOD=oauth JIRA_BASE_URL=https://example.atlassian.net JIRA_OAUTH_CLIENT_ID=your-client-id JIRA_OAUTH_CLIENT_SECRET=your-secret uvx jira-mini-mcp login
```

On Windows PowerShell, set them first:

```powershell
$env:JIRA_AUTH_METHOD="oauth"; $env:JIRA_BASE_URL="https://example.atlassian.net"; $env:JIRA_OAUTH_CLIENT_ID="your-client-id"; $env:JIRA_OAUTH_CLIENT_SECRET="your-secret"; uvx jira-mini-mcp login
```

Your browser opens Atlassian's consent screen. If it does not, open the URL the
command prints. Choose the site that matches `JIRA_BASE_URL` and approve. The
command waits up to five minutes, then prints where it saved the tokens.

The MCP server can already be running: its next tool call picks the login up.
Until you log in, every tool returns an error saying to run `login`.

## Where tokens live

One file per app and site:

| OS | Directory |
|---|---|
| Windows | `%APPDATA%\jira-mini-mcp\` |
| macOS, Linux | `$XDG_CONFIG_HOME/jira-mini-mcp/`, or `~/.config/jira-mini-mcp/` |

The file name is a hash, so it reveals neither the site nor the client ID. On
macOS and Linux the file is readable by your user only (`0600`); on Windows it
inherits your profile's permissions. It holds the access token, the refresh
token, their expiry, the site's cloud ID, and the granted scopes. Treat it like
a password.

Access tokens last an hour. The server refreshes them before they expire, and
once more if Jira rejects one, and writes each new refresh token back to the
file. Several servers on one machine can share the file. The authorization
lapses after 90 days without use, or when you revoke the app under **Connected
apps** in your Atlassian account settings.

To remove the stored authorization:

```bash
uvx jira-mini-mcp logout
```

with the same environment as `login`. This deletes the local file only; revoke
the app in your Atlassian account settings to invalidate the tokens too.

## Troubleshooting

| Message | Fix |
|---|---|
| `No OAuth authorization is stored for this Jira site` | Run `login` with the same `JIRA_BASE_URL` and `JIRA_OAUTH_CLIENT_ID` the server uses. |
| `The OAuth authorization has expired or was revoked` | Run `login` again. |
| `Atlassian rejected the OAuth client credentials` | Check `JIRA_OAUTH_CLIENT_ID` and `JIRA_OAUTH_CLIENT_SECRET` against the app's **Settings**. |
| `none of them JIRA_BASE_URL` | You approved a different site. Run `login` again and pick the site in `JIRA_BASE_URL`, or correct the variable. |
| `lacks the scope(s) ...` | Add the named scopes under the app's **Permissions**, then run `login` again. |
| `Could not listen on localhost port 8765` | Free the port, or register another callback port and pass `--port`. |
| Atlassian shows a redirect URI error | The app's callback URL must be exactly `http://localhost:<port>/callback`, with the port `login` uses. |
