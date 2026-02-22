from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

from mitra_bot import __version__
from mitra_bot.storage.cache_store import get_updater_config, set_updater_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$")


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    zipball_url: str
    html_url: str
    notes: str


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: Optional[str]
    available: bool
    release: Optional[ReleaseInfo]
    repo: Optional[str]
    error: Optional[str] = None


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    version: Optional[str] = None
    error: Optional[str] = None


def get_current_version() -> str:
    return (__version__ or "0.0.0").strip()


def _clean_version(value: str) -> str:
    v = (value or "").strip()
    return v[1:] if v.lower().startswith("v") else v


def _resolve_repo_from_git() -> Optional[str]:
    try:
        cp = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=PROJECT_ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
    except Exception:
        return None

    remote = (cp.stdout or "").strip()
    match = _GITHUB_REMOTE_RE.search(remote)
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('repo')}"


def resolve_github_repo() -> Optional[str]:
    cfg = get_updater_config()
    raw = cfg.get("github_repo")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()

    repo = _resolve_repo_from_git()
    if repo:
        set_updater_config({"github_repo": repo})
    return repo


def _release_info_from_payload(payload: dict, repo: str) -> Optional[ReleaseInfo]:
    latest_version = str(payload.get("tag_name") or "").strip()
    zipball_url = str(payload.get("zipball_url") or "").strip()
    html_url = str(payload.get("html_url") or "").strip()
    notes = str(payload.get("body") or "").strip()

    if not latest_version or not zipball_url:
        return None

    return ReleaseInfo(
        version=latest_version,
        zipball_url=zipball_url,
        html_url=html_url or f"https://github.com/{repo}/releases",
        notes=notes,
    )


def _fetch_release_payload(repo: str, *, include_prerelease: bool) -> tuple[Optional[dict], Optional[str]]:
    headers = {"Accept": "application/vnd.github+json"}
    if include_prerelease:
        url = f"https://api.github.com/repos/{repo}/releases"
        try:
            response = requests.get(
                url,
                timeout=20,
                headers=headers,
                params={"per_page": 10},
            )
            response.raise_for_status()
            payloads = response.json()
        except Exception as exc:
            return None, f"Failed to check releases: {exc}"

        if not isinstance(payloads, list):
            return None, "Unexpected releases response format from GitHub."

        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            if bool(payload.get("draft", False)):
                continue
            return payload, None
        return None, "No published releases were found."

    url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        response = requests.get(
            url,
            timeout=20,
            headers=headers,
        )
        if response.status_code == 404:
            return (
                None,
                "No non-pre-release published release found. Enable beta mode to include pre-releases.",
            )
        response.raise_for_status()
        return response.json(), None
    except Exception as exc:
        return None, f"Failed to check latest release: {exc}"


def check_latest_release() -> UpdateCheckResult:
    current_version = get_current_version()
    repo = resolve_github_repo()
    cfg = get_updater_config()
    include_prerelease = bool(cfg.get("include_prerelease", False))
    now_epoch = int(time.time())
    set_updater_config({"last_checked_epoch": now_epoch})

    if not repo:
        return UpdateCheckResult(
            current_version=current_version,
            latest_version=None,
            available=False,
            release=None,
            repo=None,
            error="Could not determine GitHub repository.",
        )

    payload, payload_error = _fetch_release_payload(
        repo,
        include_prerelease=include_prerelease,
    )
    if payload_error:
        return UpdateCheckResult(
            current_version=current_version,
            latest_version=None,
            available=False,
            release=None,
            repo=repo,
            error=payload_error,
        )

    if payload is None:
        return UpdateCheckResult(
            current_version=current_version,
            latest_version=None,
            available=False,
            release=None,
            repo=repo,
            error="No release payload available from GitHub.",
        )

    release = _release_info_from_payload(payload, repo)
    if release is None:
        return UpdateCheckResult(
            current_version=current_version,
            latest_version=None,
            available=False,
            release=None,
            repo=repo,
            error="Release response is missing required fields.",
        )

    latest_version = release.version
    available = _clean_version(latest_version) != _clean_version(current_version)
    return UpdateCheckResult(
        current_version=current_version,
        latest_version=latest_version,
        available=available,
        release=release,
        repo=repo,
        error=None,
    )


def _copy_release_tree(source_root: Path, target_root: Path) -> None:
    skip_names = {
        ".git",
        ".github",
        "__pycache__",
        ".ruff_cache",
        ".pytest_cache",
        ".mypy_cache",
        "cache.json",
        "bot.log",
    }
    for src in source_root.iterdir():
        if src.name in skip_names:
            continue
        dst = target_root / src.name
        if src.is_dir():
            shutil.copytree(
                src,
                dst,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", ".ruff_cache"),
            )
        else:
            shutil.copy2(src, dst)


def _install_requirements() -> None:
    req = PROJECT_ROOT / "requirements.txt"
    if not req.exists():
        return

    cmd = [sys.executable, "-m", "pip", "install", "-r", str(req)]
    subprocess.run(
        cmd,
        cwd=PROJECT_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


def install_release(release: ReleaseInfo) -> InstallResult:
    try:
        with tempfile.TemporaryDirectory(prefix="mitra-update-") as tmpdir:
            tmp_path = Path(tmpdir)
            zip_path = tmp_path / "release.zip"

            dl = requests.get(release.zipball_url, timeout=60)
            dl.raise_for_status()
            zip_path.write_bytes(dl.content)

            extract_dir = tmp_path / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(extract_dir)

            roots = [p for p in extract_dir.iterdir() if p.is_dir()]
            if not roots:
                return InstallResult(ok=False, error="Downloaded release archive is empty.")

            source_root = roots[0]
            _copy_release_tree(source_root, PROJECT_ROOT)
            _install_requirements()

        set_updater_config(
            {
                "installed_version": release.version,
                "pending_version": None,
                "pending_release_url": None,
                "pending_notes": None,
                "pending_notified_epoch": None,
                "last_notified_version": release.version,
            }
        )
        return InstallResult(ok=True, version=release.version)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        return InstallResult(
            ok=False,
            error=f"Dependency install failed ({exc.returncode}): {stderr[:800]}",
        )
    except Exception as exc:
        logging.exception("Update install failed.")
        return InstallResult(ok=False, error=str(exc))


def spawn_replacement_process() -> None:
    cmd = [sys.executable, *sys.argv]
    kwargs = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, **kwargs)
