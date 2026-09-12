# Cloudflare across servers, domains and accounts

Run `uv run mitra-cloudflare-setup` on each server, or select Cloudflare in the
main setup wizard. For an installation whose entry points have not been refreshed,
use `uv run python -m mitra_bot.cloudflare_setup`. Stop the bot while editing its
assignments or reconnecting accounts, then restart it to reconcile DNS.

Each server detects its own public IPv4 and updates only its own assigned A records.
It can manage several domains, including domains in different Cloudflare accounts.
Connect again with a different connection name to add another account. One token or
OAuth connection can cover several domains when the Cloudflare account grants access.
Different servers may use different Cloudflare accounts and credentials, even though
they share one Discord bot token. Cloudflare credentials are never replicated to peers.

For your example, select `henness.info`, enter `@, pq` on the primary server, and
enter `pryor` on the secondary server. The wizard shows the server ID and proposed
assignments. It reuses existing A records, preserves their TTL/proxy settings and
offers to create missing records using the local public IP. You choose the proxy
setting for new records. CNAME conflicts or multiple A records at the same name
require manual resolution. Review and approve before any DNS creations or local
configuration writes. Existing records are reconciled when the bot starts.

Setup replaces this server's assignments for selected zones and retains other
zones and other server assignments in the file. Include every name this server
should manage in each selected zone when rerunning setup. Reusing a connection
name replaces its credentials for all assignments referencing that connection.
If a DNS creation fails partway through, already-created records remain; rerun
setup to discover and reuse them. Do not delete records blindly to retry setup.

## Browser authorization

