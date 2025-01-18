# Mitra Discord Bot

<img src="mitra.png" width="500" height="500">

Mitra is a specialized Discord bot designed to monitor a server's dynamic IP address and ensure uninterrupted accessibility. It provides notifications to users and updates Cloudflare DNS records whenever the server's IP address changes, making it a valuable tool for maintaining server reliability.

## Features

- **IP Address Monitoring**: Continuously tracks the server's external IP address for changes.
- **IP Change Notifications**: Sends updates to a specified Discord channel when the IP address changes.
- **Cloudflare DNS Updates**: Automatically updates Cloudflare DNS records to reflect the new IP address, ensuring consistent server accessibility.

## Getting Started

To get started with Mitra Discord Bot, follow these steps:

1. Clone the repository:
   ```bash
   git clone https://github.com/your-username/mitra-discord-bot.git
   ```
2. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Set up a new Discord application and bot token on the [Discord Developer Portal](https://discord.com/developers/applications).
4. Configure your Cloudflare API credentials and zone settings in the configuration file.
5. Run the bot:
   ```bash
   python main.py
   ```
6. Enter the bot token and channel ID when prompted.
7. Mitra will start monitoring your server's IP and updating DNS records as needed.

## Contributing

Contributions are welcome! If you encounter issues or have suggestions for improvement, please open an issue or submit a pull request.

## License

ClipboardSync is licensed under the [GNU General Public License v3.0](LICENSE).

---

Developed with ❤️ by [Brandon Henness](https://github.com/brandonhenness).
