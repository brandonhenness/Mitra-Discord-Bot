# Mitra Discord Bot

<img src="mitra.png" width="320" height="320" alt="Mitra logo" />

Mitra is a modular Discord bot package for home/server operations.  
It monitors public IP changes, supports UPS status/graphing, and provides admin-only power controls.

## Guided setup

On Windows, extract the deployment ZIP and double-click **Setup-MitraBot.cmd**.
With Python 3.10+ or uv installed, it installs dependencies and starts a wizard
that opens Discord's setup pages, validates your bot token, generates the install
link, configures notifications/admin access, and optionally provisions private peers.
Existing installations can run `uv run mitra-setup`; headless machines can add
`--no-browser`. Manual `.env` and `config.toml` setup remains supported.
See [setup instructions](docs/setup.md).

UPS history now uses SQLite with automatic, non-destructive JSONL migration.
See [UPS history storage](docs/ups-history.md). Maintainers can publish tested,
versioned release packages through [the release workflow](docs/releases.md).

## What Changed

This project is no longer a single `bot.py` script.  
It is now a package-based app under `mitra_bot/` with cogs, services, tasks, and storage modules.

## Features

- Public IP monitoring with Discord notifications.
- UPS monitoring and `/ups` commands (status, graph, controls).
- Admin-only `/power` actions (restart, shutdown, cancel) with confirmation UI.
- GitHub release updater with `/update` commands and admin confirmation UI.
- `/about` command for runtime/version details.
- Role-based access (`Mitra Admin` and `Mitra IP Subscriber` by default).
- Persistent config via `config.toml` and runtime state via `state.db`.

## Project Layout

```text
mitra_bot/
  main.py                      # app entrypoint
  settings.py                  # settings loader (config.toml + env)
  discord_app/
    bot_factory.py             # bot + cog registration
    cogs/
      ip_cog.py
      power_cog.py
      ups_cog.py
  services/
    ip_service.py
    power_service.py
    notifier.py
    ups/
      tripplite_client.py
      ups_service.py
      ups_log.py
      ups_graph.py
  tasks/
    ip_monitor_task.py
    ups_monitor_task.py
  storage/
    storage_store.py             # storage API over config/state backends
config.toml                    # operator config (non-secrets)
state.db                       # runtime mutable state (SQLite)
```

## Requirements

