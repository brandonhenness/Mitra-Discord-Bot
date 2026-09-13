# Public invite and Discord permissions

[Back to the user guide](../README.md)

## Invite link

**[Add Mitra to your server](https://discord.com/oauth2/authorize?client_id=1181490269993058314&permissions=326417599504&integration_type=0&scope=bot%20applications.commands)**

This link installs application `1181490269993058314` into a Discord server using
the `bot` and `applications.commands` scopes. It explicitly requests the public
to-do permissions, so the repository link does not rely on the portal's default
permission selection.

```text
https://discord.com/oauth2/authorize?client_id=1181490269993058314&permissions=326417599504&integration_type=0&scope=bot%20applications.commands
```

The shorter [Discord-provided link](https://discord.com/oauth2/authorize?client_id=1181490269993058314)
uses the application's default install settings. Configure those settings below
so links shared outside the repository request the same permissions.

## Developer Portal settings

These are settings for the application owner. People inviting the hosted bot do
not need to create an application or provide a token.

1. Open [Mitra's Installation page](https://discord.com/developers/applications/1181490269993058314/installation).
2. Enable **Guild Install**. Mitra's shared lists use server channels; **User
   Install** is not needed.
3. Select **Discord Provided Link** for the install link.
4. Under **Default Install Settings → Guild Install**, select scopes **bot** and
   **applications.commands**, then select the eight permissions below and save.
5. On the application's **Bot** page, enable **Public Bot** so other server
   administrators can invite it. Leave **Requires OAuth2 Code Grant** off for
   this direct bot invite flow.

Discord documents these settings in its
[installation walkthrough](https://docs.discord.com/developers/quick-start/getting-started)
and [application reference](https://docs.discord.com/developers/resources/application).

## Permission checklist

| Select in Discord | Permission flag | Why it is needed |
| --- | --- | --- |
| View Channels | `VIEW_CHANNEL` | Access list channels and threads |
| Manage Channels | `MANAGE_CHANNELS` | Create the category, hub, and lists; position the hub |
| Send Messages | `SEND_MESSAGES` | Post boards and replies |
| Embed Links | `EMBED_LINKS` | Render task cards and boards |
| Read Message History | `READ_MESSAGE_HISTORY` | Locate and update existing bot messages |
| Create Public Threads | `CREATE_PUBLIC_THREADS` | Create task discussions |
| Send Messages in Threads | `SEND_MESSAGES_IN_THREADS` | Post task panels and add thread members |
| Manage Threads | `MANAGE_THREADS` | Remove members from public task threads when unassigning |

The combined permissions integer is **`326417599504`**. These correspond to
bits **4, 10, 11, 14, 16, 34, 35, and 38** in
[Discord's permission reference](https://docs.discord.com/developers/topics/permissions).

Do **not** select Administrator. The public features do not need Manage Roles,
Manage Messages, Attach Files, Pin Messages, Mention Everyone, kick/ban access,
voice access, or Create Private Threads. The `applications.commands` OAuth scope
registers commands; granting the bot Use Application Commands is not needed to
handle members' commands.

Manage Threads is required by the current unassignment workflow: Discord allows
removing another member from a public thread only with that permission, even if
the bot created the thread. Without it, the task's stored assignee can be removed
while Discord thread membership remains unchanged. See
[Remove Thread Member](https://docs.discord.com/developers/resources/channel#remove-thread-member).

## Member permissions and intents

The person inviting the bot needs **Manage Server**. A person creating to-do
lists needs **Manage Channels**. Members using tasks need access to their list
and thread, and **Use Application Commands** where they use slash commands.
These are member permissions, separate from the bot's invite permissions.

Gateway intents are also separate from the invite checklist. The current bot
uses **Server Members Intent** for thread-member synchronization; configure it
on the Bot page and keep it consistent with `MITRA_ENABLE_MEMBERS_INTENT`.
Message Content Intent and Presence Intent are not required. Discord's approval
requirements for privileged intents still apply as the application grows; see
the [Gateway intent documentation](https://docs.discord.com/developers/events/gateway#privileged-intents).

## Private infrastructure deployments

The public invite does not request the permissions used only by infrastructure
features. On your authorized servers, grant additional permissions for features
you enable, such as **Manage Roles** for alerts and **Attach Files** and **Pin
Messages** for dashboards. The self-hosting wizard currently requests those
infrastructure permissions; grant the to-do channel/thread permissions too if
you use lists on that installation.

Bot permissions do not authorize access to the operator's machines. That access
still requires the local [Discord server allowlist](public-access.md).
Before distributing the invite, deploy the isolation changes to every node and
verify an unlisted test server can use only the public features. This guide and
the repository link do not change the live Developer Portal settings.
