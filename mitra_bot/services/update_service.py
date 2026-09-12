from __future__ import annotations

import logging
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from packaging.version import InvalidVersion, Version

from mitra_bot import __version__
from mitra_bot.storage.storage_store import get_updater_config, set_updater_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_GITHUB_REMOTE_RE = re.compile(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$")
_INSTALL_LOCK = threading.Lock()


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    zipball_url: str
    html_url: str
    notes: str
    sha256: Optional[str] = None
    checksum_url: Optional[str] = None
    asset_name: Optional[str] = None


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


def _is_newer_version(candidate: str, current: str) -> bool:
    """Return whether a release candidate is newer under PEP 440 ordering."""
    return Version(_clean_version(candidate)) > Version(_clean_version(current))


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
        return repo
    try:
        repo = json.loads((PROJECT_ROOT / "release.json").read_text(encoding="utf-8")).get("repository")
        return repo if isinstance(repo, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", repo) else None
    except (OSError, ValueError):
        return None


def _release_info_from_payload(payload: dict, repo: str) -> Optional[ReleaseInfo]:
    version = str(payload.get("tag_name") or "").strip()
    zipball_url = str(payload.get("zipball_url") or "").strip()
    html_url = str(payload.get("html_url") or "").strip()
    notes = str(payload.get("body") or "").strip()
    digest = checksum_url = asset_name = None
    try:
        expected = f"mitra-discord-bot-{Version(_clean_version(version))}.zip"
    except InvalidVersion:
        expected = ""
    assets = payload.get("assets") or []
    for asset in assets:
        if asset.get("name") == expected and asset.get("browser_download_url"):
            zipball_url = asset["browser_download_url"]
            asset_name = expected
            raw = asset.get("digest") or ""
            if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", raw):
                digest = raw[7:].lower()
            checksum_url = next((a.get("browser_download_url") for a in assets if a.get("name") == "SHA256SUMS"), None)
            break

    if not version or not zipball_url:
        return None

    return ReleaseInfo(
        version=version,
        zipball_url=zipball_url,
        html_url=html_url or f"https://github.com/{repo}/releases",
        notes=notes,
        sha256=digest,
        checksum_url=checksum_url,
        asset_name=asset_name,
    )


def _fetch_release_payload(repo: str, *, include_prerelease: bool) -> tuple[Optional[dict], Optional[str]]:
    headers = {"Accept": "application/vnd.github+json"}

    if include_prerelease:
        api_url = f"https://api.github.com/repos/{repo}/releases"
        try:
            response = requests.get(
                api_url,
                timeout=20,
                headers=headers,
                params={"per_page": 20},
            )
            if response.status_code == 404:
                return None, (
                    f"Repository `{repo}` not found or inaccessible. "
                    "Use /update repo to set the correct owner/name."
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

    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    try:
        response = requests.get(
            api_url,
            timeout=20,
            headers=headers,
        )
        if response.status_code == 404:
            return None, (
                f"No stable published release found for `{repo}`. "
                "If you only publish pre-releases, run /update beta enabled:true."
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

    payload, error = _fetch_release_payload(
        repo,
        include_prerelease=include_prerelease,
    )
    if error:
        return UpdateCheckResult(
            current_version=current_version,
            latest_version=None,
            available=False,
            release=None,
            repo=repo,
            error=error,
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
    try:
        available = _is_newer_version(latest_version, current_version)
    except InvalidVersion as exc:
        return UpdateCheckResult(
            current_version=current_version,
            latest_version=latest_version,
            available=False,
            release=release,
            repo=repo,
            error=f"Could not compare release versions: {exc}",
        )
    return UpdateCheckResult(
        current_version=current_version,
        latest_version=latest_version,
        available=available,
        release=release,
        repo=repo,
        error=None,
    )


def _copy_release_tree(source_root: Path, target_root: Path) -> None:
    from mitra_bot.release_tools import allowed_file
    for src in source_root.rglob("*"):
        relative = src.relative_to(source_root)
        if not src.is_file() or not (allowed_file(relative.as_posix()) or relative.as_posix() == "release.json"):
            continue
        dst = target_root / relative
        if not dst.resolve().is_relative_to(target_root.resolve()):
            raise ValueError("Update target contains a path outside the installation")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _install_requirements() -> None:
    req = PROJECT_ROOT / "requirements.txt"
    pyproject = PROJECT_ROOT / "pyproject.toml"

    if pyproject.exists():
        uv = shutil.which("uv")
        if uv:
            cmd = [uv, "sync", "--no-dev", "--frozen", "--no-install-project"]
            subprocess.run(
                cmd,
                cwd=PROJECT_ROOT,
                check=True,
                text=True,
                capture_output=True,
            )
            return

        cmd = [sys.executable, "-m", "pip", "install", "-e", str(PROJECT_ROOT)]
        subprocess.run(
            cmd,
            cwd=PROJECT_ROOT,
            check=True,
            text=True,
            capture_output=True,
        )
        return

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


def _startup_preflight(version):
    # A fresh interpreter catches missing dependencies/import failures without
    # logging into Discord, starting monitoring, or executing hardware actions.
    code = (
        "from mitra_bot import __version__; "
        "from packaging.version import Version; "
        "import mitra_bot.main, mitra_bot.setup_wizard, mitra_bot.cloudflare_setup; "
        f"assert Version(__version__) == Version({version!r})"
    )
    subprocess.run([sys.executable, "-c", code], cwd=PROJECT_ROOT,
                   check=True, capture_output=True, text=True, timeout=60)


def install_release(release: ReleaseInfo) -> InstallResult:
    if not _INSTALL_LOCK.acquire(blocking=False):
        return InstallResult(ok=False, error="An update is already running on this process.")
    try:
        return _install_release(release)
    finally:
        _INSTALL_LOCK.release()


def _install_release(release: ReleaseInfo) -> InstallResult:
    try:
        with tempfile.TemporaryDirectory(prefix="mitra-update-") as tmpdir:
            tmp_path = Path(tmpdir)
            zip_path = tmp_path / "release.zip"

            dl = requests.get(release.zipball_url, timeout=60)
            dl.raise_for_status()
            expected_hash = release.sha256
            if not expected_hash and release.checksum_url:
                manifest = requests.get(release.checksum_url, timeout=20)
                manifest.raise_for_status()
                for line in manifest.text.splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == release.asset_name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                        expected_hash = parts[0].lower()
            if release.asset_name and not expected_hash:
                raise ValueError("Release asset is missing its SHA-256 checksum")
            if expected_hash and hashlib.sha256(dl.content).hexdigest() != expected_hash:
                raise ValueError("Release checksum does not match; no files were installed")
            zip_path.write_bytes(dl.content)

            extract_dir = tmp_path / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path) as zf:
                for entry in zf.infolist():
                    name = entry.filename
                    if "\\" in name or ":" in name or not (extract_dir / name).resolve().is_relative_to(extract_dir.resolve()):
                        raise ValueError("Unsafe path in release archive")
                    if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                        raise ValueError("Symlinks are not allowed in release archives")
                zf.extractall(extract_dir)

            roots = [p for p in extract_dir.iterdir() if p.is_dir()]
            if len(roots) != 1:
                return InstallResult(ok=False, error="Release archive must have exactly one root folder.")

            source_root = roots[0]
            if release.asset_name:
                from mitra_bot.release_tools import check
                check(source_root, release.version)
            from mitra_bot.services.update_recovery import Recovery
            recovery = Recovery(source_root, PROJECT_ROOT)
            dependencies_started = False
            try:
                recovery.record("copying")
                _copy_release_tree(source_root, PROJECT_ROOT)
                recovery.record("installing_dependencies")
                dependencies_started = True
                _install_requirements()
                recovery.record("checking_imports")
                _startup_preflight(release.version)
                recovery.record("validated")
            except Exception:
                recovery.failed_stage = recovery.stage
                try:
                    recovery.restore()
                except Exception:
                    return InstallResult(ok=False, error=f"Update failed and file restoration failed. Keep the bot stopped; recover from {recovery.folder}.")
                if dependencies_started:
                    try:
                        _install_requirements()
                    except Exception:
                        recovery.record("dependency_recovery_failed")
                        return InstallResult(ok=False, error=f"Update failed. Previous files restored, but dependencies need repair. Keep the bot stopped; recovery backup: {recovery.folder}.")
                recovery.record("rolled_back")
                return InstallResult(ok=False, error=f"Update failed during {recovery.failed_stage}; previous files restored. Dependency recovery was attempted when needed. Backup: {recovery.folder}.")

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
    cmd = [sys.executable, "-m", "mitra_bot.main"]
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
