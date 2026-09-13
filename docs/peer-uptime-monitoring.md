# Peer uptime monitoring

Every enabled peer network now collects availability history automatically, even
before Discord connects and without a UPS. Use the same Discord application/token
on all nodes; a two-node deployment needs no witness. Upgrade all nodes together.
See [private network setup](private-peer-network.md) for certificates and membership.

## Enable subscriber notifications

Mitra uses one permissionless **Mitra Alerts** role per Discord guild for IP
changes, every node's outage/recovery alerts, and automatic update announcements.

1. As a Mitra administrator, run `/alerts setup channel:#mitra` once per guild.
   `/servers alerts channel:#mitra` is the peer-network alias.
2. Members run `/alerts subscribe` or `/alerts unsubscribe` for all alerts.
   Subscriptions are managed only through `/alerts`; no server selection is needed.
   Mitra administrators may supply `user:` on `/alerts subscribe` or
   `/alerts unsubscribe` to manage another member, for example
   `/alerts subscribe user:@Alex`. Supplying `user:` always requires the configured
   Mitra admin role; omitting it keeps self-service available to everyone.
   The selected member must belong to this guild. Confirmations are private and
   do not ping the member; role audit reasons identify the administrator.
3. Use `/servers alerts-test server:test mention:true` to check delivery.
   Server selection here identifies the simulated event, not a subscription group.

Setup copies members of configured old IP and per-node subscriber roles into
Mitra Alerts, then removes their old subscription memberships. Old roles are left
empty for administrator review/deletion. A failed assignment preserves the old
membership; rerun setup to finish migration. The bot fetches the full member list
and needs Server Members Intent plus Manage Roles. It refuses privileged or
unmanageable subscription roles and duplicate shared role names. Place the bot's
role above the subscription roles. Existing channels and role settings remain
usable until setup is run; upgrade all peers before migrating.

The role must have no permissions or channel grants. Members need access to the
alert channel, and Discord notification settings still govern push notifications.
The channel needs View Channel, Send Messages, Embed Links and Read Message History.

In a peer network the shared channel/role is replicated. IP notifications prefer
this shared channel over legacy per-machine channel settings. `enabled:false` on
`/servers alerts` disables IP and health alerts for that guild; automatic update
announcements retain their separate updater settings. Existing node-specific
history, monitoring policies and maintenance windows remain separate.

Settings report pending peer replication. Wait for zero pending copies before
an outage test. Setup does not retrospectively announce already-open incidents.

## Status and history

| Command | Result |
| --- | --- |
| `/servers status server:server-a hours:24` | Health card, peer/Discord timelines, mean probe RTT and process restart markers. |
| `/servers dashboard hours:24` | Eight servers per page, availability timelines and current state cards. |
| `/servers incidents server:server-a` | Eight observed outage/recovery episodes per page. |
| `/servers monitoring` | Current monitoring thresholds and retention. Optional arguments change the shared policy. |
| `/servers doctor` | Read-only connectivity, certificate expiry, replication, permissions and alert queue checks. |
| `/servers alerts-test server:server-a` | Send a clearly labeled delivery test without pinging subscribers by default. |
| `/servers maintenance server:server-a minutes:60 reason:Updates` | Suppress this server's alerts network-wide for planned work; keep recording history. |
| `/servers dashboard-pin channel:#server-status interval:300` | Create/reuse a pinned dashboard that peers refresh automatically. |
| `/servers dashboard-stop` | Stop automatic refresh; retain the timestamped message. |

`observer:` selects whose observations to inspect; the default is the responding
instance. This matters during partitions: B may reach A while C cannot. The system
keeps both reports and never manufactures a global consensus. Self-observations show
the local process and gateway, not an independent network test.

Dashboard controls provide 1h/6h/24h/7d windows, Refresh, server/observer selection,
and page navigation. Selectors show the current page's nodes; slash arguments accept
any configured ID. Controls are reconstructed by any responding instance and create
a new ephemeral result, so they survive a process restart. Dashboard/history access
uses the existing Mitra administrator check. Members can subscribe without admin access.

### Diagnostics and test alerts

