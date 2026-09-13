"""Guided local setup; Discord login and app creation stay in Discord's own portal."""
from __future__ import annotations

import argparse
from mitra_bot import cli_ui as ui
import getpass
import ipaddress
import os
import re
import subprocess
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
    while True:
        answer = ui.ask(prompt+(" [Y/n]: " if default else " [y/N]: ")).strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes", "n", "no"}:
            return answer in {"y", "yes"}
        ui.message("Please type yes or no, or press Enter to use the default.")


def menu(prompt, options, default):
    while True:
        answer = ui.ask(f"{prompt} [{default}]: ").strip() or default
        if answer in options:
            return answer
        ui.message("Choose " + ", ".join(options) + ". Please try again.")


def input_path(value):
    return Path(value.strip().strip('\"').strip("'")).expanduser()


def prompt_bundle(destination):
    ui.message("Choose the folder made for THIS machine, containing peer-network.toml, ca.crt, node.crt and node.key.")
    ui.message("Example: peer-bundles/Mitra. A full path such as C:\\Mitra\\peer-bundles\\Mitra also works, with or without quotes.")
    ui.message(f"Short paths start from: {Path.cwd()}")
    ui.message("If you choose the parent peer-bundles folder, you can select this machine's folder below. Do not copy OFFLINE-CA.key here.")
    while True:
        answer = ui.ask("This machine's bundle folder (blank skips for now): ").strip()
        if not answer:
            return None
        try:
            source = input_path(answer)
            if source.is_file() and source.name == "peer-network.toml":
                source = source.parent
            if source.is_dir() and not (source / "peer-network.toml").is_file():
                children = sorted(p.parent for p in source.glob("*/peer-network.toml"))
                if children:
                    ui.message("This folder contains bundles for several machines. Choose this machine's name.")
                    source = choose("This machine", children, lambda p: p.name)
                    if source is None:
                        continue
            if not (source / "peer-network.toml").is_file():
                raise ValueError("No peer-network.toml found there. Choose the machine's bundle folder, not an empty folder or ZIP file.")
            peer = install_bundle(source, destination)
            ui.message(f"Installed bundle for {peer.node_id}. All machines in this network must use the same Discord bot token.")
            return peer
        except (ValueError, OSError) as exc:
            ui.message(f"Could not install that bundle: {exc}")
            ui.message("Your earlier setup steps are saved. Enter a corrected path, or leave it blank to finish this step later.")


def prompt_nodes():
    nodes = {}
    ui.message("Add every machine, including this one. Enter the primary machine first; it will keep shared bot settings.")
    ui.message("Names appear in Discord. Use up to 48 letters, numbers, hyphens or underscores; names are case-sensitive.")
    ui.message("For each address, enter only a reachable hostname or IP, such as mitra.example.com or 203.0.113.10.")
    ui.message("Do not include https://, a server name prefix, a slash or a port. A DNS hostname is best when your public IP changes.")
    while len(nodes) < 65:
        name = ui.ask("Machine name (blank finishes the list): ").strip()
        if not name:
            if len(nodes) >= 2:
                return nodes
            ui.message("Add at least two machines to create a network.")
            continue
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}", name) or name in nodes:
            ui.message("Use a unique name with 1–48 letters, numbers, hyphens or underscores, starting with a letter or number.")
            continue
        while True:
            host = ui.ask(f"Hostname or IP for {name} (blank re-enters the name): ").strip()
            if not host:
                break
            try:
                ipaddress.ip_address(host)
            except ValueError:
                if len(host) > 253 or all(c in "0123456789." for c in host) or not all(
                    re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                    for label in host.rstrip(".").split(".")
                ):
                    ui.message("That is not a hostname or IP. Example: mitra.example.com. Please enter the address again.")
                    continue
            nodes[name] = host
            break
    return nodes


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
        if token and not re.fullmatch(r"[A-Za-z0-9_.-]{20,256}", token):
            ui.message("That does not look like a bot token. Copy only the token from the Bot page, then paste again.")
            continue
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
        except RuntimeError as exc:
            ui.message(str(exc))
            choice = menu("1. Check the connection and retry  2. Enter a different bot token  3. Stop setup", ("1", "2", "3"), "1")
            if choice == "2":
                token = read_bot_token(browse)
            elif choice == "3":
                raise RuntimeError("Setup paused; saved credentials were retained.") from None


