from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from mitra_bot.storage.config_store import ensure_config_file, get_config_path
from mitra_bot.storage.state_store import get_state_path, get_state_store


def _copy_if_missing(src: Path, dst: Path, *, force: bool) -> str:
    if not src.exists():
        return f"missing template: {src}"
    if dst.exists() and not force:
        return f"skipped existing: {dst}"
    shutil.copyfile(src, dst)
    return f"created: {dst}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create local env/config/state files from project templates."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing target files.",
    )
    args = parser.parse_args()

    root = Path.cwd()
    ops = [
        (root / ".env.example", root / ".env"),
        (root / ".env.production.example", root / ".env.production"),
        (root / "config.example.toml", get_config_path()),
    ]

    for src, dst in ops:
        print(_copy_if_missing(src, dst, force=args.force))

    # Normalize and persist config defaults.
    cfg = ensure_config_file()
    print(f"normalized config: {get_config_path()} ({len(cfg)} top-level sections)")

    # Ensure state DB exists and migrations are applied.
    store = get_state_store()
    print(f"state db ready: {get_state_path()} (schema_version={store.get_meta('schema_version')})")


if __name__ == "__main__":
    main()
