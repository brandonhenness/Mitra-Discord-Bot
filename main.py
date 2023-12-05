import json
import requests
import discord
from discord.ext import commands, tasks
from config import TOKEN, CHANNEL

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
        with open('data.json', 'r') as file:
            data = json.load(file)
            return data.get('ip')
    except FileNotFoundError:
        return None

async def save_ip(ip:str) -> None:
    '''Save the last known IP address to the data.json file.

    Args:
        ip (str): The IP address to save.'''
    with open('data.json', 'w') as file:
        json.dump({'ip': ip}, file)

@bot.event
async def on_ready() -> None:
    '''Perform startup tasks when the bot is ready.'''
    print(f"Logged in as {bot.user.name} - {bot.user.id}")
    check_ip.start()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Failed to perform startup tasks:\n```{e}```")

@tasks.loop(minutes=60)
async def check_ip() -> None:
    '''Check if the IP address has changed, and notify the channel if it has.'''
    try:
        current_ip = await get_ip()
        stored_ip = await load_ip()

        if current_ip != stored_ip:
            channel = bot.get_channel(CHANNEL)
            if channel:
                await channel.send(f"@here\nMitra's IP address has changed to:\n```{current_ip}```")
            await save_ip(current_ip)
    except Exception as e:
        print(f"Failed to check IP change:\n```{e}```")

@check_ip.before_loop
async def before_check_ip() -> None:
    '''Wait for the bot to be ready before starting the IP check loop.'''
    await bot.wait_until_ready()
    await check_ip()

@bot.tree.command(name="ip", description="Get Mitra's external IP address.")
async def ip(interaction: discord.Interaction) -> None:
    '''Get Mitra's external IP address.'''
    try:
        ip = await get_ip()
        await interaction.response.send_message(f"IP address:\n```{ip}```", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to get IP address:\n```{e}```", ephemeral=True)

@bot.tree.command(name="ping", description="Get the bot's latency.")
async def ping(interaction: discord.Interaction) -> None:
    '''Get the bot's latency.'''
    try:
        await interaction.response.send_message(f"Pong! {round(bot.latency * 1000)}ms", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Failed to get latency:\n```{e}```", ephemeral=True)

if __name__ == "__main__":
    bot.run(TOKEN)