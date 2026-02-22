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
- Persistent settings via `cache.json`.

## Project Layout

```text
mitra_bot/
  main.py                      # app entrypoint
  settings.py                  # settings loader (cache.json + env)
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
    cache_store.py
run.py                         # convenience launcher
cache.json                     # runtime config/state
requirements.txt
```

## Requirements

- Python 3.10+ (3.11+ recommended)
- Windows for `/power` actions (`shutdown` command integration is Windows-specific)
- Dependencies in `requirements.txt`

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

Mitra reads config from `cache.json` and environment variables.

- `MITRA_TOKEN` (or `DISCORD_TOKEN`) can supply your bot token.
- If token is missing and interactive mode is enabled, Mitra prompts for it and stores it in `cache.json`.

Common `cache.json` keys:

- `token`: Discord bot token (optional if using env var)
- `channel` or `channel_id`: notification channel ID
- `ip_poll_seconds`: IP monitor interval
- `admin_role_name`: admin role for restricted commands
- `ip_subscriber_role_name`: role used for IP notifications
- `ups`: UPS settings block (`enabled`, `poll_seconds`, thresholds, logging, timezone, etc.)

## Running The Bot

From repository root, use either:

```bash
python run.py
```

or:

```bash
python -m mitra_bot.main
```

On Windows, `py` works too:

```bash
py -m mitra_bot.main
```

## Discord Setup Checklist

1. Create a Discord application + bot in the Discord Developer Portal.
2. Invite the bot to your server with slash command permissions.
3. Configure your bot token and channel ID (`cache.json` or env vars).
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

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
