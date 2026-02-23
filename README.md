# Mitra Discord Bot

<img src="mitra.png" width="320" height="320" alt="Mitra logo" />

Mitra is a modular Discord bot package for home/server operations.  
It monitors public IP changes, supports UPS status/graphing, and provides admin-only power controls.

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

```bash
$env:RUN_CLOUDFLARE_INTEGRATION="1"; uv run pytest -m integration
```

Run the bot:

```bash
uv run mitra-bot
```

Alternative entrypoint:

```bash
uv run python -m mitra_bot.main
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

## Configuration

Mitra reads config from `config.toml` and environment variables, and stores mutable runtime data in `state.db`.

- `DISCORD_APPLICATION_TOKEN` supplies the bot token.
- `MITRA_ENABLE_MEMBERS_INTENT` defaults to `true` (set `false` to disable).
- `MITRA_CONFIG_PATH` overrides the config file path (default: `config.toml`).
- `MITRA_STATE_PATH` overrides the state DB path (default: `state.db`).
- Cloudflare secret is env-only: `CLOUDFLARE_API_TOKEN`.

Using env vars with `uv`:

```bash
# uv reads .env by default
uv run mitra-bot
```

```bash
# explicit env file (useful for production)
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

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
