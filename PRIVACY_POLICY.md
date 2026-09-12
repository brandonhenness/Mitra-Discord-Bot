# Mitra Discord Bot — Privacy Policy

Effective date: September 12, 2026

## Scope and responsibility

This policy describes the default Mitra Discord Bot software distributed from
[this repository](https://github.com/brandonhenness/Mitra-Discord-Bot). Mitra is
self-hosted: the person or organization running an installation controls its
configuration, machines, data and authorized peer network. That **operator** is
the first point of contact for information processed by that installation.

The project maintainers do not automatically receive an independent installation's
database, credentials or monitoring history. A modified installation or a hosted
service offered by someone else may have different practices. Its operator must
identify itself, provide a private contact route and explain any additional
processing to its users. This policy does not describe the separate data practices
of GitHub, Discord, Cloudflare or other external services.

## Information processed and why

Depending on the features enabled, Mitra processes:

| Information | Purpose |
| --- | --- |
| Discord user/member, guild, channel, role, message and interaction identifiers; available names and role membership | Handle commands, check permissions, manage subscriptions, select destinations and maintain bot messages |
| Command options and content submitted to bot features, including ToDo items | Carry out requested actions and preserve application state |
| Requesting/confirming administrator identifiers, action targets, timestamps and outcomes | Authorize sensitive actions, diagnose problems and avoid duplicate execution |
| Node names, reachable hostnames, public IP addresses, connection state, process uptime, latency, Discord connection status and incident history | Monitor peers, produce dashboards and send outage/recovery or IP-change notifications |
| UPS battery, power, voltage, runtime and related measurements | Show status and graphs, issue power alerts and perform configured UPS actions |
| Cloudflare account/zone/record identifiers, domain names, DNS assignments and authorization credentials | Access authorized resources and update selected DNS records to a machine's public IP |
| Bot tokens, peer certificates/private keys, configuration, software versions and update/recovery records | Authenticate services and peers, run the installation, coordinate updates and restore state |
| Operational logs and error information | Troubleshoot setup, connectivity, commands and update failures |

Mitra uses Discord interactions and feature-related API requests. It does not
require Message Content Intent and is not designed to archive general Discord
conversations. Some features inspect recent channel messages to locate the bot's
own alerts or dashboards. Information intentionally submitted to bot features
may be stored or displayed; do not submit secrets in commands or ToDo content.

The default software does not include advertising trackers, data-broker
integrations or a maintainer-operated analytics/telemetry service. These statements
do not cover modifications made by another operator.

## Storage and access

Configuration, credentials, SQLite databases, logs and recovery files are stored
on operator-controlled machines. Administrators with access to those machines
may be able to read them. Credentials and backups are not universally encrypted
at rest by Mitra; operators must protect the operating system, storage and backups.
The recovery tool restricts access to newly created backup directories, but a
copied backup still requires appropriate protection at its new destination.

Within an authorized private network, peers exchange monitoring observations,
incident history, shared monitoring settings, status information and requested
operations over authenticated TLS. Health history can therefore exist on multiple
nodes. Administrative requests can include Discord actor identifiers. Mitra does
not replicate every application database or automatically promote a new
application-state owner.

Alerts, graphs, ToDo content and progress messages sent to Discord are visible
according to the destination's permissions. Some replies are private to the
requesting user, while shared dashboards and notifications are channel messages.
Removing a local record does not itself remove a Discord message or another
peer's copy.

## External services

- **Discord:** receives bot authentication and API traffic, commands/responses,
  role changes, messages and attachments needed for enabled features.
- **Cloudflare, when configured:** receives OAuth/API requests, credentials and
  the selected DNS record changes. The default OAuth callback returns to a local
  listener on the setup machine; Mitra does not require a maintainer-hosted token
  relay. Public DNS records can disclose the domain-to-IP mapping to others.
- **ipify:** public-IP discovery contacts `api.ipify.org`. Like other Internet
  endpoints, it can observe the requesting machine's public IP and request metadata.
- **GitHub:** update checks and downloads contact the configured release repository.
  Visiting this repository or filing an issue is also subject to GitHub's practices.
- **Package/interpreter providers:** installation and updates may download Python
  or dependencies through tools such as uv or pip, using their configured sources.

Each provider can receive connection metadata such as a source IP address.
Their policies govern their own processing. Operators should review the providers
they enable and any proxies, hosting, backup or monitoring services they add.

## Retention

Retention depends on the installed version and operator configuration. Current
defaults include approximately 7 days of raw peer observations, 90 days of health
rollups and 365 days for recovered incidents. Unresolved incidents and pending
alerts can remain longer. Pruning removes eligible records in batches; SQLite may
retain allocated disk space for reuse.

Bot logs rotate at 10 MiB with five older files retained. Successful-update
recovery folders older than 30 days are eligible for cleanup after a successful
install, while at least the newest three remain. Failed/incomplete update backups
and manually created backups are not automatically deleted.

UPS history, application state, ToDo records, action journals, credentials and
other configuration can remain until removed by the operator or the relevant
feature. Revoking authorization or uninstalling the bot does not automatically
erase those files. Discord and external services have their own retention rules.
Operators should periodically review retained information and remove data no
longer needed, subject to applicable obligations.

## Choices, requests and deletion

Use `/alerts unsubscribe` to stop membership in the shared operational alert
role. This does not delete historical records. You can also ask the installation's
operator to remove information associated with you, correct inaccurate data, or
explain what that installation holds. Depending on applicable law, you may have
additional rights concerning access, portability, restriction or objection.

Contact the operator privately and provide only enough information to identify
your Discord account and the affected installation. The operator may need to
verify the request and consider copies in its state database, logs, Discord
messages, peer history and backups. Do not send passwords, API tokens or keys.
Project maintainers cannot directly delete data from independent operators' machines.

Operators can disable features, revoke Cloudflare authorizations/API tokens,
remove the bot from Discord and stop its processes. When retiring an installation,
securely remove unneeded credentials, databases and backups from all applicable
machines and services. Retention required by law and third-party copies may need
separate handling. Do not delete a live database without first stopping the bot
and understanding the effect on recovery and replicated history.

## Changes and contact

This policy will be updated in the repository as the default software's practices
change. The effective date and Git history identify revisions. Independent
operators must keep their users informed of relevant changes to their deployment.

For installation-specific privacy requests, contact the operator or your Discord
server administrator through a private channel. General project questions can be
raised through [GitHub Issues](https://github.com/brandonhenness/Mitra-Discord-Bot/issues).
Public issues are not a suitable place for private data or credentials.
