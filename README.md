# Mitra Discord Bot

<img src="mitra.png" width="240" height="240" alt="Mitra logo" />

Shared to-do lists and task threads for your Discord community. Organize projects,
assign work, and track progress together without leaving your server.

**[Add Mitra to your server](https://discord.com/oauth2/authorize?client_id=1181490269993058314&permissions=326417599504&integration_type=0&scope=bot%20applications.commands)**

[Get started](#get-started) · [Commands](#commands) · [Permissions](#permissions) · [Self-hosting](#self-hosting)

## What you can do

- Create separate lists for projects, plans, or recurring work.
- Give each task its own thread for details and discussion.
- Assign yourself or another member to a task.
- Track tasks as Open, In Progress, or Done using buttons or slash commands.
- See progress on shared list boards and a central hub.

Each Discord server has its own lists and task history. Public installs include
`/todo` and `/about`. Machine monitoring, IP addresses, power controls, updates,
and infrastructure alerts are reserved for the operator's authorized servers.

## Get started

1. **Invite Mitra.** Use the link above and choose your Discord server. The person
   installing the bot needs **Manage Server** permission.
2. **Create your first list.** A member with **Manage Channels** runs
   `/todo list_create name:Projects`. Mitra creates a To-Do category, a hub, and
   your list channel. Inviting the bot alone does not create channels.
3. **Add a task.** Click **Add Task** on the list board or run
   `/todo add_task title:Plan the next event` in the list channel.
4. **Work in the task thread.** Use **Assign Me**, add details, and change the
   status as the task progresses. Use **Create List** in the hub for more lists.

Members need access to the list and task thread, including View Channel, Send
Messages, and Send Messages in Threads. Allow **Use Application Commands** for
members who use slash commands. Control access through Discord's channel and
category permissions; restrict the shared hub too if its summaries are sensitive.

## Commands

| Command | Where to use it | What it does |
| --- | --- | --- |
| `/todo list_create` | Your server | Create a list; requires Manage Channels |
| `/todo add_task` | A list channel, or choose `list_channel` | Add a task and create its thread |
| `/todo edit` | A task thread | Update its title and notes |
| `/todo status` | A task thread | Set Open, In Progress, or Done |
| `/todo assign_me` / `/todo unassign_me` | A task thread | Join or leave the task assignment |
| `/todo assign` / `/todo unassign` | A task thread | Assign or unassign another member |
| `/about` | Your server | Show bot information and policy links |

The list boards and task threads also have buttons for common actions.

## Permissions

The public invite requests these permissions for Mitra:

| Permission | Why Mitra uses it |
| --- | --- |
| View Channels | Access the lists, hub, and task threads |
| Manage Channels | Create list channels and the To-Do category; position the hub |
| Send Messages | Post boards, task panels, and replies |
| Embed Links | Display formatted task cards and boards |
| Read Message History | Find and refresh Mitra's existing boards and task panels |
| Create Public Threads | Create a discussion thread for each task |
| Send Messages in Threads | Post and update task panels in threads |
| Manage Threads | Remove thread memberships when members are unassigned |

**Administrator is not required.** The public invite does not request Manage
Roles, Manage Messages, Attach Files, moderation, or voice permissions.
Discord requires Manage Threads to remove another member from a public thread;
creating the thread does not exempt the bot from that requirement.
See [Discord's thread membership documentation](https://docs.discord.com/developers/resources/channel#remove-thread-member).

If setup cannot create a channel or post a board, check Mitra's role and the
To-Do category's permission overrides. For the exact invite URL and Developer
Portal settings, see [invite configuration](docs/discord-install.md).

## Privacy and support

Lists and task history are separate between Discord servers. Within your server,
boards and threads are shared according to channel permissions. Hosting operators
can access stored bot data; avoid putting secrets in tasks.

[Terms of Service](TERMS_OF_SERVICE.md) · [Privacy Policy](PRIVACY_POLICY.md)

For access or data requests, contact your server administrator or the bot operator
privately. Report software bugs through [GitHub Issues](https://github.com/brandonhenness/Mitra-Discord-Bot/issues);
keep private data and credentials out of public issues.

## Self-hosting

You can run your own Mitra installation with your own Discord application and
token. Self-hosting also provides optional IP and UPS monitoring, private peer
networks, infrastructure alerts, software updates, and Windows power controls.

On Windows, download and extract a [release](https://github.com/brandonhenness/Mitra-Discord-Bot/releases),
then run **Setup-MitraBot.cmd**. With Python 3.10+ and uv available, the guided
setup can also be started with:

```sh
uv sync --frozen --no-dev
uv run mitra-setup
```

Configure the authorized Discord server IDs before making your installation
public, then start it with:

```sh
uv run --env-file .env python -m mitra_bot.main
```

- [Self-hosting and operations](docs/self-hosting.md): requirements, configuration, Windows startup, updates, and legacy migration
- [Guided setup](docs/setup.md): installation walkthrough and troubleshooting
- [Public access and private infrastructure](docs/public-access.md): server authorization and rollout
- [Discord invite configuration](docs/discord-install.md): public application settings and permission checklist
- [Private peer networks](docs/private-peer-network.md), [Cloudflare](docs/cloudflare.md), and [UPS history](docs/ups-history.md)

## Development

Clone the repository, run `uv sync`, then `uv run pytest`. See the
[development guide](docs/self-hosting.md#development) and
[release workflow](docs/releases.md) for development and packaging details.

## License

Licensed under the [GNU General Public License v3.0](LICENSE).