def discord_installation(api, application, browse):
    guilds = api.request("GET", "/users/@me/guilds")
    if guilds:
        ui.message(f"This bot is already installed in {len(guilds)} Discord server(s). You can keep its current installation.")
    else:
        ui.message("This bot is not installed in a Discord server yet. Install it before selecting a notification channel.")
    ui.message("Alert subscriptions use Discord roles. Discord calls the required member-access switch Server Members Intent.")
    ui.message("If you already enabled it for this bot, answer no. Adding another machine does not require enabling it again.")
    if yes("Open Discord settings to let Mitra manage alert subscribers?", not bool(guilds)):
        ui.message("Enable Server Members Intent so Mitra can manage alert subscriptions. Save changes on the Bot page.")
        ui.message("Message Content Intent and Presence Intent are not required.")
        browse(f"{PORTAL}/{application['id']}/bot")
        ui.ask("Press Enter after saving Server Members Intent. ")
    else:
        ui.message("Keeping current intents. If subscriptions fail, check Server Members Intent in the Developer Portal.")
    ui.message("This connects the bot to your Discord community, not to another computer. If the bot already works there, answer no.")
    if yes("Open Discord to add this bot to a server or fix its permissions?", not bool(guilds)):
        ui.message("The install page requests View Channels, Send Messages, Embed Links, Attach Files,")
        ui.message("Read Message History, Manage Roles and Pin Messages. Administrator is not required.")
        ui.message("Select Add to Server and Authorize. If unavailable, enable Guild Install in Developer Portal > Installation.")
        browse(install_url(application["id"]))
        ui.ask("Press Enter after authorizing the installation. ")
        guilds = api.request("GET", "/users/@me/guilds")
    else:
        ui.message("Keeping the current Discord installation; no authorization page opened.")
    return guilds


def configure_discord_destination(api, guilds, cfg):
    ui.message("Choose your Discord community from the list, then the text channel where Mitra should post notifications.")
    ui.message("This is a Discord server, not the computer running the bot. Press Enter to keep the suggested choice, or 0 to skip.")
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
        write_config_dict(cfg)
        role_name = cfg["bot"]["admin_role_name"]
        ui.message(f"The '{role_name}' role grants access to Mitra's admin commands, including server power controls.")
        ui.message("In Discord > Server Settings > Roles, place the bot's role above any existing role it should manage.")
        ui.message(f"Mitra will create or reuse a Discord role named '{role_name}' and give it to the person who owns this Discord server.")
        if yes("Give the Discord server owner permission to use Mitra admin commands?",True):
            roles = api.request("GET",f"/guilds/{guild_id}/roles")
            matching = [role for role in roles if role["name"] == role_name]
            if len(matching) > 1:
                raise RuntimeError("Multiple administrator roles have that name. Resolve them manually before assigning access.")
            role = matching[0] if matching else api.request("POST",f"/guilds/{guild_id}/roles",json={"name":role_name,"permissions":"0"})
            api.request("PUT",f"/guilds/{guild_id}/members/{detail['owner_id']}/roles/{role['id']}")
    else:
        ui.message("No server selected. Assign the configured Mitra admin role manually before using admin commands.")


