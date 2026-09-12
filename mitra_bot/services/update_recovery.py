"""Durable file backups for the in-process updater.

Dependency restoration is best effort; backups remain for manual recovery.
Runtime data is never copied or restored by this module.
"""
from __future__ import annotations

import json
import shutil
import uuid
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