Cloudflare supports [Authorization Code with PKCE for CLI and desktop applications](https://developers.cloudflare.com/fundamentals/oauth/create-an-oauth-client/).
The wizard opens Cloudflare's consent page, where you log in and approve access
to the intended resources. It exchanges the authorization code for credentials
automatically and refreshes access tokens as needed. It does not create a traditional
permanent API token or request your Cloudflare password/global API key.

Mitra includes its registered public client identifier,
`dbf6bc3487bf8f65d528b018d2374fdf`, so normal setup does not require entering an ID.
Publisher domain verification and public visibility must still be completed in
Cloudflare before unrelated users can authorize it. Registration alone does not
confirm that the live consent and refresh flow has been tested.
Private clients work for members of the client's parent Cloudflare account. To let
unrelated users/accounts authorize the same client, Cloudflare requires public
visibility, publisher domain verification and the required client metadata.

In Cloudflare's **Manage Account → OAuth clients → Create client**, configure:

- Grant types **Authorization Code AND Refresh Token**, response type `code`,
  token authentication `none`, PKCE S256. Refresh Token is required for unattended operation.
- Redirect URL exactly `http://localhost:9876/cloudflare/callback`.
- Zone Read and DNS Write/Edit API scopes (`zone.read`, `dns.write`). Cloudflare
  automatically adds `offline_access` when the **Refresh Token** grant is enabled;
  it is not an optional DNS permission checkbox. Confirm the scope IDs against your client's
  available scopes (`GET /client/v4/oauth/scopes` with an authorized API token).
- Appropriate client name, URL and branding. For distribution to other accounts,
  complete Cloudflare's domain verification and public-client requirements.

To override the bundled client (for example for a fork), run
`uv run mitra-cloudflare-setup --client-id YOUR_CLIENT_ID`, set the public
`MITRA_CLOUDFLARE_CLIENT_ID` environment variable, or enter the ID when prompted.
For a branded release, maintainers can set `DEFAULT_CLIENT_ID` in
`mitra_bot/services/cloudflare_auth.py` after registering the real client. Client IDs
are public; no client secret belongs in distributed bot installations.

The callback listens only on loopback for five minutes. Requests must match the
callback path and random state, and token exchange uses PKCE. Authorization codes
and tokens are not printed or sent to other peers. Each machine stores its own
access/refresh tokens in `.env.cloudflare-oauth.json` beside `config.toml`; rotating
refreshes are serialized and saved atomically. Protect that file with local access
controls. Revocation or expiration of the refresh grant requires reconnecting the
affected account. Do not copy OAuth refresh credentials between machines.

If the callback fails, both the browser page and console report a safe OAuth error
code. `access_denied` may mean user cancellation or an account policy restriction;
`invalid_scope` means the requested permissions were rejected; `invalid_client` or
`unauthorized_client` indicates a client-registration/access problem. These are
different errors. Provider descriptions and callback URLs are not reflected into
diagnostic messages because they may contain sensitive data.

The wizard offers retry, manual-token setup, or leaving Cloudflare for later while
retaining completed Discord setup. To resume only this step, run
`uv run mitra-cloudflare-setup`. An old generic “authorization was declined” message
does not identify the original provider error; retry with the updated wizard to
obtain the specific code. Every retry starts a fresh authorization session.

If a client was created with only Authorization Code, requests for `offline_access`
fail with `invalid_scope` before consent. Edit the client and enable **Refresh Token**
alongside Authorization Code. Cloudflare's [client update API](https://developers.cloudflare.com/api/resources/iam/subresources/oauth_clients/methods/update/)
documents that this grant automatically enables the protocol scope. Keep DNS Edit,
Zone Read, callback URLs and token authentication unchanged.

If the dashboard does not expose the grant setting, the client owner can run
`uv run mitra-cloudflare-setup --repair-client`. This guided maintainer command
asks for the owning account ID and a temporary account-scoped OAuth Client Write
API token, reads the existing client, previews the grant change, asks for approval,
patches only `grant_types`, and verifies Refresh Token/`offline_access` afterward.
The management token is never saved; revoke it afterward. End users do not need
management permissions. Then retry ordinary Cloudflare setup with a fresh link.

For headless servers, use `--no-browser`. Forward the callback port from your browser
machine, for example `ssh -L 9876:127.0.0.1:9876 user@server`, and open the printed
authorization URL locally. Without a loopback tunnel, use manual-token setup.
This does not depend on a centralized Mitra server or hosted token relay.

## Manual tokens and configuration

Choose Manual API token in the wizard. It opens Cloudflare's token page; create a
token with **Zone Read + DNS Edit** restricted to the required zones and paste it
into the hidden prompt. The wizard discovers zones/record IDs and writes a named
environment variable such as `CLOUDFLARE_HOME_API_TOKEN` into `.env`. `--env-file`
selects another file; start with `uv run --env-file THAT_FILE mitra-bot` so named
tokens reach the process. Direct Python launches also read `.env`; `MITRA_ENV_FILE`
can select the fallback file for named Cloudflare credentials.

The following is an example, not live configuration. Replace zone/record IDs and
node IDs with your actual values. On the primary machine:

```toml
[cloudflare]
enabled = true

[[cloudflare.targets]]
name = "home"
node_id = "primary"
zone_id = "HENNESS_ZONE_ID"
record_ids = ["HENNESS_ROOT_A_RECORD_ID", "PQ_A_RECORD_ID"]
token_env = "CLOUDFLARE_HOME_API_TOKEN"

[[cloudflare.targets]]
name = "another_account"
node_id = "primary"
zone_id = "OTHER_DOMAIN_ZONE_ID"
record_ids = ["OTHER_DOMAIN_A_RECORD_ID"]
token_env = "CLOUDFLARE_OTHER_API_TOKEN"
```

On the secondary machine:

```toml
[cloudflare]
enabled = true

[[cloudflare.targets]]
name = "pryor"
node_id = "secondary"
zone_id = "HENNESS_ZONE_ID"
record_ids = ["PRYOR_A_RECORD_ID"]
token_env = "CLOUDFLARE_PRYOR_API_TOKEN"
```

`node_id` must match `peer-network.toml`. Without a peer network, use `local`.
The wizard binds assignments to the actual node ID when networking is enabled.
A common non-secret assignment file may list all nodes: each filters to its own
node ID and needs only its own credentials. `local` always means the executing
machine, so do not copy a `local` assignment unchanged across servers. If you enable
peering after standalone DNS setup, rerun setup to bind the assignments to a node.

OAuth-backed targets contain `oauth_profile = "home"` instead of requiring the
named token. The corresponding credentials live only in the local secret file.
Never put literal tokens into `config.toml` or peer bundles.

When `targets` is present it replaces the legacy single-zone fields. Existing
`[cloudflare] zone_id`, `record_ids` and `CLOUDFLARE_API_TOKEN` continue to work when
`targets` is absent. The wizard carries forward legacy assignments for unselected
zones when converting. `enabled = false` disables all assignments.

## Operation and recovery

The bot reconciles DNS on startup and whenever its public IP changes. It verifies
readback, preserves existing record settings and retries failed changes on the next
IP poll. Healthy zones/accounts are attempted even when another fails. Logs identify
the target and server without exposing credentials. The Windows update helper also
verifies only this node's assignments and backs up OAuth credentials.

This is per-server dynamic DNS, not automatic DNS failover: if the primary is down,
the secondary does not point the primary's names at its own IP. Assign every record
to exactly one server. Duplicate assignments in one configuration are rejected;
independent files on different machines cannot be checked for global conflicts.
Private-network uptime monitoring continues to report outages separately.

Cloudflare tokens are typically zone-scoped; an account may authorize more records
than this server should update. Mitra's assignment list limits what it writes, while
the Cloudflare grant limits what the credential can access. Existing IPv6/AAAA records
are left alone because public-IP discovery currently uses IPv4.

OAuth and API integration tests use mocked credentials and responses. An actual
registered client must complete a live authorization/refresh test before distributing
the browser flow to users; repository tests cannot verify Cloudflare account policy,
client registration or consent-screen behavior.
