import signal
import sys
import json
import requests
import logging
import discord
from discord.ext import commands, tasks

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

def get_valid_token():
    '''Get a valid bot token from the user.

    Returns:
        str: The bot token.'''
    while True:
        token = input("Please enter the bot token (72 characters): ").strip()
        if len(token) == 72:
            return token
        else:
            logging.warning("Invalid input. The bot token must be exactly 72 characters.")

def get_valid_channel_id():
    '''Get a valid channel ID from the user.
    
    Returns:
        int: The channel ID.'''
    while True:
        channel_id = input("Please enter the channel ID (18 digits): ").strip()
        if channel_id.isdigit() and len(channel_id) == 18:
            return int(channel_id)
        else:
            logging.warning("Invalid input. The channel ID must be exactly 18 digits.")

def load_connection_data() -> tuple:
    '''Load the connection data from the cache file.

    Returns:
        tuple: A tuple containing the bot token, channel ID, Cloudflare API key, Cloudflare email, Cloudflare zone ID, and Cloudflare DNS record IDs.'''
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
                data["zone_id"] = selected_zone['id']

        if len(record_ids) == 0:
            logging.warning("Invalid record IDs in cache file, please enter valid record IDs.")
            records = get_dns_records(api_key, email, zone_id)
            record_ids_temp = []
            if records:
                while True:
                    for i, record in enumerate(records):
                        print(f"{i}: {record['type']} {record['name']} -> {record['content']}")
                    record_index = int(input("Select a record to update by entering the corresponding number (or -1 to finish): "))
                    if record_index == -1:
                        break
                    record_ids_temp.append(records[record_index]['id'])
                data["record_ids"] = record_ids_temp

        with open("cache.json", "w") as file:
            json.dump(data, file)

        logging.info("Cache file loaded")
        return token, channel, api_key, email, zone_id, record_ids
    except ValueError:
        logging.error("Failed to load cache file.")
        raise

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

async def save_subscribers(subscribers:set) -> None:
    '''Save the list of subscribers to the cache.json file.'''
    # Read the existing data
    with open('cache.json', 'r') as file:
        data = json.load(file)

    # Update the subscribers
    data['subscribers'] = list(subscribers)

    # Write the updated data back to the file
    with open('cache.json', 'w') as file:
        json.dump(data, file)

    logging.info(f"Subscribers saved to cache file: {', '.join([str(subscriber) for subscriber in subscribers])}")

def load_subscribers() -> set:
    '''Load the list of subscribers from the cache.json file.
    
    Returns:
        set: The list of subscribers.'''
    try:
        logging.info("Loading subscribers from cache file...")
        with open('cache.json', 'r') as file:
            data = json.load(file)
            if not data.__contains__('subscribers'):
                logging.warning("No subscribers found in cache file.")
                return set()
            logging.info(f"Subscribers loaded from cache file: {', '.join([str(subscriber) for subscriber in data['subscribers']])}")
            return set(data.get('subscribers', []))
    except FileNotFoundError:
        logging.error("Cache file not found.")

