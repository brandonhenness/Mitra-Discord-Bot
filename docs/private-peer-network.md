# Private peer networks: one bot, multiple active machines

Every instance uses the **same Discord application token** and connects independently.
Discord shows one bot. Two machines work without a witness, elected gateway owner,
or quorum. Losing one instance does not stop another accepting monitoring and power
commands. Each machine continues its own IP/UPS monitoring.

Peers communicate directly over mutually authenticated TLS. Separate deployments
create independent private networks using separate CAs, network IDs and Discord
applications. No registration server or online CA is required.

## Setup

1. Install Mitra on every machine with separate local `.env`, `config.toml`, state
   and UPS files. Put the **same `DISCORD_APPLICATION_TOKEN`** in every `.env`.
   Invite that application to Discord once. Use the same administrator role name
   everywhere. Initialize its roles/commands using the application-state owner.
2. On a trusted provisioning machine with OpenSSL (Git for Windows includes it), run:

   ```powershell
   python -m mitra_bot.init_peers --output peer-bundles --node server-a=192.168.1.11 --node server-b=192.168.1.12
   ```

   Add more `--node` arguments for larger meshes. The destination must not exist.
   Node IDs are case-sensitive and contain letters, digits, underscores or hyphens.
   Peer addresses must be directly reachable; there is no NAT traversal.
3. Give each machine **only its own bundle folder**. Copy `peer-network.toml`,
   `ca.crt`, `node.crt`, and `node.key` beside the bot, or set the process environment
   variable `MITRA_PEER_CONFIG_PATH` to its TOML path. That variable is not loaded
   from `.env`. Relative paths resolve beside the TOML file.
4. Restrict key permissions to the service account. Keep `OFFLINE-CA.key` offline.
   Protect the provisioning directory: it contains every node's key. Certificates
   expire after one year; renew and distribute fingerprints before expiration.
5. Allow inbound TCP 9843 from peer hosts. Adjust addresses/ports as needed. Restart
   after configuration changes. Keep peer lists and `state_owner` consistent.
6. A peer can read telemetry by default. On each target, set `allow_power = true`
   for authorized forwarding peers. Provision with `--allow-power` to grant this
   permission to all members.

The first provisioning node becomes `state_owner`. Set it explicitly on all nodes
when editing configuration manually. If omitted, the lowest sorted node ID is used.
Keep it stable when adding members. It owns pre-existing unreplicated application
data, not the network or the other machines. Without enabled peer configuration,
standalone operation remains and no peer listener/polling/database is created.

## Command handling

