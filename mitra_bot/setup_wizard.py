"""Guided local setup; Discord login and app creation stay in Discord's own portal."""
from __future__ import annotations

import argparse
from mitra_bot import cli_ui as ui
import getpass
import os
import re
import tempfile
import webbrowser
from pathlib import Path
from urllib.parse import urlencode

import requests

from mitra_bot.peer_config import PeerConfig
from mitra_bot.storage.config_store import read_config_dict,write_config_dict,tomllib

PORTAL = "https://discord.com/developers/applications"
# View/send/embed/attach/history, manage roles, and Pin Messages. No Administrator.
INSTALL_PERMISSIONS = sum(1 << bit for bit in (10,11,14,15,16,28,51))


class DiscordAuthenticationError(RuntimeError):
    """Discord rejected the bot credential, before server permissions apply."""


def install_url(application_id):
    if not re.fullmatch(r"[0-9]{1,20}",str(application_id)):
        raise ValueError("Invalid application ID")
    return "https://discord.com/oauth2/authorize?"+urlencode(dict(client_id=str(application_id),
        scope="bot applications.commands",permissions=str(INSTALL_PERMISSIONS),integration_type="0"))


class DiscordSetup:
    def __init__(self,token):
        self.token = token

    @ui.busy("Contacting Discord")
    def request(self,method,path,**kwargs):
        try:
            response = requests.request(method,"https://discord.com/api/v10"+path,
                headers={"Authorization":"Bot "+self.token},timeout=20,**kwargs)
        except requests.RequestException:
            raise RuntimeError("Could not reach Discord. Check connectivity and retry.") from None
        if response.status_code == 401:
            raise DiscordAuthenticationError(
                "Discord returned HTTP 401: the bot token was rejected. Copy the token from "
                "Developer Portal > your application > Bot > Token. The OAuth2 Client Secret, "
                "Client ID and Public Key will not work here. Server permissions do not fix this error."
            )
        if response.status_code == 403:
            raise RuntimeError(
                "Discord returned HTTP 403: access is forbidden. Install the bot in the selected server; "
                "check its channel permissions. For role assignment, grant Manage Roles and move the "
                "bot's role above the configured Mitra admin role in Server Settings > Roles."
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Discord returned HTTP {response.status_code}; check the bot token, permissions and role hierarchy.")
        return response.json() if response.content else None

    def application(self):
        value = self.request("GET","/oauth2/applications/@me")
        if not isinstance(value,dict) or not re.fullmatch(r"[0-9]{1,20}",str(value.get("id",""))):
            raise RuntimeError("Discord did not return a valid bot application.")
        return value


def save_token(path,token, *, variable="DISCORD_APPLICATION_TOKEN"):
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", variable):
        raise ValueError("Invalid secret environment variable name")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{20,256}",token):
        raise ValueError("The bot token has unexpected characters. Copy the token, not a URL or authorization header.")
    path = Path(path)
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    lines = [line for line in lines if not re.match(r"^\s*(?:export\s+)?"+re.escape(variable)+r"\s*=",line)]
    lines.append(variable+"="+token)
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp = tempfile.mkstemp(prefix=".mitra-env-",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as stream:
            stream.write("\n".join(lines)+"\n")
        if os.name != "nt":
            os.chmod(temp,0o600)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def install_bundle(source,destination):
    source,destination = Path(source).resolve(),Path(destination).resolve()
    cfg = PeerConfig.model_validate(tomllib.loads((source/"peer-network.toml").read_text(encoding="utf-8")))
    if not cfg.enabled:
        raise ValueError("The peer bundle is not enabled.")
    names = ["peer-network.toml",cfg.ca_file,cfg.cert_file,cfg.key_file]
    if any(Path(name).name != name or name in {"", ".", ".."} for name in names) or len(set(names)) != 4:
        raise ValueError("A bundle must contain distinct local configuration/certificate/key files.")
    for name in names:
        path = (source/name).resolve()
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError("Bundle file is missing or outside its folder.")
        target = destination/name
        if target.exists() and target.read_bytes() != path.read_bytes():
            raise ValueError(f"Existing {name} differs; use manual setup to review and replace it.")
    destination.mkdir(parents=True,exist_ok=True)
    for name in names:
        target = destination/name
        if not target.exists():
            with target.open("xb") as stream:
                stream.write((source/name).read_bytes())
            if os.name != "nt" and name == cfg.key_file:
                target.chmod(0o600)
    return cfg


def yes(prompt,default=True):
    answer = ui.ask(prompt+(" [Y/n]: " if default else " [y/N]: ")).strip().lower()
    return default if not answer else answer in {"y","yes"}


def choose(prompt,items,label, *, default=1):
    ui.choices(str(label(item)) + (" (default)" if index == default else "") for index,item in enumerate(items,1))
    while True:
        answer = ui.ask(prompt+f" [{default}; 0 keeps existing/skips]: ").strip() or str(default)
        if answer.isdigit() and 0 <= int(answer) <= len(items):
            return items[int(answer)-1] if int(answer) else None
        ui.message("Choose one of the listed numbers.")


def read_bot_token(browse):
    browse(PORTAL)
    ui.message("Discord credential setup:")
    ui.message("  1. Open your existing Discord application, or click New Application to create one.")
    ui.message("  2. Select Bot in the left sidebar and find the Token section.")
    ui.message("  3. Copy your saved bot token, or use Reset Token if you need a new one.")
    ui.message("     Resetting invalidates the old token: update every machine using this Discord bot.")
    ui.message("Use the BOT TOKEN, not the OAuth2 Client Secret, Client ID, Public Key or a Cloudflare credential.")
    ui.message("Paste into this console, then press Enter. Hidden input shows no characters or asterisks.")
    ui.message("After Enter, I will confirm whether input was received. Ctrl+C cancels setup.")
    while True:
        token = ui.secret("Discord bot token (hidden; press Enter after pasting): ").strip()
        if token:
            ui.message(f"Received {len(token)} characters. Checking the bot token with Discord...")
            return token
        ui.message("No token was received. Try pasting again, then press Enter.")


def authenticate_discord(existing, browse):
    token = existing.strip() if existing else ""
    if not token or yes("Replace the existing bot token?", False):
        token = read_bot_token(browse)
    else:
        ui.message("Checking the saved bot token with Discord...")
    while True:
        api = DiscordSetup(token)
        try:
            return token, api, api.application()
        except DiscordAuthenticationError as exc:
            ui.message(str(exc))
            ui.message("Your saved credentials have not been changed.")
            if not yes("Try another bot token?", True):
                raise RuntimeError("Bot authentication cancelled; saved credentials were retained.") from None
            token = read_bot_token(browse)


def discord_installation(api, application, browse):
    guilds = api.request("GET", "/users/@me/guilds")
    if guilds:
        ui.message(f"This bot is already installed in {len(guilds)} Discord server(s). You can keep its current installation.")
    else:
        ui.message("This bot is not installed in a Discord server yet. Install it before selecting a notification channel.")
    if yes("Review Server Members Intent in Discord's Bot settings?", not bool(guilds)):
        ui.message("Enable Server Members Intent so Mitra can manage alert subscriptions. Save changes on the Bot page.")
        ui.message("Message Content Intent and Presence Intent are not required.")
        browse(f"{PORTAL}/{application['id']}/bot")
        ui.ask("Press Enter after saving Server Members Intent. ")
    else:
        ui.message("Keeping current intents. If subscriptions fail, check Server Members Intent in the Developer Portal.")
    if yes("Install in another Discord server or repair installation permissions?", not bool(guilds)):
        ui.message("The install page requests View Channels, Send Messages, Embed Links, Attach Files,")
        ui.message("Read Message History, Manage Roles and Pin Messages. Administrator is not required.")
        ui.message("Select Add to Server and Authorize. If unavailable, enable Guild Install in Developer Portal > Installation.")
        browse(install_url(application["id"]))
        ui.ask("Press Enter after authorizing the installation. ")
        guilds = api.request("GET", "/users/@me/guilds")
    else:
        ui.message("Keeping the current Discord installation; no authorization page opened.")
    return guilds


def guided_setup(*,env_file=".env",open_browser=True):
    opened = set()
    def browse(url):
        ui.message(url)
        if open_browser and url not in opened:
            webbrowser.open(url)
            opened.add(url)
    ui.banner("MITRA  /  SETUP", "Your servers. One bot. Let's get connected.")
    ui.message("Existing settings are retained unless you change them.")
    ui.step("Discord connection", 1, 5, purpose="Connect this machine to your bot identity. Existing bots can keep their token, intents and installation.")
    ui.message("Discord app creation happens in the Developer Portal. This wizard never asks for your Discord password.")
    ui.message("For another peer in an existing private network, reuse that network's bot token.")
    from mitra_bot.settings import EnvSettings
    existing = EnvSettings(_env_file=env_file).token
    token, api, application = authenticate_discord(existing, browse)
    save_token(env_file,token)
    ui.message(f"Validated application: {application.get('name','Mitra')} ({application['id']}). Token saved locally.")
    guilds = discord_installation(api, application, browse)
    ui.step("Server & notifications", 2, 5, purpose="Choose the Discord community and channel for alerts, then configure who can use administrative commands.")
    cfg = read_config_dict()
    saved_guild = cfg["bot"].get("guild_id")
    if not saved_guild and cfg["bot"].get("channel_id"):
        try:
            saved_guild = int(api.request("GET", f"/channels/{cfg['bot']['channel_id']}")["guild_id"])
        except (RuntimeError, KeyError, TypeError, ValueError):
            ui.message("Could not identify the saved channel's Discord server. Existing settings will be kept unless you select a server.")
    default_guild = next((i for i,g in enumerate(guilds,1) if str(g["id"]) == str(saved_guild)),
                         0 if saved_guild or cfg["bot"].get("channel_id") else 1)
    guild = choose("Discord server",guilds,lambda item:item["name"], default=default_guild) if guilds else None
    if guild:
        guild_id = str(guild["id"])
        cfg["bot"]["guild_id"] = int(guild_id)
        detail = api.request("GET",f"/guilds/{guild_id}")
        channels = [c for c in api.request("GET",f"/guilds/{guild_id}/channels") if c["type"] == 0]
        from mitra_bot.storage.storage_store import get_notification_channel_id_for_guild
        saved_channel = get_notification_channel_id_for_guild(int(guild_id))
        if not saved_channel and str(saved_guild) == guild_id:
            saved_channel = cfg["bot"].get("channel_id")
        default_channel = next((i for i,c in enumerate(channels,1) if str(c["id"]) == str(saved_channel)), 0 if saved_channel else 1)
        channel = choose("Default notification channel",channels,lambda item:"#"+item["name"], default=default_channel) if channels else None
        if channel:
            cfg["bot"]["channel_id"] = int(channel["id"])
            from mitra_bot.storage.storage_store import set_notification_channel_id_for_guild
            set_notification_channel_id_for_guild(int(guild_id),int(channel["id"]))
        role_name = cfg["bot"]["admin_role_name"]
        ui.message(f"The '{role_name}' role grants access to Mitra's admin commands, including server power controls.")
        ui.message("In Discord > Server Settings > Roles, place the bot's role above any existing role it should manage.")
        if yes(f"Create/reuse '{role_name}' and assign it to this Discord server's owner?",True):
            roles = api.request("GET",f"/guilds/{guild_id}/roles")
            matching = [role for role in roles if role["name"] == role_name]
            if len(matching) > 1:
                raise RuntimeError("Multiple administrator roles have that name. Resolve them manually before assigning access.")
            role = matching[0] if matching else api.request("POST",f"/guilds/{guild_id}/roles",json={"name":role_name,"permissions":"0"})
            api.request("PUT",f"/guilds/{guild_id}/members/{detail['owner_id']}/roles/{role['id']}")
    else:
        ui.message("No server selected. Assign the configured Mitra admin role manually before using admin commands.")
    ui.step("UPS monitoring", 3, 5, purpose="Collect battery and power history from a supported UPS connected to this machine by USB. Skip if this machine has no UPS.")
    cfg["ups"]["enabled"] = yes("Monitor a supported USB UPS on this machine?",cfg["ups"]["enabled"])
    write_config_dict(cfg)
    from mitra_bot.services.ups.ups_database import UPSLogStore
    if cfg["ups"]["enabled"]:
        store = UPSLogStore(log_file=cfg["ups"].get("log_file","ups_stats.db"),database_file=cfg["ups"].get("database_file"))
        ui.message(f"UPS database ready: {store.log_path}")
    ui.step("Private network", 4, 5, purpose="Connect machines to the same private bot network. Create bundles once, then install each machine's own bundle.")
    if not Path("peer-network.toml").exists():
        ui.choices(["Single server — run independently", "Create a network — provision bundles for every machine", "Install an existing peer bundle — join your network"])
        mode = ui.ask("Choose [1]: ").strip() or "1"
        if mode == "2":
            from mitra_bot.init_peers import provision
            nodes = {}
            ui.message("Enter each server as a unique name and a hostname/IP reachable from the other machines.")
            while True:
                name = ui.ask("Server name (blank finishes): ").strip()
                if not name:
                    if len(nodes) >= 2:
                        break
                    ui.message("A private network needs at least two nodes.")
                    continue
                if name in nodes:
                    ui.message("That server name is already used.")
                    continue
                host = ui.ask(f"Reachable hostname/IP for {name}: ").strip()
                nodes[name] = host
            output = Path(ui.ask("Bundle output folder [peer-bundles]: ").strip() or "peer-bundles")
            allow_power = yes("Allow these peers to forward authorized power commands?",False)
            ui.run("Creating private network certificates", provision, output, nodes, allow_power=allow_power)
            local = choose("This machine",list(nodes),lambda item:item)
            if local:
                install_bundle(output/local,Path.cwd())
            ui.message(f"Give each other server ONLY its own folder inside {output}; run this wizard there and choose Install a bundle.")
            ui.message("Keep OFFLINE-CA.key and the provisioning folder private. Permit TCP 9843 between peer machines.")
        elif mode == "3":
            bundle = ui.ask("Path to this machine's peer bundle folder: ").strip().strip('"')
            peer = install_bundle(bundle,Path.cwd())
            ui.message(f"Installed peer {peer.node_id}; reuse the same Discord bot token on all peers.")
        elif mode != "1":
            raise ValueError("Unknown private network setup choice.")
    else:
        ui.message("Existing peer-network.toml found. Keeping this machine's network identity and peer settings.")
    ui.step("Cloudflare DNS", 5, 5, purpose="Keep selected domain names pointed at this machine when its public IP changes. Optional; each machine can use its own account.")
    cloudflare_result = "Existing settings retained"
    if yes("Set up Cloudflare DNS updates for this server?", False):
        from mitra_bot.cloudflare_setup import run_cloudflare_setup
        outcome = run_cloudflare_setup(env_file=env_file, open_browser=open_browser)
        cloudflare_result = {True: "Setup finished", False: "Unfinished - resume with mitra-cloudflare-setup",
                             None: "Skipped - existing settings retained"}[outcome]
    from mitra_bot.setup_health import show_health
    show_health(env_file=env_file)
    ui.summary([("Discord", application.get("name", "Connected")), ("UPS monitoring", "Enabled" if cfg["ups"]["enabled"] else "Disabled"), ("Cloudflare", cloudflare_result)])
    ui.message("Setup saved. Start with: uv run --env-file " + str(env_file) + " python -m mitra_bot.main")
    ui.message("Without uv: .venv\\Scripts\\python.exe -m mitra_bot.main (uses .env by default).")
    ui.message("In Discord: /servers doctor (when peers are enabled), then /servers alerts to choose outage subscriptions.")
    ui.message("Windows automatic startup is available with scripts/Install-MitraBotStartup.ps1. Manual configuration remains supported.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-browser",action="store_true",help="Print Discord URLs instead of opening them")
    parser.add_argument("--env-file",default=".env",help="Secret file to create/update (default .env)")
    parser.add_argument("--plain", action="store_true", help="Disable colors and animations")
    args = parser.parse_args()
    ui.configure(plain=args.plain)
    try:
        guided_setup(env_file=args.env_file,open_browser=not args.no_browser)
    except (RuntimeError,ValueError,OSError,KeyboardInterrupt) as exc:
        # Do not emit request objects/headers or token-bearing tracebacks.
        parser.exit(1,f"Setup stopped: {exc}\nExisting saved steps can be reused; rerun the wizard or continue manually.\n")


if __name__ == "__main__":
    main()
