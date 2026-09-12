"""Release-only helpers: consistent versions, data-free bundles and retryable publishing."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

from packaging.version import Version
from mitra_bot.storage.config_store import tomllib

VERSION_FILES = ("pyproject.toml","mitra_bot/__init__.py","uv.lock")
ROOT_FILES = {"pyproject.toml","uv.lock","README.md","LICENSE","config.example.toml","peer-network.example.toml",
              ".env.example",".env.production.example","Setup-MitraBot.cmd"}
DIRECTORIES = {"mitra_bot","scripts","docs","tests"}


def version(value):
    if not isinstance(value, str):
        raise ValueError("A release version is required")
    text = value.removeprefix("v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+|-(?:alpha|beta|rc)\.[0-9]+)?",text):
        raise ValueError("Use X.Y.Z or X.Y.ZrcN (for example 0.2.0 or 0.2.0rc1)")
    return str(Version(text))


def project_versions(root):
    metadata = tomllib.loads((root/"pyproject.toml").read_text(encoding="utf-8"))
    runtime = re.search(r'^__version__\s*=\s*"([^"\n]+)"',(root/"mitra_bot/__init__.py").read_text(encoding="utf-8"),re.M)
    lock = tomllib.loads((root/"uv.lock").read_text(encoding="utf-8"))
    package = next(p for p in lock["package"] if p["name"] == "mitra-discord-bot")
    return metadata["project"]["version"],runtime.group(1),package["version"]


def stamp(root,value):
    value = version(value)
    current = project_versions(root)
    if len(set(current)) != 1:
        raise ValueError("Project/runtime/lock versions disagree; fix them before releasing")
    if Version(value) <= Version(current[0]):
        raise ValueError("The new version must be greater than the development version")
    replacements = {
        "pyproject.toml":(r'(?ms)(^\[project\]\s.*?^version\s*=\s*)"[^"\n]+"',rf'\g<1>"{value}"'),
        "mitra_bot/__init__.py":(r'(?m)^__version__\s*=\s*"[^"\n]+"',f'__version__ = "{value}"'),
        "uv.lock":(r'(?m)(^name = "mitra-discord-bot"\nversion = )"[^"\n]+"',rf'\g<1>"{value}"'),
    }
    updates = {}
    for name,(pattern,replacement) in replacements.items():
        text,count = re.subn(pattern,replacement,(root/name).read_text(encoding="utf-8"),count=1)
        if count != 1:
            raise ValueError(f"Could not update version in {name}")
        updates[name] = text
    for name,text in updates.items():
        (root/name).write_text(text,encoding="utf-8",newline="\n")
    return value


def check(root,value=None):
    versions = project_versions(root)
    if len(set(versions)) != 1 or (value is not None and versions[0] != version(value)):
        raise ValueError("Release tag, package, runtime and lock versions must match")
    return version(versions[0])


def allowed_file(name):
    if not name or "\\" in name or ":" in name:
        return False
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        return False
    if path.name.startswith(".env") and path.name not in {".env.example",".env.production.example"}:
        return False
    if any(part in {".git",".venv","__pycache__",".recovery","peer-bundles"} for part in path.parts):
        return False
    if re.search(r"\.(?:db(?:-.*)?|sqlite(?:3)?(?:-.*)?|key|jsonl|pyc|log)$",path.name,re.I):
        return False
    if path.name in {"config.toml","peer-network.toml","cache.json","data.json"}:
        return False
    return name in ROOT_FILES or (len(path.parts)>1 and path.parts[0] in DIRECTORIES)


def git(root,*args):
    return subprocess.check_output(["git",*args],cwd=root,text=True).strip()


def bundle(root,output,*,files=None,repository=None):
    current = check(root)
    output.mkdir(parents=True,exist_ok=True)
    files = files if files is not None else git(root,"ls-files","-z").split("\0")
    target = output/f"mitra-discord-bot-{current}.zip"
    prefix = f"mitra-discord-bot-{current}/"
    with zipfile.ZipFile(target,"w",compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            if not allowed_file(name):
                continue
            path = root/name
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Release source contains a symlink/outside path: {name}")
            archive.writestr(prefix+Path(name).as_posix(),path.read_bytes())
        archive.writestr(prefix+"release.json",json.dumps(dict(version=current,repository=repository)))
    return target


def checksums(output):
    paths = sorted(p for p in output.iterdir() if p.is_file() and p.name.endswith((".whl", ".tar.gz", ".zip")))
    (output/"SHA256SUMS").write_text("".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in paths),encoding="utf-8")


def publish(root,output,source_sha):
    """Called only in the write-enabled GitHub job after all tests/builds pass."""
    current = check(root)
    tag = "v"+current
    if os.getenv("GITHUB_EVENT_NAME") == "push" and os.getenv("GITHUB_REF_NAME") != tag:
        raise ValueError(f"Use the canonical release tag {tag}")
    def gh(*args):
        return subprocess.check_output(["gh",*args],cwd=root,text=True).strip()
    releases = json.loads(gh("release","list","--limit","100","--json","tagName,isDraft"))
    for release in releases:
        if not release["isDraft"]:
            try:
                older = Version(release["tagName"].removeprefix("v")) >= Version(current)
            except ValueError:
                continue
            if older:
                raise ValueError("An equal or newer version is already published; published releases are never overwritten")
    git(root,"add",*VERSION_FILES)
    tree = git(root,"write-tree")
    existing = subprocess.run(["git","rev-parse","--verify",f"refs/tags/{tag}^{{tree}}"],cwd=root,capture_output=True,text=True)
    if existing.returncode == 0:
        if existing.stdout.strip() != tree:
            raise ValueError("Existing tag contents differ from this tested release; refusing to move it")
        git(root,"checkout","--detach",tag)
    else:
        if git(root,"rev-parse","HEAD") != source_sha:
            raise ValueError("Release source commit changed")
        if subprocess.run(["git","diff","--cached","--quiet"],cwd=root).returncode:
            git(root,"commit","-m",f"Release {tag}")
        git(root,"tag","-a",tag,"-m",f"Mitra {tag}")
        git(root,"push","origin",f"refs/tags/{tag}")
    draft = next((r for r in releases if r["tagName"] == tag),None)
    if not draft:
        args = ["release","create",tag,"--verify-tag","--draft","--generate-notes","--title",f"Mitra {tag}"]
        if Version(current).is_prerelease:
            args.append("--prerelease")
        gh(*args)
    names = [f"mitra-discord-bot-{current}.zip", f"mitra_discord_bot-{current}.tar.gz",
             f"mitra_discord_bot-{current}-py3-none-any.whl", "SHA256SUMS"]
    assets = [str((output/name).resolve()) for name in names]
    if not all(Path(path).is_file() for path in assets):
        raise ValueError("Required release assets are missing")
    gh("release","upload",tag,*assets,"--clobber")  # Only the still-unpublished draft can be retried.
    gh("release","edit",tag,"--draft=false","--latest="+("false" if Version(current).is_prerelease else "true"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=["stamp","check","bundle","checksums","publish"])
    parser.add_argument("value",nargs="?")
    parser.add_argument("--output",type=Path,default=Path("dist"))
    args = parser.parse_args()
    root = Path.cwd()
    if args.command == "stamp":
        print(stamp(root,args.value))
    elif args.command == "check":
        print(check(root,args.value))
    elif args.command == "bundle":
        print(bundle(root,args.output,repository=os.getenv("GITHUB_REPOSITORY")))
    elif args.command == "checksums":
        checksums(args.output)
    else:
        publish(root,args.output,os.environ["GITHUB_SHA"])


if __name__ == "__main__":
    main()
