# Guided and manual setup

To invite the hosted bot, use the [user guide](../README.md#get-started).
This page is for running your own installation with your own Discord application.
See [self-hosting and operations](self-hosting.md) for configuration and maintenance.

Install Python 3.10+ or [uv](https://docs.astral.sh/uv/getting-started/installation/).
Extract the `mitra-discord-bot-VERSION.zip` release asset into a permanent folder.
On Windows, double-click `Setup-MitraBot.cmd` or run
`powershell -File scripts/Setup-MitraBot.ps1`. With uv on any platform:

```sh
uv sync --frozen --no-dev
uv run mitra-setup
```

Interactive terminals show colored prompts, numbered setup sections, and animated
dots while network requests or browser authorization are pending. The final summary
shows the saved setup status. Token input stays hidden.

Use `uv run mitra-setup --plain` to disable styling and animation (or `-Plain` with
the PowerShell launcher). `mitra-cloudflare-setup` also accepts `--plain`.
Redirected output, `NO_COLOR`, and `TERM=dumb` automatically use plain text.

The wizard preserves settings you do not change. It opens the Developer Portal,
asks for a bot token using hidden input, validates it before replacing `.env`,
opens the Server Members Intent settings and builds the bot installation URL.
It requests view/send messages, embed links, attach files, read message history,
manage roles and pin messages. It does not request Administrator.
For to-do lists, also grant Manage Channels, Create Public Threads, Send Messages
in Threads, and Manage Threads. The hosted public bot uses a smaller set without
infrastructure permissions; see the [invite checklist](discord-install.md).

Discord's [supported application setup](https://docs.discord.com/developers/quick-start/getting-started)
requires creating the application in the Developer Portal. The wizard cannot
create it through a normal Discord account OAuth login. Log in only on Discord's
own pages; the wizard never requests your account password or account token.
Enable Server Members Intent manually when prompted, then approve the installation.

Use the credential from **your application → Bot → Token**. The OAuth2 **Client
Secret**, Client ID and Public Key are different values and will not authenticate
the bot. If you already saved the bot token, reuse it. Reset Token invalidates the
old credential, so update all peers if you reset it.

Hidden token input shows no characters or asterisks while pasting. Press Enter;
the wizard confirms the number of characters received, then validates the token.
Empty input prompts again. HTTP 401 means the credential was rejected, not that
server permissions are missing; the wizard lets you retry and retains the saved
credential until a replacement succeeds. HTTP 403 indicates forbidden access and
provides installation/channel/role-hierarchy guidance.

Choose your server and notification channel. The optional admin-role step creates
or reuses the configured role and assigns it to the server owner. To use an existing
role, ensure the bot's role is above it in Discord's role hierarchy. Other admins
can be assigned that role manually. API errors stop setup with instructions; rerun
to reuse completed steps. Protect `.env` and private keys with local filesystem access controls.

For private peers, choose Create a network on the first machine. Enter at least two
names and reachable addresses. Certificate creation requires OpenSSL (Git for Windows
includes a supported copy). Select the local machine, and securely distribute
only each machine's own generated bundle folder. On additional machines, reuse
the **same bot token**, choose Install a bundle and select that machine's folder.
Keep the provisioning directory and offline CA key private. Allow TCP 9843 between
peers; the wizard does not change firewall rules. See [peer setup](private-peer-network.md)
for advanced network configuration and adding nodes later.

Start with `uv run --env-file .env python -m mitra_bot.main`. Without uv on Windows, use
`.venv\Scripts\python.exe -m mitra_bot.main`. Automatic Windows startup remains
available through `scripts/Install-MitraBotStartup.ps1`.

For headless setup, use `uv run mitra-setup --no-browser` (URLs are printed),
or the PowerShell launcher's `-NoBrowser` option. `--env-file` selects another secret file;
use the same file when starting the bot. `mitra-init-config --guided` also launches the wizard.

Manual setup remains available: run `uv run mitra-init-config`, fill `.env` from its
example, edit `config.toml`, create/install the Discord app in the Portal and assign
the configured admin role. Use `uv run mitra-peer-init --help` for manual peer provisioning.
After startup, use `/servers doctor` and `/servers alerts` to verify peer connectivity
and configure subscriptions. Creating the network does not subscribe users automatically.

The wizard also offers Cloudflare setup after private-peer setup. You can run it
independently with `uv run mitra-cloudflare-setup`. See [Cloudflare setup](cloudflare.md)
for browser authorization, multiple accounts/domains and per-server DNS assignments.

## Final health checks

Setup finishes with health checks. Rerun them with
`uv run mitra-doctor --env-file .env` (add `--plain` for plain output).
Checks authenticate the Discord token, read the configured channel, check UPS
database integrity, verify peer TLS certificates, and read this server's
Cloudflare A records. They send no Discord messages and make no DNS changes;
expired OAuth credentials may be refreshed locally.
CHECK results include a next step and do not discard saved setup. The standalone
command exits with code 1 when attention is needed.

These checks do not prove Discord send/role permissions, USB operation, or full
peer application compatibility. A peer not started yet will show CHECK; start
both bots and run `/servers doctor` for application-level diagnostics.
Cancelled Cloudflare setup is reported as skipped, separately from failure.