def get_zones(api_key, email):
    url = "https://api.cloudflare.com/client/v4/zones"
    headers = {
        'X-Auth-Email': email,
        'X-Auth-Key': api_key,
        'Content-Type': 'application/json',
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()['result']
    else:
        print("Failed to retrieve zones.")
        return None

def get_dns_records(api_key, email, zone_id):
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records"
    headers = {
        'X-Auth-Email': email,
        'X-Auth-Key': api_key,
        'Content-Type': 'application/json',
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        return response.json()['result']
    else:
        print("Failed to retrieve DNS records.")
        return None

async def update_dns_record(api_key, email, zone_id, record_id, ip):
    headers = {
        'X-Auth-Email': email,
        'X-Auth-Key': api_key,
        'Content-Type': 'application/json',
    }
    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}"
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        logging.error(f"Failed to retrieve DNS record {record_id}.")
        return
    record = response.json()['result']
    record['content'] = ip
    response = requests.put(url, headers=headers, json=record)
    if response.status_code == 200:
        logging.info(f"DNS record {record['name']} updated successfully!")
    else:
        logging.error(f"Failed to update DNS record {record['name']}.")

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
            for subscriber in subscribers:
                user = await bot.fetch_user(subscriber)
                if user:
                    await user.send(f"Mitra's IP address has changed to:\n```{current_ip}```")
                    logging.info(f"IP change alert sent to user {subscriber}")
                else:
                    logging.warning(f"Failed to send IP change alert to user {subscriber}: user not found")
            channel = bot.get_channel(CHANNEL)
            if channel:
                await channel.send(f"Mitra's IP address has changed to:\n```{current_ip}```")
                logging.info(f"IP change alert sent to channel {CHANNEL}")
            for record in RECORD_IDS:
                await update_dns_record(API_KEY, EMAIL, ZONE_ID, record, current_ip)
            await save_ip(current_ip)
    except Exception as e:
        logging.error(f"Failed to check IP address: {e}")
    finally:
        logging.info("IP address check complete")
        logging.info("Waiting 60 minutes before checking again...")

@bot.tree.command(name="ip", description="Get Mitra's external IP address.")
async def ip(interaction: discord.Interaction) -> None:
    '''Get Mitra's external IP address.
    
    Args:
        interaction (discord.Interaction): The interaction that triggered this command.'''
    logging.info(f"IP command called by {interaction.user.name}#{interaction.user.discriminator}")
    try:
        ip = await get_ip()
        await interaction.response.send_message(f"IP address:\n```{ip}```", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to get IP address:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to get IP address: {e}")

@bot.tree.command(name="ping", description="Get the bot's latency.")
async def ping(interaction: discord.Interaction) -> None:
    '''Get the bot's latency.
    
    Args:
        interaction (discord.Interaction): The interaction that triggered this command.'''
    logging.info(f"Ping command called by {interaction.user.name}#{interaction.user.discriminator}")
    try:
        await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to get latency:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to get latency: {e}")

@bot.tree.command(name='subscribe', description='Subscribe to get IP change alerts sent my direct message.')
async def subscribe(interaction: discord.Interaction) -> None:
    '''Subscribe to get IP change alerts sent my direct message.

    Args:
        interaction (discord.Interaction): The interaction that triggered this command.'''
    logging.info(f"Subscribe command called by {interaction.user.id}, {interaction.user.name}")
    try:
        if interaction.user.id in subscribers:
            await interaction.response.send_message(f"You are already subscribed to IP change alerts.", ephemeral=True)
            logging.info(f"User {interaction.user.id} already subscribed to IP change alerts.")
            return
        subscribers.add(interaction.user.id)
        await interaction.response.send_message(f"Your subscription to IP change alerts has been confirmed.", ephemeral=True)
        logging.info(f"User {interaction.user.id} subscribed to IP change alerts.")
        await interaction.user.send(f"You have subscribed to IP change alerts. You will be notified here when Mitra's IP address changes.\n\nTo unsubscribe, use the `/unsubscribe` command.\n\nThe current IP address is:\n```{await get_ip()}```")
        logging.info(f"IP change alert subscription confirmation sent to user {interaction.user.id}")
        await save_subscribers(subscribers)
    except Exception as e:
        await interaction.response.send_message(f"Failed to subscribe:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to subscribe: {e}")
    

@bot.tree.command(name='unsubscribe', description='Unsubscribe to stop getting IP change alerts sent my direct message.')
async def unsubscribe(interaction: discord.Interaction) -> None:
    '''Unsubscribe to stop getting IP change alerts sent my direct message.
    
    Args:
        interaction (discord.Interaction): The interaction that triggered this command.'''
    logging.info(f"Unsubscribe command called by {interaction.user.name}#{interaction.user.discriminator}")
    try:
        if not subscribers.__contains__(interaction.user.id):
            await interaction.response.send_message(f"You are not subscribed to IP change alerts.", ephemeral=True)
            logging.info(f"User {interaction.user.id} not subscribed to IP change alerts.")
            return
        subscribers.remove(interaction.user.id)
        await save_subscribers(subscribers)
        await interaction.response.send_message(f"You have unsubscribed from IP change alerts.", ephemeral=True)
        logging.info(f"User {interaction.user.id} unsubscribed from IP change alerts.")
    except Exception as e:
        await interaction.response.send_message(f"Failed to unsubscribe:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to unsubscribe: {e}")

@bot.tree.command(name="subscribers", description="List all subscribers.")
async def list_subscribers(interaction: discord.Interaction) -> None:
    '''List all subscribers.
    
    Args:
        interaction (discord.Interaction): The interaction that triggered this command.'''
    logging.info(f"Subscribers command called by {interaction.user.id}, {interaction.user.name}")
    try:
        if len(subscribers) == 0:
            await interaction.response.send_message(f"No subscribers.", ephemeral=True)
            logging.info(f"No subscribers.")
        else:
            subscriber_names = []
            bad_subscribers = []
            logging.info(f"looking up {len(subscribers)} subscribers...")
            for subscriber in subscribers:
                try:
                    user = await bot.fetch_user(subscriber)
                    if user:
                        subscriber_names.append(f"{user.id}: {user.name}")
                    else:
                        bad_subscribers.append(subscriber)
                except discord.NotFound:
                    bad_subscribers.append(subscriber)
            if len(bad_subscribers) > 0:
                logging.warning(f"Failed to look up {len(bad_subscribers)} subscribers: {', '.join([str(subscriber) for subscriber in bad_subscribers])}")
                for subscriber in bad_subscribers:
                    subscribers.remove(subscriber)
                    logging.info(f"Removed subscriber {subscriber} from list")
                await save_subscribers(subscribers)
            if len(subscriber_names) == 0:
                await interaction.response.send_message(f"No subscribers.", ephemeral=True)
                logging.info(f"No subscribers.")
            else:
                await interaction.response.send_message(f"Subscribers:\n```{', '.join([str(subscriber) for subscriber in subscriber_names])}```", ephemeral=True)
                logging.info(f"Subscribers: {', '.join([str(subscriber) for subscriber in subscribers])}")
    except Exception as e:
        await interaction.response.send_message(f"Failed to list subscribers:\n```{e}```", ephemeral=True)
        logging.error(f"Failed to list subscribers: {e}")

if __name__ == "__main__":
    formatter = CustomFormatter()

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

    subscribers = load_subscribers()
    logging.info("Starting bot...")
    bot.run(TOKEN)