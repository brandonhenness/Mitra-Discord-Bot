# UPS history storage

UPS samples are stored in indexed SQLite tables. No database service is required.
The default file is `ups_stats.db`; configure `[ups] database_file = "path/to/ups.db"`
to select another location. Each machine keeps its own database. Existing UPS graphs,
dashboards and peer snapshots use the same history interface.

On startup, a legacy `log_file = "path/to/history.jsonl"` automatically maps to
`path/to/history.db`. The JSONL file is imported and left untouched. When using
`database_file`, an explicitly configured legacy `log_file` is imported; otherwise
the database's sibling `.jsonl` file is checked. Invalid records are skipped and
counts are logged. UTC timestamps are normalized; naive timestamps are treated as UTC.

An import journal and per-line identifiers prevent duplicates after restart or an
interrupted migration. Migration reads incrementally and commits in batches. Allow
space for both the retained original and new database. After checking the migration,
you can archive the original manually. New samples are written only to SQLite.

All raw samples remain stored; there is currently no automatic retention deletion.
Chart queries return a bounded representative sample spanning the requested time
window, including its endpoints. Remote snapshots carry at most 5,000 samples from
the last seven days; they are not full database replicas.

Back up with SQLite's online backup API, or stop the bot and copy the database and
any `-wal`/`-shm` sidecars together. Never copy only the main database while it is
being written. The Windows update helper includes these files and discovers ordinary
double-quoted custom UPS paths in `config.toml`. Unusual TOML path syntax or customized
certificate locations should be included in your own backups. Release bundles exclude
databases, private keys and local settings; the in-bot updater preserves them.
