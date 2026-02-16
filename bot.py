import os
import platform
import asyncio
import subprocess
import signal
import sys
import json
import logging
import hashlib
from typing import Optional, Tuple, List, Set, Dict, Any

import requests
import aiohttp

import discord
from discord.ext import commands, tasks

# Pycord supports slash commands via @bot.slash_command
bot = commands.Bot(command_prefix='$', intents=discord.Intents.default())


class LogColors:
    GRAY = "\x1b[90m"  # Bright black (gray)
    BRIGHT_BLUE = "\x1b[94m"  # Brighter blue
    YELLOW = "\x1b[33;1m"
    RED = "\x1b[31;1m"
    MAGENTA = "\x1b[35m"  # Magenta
    RESET = "\x1b[0m"
    CYAN = "\x1b[36;1m"  # Turquoise


class CustomFormatter(logging.Formatter):
    FORMAT = "[" + LogColors.GRAY + "%(asctime)s" + LogColors.RESET + "] [%(levelname)-8s" + LogColors.RESET + "] %(name)s" + LogColors.RESET + ": %(message)s"

    COLOR_FORMAT = {
        logging.DEBUG: FORMAT.replace("%(levelname)-8s", LogColors.CYAN + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.INFO: FORMAT.replace("%(levelname)-8s", LogColors.BRIGHT_BLUE + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.WARNING: FORMAT.replace("%(levelname)-8s", LogColors.YELLOW + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.ERROR: FORMAT.replace("%(levelname)-8s", LogColors.RED + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.CRITICAL: FORMAT.replace("%(levelname)-8s", LogColors.RED + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s")
    }

    def format(self, record):
        log_fmt = self.COLOR_FORMAT.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def signal_handler(signum, frame):
    logging.info("Application received a signal to close.")
    sys.exit(0)


def get_valid_token() -> str:
    """Get a valid bot token from the user.

    Returns:
        str: The bot token.
    """
    while True:
        token = input("Please enter the bot token (72 characters): ").strip()
        if len(token) == 72:
            return token
        logging.warning("Invalid input. The bot token must be exactly 72 characters.")


def get_valid_channel_id() -> int:
    """Get a valid channel ID from the user.

    Returns:
        int: The channel ID.
    """
    while True:
        channel_id = input("Please enter the channel ID (18 digits): ").strip()
        if channel_id.isdigit() and len(channel_id) == 18:
            return int(channel_id)
        logging.warning("Invalid input. The channel ID must be exactly 18 digits.")


def _read_cache_json() -> Dict[str, Any]:
    try:
        with open("cache.json", "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cache_json(data: Dict[str, Any]) -> None:
    with open("cache.json", "w") as f:
        json.dump(data, f)


def load_connection_data() -> Tuple[str, int, str, str, str, List[str]]:
    """Load the connection data from the cache file.

    Returns:
        tuple: (token, channel_id, cloudflare_api_key, cloudflare_email, zone_id, record_ids)
    """
    try:
        data = {}
        logging.info("Loading cache file...")
        try:
            with open("cache.json", "r") as file:
                data = json.load(file)
        except (FileNotFoundError, json.JSONDecodeError):
            logging.warning("Cache file not found or invalid. Creating cache file...")

        token = data.get("token", "")
        channel = data.get("channel", 0)
        api_key = data.get("api_key", "")
        email = data.get("email", "")
        zone_id = data.get("zone_id", "")
        record_ids = data.get("record_ids", [])

        if not token or len(token) != 72:
            logging.warning("Invalid token in cache file, please enter a valid token.")
            token = get_valid_token()
            data["token"] = token

        if not channel or not (isinstance(channel, int) and len(str(channel)) == 18):
            logging.warning("Invalid channel ID in cache file, please enter a valid channel ID.")
            channel = get_valid_channel_id()
            data["channel"] = channel

        if not api_key:
            logging.warning("Invalid API key in cache file, please enter a valid API key.")
            api_key = input("Please enter your Cloudflare API key: ").strip()
            data["api_key"] = api_key

        if not email:
            logging.warning("Invalid email in cache file, please enter a valid email.")
            email = input("Please enter your Cloudflare email: ").strip()
            data["email"] = email

        if not zone_id:
            logging.warning("Invalid zone ID in cache file, please enter a valid zone ID.")
            zones = get_zones(api_key, email)
            if zones:
                for i, zone in enumerate(zones):
                    print(f"{i}: {zone['name']}")
                zone_index = int(input("Select a zone by entering the corresponding number: "))
                selected_zone = zones[zone_index]
                zone_id = selected_zone["id"]
                data["zone_id"] = zone_id

        if len(record_ids) == 0:
            logging.warning("Invalid record IDs in cache file, please enter valid record IDs.")
            records = get_dns_records(api_key, email, zone_id)
            record_ids_temp: List[str] = []
            if records:
                while True:
                    for i, record in enumerate(records):
                        print(f"{i}: {record['type']} {record['name']} -> {record['content']}")
                    record_index = int(input("Select a record to update by entering the corresponding number (or -1 to finish): "))
                    if record_index == -1:
                        break
                    record_ids_temp.append(records[record_index]["id"])
                record_ids = record_ids_temp
                data["record_ids"] = record_ids

        existing = _read_cache_json()
        existing.update(data)
        _write_cache_json(existing)

        logging.info("Cache file loaded")
        return token, channel, api_key, email, zone_id, record_ids
    except ValueError:
        logging.error("Failed to load cache file.")
        raise


async def get_ip() -> str:
    """Get the external IP address of the machine running this bot."""
    async with aiohttp.ClientSession() as session:
        async with session.get("https://api.ipify.org?format=json") as resp:
            resp.raise_for_status()
            payload = await resp.json()
            return payload["ip"]


async def load_ip() -> Optional[str]:
    """Load the last known IP address from the cache.json file."""
    try:
        with open("cache.json", "r") as file:
            data = json.load(file)
            return data.get("ip")
    except FileNotFoundError:
        return None


async def save_ip(ip: str) -> None:
    """Save the last known IP address to the cache.json file."""
    data = _read_cache_json()
    data["ip"] = ip
    _write_cache_json(data)
    logging.info(f"IP address saved to cache file: {ip}")


async def save_subscribers(subscribers: Set[int]) -> None:
    """Save the list of subscribers to the cache.json file."""
    data = _read_cache_json()
    data["subscribers"] = list(subscribers)
    _write_cache_json(data)
    logging.info(f"Subscribers saved to cache file: {', '.join([str(s) for s in subscribers])}")


def load_subscribers() -> Set[int]:
    """Load the list of subscribers from the cache.json file."""
    try:
        logging.info("Loading subscribers from cache file...")
        with open("cache.json", "r") as file:
            data = json.load(file)
            if "subscribers" not in data:
                logging.warning("No subscribers found in cache file.")
                return set()
            return set(data.get("subscribers", []))
    except FileNotFoundError:
        logging.error("Cache file not found.")
        return set()


def get_zones(api_key: str, email: str):
    url = "https://api.cloudflare.com/client/v4/zones"
    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": api_key,
        "Content-Type": "application/json",
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()["result"]
    print("Failed to retrieve zones.")
    return None


def get_dns_records(api_key: str, email: str, zone_id: str):
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": api_key,
        "Content-Type": "application/json",
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()["result"]
    print("Failed to retrieve DNS records.")
    return None


async def update_dns_record(api_key: str, email: str, zone_id: str, record_id: str, ip: str):
    headers = {
        "X-Auth-Email": email,
        "X-Auth-Key": api_key,
        "Content-Type": "application/json",
    }
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        logging.error(f"Failed to retrieve DNS record {record_id}.")
        return
    record = response.json()["result"]
    record["content"] = ip
    response = requests.put(url, headers=headers, json=record)
    if response.status_code == 200:
        logging.info(f"DNS record {record['name']} updated successfully!")
    else:
        logging.error(f"Failed to update DNS record {record['name']}.")


def _commands_fingerprint() -> str:
    """
    Fingerprint local slash commands including options, required flags, choices, defaults, etc.
    This will detect meaningful changes and trigger a resync.
    """
    payloads = []
    for cmd in bot.application_commands:
        try:
            d = cmd.to_dict()
        except Exception:
            d = {
                "name": getattr(cmd, "name", ""),
                "description": getattr(cmd, "description", ""),
                "type": getattr(cmd, "type", None),
            }
        payloads.append(d)

    payloads.sort(key=lambda x: (x.get("type", 1), x.get("name", "")))
    raw = json.dumps(payloads, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_admins() -> Set[int]:
    data = _read_cache_json()
    admins_list = data.get("admins", [])
    out: Set[int] = set()
    for x in admins_list:
        try:
            out.add(int(x))
        except Exception:
            pass
    return out


def is_admin_user(user_id: int) -> bool:
    return user_id in admins


def build_power_command(action: str, delay: int, force: bool) -> List[str]:
    """
    action: restart | shutdown | cancel
    delay: seconds (Windows supports seconds; Linux/macOS use minutes)
    force: Windows only (ignored elsewhere)
    """
    system = platform.system().lower()
    action = (action or "").lower().strip()
    delay = max(0, int(delay))

    if system == "windows":
        if action == "cancel":
            return ["shutdown", "/a"]

        if action == "restart":
            args = ["shutdown", "/r"]
        elif action == "shutdown":
            args = ["shutdown", "/s"]
        else:
            raise RuntimeError(f"Unsupported power action: {action}")

        if force:
            args.append("/f")

        args.extend(["/t", str(delay)])
        return args

    if system in ("linux", "darwin"):
        if action == "cancel":
            return ["shutdown", "-c"]

        if action == "restart":
            flag = "-r"
        elif action == "shutdown":
            flag = "-h"
        else:
            raise RuntimeError(f"Unsupported power action: {action}")

        if delay <= 0:
            return ["shutdown", flag, "now"]

        minutes = max(1, (delay + 59) // 60)
        return ["shutdown", flag, f"+{minutes}"]

    raise RuntimeError(f"Unsupported OS: {platform.system()}")


async def execute_power_action(action: str, delay: int, force: bool) -> None:
    args = build_power_command(action, delay, force)
    logging.warning(f"Power action executing. OS={platform.system()} action={action} args={args}")
    subprocess.run(args, check=True)


@bot.event
async def on_ready() -> None:
    logging.info(f"Logged in as {bot.user.name} - {bot.user.id}")

    if not check_ip.is_running():
        check_ip.start()

    logging.info(f"Local slash commands loaded: {len(bot.application_commands)}")
    for c in bot.application_commands:
        logging.info(f" - /{c.name}")

    try:
        cache = _read_cache_json()
        force_sync = bool(cache.get("force_sync", False))

        current_fp = _commands_fingerprint()
        last_fp = cache.get("commands_fingerprint")

        if force_sync or last_fp != current_fp:
            await bot.sync_commands()
            cache["commands_fingerprint"] = current_fp
            cache["force_sync"] = False
            _write_cache_json(cache)
            logging.info("Slash commands synced.")
        else:
            logging.info("Slash commands unchanged; skipping sync.")
    except Exception as e:
        logging.error(f"Failed to sync commands: {e}")


@tasks.loop(minutes=60)
async def check_ip() -> None:
    """Check if the IP address has changed, and notify the channel if it has."""
    logging.info("Checking IP address...")
    try:
        current_ip = await get_ip()
        stored_ip = await load_ip()

        if current_ip != stored_ip:
            logging.info(f"IP address changed from {stored_ip} to {current_ip}")

            for subscriber in list(subscribers):
                try:
                    user = await bot.fetch_user(subscriber)
                    if user:
                        await user.send(f"Mitra's IP address has changed to:\n```{current_ip}```")
                        logging.info(f"IP change alert sent to user {subscriber}")
                    else:
                        logging.warning(f"Failed to send IP change alert to user {subscriber}: user not found")
                except discord.NotFound:
                    logging.warning(f"Subscriber {subscriber} not found. Removing from subscriber list.")
                    subscribers.discard(subscriber)
                    await save_subscribers(subscribers)
                except Exception as ex:
                    logging.warning(f"Failed DM to subscriber {subscriber}: {ex}")

            channel = bot.get_channel(CHANNEL)
            if channel:
                await channel.send(f"Mitra's IP address has changed to:\n```{current_ip}```")
                logging.info(f"IP change alert sent to channel {CHANNEL}")
            else:
                logging.warning(f"Channel {CHANNEL} not found (bot may not share a guild/cache yet).")

            for record in RECORD_IDS:
                await update_dns_record(API_KEY, EMAIL, ZONE_ID, record, current_ip)

            await save_ip(current_ip)

    except Exception as e:
        logging.error(f"Failed to check IP address: {e}")
    finally:
        logging.info("IP address check complete")
        logging.info("Waiting 60 minutes before checking again...")


@bot.slash_command(name="ip", description="Get Mitra's external IP address.")
async def ip(ctx: discord.ApplicationContext) -> None:
    logging.info(f"IP command called by {ctx.user.name}#{ctx.user.discriminator}")
    try:
        current_ip = await get_ip()
        await ctx.respond(f"IP address:\n```{current_ip}```", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Failed to get IP address:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to get IP address: {e}")


@bot.slash_command(name="ping", description="Get the bot's latency.")
async def ping(ctx: discord.ApplicationContext) -> None:
    logging.info(f"Ping command called by {ctx.user.name}#{ctx.user.discriminator}")
    try:
        await ctx.respond(f"Pong! {round(bot.latency * 1000)}ms", ephemeral=True)
    except Exception as e:
        await ctx.respond(f"Failed to get latency:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to get latency: {e}")


@bot.slash_command(name="subscribe", description="Subscribe to get IP change alerts sent my direct message.")
async def subscribe(ctx: discord.ApplicationContext) -> None:
    logging.info(f"Subscribe command called by {ctx.user.id}, {ctx.user.name}")
    try:
        if ctx.user.id in subscribers:
            await ctx.respond("You are already subscribed to IP change alerts.", ephemeral=True)
            logging.info(f"User {ctx.user.id} already subscribed to IP change alerts.")
            return

        subscribers.add(ctx.user.id)
        await ctx.respond("Your subscription to IP change alerts has been confirmed.", ephemeral=True)
        logging.info(f"User {ctx.user.id} subscribed to IP change alerts.")

        await ctx.user.send(
            "You have subscribed to IP change alerts. You will be notified here when Mitra's IP address changes.\n\n"
            "To unsubscribe, use the `/unsubscribe` command.\n\n"
            f"The current IP address is:\n```{await get_ip()}```"
        )
        await save_subscribers(subscribers)

    except Exception as e:
        await ctx.respond(f"Failed to subscribe:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to subscribe: {e}")


@bot.slash_command(name="unsubscribe", description="Unsubscribe to stop getting IP change alerts sent my direct message.")
async def unsubscribe(ctx: discord.ApplicationContext) -> None:
    logging.info(f"Unsubscribe command called by {ctx.user.name}#{ctx.user.discriminator}")
    try:
        if ctx.user.id not in subscribers:
            await ctx.respond("You are not subscribed to IP change alerts.", ephemeral=True)
            return

        subscribers.remove(ctx.user.id)
        await save_subscribers(subscribers)
        await ctx.respond("You have unsubscribed from IP change alerts.", ephemeral=True)

    except Exception as e:
        await ctx.respond(f"Failed to unsubscribe:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to unsubscribe: {e}")


@bot.slash_command(name="subscribers", description="List all subscribers.")
async def list_subscribers(ctx: discord.ApplicationContext) -> None:
    logging.info(f"Subscribers command called by {ctx.user.id}, {ctx.user.name}")
    try:
        if len(subscribers) == 0:
            await ctx.respond("No subscribers.", ephemeral=True)
            return

        subscriber_names = []
        bad_subscribers = []
        for subscriber in list(subscribers):
            try:
                user = await bot.fetch_user(subscriber)
                if user:
                    subscriber_names.append(f"{user.id}: {user.name}")
                else:
                    bad_subscribers.append(subscriber)
            except discord.NotFound:
                bad_subscribers.append(subscriber)

        if bad_subscribers:
            for s in bad_subscribers:
                subscribers.discard(s)
            await save_subscribers(subscribers)

        if not subscriber_names:
            await ctx.respond("No subscribers.", ephemeral=True)
        else:
            await ctx.respond(f"Subscribers:\n```{', '.join(subscriber_names)}```", ephemeral=True)

    except Exception as e:
        await ctx.respond(f"Failed to list subscribers:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to list subscribers: {e}")


# ---- Power command group: /power restart|shutdown|cancel ----
power = bot.create_group("power", "Admin power controls: restart/shutdown/cancel (admins only).")


@power.command(name="restart", description="Restart the server running this bot (admins only).")
async def power_restart(
    ctx: discord.ApplicationContext,
    confirm: str = discord.Option(
        str,
        description="Type RESTART exactly to confirm.",
        required=True
    ),
    mode: str = discord.Option(
        str,
        description="Restart timing. immediate = now, delayed = wait then restart.",
        choices=["immediate", "delayed"],
        default="immediate"
    ),
    delay: int = discord.Option(
        int,
        description="Delay in seconds (only used when mode=delayed). Example: 60",
        default=0,
        min_value=0,
        max_value=86400
    ),
    force: bool = discord.Option(
        bool,
        description="Force close apps without warning (Windows only).",
        default=False
    ),
) -> None:
    logging.info(f"/power restart requested by {ctx.user.id} ({ctx.user.name}) mode={mode} delay={delay} force={force}")

    if not is_admin_user(ctx.user.id):
        logging.warning(f"/power restart denied (not admin): user_id={ctx.user.id}")
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    if confirm.strip().upper() != "RESTART":
        logging.warning(f"/power restart denied (bad confirm): user_id={ctx.user.id}")
        await ctx.respond("Confirmation failed. You must type RESTART exactly.", ephemeral=True)
        return

    mode = mode.lower().strip()
    if mode == "immediate":
        delay = 0
    else:
        if delay < 1:
            await ctx.respond("For delayed mode, delay must be at least 1 second.", ephemeral=True)
            return

    logging.warning(f"/power restart authorized: user_id={ctx.user.id} mode={mode} delay={delay} force={force}")
    await ctx.respond(f"Server restart scheduled.\nMode: {mode}\nDelay: {delay} seconds\nForce: {force}", ephemeral=True)

    try:
        ch = bot.get_channel(CHANNEL)
        if ch:
            await ch.send(f"⚠️ Server restart requested by <@{ctx.user.id}>. Mode: {mode} | Delay: {delay}s | Force: {force}")
    except Exception as ex:
        logging.warning(f"Failed to notify channel about restart: {ex}")

    async def _do():
        try:
            logging.warning("Executing restart command now.")
            await execute_power_action("restart", delay, force)
        except Exception as ex:
            logging.error(f"Restart execution failed: {ex}")

    asyncio.create_task(_do())


@power.command(name="shutdown", description="Shut down (power off) the server running this bot (admins only).")
async def power_shutdown(
    ctx: discord.ApplicationContext,
    confirm: str = discord.Option(
        str,
        description="Type SHUTDOWN exactly to confirm.",
        required=True
    ),
    mode: str = discord.Option(
        str,
        description="Shutdown timing. immediate = now, delayed = wait then shut down.",
        choices=["immediate", "delayed"],
        default="immediate"
    ),
    delay: int = discord.Option(
        int,
        description="Delay in seconds (only used when mode=delayed). Example: 60",
        default=0,
        min_value=0,
        max_value=86400
    ),
    force: bool = discord.Option(
        bool,
        description="Force close apps without warning (Windows only).",
        default=False
    ),
) -> None:
    logging.info(f"/power shutdown requested by {ctx.user.id} ({ctx.user.name}) mode={mode} delay={delay} force={force}")

    if not is_admin_user(ctx.user.id):
        logging.warning(f"/power shutdown denied (not admin): user_id={ctx.user.id}")
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    if confirm.strip().upper() != "SHUTDOWN":
        logging.warning(f"/power shutdown denied (bad confirm): user_id={ctx.user.id}")
        await ctx.respond("Confirmation failed. You must type SHUTDOWN exactly.", ephemeral=True)
        return

    mode = mode.lower().strip()
    if mode == "immediate":
        delay = 0
    else:
        if delay < 1:
            await ctx.respond("For delayed mode, delay must be at least 1 second.", ephemeral=True)
            return

    logging.warning(f"/power shutdown authorized: user_id={ctx.user.id} mode={mode} delay={delay} force={force}")
    await ctx.respond(f"Server shutdown scheduled.\nMode: {mode}\nDelay: {delay} seconds\nForce: {force}", ephemeral=True)

    try:
        ch = bot.get_channel(CHANNEL)
        if ch:
            await ch.send(f"⚠️ Server shutdown requested by <@{ctx.user.id}>. Mode: {mode} | Delay: {delay}s | Force: {force}")
    except Exception as ex:
        logging.warning(f"Failed to notify channel about shutdown: {ex}")

    async def _do():
        try:
            logging.warning("Executing shutdown command now.")
            await execute_power_action("shutdown", delay, force)
        except Exception as ex:
            logging.error(f"Shutdown execution failed: {ex}")

    asyncio.create_task(_do())


@power.command(name="cancel", description="Cancel a pending shutdown/restart (admins only).")
async def power_cancel(
    ctx: discord.ApplicationContext,
    confirm: str = discord.Option(
        str,
        description="Type CANCEL exactly to confirm.",
        required=True
    ),
) -> None:
    logging.info(f"/power cancel requested by {ctx.user.id} ({ctx.user.name})")

    if not is_admin_user(ctx.user.id):
        logging.warning(f"/power cancel denied (not admin): user_id={ctx.user.id}")
        await ctx.respond("You do not have permission to use this command.", ephemeral=True)
        return

    if confirm.strip().upper() != "CANCEL":
        logging.warning(f"/power cancel denied (bad confirm): user_id={ctx.user.id}")
        await ctx.respond("Confirmation failed. You must type CANCEL exactly.", ephemeral=True)
        return

    await ctx.respond("Attempting to cancel any pending shutdown/restart.", ephemeral=True)

    try:
        await execute_power_action("cancel", 0, False)
        logging.warning(f"/power cancel executed by user_id={ctx.user.id}")
    except Exception as ex:
        logging.error(f"Cancel failed: {ex}")
        await ctx.respond(f"Cancel failed:\n```{ex}```", ephemeral=True)


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error: Exception):
    if isinstance(error, commands.errors.CommandNotFound):
        return
    raise error


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.errors.CommandNotFound):
        return
    raise error


if __name__ == "__main__":
    handler = logging.StreamHandler()
    handler.setFormatter(CustomFormatter())

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    file_handler = logging.FileHandler("bot.log")
    log_formatter = logging.Formatter("[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(log_formatter)
    root_logger.addHandler(file_handler)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    TOKEN, CHANNEL, API_KEY, EMAIL, ZONE_ID, RECORD_IDS = load_connection_data()

    admins = load_admins()
    logging.info(f"Loaded {len(admins)} admin(s): {', '.join(str(a) for a in admins) if admins else 'none'}")

    subscribers = load_subscribers()
    logging.info(f"Loaded {len(subscribers)} subscriber(s): {', '.join(str(s) for s in subscribers) if subscribers else 'none'}")

    logging.info("Starting bot...")
    bot.run(TOKEN)