`/servers doctor` performs fresh authenticated health/history requests without changing
settings, incidents, cursors or health observations. It checks local public certificate
expiry and the expiry of certificates presented by reachable peers, warns about upcoming
renewals, and reports history catch-up or regressed sequences. It also reports task state,
queued alerts, delivery errors, configured dashboard and alert-channel permissions.
It never prints private keys or the bot token and never sends channel notifications.

`/servers alerts-test` sends a **TEST ONLY** message to the configured, enabled alert
destination. Add `mention:true` to explicitly test the selected subscriber role. The
default does not ping subscribers. The test does not create incidents or fake downtime,
and it is not retried through the outage outbox. An ambiguous send instructs the admin
to check the channel before retrying. Both commands require the Mitra administrator role.

### Maintenance windows

Maintenance is a replicated setting for the selected node across all guilds in this
private network. A duration of zero ends it early; the maximum is seven days. The status
card shows its reason and expiry. Peers continue collecting samples and recording
incidents, so planned downtime remains visible in availability history.

While maintenance is active, outage alerts wait. If the incident recovers during the
window, its pending notifications are marked suppressed and are not replayed later.
If the node remains unreachable when the window expires or is ended early, its pending
outage can be delivered by an observer. Already delivered notifications are not removed.
Pending settings replication is reported: a disconnected peer cannot honor a new window
until it receives it. Set maintenance before disconnecting a machine and check replication.

### Shared pinned dashboard

