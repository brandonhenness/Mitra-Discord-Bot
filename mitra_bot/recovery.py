"""Back up node state and verify restoration into a NEW directory."""
import argparse
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from dotenv import dotenv_values
from mitra_bot import __version__
from mitra_bot.storage.config_store import tomllib
from mitra_bot.private_files import private_directory


def inside(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("Recovery paths must be files inside the installation directory")
    return path


def digest(path):
    with path.open("rb") as stream:
        result = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            result.update(chunk)
        return result.hexdigest()


def files_for(root, env_file):
    if not inside(root, env_file).is_file():
        raise ValueError("Choose the node's existing environment file")
    env = {**dotenv_values(inside(root, env_file)), **os.environ}
    cfg_path = inside(root, env.get("MITRA_CONFIG_PATH") or "config.toml")
    cfg = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
    paths = {cfg_path, inside(root, env_file), cfg_path.parent / ".env.cloudflare-oauth.json",
             inside(root, env.get("MITRA_STATE_PATH") or "state.db")}
    ups = cfg.get("ups", {})
    ups_path = inside(root, ups.get("database_file") or ups.get("log_file") or "ups_stats.db")
    if ups_path.suffix in {".jsonl", ".ndjson"}:
        paths.add(ups_path)
        ups_path = ups_path.with_suffix(".db")
    paths.add(ups_path)
    peer_path = inside(root, env.get("MITRA_PEER_CONFIG_PATH") or "peer-network.toml")
    identity = None
    if peer_path.exists():
        from mitra_bot.peer_config import PeerConfig
        peer = PeerConfig.model_validate(tomllib.loads(peer_path.read_text(encoding="utf-8")))
        paths.add(peer_path)
        if peer.enabled:
            identity = {"network": peer.network_id, "node": peer.node_id}
            for name in ("ca_file", "cert_file", "key_file", "state_file"):
                paths.add(inside(root, str(peer_path.parent / getattr(peer, name))))
            for name in ("ca_file", "cert_file", "key_file"):
                if not (peer_path.parent / getattr(peer, name)).is_file():
                    raise ValueError("A configured peer certificate or key is missing")
    paths = {inside(root, str(p)) for p in paths}
    return sorted(p for p in paths if p.is_file()), identity


def check_database(path):
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("Database integrity check failed")


def backup(root, output, env_file=".env"):
    root, output = Path(root).resolve(), Path(output).resolve()
    paths, identity = files_for(root, env_file)
    private_directory(output)
    manifest = dict(format=1, version=__version__, identity=identity, files=[])
    for path in paths:
        relative = path.relative_to(root).as_posix()
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("rb") as source:
            database = source.read(16) == b"SQLite format 3\x00"
        if database:
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as source, closing(sqlite3.connect(target)) as dest:
                source.backup(dest)
            check_database(target)
        else:
            shutil.copyfile(path, target)
        target.chmod(0o600)
        manifest["files"].append(dict(path=relative, sha256=digest(target), database=database))
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def restore(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or not manifest.get("files"):
        raise ValueError("Unsupported or empty backup manifest")
    seen = set()
    for entry in manifest["files"]:
        name = entry["path"]
        if Path(name).is_absolute() or ":" in name or ".." in name.replace("\\", "/").split("/"):
            raise ValueError("Invalid backup path")
        path = inside(source, name)
        relative = str(inside(output, name)).casefold()
        if relative in seen or name == "manifest.json":
            raise ValueError("Duplicate or reserved backup path")
        seen.add(relative)
        if digest(path) != entry["sha256"]:
            raise ValueError("Backup checksum mismatch; nothing restored")
        if entry["database"]:
            check_database(path)
    private_directory(output)
    for entry in manifest["files"]:
        target = inside(output, entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(inside(source, entry["path"]), target)
        target.chmod(0o600)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("backup", help="Stop the bot first for a consistent set of files")
    create.add_argument("--root", type=Path, default=Path.cwd())
    create.add_argument("--env-file", default=".env")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--bot-stopped", action="store_true", required=True,
                        help="Confirm this node is stopped; SQLite files are also copied using the backup API")
    recover = sub.add_parser("restore", help="Verify checksums/databases and restore into a new empty path")
    recover.add_argument("--source", type=Path, required=True)
    recover.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = backup(args.root, args.output, args.env_file) if args.action == "backup" else restore(args.source, args.output)
        print(f"Verified {len(result['files'])} files. This folder contains credentials: keep it private and outside Git.")
        print("Restore uses the same node identity. Keep the old instance stopped. See docs/operations-recovery.md.")
    except (OSError, ValueError, sqlite3.Error, KeyError) as exc:
        parser.exit(1, f"Recovery stopped: {type(exc).__name__}. Check paths, manifest and file integrity; existing installations were not overwritten.\n")


if __name__ == "__main__":
    main()
