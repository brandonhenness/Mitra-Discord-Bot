"""Durable file backups for the in-process updater.

Dependency restoration is best effort; backups remain for manual recovery.
Runtime data is never copied or restored by this module.
"""
from __future__ import annotations

import json
import shutil
import uuid
import re
import time
from pathlib import Path

from mitra_bot.release_tools import allowed_file


def release_files(source):
    return [p.relative_to(source) for p in source.rglob("*")
            if p.is_file() and (allowed_file(p.relative_to(source).as_posix())
                               or p.relative_to(source).as_posix() == "release.json")]


def safe_target(root, relative):
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Update target contains an unsafe link or outside path")
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Update target contains a linked directory")
    return path


def prune_successful_backups(root, *, keep=3, days=30, now=None):
    """Retain recent successful rollbacks and every unfinished/failed backup."""
    root = Path(root).resolve()
    directory = root / ".recovery"
    if directory.resolve() != directory or not directory.is_dir():
        return []
    now = time.time() if now is None else now
    candidates = []
    for folder in directory.iterdir():
        if not re.fullmatch(r"update-[a-f0-9]{32}", folder.name) or folder.resolve() != folder or not folder.is_dir():
            continue
        manifest = folder / "manifest.json"
        try:
            if manifest.resolve() != manifest or json.loads(manifest.read_text(encoding="utf-8")).get("stage") != "validated":
                continue
            candidates.append((manifest.stat().st_mtime, folder))
        except (OSError, ValueError):
            continue
    removed = []
    for stamp, folder in sorted(candidates, reverse=True)[max(3, keep):]:
        if stamp >= now - max(1, days)*86400:
            continue
        # Verify the exact absolute tree before any recursive deletion. Linked
        # files/directories (including Windows junctions) are never traversed.
        if any(p.resolve() != p or not p.resolve().is_relative_to(folder) for p in folder.rglob("*")):
            continue
        if folder.resolve().parent != directory:
            continue
        shutil.rmtree(folder)
        removed.append(folder.name)
    return removed


class Recovery:
    def __init__(self, source, target):
        self.root = target.resolve()
        self.folder = safe_target(self.root, Path(".recovery") / ("update-" + uuid.uuid4().hex))
        self.entries = []
        self.failed_stage = None
        # Validate every destination before backing up or replacing any file.
        for relative in release_files(source):
            destination = safe_target(self.root, relative)
            if destination.exists() and not destination.is_file():
                raise ValueError("Update would replace a directory")
            self.entries.append((relative.as_posix(), destination.exists()))
        self.folder.mkdir(parents=True)
        for name, existed in self.entries:
            if existed:
                saved = self.folder / "files" / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / name, saved)
        self.record("prepared")

    def record(self, stage):
        self.stage = stage
        manifest = self.folder / "manifest.json"
        pending = self.folder / "manifest.tmp"
        pending.write_text(json.dumps({"stage": stage, "failure_after_stage": self.failed_stage,
                                       "files": self.entries}, indent=2), encoding="utf-8")
        pending.replace(manifest)

    def restore(self):
        for name, existed in self.entries:
            destination = safe_target(self.root, Path(name))
            if existed:
                shutil.copy2(self.folder / "files" / name, destination)
            elif destination.is_file():
                destination.unlink()
        self.record("files_restored")