- Python 3.10+ (3.11+ recommended)
- [uv](https://docs.astral.sh/uv/) for environment + package management
- Windows for `/power` actions (`shutdown` command integration is Windows-specific)

## Development

Install runtime + development dependencies (includes `pytest`):

```bash
uv sync
```

Initialize local config files from templates:

```bash
uv run mitra-init
```

Run tests:

```bash
uv run pytest
```

Run Cloudflare integration tests (opt-in, live API calls):

```powershell
$env:RUN_CLOUDFLARE_INTEGRATION="1"
uv run --env-file .env pytest tests/test_cloudflare_service_integration.py -m integration -q
Remove-Item Env:RUN_CLOUDFLARE_INTEGRATION -ErrorAction SilentlyContinue
```

Run the bot:

```bash
uv run --env-file .env mitra-bot
```

Alternative entrypoint:

```bash
uv run --env-file .env python -m mitra_bot.main
```

## Production

Install runtime dependencies only and enforce the lockfile:

```bash
uv sync --no-dev --frozen
```

Initialize production config files from templates:

```bash
uv run mitra-init
```

Start the bot:

```bash
uv run --env-file .env.production mitra-bot
```

For Windows service/task setups, use the same command after setting required environment variables and ensuring `config.toml` exists.

### Starting automatically on Windows

Double-click `Start-MitraBot.cmd` for a foreground launch with visible errors.
The launcher resolves the repository location automatically and loads `.env`.

To install an elevated scheduled task that starts Mitra one minute after
Windows boots, open PowerShell as Administrator in the repository and run:

```powershell
.\scripts\Install-MitraBotStartup.ps1 -AccountName "Mitra"
```

Enter that Windows account's password when prompted. The password is passed
directly to Windows Task Scheduler and is not written to a project file. The
task runs whether or not the account is logged on, restarts Mitra after a
failure, and refuses to launch a second instance. Manage it with:

Simple local account names such as `Mitra` are automatically qualified as
`COMPUTERNAME\Mitra`. A domain-qualified name or user principal name can also
be supplied directly when applicable.

```powershell
Start-ScheduledTask -TaskName "Mitra Discord Bot"
Stop-ScheduledTask -TaskName "Mitra Discord Bot"
Get-ScheduledTaskInfo -TaskName "Mitra Discord Bot"
```

Stop the scheduled task before using the deployment updater, then start it
again after the update succeeds.

### Updating a Windows server

Use [`scripts/Update-MitraBot.ps1`](scripts/Update-MitraBot.ps1) for manual
deployments. Keep a copy beside the repository so the updater itself is not
replaced while Git is changing the checkout. The script requires the bot to be
stopped, makes a private hash-verified backup outside the repository, and never
restarts the bot or reapplies a Git stash automatically.

First stop the service, scheduled task, or console process. Then run once
without `-AutoStash` so any local Git changes are displayed and backed up:

```powershell
& "C:\Users\Mitra\Documents\GitHub\Update-MitraBot.ps1" `
  -RepoPath "C:\Users\Mitra\Documents\GitHub\Mitra-Discord-Bot" `
  -EnvFileName ".env" `
  -ConfirmBotStopped
```

Review the reported paths. If those changes should be preserved in a stash,
run the update with both `-AutoStash` and `-VerifyCloudflareWrite`:

```powershell
& "C:\Users\Mitra\Documents\GitHub\Update-MitraBot.ps1" `
  -RepoPath "C:\Users\Mitra\Documents\GitHub\Mitra-Discord-Bot" `
  -EnvFileName ".env" `
  -ConfirmBotStopped `
  -AutoStash `
  -VerifyCloudflareWrite
```

The updater leaves the stash in place for manual review, pulls `main` with
fast-forward-only Git operations, runs `uv sync --no-dev --frozen`, migrates a
legacy `cache.json` when present, and validates the package, selected env file,
TOML, and SQLite database. It always performs a live Cloudflare read when that
integration is enabled. `-VerifyCloudflareWrite` additionally discovers the
server's public IPv4, updates every configured A record, and requires a second
API read to match. After a verified migration, the updater moves the obsolete
cache into the private external backup so future updates cannot re-import
stale data.

If both `.env` and `.env.production` exist, `-EnvFileName` is required and must
match the file used by the service launch command. The updater also honors
`MITRA_CONFIG_PATH` and `MITRA_STATE_PATH` from that env file. It intentionally
stops if Cloudflare is enabled but the selected env file lacks a nonempty
`CLOUDFLARE_API_TOKEN`.

After the updater succeeds, restart the existing service/task. For a foreground
launch that explicitly uses `.env`:

```powershell
Set-Location "C:\Users\Mitra\Documents\GitHub\Mitra-Discord-Bot"
uv run --env-file .env mitra-bot
```

On startup, Mitra reconciles every configured Cloudflare record even when the
stored IP has not changed. Confirm the live server result in `bot.log`:

```powershell
Get-Content .\bot.log -Tail 200 |
  Select-String -Pattern "Cloudflare DNS readback verified|Cloudflare DNS update complete|Updated DNS record|Failed to.*Cloudflare"
```

`Updated DNS record` confirms a PATCH was accepted, and `Cloudflare DNS
readback verified` confirms the configured records were subsequently read at
the current public IP. If every record already contained that IP, the update
count is zero and the initial API read serves as the verification.

### Migrating a legacy `cache.json`

The Windows updater performs this automatically. For a standalone migration,
stop the bot and run a dry run first:

```powershell
uv run --no-sync mitra-migrate-cache --root . --env-file .env
```

Apply only after reviewing the plan:

```powershell
uv run --no-sync mitra-migrate-cache --root . --env-file .env `
  --apply --confirm-bot-stopped
```

The migrator copies the Discord token into the selected env file without
overwriting a modern value, writes bot/UPS/Cloudflare non-secrets to
`config.toml`, and writes mutable state such as the last IP to `state.db`.
Legacy Cloudflare Global API Key/email credentials and obsolete command-sync
state are retained only in the private backup. A scoped token must be supplied
as `CLOUDFLARE_API_TOKEN`. Unknown fields or conflicting modern values stop the
migration without changing targets; the source cache is never modified by the
migrator itself.

## Configuration

Mitra reads config from `config.toml` and environment variables, and stores mutable runtime data in `state.db`.

- `DISCORD_APPLICATION_TOKEN` supplies the bot token.
- `MITRA_ENABLE_MEMBERS_INTENT` defaults to `true` (set `false` to disable).
- `MITRA_CONFIG_PATH` overrides the config file path (default: `config.toml`).
- `MITRA_STATE_PATH` overrides the state DB path (default: `state.db`).
- Cloudflare secret is env-only: `CLOUDFLARE_API_TOKEN`.

Pass the env file explicitly to `uv run`. This makes every setting available to
the whole process, including path overrides, Discord intent configuration, and
Cloudflare verification code:

```bash
uv run --env-file .env mitra-bot
```

```bash
uv run --env-file .env.production mitra-bot
```

Best practice:

- Put secrets and deploy-specific values in env files (`DISCORD_APPLICATION_TOKEN`, Cloudflare API credentials, etc.).
- Keep long-lived non-secret defaults in `config.toml` (poll intervals, role names, UPS defaults).
- Keep runtime state and bot-managed data in `state.db` (subscribers, notifications map, todo state, updater state).
- Commit only template files (`.env.example`, `.env.production.example`, `config.example.toml`), never real secret files.

Common `config.toml` sections:

- `[bot]`: channel ID, IP polling interval, role names
- `[ups]`: UPS monitoring defaults (`enabled`, `poll_seconds`, thresholds, logging, timezone, etc.)
- `[cloudflare]`: non-secret Cloudflare options (`enabled`, `zone_id`, `record_ids`)

Common `[bot]` keys:

- `channel_id`: notification channel ID
- `ip_poll_seconds`: IP monitor interval
- `admin_role_name`: admin role for restricted commands
- `ip_subscriber_role_name`: role used for IP notifications

### Cloudflare DNS updates

For multi-server, multi-domain or separate-account setups, run
`uv run mitra-cloudflare-setup` on each server. It supports browser OAuth consent
(with a registered Mitra client ID), manual tokens, domain/subdomain selection,
and reviewed creation of missing A records. Each server updates only its own
assignments. See [Cloudflare setup and examples](docs/cloudflare.md).

The single-zone configuration below remains supported for existing installations.

Mitra authenticates to Cloudflare with `CLOUDFLARE_API_TOKEN` as a Bearer API
token. Create a scoped API token for the intended zone and grant DNS read and
update access—for example, `Zone / DNS / Edit`, or DNS Read plus DNS Write when
the token UI exposes separate permissions. Legacy Global API Key and email
authentication are not supported.

Keep the token in `.env` and enable/configure the non-secret identifiers in
`config.toml`:

```toml
[cloudflare]
enabled = true
zone_id = "0123456789abcdef0123456789abcdef"
record_ids = [
  "11111111111111111111111111111111",
  "22222222222222222222222222222222",
]
```

- `zone_id` is the Cloudflare zone ID for the domain, not the domain name or
  account ID.
- `record_ids` contains the exact IDs of existing DNS records to maintain, not
  hostnames. Configure `A` record IDs only; the current public-IP monitor is
  IPv4-only and treats other record types as configuration errors.
- The IDs are available from the Cloudflare dashboard or API. Restrict the API
  token to the same zone.
- `enabled = false` disables reconciliation even when the token and IDs are
  present.

You can safely verify the token and `zone_id` with the opt-in integration test:

```powershell
$env:RUN_CLOUDFLARE_INTEGRATION="1"
uv run --env-file .env pytest tests/test_cloudflare_service_integration.py -m integration -q
Remove-Item Env:RUN_CLOUDFLARE_INTEGRATION -ErrorAction SilentlyContinue
```

This test is read-only. It verifies that the token can list DNS records and that
every configured `record_id` exists and refers to an `A` record. It
does not update a record or prove that the token has edit access. A successful
check reports `1 passed`; `1 skipped` means the opt-in flag, token, or `zone_id`
was not available to the test. When the bot runs, missing record IDs or update
failures are logged and retried without advancing the stored public-IP baseline.

## Discord Setup Checklist

1. Create a Discord application + bot in the Discord Developer Portal.
2. Invite the bot to your server with slash command permissions.
3. Configure your bot token (`.env`) and bot defaults (`config.toml`).
4. Start the bot.
5. Use commands:
   - `/about`
   - `/ip status`
   - `/ip subscribe`
   - `/ups status`
   - `/power restart`, `/power shutdown`, `/power cancel` (admin role required)
   - `/update check`, `/update install`, `/update changelog` (or `/update changelong`), `/update status`, `/update auto`, `/update startup`, `/update interval`, `/update repo`, `/update dismiss` (admin role required)

## Notes

- The bot ensures required roles exist on startup.
- UPS support depends on the `tripplite` package and hardware availability.
- Build artifacts under `build/` and `dist/` are packaging outputs, not source entrypoints.
- Dependencies are managed with `pyproject.toml` and pinned in `uv.lock`.

## Private multi-server networks

Optional peer networking adds `/servers list`, server targeting for `/power`
and `/ups status`, and cached per-server UPS history. All instances share one
Discord identity and connect independently; two servers work without a quorum.
Peer uptime monitoring adds persistent incidents, replicated history, subscriber
outage/recovery alerts, and `/servers status` / `/servers dashboard` graphs.
See [private network setup and limitations](docs/private-peer-network.md) and
[uptime monitoring setup](docs/peer-uptime-monitoring.md). Alerts are opt-in through
`/servers alerts`; members subscribe with `/servers subscribe`.
Use `/servers doctor` to diagnose setup and `/servers alerts-test` to verify delivery.
Planned work can use `/servers maintenance`; `/servers dashboard-pin` publishes an
automatically refreshed shared dashboard, stopped with `/servers dashboard-stop`.

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