def guided_setup(*,env_file=".env",open_browser=True):
    opened = set()
    def browse(url):
        ui.message(url)
        if open_browser and url not in opened:
            webbrowser.open(url)
            opened.add(url)
    ui.banner("MITRA  /  SETUP", "Your servers. One bot. Let's get connected.")
    ui.message("Existing settings are retained unless you change them.")
    ui.step("Discord connection", 1, 5, purpose="Sign this machine in as your Discord bot. If you already set up this bot, keep the saved details.")
    ui.message("Discord app creation happens in the Developer Portal. This wizard never asks for your Discord password.")
    ui.message("For another peer in an existing private network, reuse that network's bot token.")
    from mitra_bot.settings import EnvSettings
    existing = EnvSettings(_env_file=env_file).token
    token, api, application = authenticate_discord(existing, browse)
    save_token(env_file,token)
    ui.message(f"Validated application: {application.get('name','Mitra')} ({application['id']}). Token saved locally.")
    while True:
        try:
            guilds = discord_installation(api, application, browse)
            break
        except (RuntimeError, ValueError, OSError) as exc:
            ui.message(f"Could not finish the Discord connection step: {exc}")
            choice = menu("1. Review Discord setup again  2. Set server and channel up later", ("1", "2"), "1")
            if choice == "2":
                guilds = []
                break
    ui.step("Server & notifications", 2, 5, purpose="Choose the Discord community and channel for alerts, then configure who can use administrative commands.")
    cfg = read_config_dict()
    while True:
        try:
            configure_discord_destination(api, guilds, cfg)
            write_config_dict(cfg)
            break
        except (RuntimeError, ValueError, OSError) as exc:
            ui.message(f"This Discord step could not finish: {exc}")
            ui.message("Your bot token is saved. Any role already created remains and will be reused.")
            choice = menu("1. Choose server/channel again after fixing permissions  2. Set this up later", ("1", "2"), "1")
            if choice == "2":
                break
    ui.step("UPS monitoring", 3, 5, purpose="Collect battery and power history from a supported UPS connected to this machine by USB. Skip if this machine has no UPS.")
    cfg["ups"]["enabled"] = yes("Monitor a supported USB UPS on this machine?",cfg["ups"]["enabled"])
    write_config_dict(cfg)
    from mitra_bot.services.ups.ups_database import UPSLogStore
    if cfg["ups"]["enabled"]:
        store = UPSLogStore(log_file=cfg["ups"].get("log_file","ups_stats.db"),database_file=cfg["ups"].get("database_file"))
        ui.message(f"UPS database ready: {store.log_path}")
    ui.step("Private network", 4, 5, purpose="Let multiple computers work together as one bot. Create the network once; on other computers, install the folder made for that computer.")
    if not Path("peer-network.toml").exists():
        ui.choices(["One machine - run the bot only on this computer", "Create a network - make setup folders for all your computers", "Join a network - use the setup folder made on your first computer"])
        mode = menu("Choose how this machine will run", ("1", "2", "3"), "1")
        if mode == "2":
            from mitra_bot.init_peers import provision
            nodes = prompt_nodes()
            ui.message("Mitra will create one private setup folder per machine inside a NEW output folder.")
            ui.message("Example: peer-bundles creates peer-bundles/Mitra and peer-bundles/Anubis. Full paths also work.")
            allow_power = yes("Allow an authorized bot administrator to restart or shut down another machine through this network?", False)
            while True:
                output = input_path(ui.ask("New bundle output folder [peer-bundles]: ").strip() or "peer-bundles")
                try:
                    ui.run("Creating private network certificates", provision, output, nodes, allow_power=allow_power)
                    break
                except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as exc:
                    ui.message(f"Could not create the bundles: {exc}")
                    ui.message("Your machine names and addresses are retained. Existing folders are never overwritten; enter a new folder name.")
                    if not yes("Choose another output folder and try again?", True):
                        return finish_setup(cfg, application, env_file, open_browser)
            local = choose("This machine",list(nodes),lambda item:item)
            if local:
                try:
                    install_bundle(output/local,Path.cwd())
                except (ValueError, OSError) as exc:
                    ui.message(f"Bundles were created, but this machine's bundle could not be installed: {exc}")
                    prompt_bundle(Path.cwd())
            ui.message(f"Give each other server ONLY its own folder inside {output}; run this wizard there and choose Install a bundle.")
            ui.message("Keep OFFLINE-CA.key and the provisioning folder private. Permit TCP 9843 between peer machines.")
        elif mode == "3":
            prompt_bundle(Path.cwd())
    else:
        ui.message("Existing peer-network.toml found. Keeping this machine's network identity and peer settings.")
    finish_setup(cfg, application, env_file, open_browser)


def finish_setup(cfg, application, env_file, open_browser):
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
