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
