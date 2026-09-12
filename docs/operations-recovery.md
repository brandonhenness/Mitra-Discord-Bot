# Operations, recovery and beta acceptance

## Rolling updates

`/update check` defaults to all configured nodes. Confirming Install creates a
single progress message in that channel. The message reports waiting, installing,
reconnecting, complete, already-current and failed states. Its message ID and
rollout are persisted, so the owner can resume reporting after restarting.
The bot needs View Channel, Send Messages and Read Message History there. If
reporting fails, `/update status` remains the authoritative progress view.

Only one node installs at a time; the coordinator installs last. Each node must
return with a new process identity, the requested version, and a Discord
connection on two checks before the next node begins. A failed or timed-out
restart stops the rollout. An unavailable node blocks preflight before installs
start. `/update cancel` prevents subsequent nodes starting; it does not interrupt
an installer already running. Avoid starting another bot process to repair an
installation until the existing process is stopped.

Each updating node gets a 30-minute planned-maintenance window. Health history
continues, but normal outage/recovery alerts are suppressed during that window.
The window ends early after a verified healthy restart. If the node does not
recover, the window expires and the usual outage notification can be delivered.
An administrator's existing active maintenance window takes precedence and is
not shortened. Disconnected observers may not receive the maintenance setting
immediately, so perfect suppression across a partition cannot be guaranteed.
On cancellation or failure, an update window still expires at its original
deadline; use `/servers maintenance` to explicitly change it if appropriate.

Failure recovery: inspect `/update status`, `/servers doctor`, the failing node's
`bot.log` and its `.recovery` files. Repair or restore that node, verify its
version and connectivity, then start a new rollout. Already-current nodes will
be skipped. Do not manually overwrite a live virtual environment.

## Back up and restore a node

Stop the node before backup, so configuration, credentials and databases describe
the same point in time. Run from the installation directory:

```powershell
uv run --no-sync python -m mitra_bot.recovery backup --bot-stopped --env-file .env --output .recovery/node-backup-01
```

The tool includes the selected environment file, application configuration/state,
UPS database, Cloudflare OAuth credential file, peer configuration/state and this
node's certificates/key. SQLite uses its backup API, including committed WAL
contents, and each database is integrity-checked. A manifest records SHA-256
checksums. The new directory is private to the current Windows account or mode
0700 on Unix. It is not encrypted: copy it to private encrypted storage and keep
it outside Git. Back up the offline CA provisioning folder separately.

The supported layout keeps data paths inside the installation directory. Paths
outside it are rejected rather than silently omitted; back up such deployments
manually or first organize a self-contained installation. Credentials injected
only through a service environment or external secret manager must be backed up
through that system as well. No environment-variable values are dumped.

Test a restoration without touching the current installation:

```powershell
uv run --no-sync python -m mitra_bot.recovery restore --source .recovery/node-backup-01 --output .recovery/restored-node-01
```

All checksums and database integrity checks run before restoration begins.
The destination must not already exist. This creates data/configuration only;
it does not start a bot or install program files. Restore into a fresh deployment
of the backup's recorded Mitra version, install dependencies, and place the
verified data at its recorded relative paths. Review absolute paths if moving
to another directory. Run `mitra-doctor` with the correct environment file,
then start one process and check `/servers doctor` and `/about`.

Never start the restored copy while the old node identity is still running.
Restore a node's own peer-state database and key, not another node's database.
Use the latest backup available: older peer history can cause sequence-regression
diagnostics on other observers, which requires investigation before considering
history replication healthy. Do not delete history to hide that diagnostic.

## What survives an owner outage

`/servers doctor` now calls out the configured owner and its dependencies.
Surviving nodes can handle monitoring, shared alerts, IP/about reads and targeted
power/UPS status. UPS configuration changes, rolling updates, channel settings
and ToDo writes still require the fixed application-state owner. Application
state is not automatically replicated, and there is no automatic owner promotion.