`/servers dashboard-pin` publishes a server-health dashboard visible to everyone with
access to the selected channel. It requires the existing channel permissions plus
Attach Files and [Pin Messages](https://docs.discord.com/developers/resources/message#pin-message).
The command creates or reuses this network's dashboard message and replicates its ID,
window, page and refresh interval. The default refresh is 300 seconds, configurable
from 60 to 3600 seconds. There is one configured shared dashboard per guild.

Every connected peer can update the same saved message. Nodes stagger takeover by
15 seconds per sorted node rank and inspect the message's last update time, checking
again after rendering. A surviving node uses its own observations and labels itself
as the observer. The dashboard has no subscription mentions or interactive controls;
use the ephemeral `/servers dashboard` for selectors and arbitrary pages.

This is best-effort coordination for a display, not a distributed lock. Concurrent
updates can occasionally occur, and simultaneous initial setup during a partition
can create duplicate messages. Existing pins/recent messages are checked when setup
is retried. Moving the dashboard to a different channel leaves the old message as a
timestamped snapshot. If the message is deleted, use `/servers dashboard-pin` to
recreate it; workers do not create replacement messages automatically. A failed pin
can leave a saved, updating but unpinned message; inspect the response and channel.

`/servers dashboard-stop` replicates a stop setting and retains the old message.
Peers check the setting again before edits, but a disconnected peer may continue
until the stop setting reaches it. Use `/servers doctor` to inspect refresh failures.

All graph timestamps are UTC. Green means successful peer probes (or a reported
connected gateway), red means failed probes (or a reported disconnected gateway),
and gray means unknown. A red sample can be a short transient below the alert
threshold; the current state card distinguishes suspect, unreachable and recovering.
RTT gaps are absent measurements, not zero latency. Purple dotted markers indicate
an observed process boot ID change, not proof of an OS reboot.

Availability is successful observed duration divided by all observed duration.
Coverage is observed duration divided by the requested window; no known duration
produces **Unknown**, not 100%. Ten-second samples estimate the preceding probe
interval. The first observation after startup, a clock discontinuity, or a gap
over 2.5 intervals adds no known duration. Observer downtime therefore remains gray.
Five-minute buckets preserve successful/failed/unknown duration, but positions within
a bucket are approximate. Window boundaries use raw intervals when available and
proportional bucket estimates after raw retention expires.
Long graph windows combine buckets to at most approximately 720 plotted columns;
the axis states the rendered resolution while numeric metrics use the stored durations.

The last observation and last successful peer response are shown separately. Stale
observer data makes current state unknown. Process uptime and gateway connectivity
are last reported measurements; an unreachable machine's gateway state is unknown.
The bot cannot establish that a host is physically powered off.

## Detection and delivery

Default probes run every 10 seconds with a 3-second timeout, independently of UPS
snapshot polling and Discord readiness. Eight concurrent probes and four concurrent
history synchronizations bound network work. Startup gives a never-seen peer 60
seconds of grace. A known peer requires three failed probes and at least 30 seconds
since successful contact before opening an incident. Actual delivery also includes
probe timeout, scheduling, and reporter staggering. Two consecutive successful
probes confirm recovery. A failure while recovering keeps the same episode open.

Peer reachability and reported Discord connectivity have separate incidents. Probe
errors distinguish timeout, connection failure, certificate/TLS failure and protocol
or identity rejection. Timers use monotonic time; stored/displayed times use UTC.
Remote timestamp skew is flagged when a valid response can be received; large clock
skew can also cause the peer protocol's timestamp validation to reject an RPC.

Incident transitions and notification intent commit together in SQLite. Each observer
retains its own outbox across restarts. A failed Discord send retries with exponential
backoff; recent bot-authored message markers and a saved nonce are checked on retries.
If recovery occurs before outage delivery, the eventual message is a historical
summary containing both timestamps, rather than a fresh offline alert.

With two nodes, B reports A's loss. With more nodes, reporters sort by node ID and
stagger by 15 seconds per rank. Later reporters inspect the latest 100 channel messages
for an overlapping report before sending. A five-minute default cooldown suppresses
repeated role pings during flapping; subsequent episodes remain in history.

This is best-effort duplicate suppression. Partitioned observers, a busy channel,
deleted messages, or an ambiguous delivery older than Discord's recent nonce window
can produce duplicates. Discord's [`enforce_nonce`](https://docs.discord.com/developers/resources/message#create-message)
does not provide a durable global transaction. Reports always identify their observer.
Use a dedicated alert channel for effective reconciliation. Read failures defer
delivery instead of blindly resending. No monitoring mechanism authorizes power actions.

## Storage, replication and retention

Monitoring tables live in the configured `peer-state.db`, alongside but separate from
the power journal. Defaults retain raw events for seven days, five-minute rollups
for 90 days, and closed incident/outbox summaries for one year. Open incidents and
last-known status remain available. Pruning uses bounded transactions; SQLite reuses
freed pages and the file need not shrink immediately.

Every 15 seconds peers pull directly from each authenticated observer, at most four
500-record pages per pass. Sequence IDs survive restarts, imports are idempotent,
and each replica builds rollups from the original observer's samples. Configuration
snapshots also travel with history pages. A replica can display previously copied
history while the observer is offline. Outbox rows are local; Discord message markers
coordinate delivery. A retained raw-history gap is explicitly surfaced on the dashboard.

New/reconnecting replicas can backfill only raw records still retained by the source.
Older rollups already on a replica remain; this release does not transfer long-term
archives or relay another observer's records through third parties. Configure retention
and intervals accordingly. Full-mesh history volume grows with the square of node
count; a two-node mesh collects about 242,000 raw samples per week at defaults, before
incidents/settings, so budget disk space and measure the database in your deployment.

Back up each database with SQLite's backup mechanism or while Mitra is stopped.
Do not share a live SQLite file between servers. Keep the database when restarting
or upgrading a node. Replacing/restoring an observer database with an older sequence
requires rebuilding its replica cursors/history or provisioning a new node ID;
otherwise history sync rejects the regressed cursor instead of merging conflicting IDs.

`health_*` fields in `peer-network.toml` set initial local policy. Once changed through
`/servers monitoring`, the persisted replicated policy overrides those fields. Run
the command again to change it. Defaults and bounds are in `peer-network.example.toml`.

## Validation and remaining extensions

Automated tests cover outage/recovery thresholds, startup grace, restart durability,
observer gaps, clock jumps, Discord-only failure, replication/retention, exact recent
window boundaries, role privilege checks, interaction claiming, delayed summaries,
nonce/mention payloads, and real TLS history/settings exchange. A blocked UPS snapshot
test verifies health collection continues. PNGs are rendered and visually inspected
with outages, missing coverage, gateway disconnection and multiple servers.

No live Discord messages or OS power actions were sent during development. Before
enabling production alerts, validate two/three actual instances with process loss,
peer-link loss, Discord-only loss, recovery, and observer restart. Confirm actual role
delivery, channel permissions, rate limits and duplicate behavior in that environment.

Optional future extensions include long-term archive bootstrap,
DM subscriptions, IANA display timezones, an explicit
multi-observer aggregate view, and CPU/memory/disk charts. They are not required for
the implemented peer availability monitoring and channel notifications.
