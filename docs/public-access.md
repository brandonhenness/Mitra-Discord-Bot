# Public access and private infrastructure

## Repairing a peer without desktop access

Releases supporting `/servers sync-access` can copy the configured settings owner's
effective infrastructure allowlist to one named peer. Update both nodes first,
then run `/servers sync-access server:Anubis` in an already authorized Discord
server as a Mitra administrator (substitute your peer's name).

This explicitly replaces only the target's `bot.infrastructure_guild_ids`, saves
it to the target's active config file, and applies it immediately. It does not
copy tokens, Cloudflare assignments, UPS settings, todo data, or the state-owner
identity. No restart or automatic ongoing synchronization is involved. Changes
to the owner's allowlist later require another explicit sync or a local edit.
Only the configured owner can send this operation over the certificate-pinned,
mutually authenticated peer connection. A public server's administrator role
does not grant permission to change this setting.

If Discord commands cannot be used, run the following from the owner's bot
installation directory, using the same environment/configuration as its bot:

```powershell
& .\.venv\Scripts\python.exe -m mitra_bot.sync_access Anubis
```

This makes an outbound request using the existing peer credentials; it does not
start a second bot or bind the peer listener. The target must be running the new
release and reachable. Older targets report that an update is required.

Afterward, `/servers doctor` checks connectivity and `/ip status server:Anubis`
can verify infrastructure use from the trusted server. Public servers must still
have only `/about` and `/todo` available. The startup log now records the active
config path, version, allowlist, node and owner, and logs startup exceptions.

One bot identity can serve public Discord servers while retaining private
infrastructure features. Authorization has two layers: the operator authorizes
a Discord server locally, then existing command-specific role checks authorize
members in that server. Discord server owners cannot authorize themselves for
the operator's infrastructure by creating a role or changing Discord command
permissions.

| Feature | Public server | Operator-authorized server |
| --- | --- | --- |
| `/todo` | Its own lists and task history | Its own lists and task history |
| `/about` | General help and policy links | Existing runtime/network information |
| IP, UPS, power, updater, servers, notifications, alerts | Unavailable | Existing member/administrator checks apply |
| Infrastructure alert delivery and shared dashboards | Blocked | Authorized guild channels only |

## Authorize your Discord servers

Set this in the deployment's `config.toml` (use your real Discord server IDs):

```toml
[bot]
infrastructure_guild_ids = [123456789012345678, 234567890123456789]
admin_role_name = "Mitra Admin"
```

This is an operator-only setting. There is no Discord command to change it.
Each listed server is trusted to access this installation's infrastructure;
do not add public customers' servers. Their administrators should run a separate
installation and bot identity if they want to manage their own machines.

When `infrastructure_guild_ids` is omitted, the explicitly saved `[bot].guild_id`
from the setup wizard is the sole authorized server. If neither is configured,
infrastructure access is disabled everywhere. An explicit empty list `[]`
overrides `guild_id` and enables public utilities only. Notification channel
mappings, subscriber roles and membership in the bot's guild list never confer
authorization. Invalid allowlist values stop configuration loading.

Use the same allowlist on every node sharing the Discord identity and restart
all nodes after changes. Ordinary to-do/settings saves preserve this setting.
There is no hot reload of access policy.

## To-do setup and isolation

A member with Discord's **Manage Channels** permission runs `/todo list_create`.
Mitra creates a category, hub and list in that Discord server. The hub's Create
List button also requires Manage Channels. Members work on tasks using the
list's Discord channel permissions. Adding tasks requires View Channel, Send
Messages and Send Messages in Threads. Use Discord channel/category permissions
to control who can see shared boards and the hub.

Mitra needs View Channels, Send Messages, Embed Links, Read Message History,
Manage Channels, Create Public Threads, Send Messages in Threads, and Manage
Threads for these workflows. Manage Threads allows removing thread memberships
when members are unassigned. Public to-do use does not require Administrator or Manage Roles.
Use server installation with the `bot` and `applications.commands` scopes.
See the [public invite and permission checklist](discord-install.md).

Lists are keyed by Discord's globally unique channel IDs with an owning guild
ID. Guild queries exclude other owners and unowned legacy records. Task reads
and writes validate channel ownership; submitted modals and persistent buttons
validate their current guild and list access. Existing correctly owned lists
are preserved. Legacy records with no owner are retained but excluded from
guild-wide lookups; an existing list in its real guild's configured To-Do
category can be recovered using that actual Discord channel. Data is never
copied into a new server when the bot joins it.

This is application-level isolation. The hosting operator can read the local
database and backups; it is not encryption from the operator. Shared hub
summaries are visible to anyone who can view that hub, so restrict its access
if your lists contain sensitive information.

## Rollout

1. Back up the stopped deployment's configuration and state database.
2. Set/review the allowlist locally. Do not derive it from old notification
   mappings, because those might have been configured by an untrusted server.
3. Deploy this version to every node and restart them. The state owner syncs
   public commands globally and infrastructure commands only to authorized
   guilds; it also removes stale guild commands on startup. Runtime checks deny
   unauthorized requests even while Discord is still refreshing its command UI.
4. In a separate test Discord server, create a `Mitra Admin` role and confirm
   only `/todo` and general `/about` are available. Create a list and task and
   verify they do not appear in your private server. Verify private commands
   and alerts still work in your authorized server.

Existing alert subscriptions and saved destinations in unauthorized guilds are
ignored by delivery checks. Legacy subscriber DMs are disabled because those
user IDs carry no guild authorization. Use the authorized server's alert
channel and `/alerts subscribe` role instead.

Removing a guild from the allowlist stops future access/delivery after restart.
It does not erase messages already sent there, revoke knowledge of previously
exposed information, or delete its to-do data. Review old messages separately
if the bot was already invited to untrusted servers before this change.

For stronger process-level separation, run the public utilities under a separate
Discord application and deployment with no infrastructure credentials or peer
network. The allowlist remains useful defense in depth for the private bot.

Discord supports separate global and guild command registration; see the
[Discord application command documentation](https://docs.discord.com/developers/interactions/application-commands).
