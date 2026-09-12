# Mitra Discord Bot — Terms of Service

Effective date: September 12, 2026

## Scope and operator

Mitra Discord Bot ("Mitra") is open-source software for Discord-based server
monitoring, notifications, DNS updates and administrative controls. It can run
on one machine or in an operator's private peer network. The project does not
provide a centrally hosted bot service as part of the software distribution.

The person or organization running a particular installation is its **operator**.
These terms describe use of Mitra's bot features. An independent operator may
provide additional terms and privacy information for its own installation and
must identify how users can contact it. Project maintainers do not automatically
operate or administer installations run by other people.

## Open-source license

The software is licensed under the [GNU General Public License, version 3](LICENSE).
Your rights to use, copy, modify and distribute the software are governed by that
license. These terms do not replace, restrict or add conditions to rights granted
by the license. Third-party software retains its respective licenses.

## Permitted use and responsibilities

Use bot features only with permission from the relevant server, machine, network
and account owners. Meet Discord's eligibility requirements and comply with
applicable law and the terms of services you connect, including Discord and
Cloudflare. Do not use Mitra to gain unauthorized access, disrupt other people's
systems, harass users, send unwanted messages or evade platform safeguards.

Operators are responsible for choosing authorized administrators, assigning
Discord permissions, obtaining permission for monitoring and DNS changes,
protecting credentials, maintaining backups and informing users about their
installation's data practices. Operators should provide a private way for users
to request access, correction or deletion of information held by that operator.

Keep bot tokens, Cloudflare tokens, node private keys, offline CA keys and backup
credentials private. Remove access and rotate affected credentials if they are
compromised. A shared bot identity or trusted peer membership grants sensitive
access; distribute installation bundles only to authorized operators.

## Administrative actions and notifications

Authorized commands can restart or shut down machines, change DNS records, alter
roles and subscriptions, install software updates and change monitoring settings.
These actions can interrupt services or cause data loss. Check the selected node,
permissions and intended action before confirming them.

Alert subscriptions can be managed through `/alerts subscribe` and
`/alerts unsubscribe`. Authorized administrators can also manage another member's
subscription. Unsubscribing removes the operational alert role; messages may
remain visible in channels you can access, and past records are not automatically
deleted by unsubscribing.

Monitoring depends on connectivity, hardware, permissions and external services.
An unreachable peer is not proof that its machine is powered off. Updates,
failover behavior, DNS changes and notifications may be delayed or fail. Do not
rely on Mitra as the sole safeguard for emergencies or systems where interruption
could cause injury or substantial damage.

## External services and privacy

Discord, Cloudflare, GitHub and other services contacted by enabled features have
their own terms and privacy practices. Their availability and policies are outside
the project's control. The [Privacy Policy](PRIVACY_POLICY.md) explains the default
software's data handling and the responsibilities of independent operators.

## Availability, warranties and liability

The software is provided "as is" and "as available". There is no promise of
uninterrupted service, accurate monitoring, successful recovery, continued
maintenance or support. Warranty disclaimers and limitations of liability for
the software are governed by sections 15–17 of the GPL in [LICENSE](LICENSE),
to the extent permitted by applicable law. Nothing here excludes rights or
liabilities that cannot lawfully be excluded.

## Stopping use and changes

You may stop using the bot or unsubscribe at any time. Operators can remove the
bot from Discord, stop its processes and revoke third-party authorizations.
Stopping the bot does not automatically erase stored data, backups or previously
posted Discord messages; see the Privacy Policy for deletion requests.

The project may update these terms as its features change. Revisions will be
published in this repository with an updated effective date and Git history.
Operators are responsible for communicating material changes that affect their
users. Changes do not retroactively change the open-source license on copies
already received.

## Contact

For matters concerning a particular installation, contact its operator or your
Discord server administrator through a private channel. For general project
questions, use [GitHub Issues](https://github.com/brandonhenness/Mitra-Discord-Bot/issues).
Do not post passwords, tokens, private keys or personal information in public issues.
