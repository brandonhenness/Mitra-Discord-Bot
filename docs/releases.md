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
