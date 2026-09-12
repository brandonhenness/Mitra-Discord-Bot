# Automated releases

After merging `.github/workflows/release.yml` into the default branch, open GitHub
**Actions → Release → Run workflow**, select the default branch and supply a new
version such as `0.2.0` or `0.2.0rc1`. Actions must be enabled and repository/tag policies
must allow the workflow's `GITHUB_TOKEN` to create tags and releases (`contents: write`).
No Discord token or additional publishing secret is needed.

The workflow updates package, runtime and lockfile versions together; runs the
non-integration tests on Windows and Linux with Python 3.10 and 3.13; builds a wheel,
source distribution and deployment ZIP; smoke-tests the installed wheel; and produces
SHA256SUMS. It then creates a version-only commit and annotated tag, uploads assets to
a draft release, generates release notes and publishes. Prereleases are marked
automatically. The default branch is not modified: the version commit lives on the
release tag, which works with protected default branches.

Versions must exceed both the development version and previously published versions.
Published releases are never overwritten. A failed upload can be retried from the
same source and version while the release is still a draft; an existing tag must
match the exact tested source tree. Run a new version when source changes.

Alternatively, manually update all three versions and push a canonical matching tag
(`v0.2.0`, `v0.2.0rc1`). The same pipeline verifies and publishes it. Local checks:

```sh
uv run python -m mitra_bot.release_tools check
uv run python -m pytest -q -m "not integration" --basetemp=.recovery/release-check
uv build
uv run python -m mitra_bot.release_tools bundle
uv run python -m mitra_bot.release_tools checksums
```

The deployment ZIP uses a tracked-source allowlist and includes setup scripts and
repository metadata so ZIP installations can discover future releases without Git.
Local credentials, databases, peer keys and runtime configuration are excluded.
The updater prefers this ZIP, verifies its SHA-256 digest (or SHA256SUMS), checks its
version and rejects unsafe archive paths before copying application files. Legacy
GitHub source ZIP releases remain supported without the new asset checksum requirement.

This automates **release creation and publication**. Installing on running machines
still uses the existing admin-confirmed updater/startup procedure. It does not enable
unattended fleet-wide upgrades or add transactional rollback. Test upgrades on one
peer first, back up runtime files, then update other peers. GitHub's actual hosted
workflow must run after the changes reach the repository; local tests cannot exercise
repository permissions, tag protection or release publication.

## Before promoting a beta

Pull-request CI builds the wheel, source distribution, deployment ZIP and checksum
manifest on Windows/Linux and Python 3.10/3.13. It installs the wheel outside the
checkout and the ZIP in a separate environment and checks setup entry points.
These jobs have read-only repository permissions and do not publish releases.
Follow [two-server beta acceptance](beta-acceptance.md) before publishing stable.

## In-process update recovery

The Discord updater saves files it will replace under `.recovery/update-<id>/`
and journals progress in `manifest.json` before changing code. A copy, dependency
installation, or fresh-interpreter import/version check failure restores prior
files and removes newly added files. When dependency installation has started,
it also attempts to reinstall dependencies from the restored project metadata.
Backups remain after success or failure. Runtime secrets, configuration and
telemetry are excluded from the file replacement/rollback operation.

If dependency recovery fails, keep the bot stopped and repair the environment
using the restored project metadata before restarting. Preserve the backup until
recovery is verified. This is not a filesystem transaction: forced termination,
power loss, or disk failure can require manual recovery from the manifest and
backup. Do not run two updater processes against one installation.

The import check does not start the bot or prove Discord reconnection. Live
startup/failover acceptance is still required. The separate PowerShell deployment
script retains its existing backup-first, manual-recovery procedure.

## Windows launcher lock during an upgrade

If an older bot reports Windows error 32 while removing
`.venv/Scripts/mitra-bot.exe`, stop that bot process before repairing its environment.
Older updater versions copied new files before installing dependencies, so the
installation may already contain new source files even though Discord reports a
failed update. Do not repeatedly retry the install from the still-running process.

In the installation directory, run `uv sync --frozen --no-dev`, then start with
`uv run --no-sync --env-file .env python -m mitra_bot.main`. Substitute
`.env.production` if that is the file normally used on the server. If dependency
repair fails, keep the bot stopped and preserve the installation for diagnosis.
This repairs the copied version; it does not download a different release.

The Windows startup script now uses the Python module rather than keeping the
generated console launcher open. In-process uv dependency sync uses `--inexact`
with `--no-install-project` to retain the installed project and its launcher, and
targets the running Python environment explicitly. Project package metadata and
new console entry points are refreshed by a normal `uv sync --frozen --no-dev`
while the bot is stopped. These fixes cannot change an older updater already
loaded in a running process.


## Rolling peer updates

Starting with beta 9, `/update check` and `/update install` default to **all nodes**
when peer mode is enabled. Both show a confirmation before installing. Use
`/update install server:test` (or `/update check server:test`) for a single node.
Standalone installations retain local updates. Automatic update prompts also
update all nodes when accepted in peer mode.

The configured application-state owner coordinates the plan. Other nodes cannot
initiate an installation through peer RPC. Discord still requires the Mitra admin
role both to prepare and to confirm an update. If the owner is unavailable, the
update command does not fail over to an unrelated coordinator.

The coordinator checks that every selected node supports the protocol and is
reachable with Discord connected before starting. Remote nodes update in sorted
node-ID order; the coordinator updates last. Already-current or newer nodes are
skipped. Each node resolves the exact confirmed release from its own configured
GitHub repository and requires a checksummed artifact, using the existing backup,
installation and rollback checks. A confirmed beta can be installed even if that
node's automatic release-discovery preference excludes betas. No download URL or
shell command is accepted from a peer.

After each installation the coordinator waits for a changed process boot ID,
the exact expected running version, and two Discord-connected responses five
seconds apart. It stops on reported failure, or after 30 minutes without confirmed
recovery. Previously updated nodes are not downgraded if a later node fails.

`/update status` shows the latest persistent rollout and each node's progress.
`/update cancel` prevents further nodes from starting; an already-started install
continues. Plans and target jobs live in the local peer database. A restarted
coordinator resumes its recorded plan, including verification of its own final
restart. A lost acknowledgement reuses the same target job ID. Interrupted
installations are not automatically repeated: inspect the recovery files on that
node. After resolving a failure, confirm a new plan; current nodes will be skipped.

These are coordinated software updates, not a guarantee of zero downtime. They
may generate normal outage/recovery alerts, and nonreplicated application commands
are unavailable while the owner restarts. Do not run separate manual installers
against these folders during a rollout. Keep peer databases and credentials in
backups and keep node clocks synchronized.

### One-time bootstrap

Older betas do not implement the new RPCs. Install beta 9 or newer on **every node
once** before attempting a rolling update. An older primary can use its existing
local `/update check` installer; other older nodes need their existing local/manual
update procedure. For this development checkout, restart after the new code is
installed. Preflight refuses incompatible peers instead of partially updating the
rest of the network.

Run a live beta test with two machines before relying on this for production:
confirm the remote node installs and reconnects before the coordinator restarts;
then verify `/update status` reports completion on the expected release. Also test
an unavailable peer, a rejected release, and cancellation. Automated tests use
mocked installers/restarts and real TLS for RPC authorization; they do not replace
that cross-machine deployment test.
