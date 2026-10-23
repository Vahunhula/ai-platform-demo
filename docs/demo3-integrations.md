# Demo 3 Phase 1 integrations

This phase adds optional, administrator-managed GitHub App and Jira Cloud connections.
It does not create branches, commits, pushes, pull requests, Jira issues, Jira comments,
or TaskSessions from provider data.

## GitHub App

Create a GitHub App in the owning user or organization account. Configure:

- Homepage URL: the public AI Platform URL.
- Setup URL: `https://<platform-host>/api/integrations/github/callback`.
- Redirect on update: optional. Repository changes are normally handled by webhooks.
- Webhook URL: `https://<platform-host>/api/webhooks/github` with SSL verification on.
- Webhook secret: a high-entropy value supplied as `GITHUB_APP_WEBHOOK_SECRET`.
- Repository permission: **Metadata — Read-only**. No Contents, Pull requests, Actions,
  Checks, Administration, or other write permission is required in Phase 1. GitHub's
  installation-repositories endpoint itself requires no extra fine-grained permission;
  Metadata read permits future basic repository metadata reads. Phase 2/3 must separately
  review and request Contents and Pull requests access only when delivery is implemented.
- Subscribe to `installation` and `installation_repositories`. Phase 1 uses these only to
  reflect removal/suspension and repository-access-count changes.

Official references: [choosing GitHub App permissions](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app),
[installation authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/authenticating-as-a-github-app-installation),
[installation endpoints](https://docs.github.com/en/rest/apps/installations), and
[setup URL security](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url).

Server configuration:

```text
GITHUB_APP_ID=<numeric app id>
GITHUB_APP_SLUG=<public app slug>
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/ai-platform/github-app.pem
GITHUB_APP_WEBHOOK_SECRET=<random secret>
```

`GITHUB_APP_PRIVATE_KEY` may supply PEM content directly instead of the path. The key is
loaded only by the server and is never persisted in `platform.db`. `GITHUB_APP_CLIENT_ID`
and `GITHUB_APP_CLIENT_SECRET` are accepted for future user-authorization needs but are
not required or used by this installation-authentication phase. Do not enable “Request
user authorization during installation.”

An admin clicks Connect. The backend creates a one-time, ten-minute state and returns the
standard `https://github.com/apps/<slug>/installations/new` URL. GitHub preserves `state`
and returns `installation_id` to the Setup URL. The backend validates the state, then
authenticates as the configured App, fetches the installation, creates a short-lived
installation token (about one hour), and lists accessible repositories. The token is kept
only for that operation and is never stored or returned. A supplied installation ID is
therefore not trusted as account metadata.

Disconnect removes the local association. It deliberately does not uninstall the App at
GitHub; an operator may uninstall it in GitHub settings. Reconnect always repeats the
normal installation flow.

## Jira Cloud OAuth 2.0 (3LO)

Create an OAuth 2.0 integration in the Atlassian developer console. Add Jira Platform and
User Identity APIs as needed by the console and register exactly:

```text
https://<platform-host>/api/integrations/jira/callback
```

Set that identical URL as `JIRA_OAUTH_REDIRECT_URI`; it is never taken from a browser
parameter. Configure these exact Phase-1 scopes:

- `read:me` — resolve the consenting Atlassian account through the User Identity API.
- `read:jira-user` — call Jira `GET /rest/api/3/myself` for a basic site health check.
- `offline_access` — obtain rotating refresh tokens for a durable connection.

No Jira write, issue-read (`read:jira-work`), administration, or webhook scope is
requested. Site discovery uses Atlassian's `accessible-resources` endpoint. If it returns
multiple Jira sites, the browser shows a selector; the application never guesses.

Official references: [Atlassian OAuth 2.0 3LO](https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/),
[User Identity `read:me`](https://developer.atlassian.com/cloud/oauth/getting-started/faq/),
and [Jira user scopes](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-users/).

Server configuration:

```text
JIRA_OAUTH_CLIENT_ID=<client id>
JIRA_OAUTH_CLIENT_SECRET=<client secret>
JIRA_OAUTH_REDIRECT_URI=https://<platform-host>/api/integrations/jira/callback
AI_PLATFORM_INTEGRATION_ENCRYPTION_KEY=<base64 or hex encoded 32-byte key>
```

The state value is cryptographically random, stored only as a SHA-256 digest, bound to the
admin who started the flow, expires after ten minutes, and is consumed atomically once.
Authorization codes are exchanged only server-side and are not persisted. Access and
refresh tokens are stored in an AES-256-GCM envelope. Refresh is serialized with a local
mutex and a short, expiring SQLite lease shared by server workers, then persisted with a
revision compare-and-swap. Every successful refresh atomically replaces the old rotating
refresh token; a stale worker cannot overwrite the new pair. Invalid or revoked refresh
authorization becomes `REAUTH_REQUIRED` without unbounded retries.

Atlassian does not provide a general 3LO token-revocation endpoint for this application
flow. Disconnect securely removes all locally usable connection and pending-selection
credentials. Reconnect starts a new 3LO grant and never revives deleted credentials.

## Encryption and operations

Generate the encryption key once, for example with `openssl rand -base64 32`, and keep it
outside Git. The service never generates or stores a fallback key. If encrypted credentials
exist and the key is absent or wrong, startup fails closed. Back up this key separately from
`platform.db`: a database restore is unusable without the matching key, while losing both
does not permit recovery. Rotate credentials by reconnecting providers; envelope-key
rotation needs a separately rehearsed decrypt/re-encrypt maintenance procedure.

For the current single-host deployment, AES-GCM with a server environment key is the
accepted boundary. A production multi-tenant SaaS should use a dedicated secret manager or
KMS with key versioning, access audit, and automated rotation. If systemd supplies secrets,
prefer a root-owned `EnvironmentFile` or credential mechanism with mode `0600`; do not put
provider secrets in a world-readable unit, shell history, logs, or deployment output.

Connection metadata and a separate append-only integration audit stream contain only safe
provider/account/site IDs, status, counts, and timestamps. They never contain tokens,
authorization codes, client secrets, App private keys, webhook secrets, or raw webhook
payloads. Management APIs are admin-only. The webhook endpoint instead requires a valid
`X-Hub-Signature-256` HMAC and deduplicates `X-GitHub-Delivery` IDs.

## Manual rehearsal

After supplying real server configuration, sign in as an admin and open Settings →
Integrations. Install the GitHub App on a test user/organization, verify its account and
repository count, Check connection, Disconnect, and Reconnect. For Jira, consent with a
test Atlassian account, select a site if prompted, verify site/account/scopes, Check
connection, allow an access token to refresh, Disconnect, and Reconnect. Do not perform
repository writes or modify Jira issues during this rehearsal.