Discord supports [multiple sessions for an application](https://docs.discord.com/developers/events/gateway).
Mitra staggers IDENTIFY attempts. All instances share Discord's application limits;
this is not unlimited horizontal scaling. The state owner synchronizes command
definitions. Other nodes use the registered commands without overwriting them.

The preferred node attempts each slash command's initial acknowledgement first.
Other nodes wait a short, bounded delay. **Only a successful ACK permits invoking
the handler.** A duplicate ACK, expired interaction or ambiguous network error
stops processing on that instance. Rejected ACKs never become followups that
execute anyway. The timeout budget fits Discord's
[three-second response window](https://docs.discord.com/developers/interactions/receiving-and-responding).
Failure after a successful ACK may require a new user command; acknowledgements
are not durable jobs.

Mesh command responses are ephemeral. Only the requester sees a power confirmation
and must have the configured administrator role.

- `/servers list`: responding instance, state owner, reachability, last health
  samples, this node's process uptime and peers' reported Discord connectivity.
- `/ups status server:server-b hours:24`: server-b is preferred; another instance
  can query it over TLS or return its labeled cached UPS history.
- `/power restart server:server-b delay_seconds:60`: signed confirmation for
  server-b. Shutdown and cancel also accept `server`.
- Omitting `server` uses **state_owner** as a stable default, even if another node
  answers. Unknown IDs fail; failed requests never execute on a different machine.

## Power safety

Compact signed button IDs contain target, operation ID, action, delay, force,
requester and expiry. The signature binds them to the network, guild and channel.
The original message must belong to the bot. Any instance can validate the buttons;
they do not require a process-local view and expire after ten minutes. Changing
the shared token invalidates outstanding confirmations.

Only the target executes its OS action. Local and forwarded requests share a
**target-owned journal** keyed by stable operation ID, independent of forwarding
node. It commits an uncertain result before execution; a crash cannot cause
automatic execution of that operation again. Records persist across restarts.
Never delete or share the peer database between machines.

Confirmation and cancellation serialize at the target. Canceling before confirmation
records cancellation without calling the OS; later confirmation cannot execute it.
Canceling after successful scheduling may call the OS abort once. A fresh
`/power cancel server:...` can abort a scheduled shutdown after buttons disappear.
An uncertain result requires checking the target; there is no automatic power retry.

`allow_power` controls forwarding peers. A machine handling its own Discord command
does not need another peer's permission. Approved forwarding peers are trusted to
enforce Discord administrator checks. There is no remote shell. Power actions
retain the existing Windows-only implementation and cannot turn on an offline host.

## Monitoring and application state

Cloudflare DNS assignments are local to each server and can span multiple accounts
and zones. Each node updates assigned records with its own public IPv4, regardless
of which node owns legacy shared application state. Credentials are never sent to
peers. Use `mitra-cloudflare-setup` on each machine; see [Cloudflare configuration](cloudflare.md).

UPSCog records history locally in `ups_stats.db`, automatically importing legacy
JSONL history without changing the original file. Each peer caches remote UPS
snapshots in its own database, retaining recent graphs after source/viewer restart.
Snapshots contain up to 5,000 representative samples across the last seven days;
the source database retains the full archive. It is not a complete replicated archive. Offline graphs show capture
time and use a window ending at capture, never presenting old samples as current.

A lightweight health RPC reports node ID, boot ID, process uptime and self-reported
Discord connectivity without USB reads. Independent probes now persist availability
history and incidents, replicate observer records, and deliver opt-in outage/recovery
alerts. `/servers status` and `/servers dashboard` render history graphs with explicit
observation coverage. See [uptime monitoring setup](peer-uptime-monitoring.md).

IP/UPS events notify through their observing instance's Discord connection, labeled
with its node ID. Delivery is best effort; ambiguous messages are not resent through
another instance. Configure destinations on each node and coordinate Cloudflare
records yourself.

Existing To-Do, notification-setting, updater, IP command and UPS-setting commands
remain assigned to `state_owner`. To-Do listeners and periodic update notifications
also run there. Their databases are not replicated. Other nodes report that the
owner did not accept a stateful command instead of modifying a different database.
Monitoring/power commands continue without that owner. `/update` updates that
owner; update other installations with the existing local update procedure.

## Membership and validation

TLS checks CA trust/expiry both ways and pins exact SHA-256 certificate identities.
Hostname checking is replaced by certificate pinning before sending application
data. See Python's [mutual TLS requirements](https://docs.python.org/3.12/library/ssl.html).
RPCs also check source, target, network and a token-derived application identity.
Synchronize clocks: envelopes expire after 60 seconds and confirmations use UTC expiry.

Revoke a member by removing it from all peer lists and restarting those instances.
To add one, issue its own client/server certificate with the offline CA, distribute
its address/fingerprint, and configure the same network, state owner and Discord
token. Automatic enrollment is not included. Do not copy another node's key/database.
Use a new database when changing node/network identity. All members must be trusted:
bot-token holders can act as the application outside this software too.

Tests use real local mutual TLS and mocked Discord/OS calls. Before production,
run a controlled two-machine Discord acceptance test: event delivery to both
sessions, one response per interaction, confirmation after origin failure, and
monitoring/power access with the other node off. These live checks are not yet run.

## Repairing an early-beta CA certificate

Some OpenSSL installations added their default CA extensions alongside Mitra's
requested extensions. The resulting duplicate Basic Constraints made the CA invalid:
TCP connections worked, but TLS failed with `CERTIFICATE_VERIFY_FAILED` and peers
appeared unreachable. Provisioning now uses an explicit configuration and verifies
the CA and every node certificate before completing.

For an affected existing network, use the original provisioning machine with its
`peer-bundles/ca.crt`, `peer-bundles/OFFLINE-CA.key` and node bundle folders. After
updating the software to a version containing the repair utility, run:

```powershell
uv run --no-sync python -m mitra_bot.repair_peer_ca --bundle-root peer-bundles --output repaired-ca.crt
```

The utility writes a new public certificate only after validating every existing
node against it. It preserves the CA's public key, subject, serial and validity
dates. It refuses to overwrite files or proceed with a mismatched offline key.

After successful verification:

1. Stop every bot in this private network.
2. Back up each installation's existing `ca.crt`, then copy `repaired-ca.crt` to
   that installation as `ca.crt` (or the path configured by `ca_file`). Give all
   peers the same repaired certificate.
3. Back up and replace the CA certificate in the original provisioning folder
   and each saved node bundle too, so future installations use the repaired CA.
4. Restart the bots and run `/servers list` and `/servers doctor`.

Keep existing `node.crt`, `node.key`, peer configuration and databases. Do not
transfer `OFFLINE-CA.key` to other machines; only the repaired public certificate
needs to be distributed. If the offline key is unavailable, this repair cannot
preserve the existing certificates: a replacement network must be provisioned.
