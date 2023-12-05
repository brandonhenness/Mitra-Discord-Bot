import json
import requests
import os
import logging
import discord
from discord.ext import commands, tasks

class LogColors:
    GRAY = "\x1b[90m"  # Bright black (gray)
    BRIGHT_BLUE = "\x1b[94m"  # Brighter blue
    YELLOW = "\x1b[33;1m"
    RED = "\x1b[31;1m"
    MAGENTA = "\x1b[35m"  # Magenta
    RESET = "\x1b[0m"
    CYAN = "\x1b[36;1m"  # Turquoise

class CustomFormatter(logging.Formatter):
    FORMAT = "%(asctime)s " + LogColors.RESET + "%(levelname)-8s " + "%(name)s " + LogColors.RESET + "%(message)s"

    COLOR_FORMAT = {
        logging.DEBUG: LogColors.GRAY + FORMAT.replace("%(levelname)-8s", LogColors.CYAN + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.INFO: LogColors.GRAY + FORMAT.replace("%(levelname)-8s", LogColors.BRIGHT_BLUE + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.WARNING: LogColors.GRAY + FORMAT.replace("%(levelname)-8s", LogColors.YELLOW + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.ERROR: LogColors.GRAY + FORMAT.replace("%(levelname)-8s", LogColors.RED + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s"),
        logging.CRITICAL: LogColors.GRAY + FORMAT.replace("%(levelname)-8s", LogColors.RED + "%(levelname)-8s").replace("%(name)s", LogColors.MAGENTA + "%(name)s")
    }

    def format(self, record):
        log_fmt = self.COLOR_FORMAT.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)
    
formatter = CustomFormatter()

handler = logging.StreamHandler()
handler.setFormatter(CustomFormatter())
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.addHandler(handler)

if not os.path.isfile("cache.json"):
    logging.info("No cache file found, creating one...")
    with open("cache.json", "w") as file:
        TOKEN = input("Please enter the bot token: ")
        CHANNEL = input("Please enter the channel ID: ")
        json.dump({"token": TOKEN, "channel": CHANNEL}, file)

with open("cache.json", "r") as file:
    data = json.load(file)
    TOKEN = data["token"]
    CHANNEL = int(data["channel"])
    logging.info("Cache file loaded")

bot = commands.Bot(command_prefix=None, intents=discord.Intents.default())

async def get_ip() -> str:
    '''Get the external IP address of the machine running this bot.
    
    Returns:
        str: The external IP address of the machine running this bot.'''
    return requests.get('https://api.ipify.org?format=json').json()['ip']

async def load_ip() -> str:
    '''Load the last known IP address from the data.json file.

    Returns:
        str: The last known IP address, or None if no IP address is stored.'''
    try:
        with open('cache.json', 'r') as file:
            data = json.load(file)
            return data.get('ip')
    except FileNotFoundError:
        return None

async def save_ip(ip: str) -> None:
    """
    Save the last known IP address to the cache.json file.

    Args:
        ip (str): The IP address to save.
    """
    # Read the existing data
    with open('cache.json', 'r') as file:
        data = json.load(file)

    # Update the IP address
    data['ip'] = ip

    # Write the updated data back to the file
    with open('cache.json', 'w') as file:
        json.dump(data, file)

    logging.info(f"IP address saved to cache file: {ip}")

@bot.event
async def on_ready() -> None:
    '''Perform startup tasks when the bot is ready.'''
    logging.info(f"Logged in as {bot.user.name} - {bot.user.id}")
    check_ip.start()
    try:
        synced = await bot.tree.sync()
        logging.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logging.error(f"Failed to perform startup tasks: {e}")

@tasks.loop(minutes=60)
async def check_ip() -> None:
    '''Check if the IP address has changed, and notify the channel if it has.'''
    logging.info("Checking IP address...")
    try:
        current_ip = await get_ip()
        stored_ip = await load_ip()

        if current_ip != stored_ip:
            logging.info(f"IP address changed from {stored_ip} to {current_ip}")
            channel = bot.get_channel(CHANNEL)
            if channel:
                await channel.send(f"@here\nMitra's IP address has changed to:\n```{current_ip}```")
            await save_ip(current_ip)
    except Exception as e:
        logging.error(f"Failed to check IP address:\n```{e}```")
    finally:
        logging.info("IP address check complete")

@bot.tree.command(name="ip", description="Get Mitra's external IP address.")
async def ip(interaction: discord.Interaction) -> None:
    '''Get Mitra's external IP address.'''
    logging.info("IP command called by {interaction.user.name}#{interaction.user.discriminator}")
    try:
        ip = await get_ip()
        await interaction.response.send_message(f"IP address:\n```{ip}```", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to get IP address:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to get IP address: {e}")

@bot.tree.command(name="ping", description="Get the bot's latency.")
async def ping(interaction: discord.Interaction) -> None:
    '''Get the bot's latency.'''
    logging.info("Ping command called by {interaction.user.name}#{interaction.user.discriminator}")
    try:
        await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to get latency:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to get latency: {e}")

if __name__ == "__main__":
    bot.run(TOKEN)