For owner recovery, fence the old process (ensure it cannot restart), restore
that same owner's backup onto its replacement, and preserve its node identity.
Ensure its reachable DNS address now reaches the replacement, or update the
other peers' address configuration. Start it only after the original is fenced.
This is recovery of the original owner, not promotion of an independently
modified follower database. Automatic state-owner failover needs a separately
reviewed replication/conflict-resolution design and partition tests.

## Add or remove a peer

Use the complete, current provisioning snapshot; additions require its original
`OFFLINE-CA.key` and `ca.crt`. Existing keys and network identity are preserved.
Prefer a reachable DNS-only hostname for dynamic public IPs. The tool creates a
new output folder and does not change live installations.

```powershell
uv run --no-sync python -m mitra_bot.peer_membership --bundle-root peer-bundles --add third=third.example.net --output .recovery/membership-add-third
```

Stop and back up existing peers. On each existing machine, apply only its own
prepared configuration:

```powershell
uv run --no-sync python -m mitra_bot.peer_membership --apply .recovery/membership-add-third/mitra/peer-network.toml --config peer-network.toml --bot-stopped
```

Use `test/peer-network.toml` on test, not mitra's file. Apply verifies the local
node/network/owner/certificate identity, backs up the old configuration, and
changes only its peer list. It retains local paths, tuning and certificates.
Install the new node's own bundle through `mitra-setup` and reuse the network's
Discord bot token. New trust entries do not automatically grant power control;
review `allow_power` separately if the new node must forward power commands.

Restart all nodes and run `mitra-doctor`, `/servers list` and `/servers doctor`.
The offline check verifies TLS/certificate pins; the running doctor additionally
checks application identity, clocks and history replication. Resolve failures
before considering the membership change complete. Keep the output as the new
provisioning snapshot; securely retain the original offline key with that
snapshot for future additions. Never distribute the offline key to peers.

Removal uses the same prepare/apply sequence:

```powershell
uv run --no-sync python -m mitra_bot.peer_membership --bundle-root current-bundles --remove third --output .recovery/membership-remove-third
```

Removal takes effect only when every remaining node has applied its new trust
list and restarted. The owner cannot be removed with this workflow; at least two
members must remain. Use single-server setup to leave a two-node network.

## Live two-machine beta acceptance (operator required)

Automated tests exercise update ordering, lost acknowledgements, restart
verification, persistent progress, TLS authorization and maintenance deadlines.
They do not establish that two deployed Windows processes can actually install
and reconnect through Discord. Record the beta version and results below during
the next release test; these steps have not been performed automatically.

1. Back up both stopped nodes, then start both. Verify `/servers doctor`,
   `/about` and `/update status`. Confirm the same application and expected node
   identities. Both nodes must contain the new progress/maintenance code to test
   all improvements; the first upgrade from beta 9 still uses beta 9's coordinator
   until its restart. Test the full experience with a subsequent beta.
2. Stop test before `/update check server:all`. Confirm preflight names test and
   that neither node starts installing. Restart test and verify TLS/Discord.
3. Install the next beta on all nodes. Record one progress message, test updating
   first, its return on the expected version, and mitra updating last. Confirm
   that the progress message finishes after mitra returns.
4. Confirm planned restarts appear in health history without normal outage
   mentions. For a controlled failure test on the test machine, prevent its
   restart after installation. Verify the owner does not update, progress stops
   with recovery instructions, and the outage alerts after maintenance expires.
   Restore test, verify its version and health, and resume via a new rollout.
5. Stop mitra separately. Confirm test still serves monitoring, IP/about reads
   and alerts. Confirm doctor explains owner-dependent commands. Restore mitra.
6. Restore a backup into a separate directory and verify its manifest/databases.
   Do not start the duplicate identity. Record the result before production use.

Release gate: require green CI and recorded success for normal rolling restart,
failure-stop behavior, outage/recovery delivery, and backup restoration before
promoting the beta to a full release.
