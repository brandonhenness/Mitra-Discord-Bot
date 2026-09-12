# Two-server beta acceptance

Test the beta on this computer and the current production server. Add the second
production server after the stable release passes acceptance.

## Prepare

1. Stop the bot and back up each installation, configuration, secrets and databases.
   Keep backups private and recoverable.
2. Publish a prerelease such as `0.2.0b1` through the Release workflow. Confirm it
   is marked prerelease; keep beta updates disabled on stable installations.
3. Install the same beta on both machines, sharing one Discord bot token and using
   separate peer bundles with unique node IDs. Stop the previous bot process
   before starting its replacement.
4. Review DNS assignments: only the intended machine should manage each record.
   Avoid assigning production records to this test computer.
5. Run `mitra-doctor` on both machines, start both bots, and run `/servers doctor`.
   Configure test-channel subscriptions with `/servers alerts`.

## Record results

Record versions, node IDs, timestamps, detection thresholds, notification counts,
and graph observations for each scenario.

| Scenario | Expected result |
| --- | --- |
| Both running | One Discord bot identity; both nodes visible; targeted UPS history belongs to the selected node. |
| Stop A, keep B running | B reports A unreachable after configured grace/failure thresholds; subscribers receive an alert. |
| Restart A | Recovery follows the configured successful probes; history retains the outage. |
| Stop/restart B | Repeat the same checks with observer roles reversed. |
| Interrupt only peer connectivity | Reachability loss is reported without claiming proof of physical shutdown; record duplicates. |
| Interrupt only Discord access on A | Local monitoring continues; inspect connectivity status and notification delivery through B. |
| Restart both | History persists; startup grace prevents immediate misleading alerts. |
| Target status commands at each node | Responses identify the correct node and are not executed twice. |
| Cancel a power command | Target identification and cancellation work; test actual restart only in a maintenance window. |
| DNS reconciliation | Each machine changes only its assigned records/account. |
| UPS migration | Legacy JSONL remains intact, graphs survive restarts, and new samples appear once. |

Allow normal monitoring and several polling cycles after every recovery. Record
any duplicate notifications during partitions; suppression is best effort.

## Promote

Fix failed checks and repeat affected scenarios on another beta. Record the tested
commit and results. Publish stable only after acceptance, deploy to the second
production server, and rerun health checks there.
