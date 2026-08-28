from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomli_w

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from mitra_bot.storage.config_store import FileConfigModel
from mitra_bot.storage.state_store import StateStore
from mitra_bot.storage.storage_schema import normalize_storage_data


class LegacyMigrationError(RuntimeError):
    """Raised when migration cannot continue without risking data loss."""


@dataclass
class MigrationPlan:
    source_path: Path
    source_sha256: str
    config_path: Path
    env_path: Path
    state_path: Path
    config_data: dict[str, Any]
    env_text: str
    state_updates: dict[str, Any]
    target_sha256: dict[Path, str | None]
    target_metadata: dict[Path, tuple[int, int, int] | None]
    config_changed: bool
    env_changed: bool
    migrated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


_ENV_ASSIGNMENT_RE = re.compile(
    r"^(?P<prefix>\s*(?:export\s+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=(?P<value>.*)$"
)

_UPS_KEYS = {
    "enabled",
    "poll_seconds",
    "warn_time_to_empty_seconds",
    "critical_time_to_empty_seconds",
    "auto_shutdown_enabled",
    "auto_shutdown_action",
    "auto_shutdown_delay_seconds",
    "auto_shutdown_force",
    "log_enabled",
    "log_file",
    "graph_default_hours",
    "timezone",
}

_CLOUDFLARE_KEYS = {
    "api_token",
    "api_key",
    "email",
    "zone_id",
    "record_ids",
    "enabled",
}

_KNOWN_LEGACY_KEYS = {
    "token",
    "api_token",
    "api_key",
    "email",
    "channel",
    "channel_id",
    "ip",
    "ip_poll_seconds",
    "admin_role_name",
    "ip_subscriber_role_name",
    "ups",
    "cloudflare",
    "enabled",
    "zone_id",
    "record_ids",
    "admins",
    "subscribers",
    "notifications",
    "updater",
    "power_restart_notice",
    "todo",
    "todo_config",
    "todo_channel_id",
    "todo_category_id",
    "todo_board_messages",
    "force_sync",
    "commands_fingerprint",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_legacy_cache(path: Path) -> tuple[dict[str, Any], str]:
    try:
        payload = path.read_bytes()
        raw = json.loads(payload.decode("utf-8"))
    except FileNotFoundError as exc:
        raise LegacyMigrationError(f"Legacy cache not found: {path}") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyMigrationError(f"Legacy cache is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise LegacyMigrationError("Legacy cache root must be a JSON object.")
    unknown = sorted(set(raw) - _KNOWN_LEGACY_KEYS)
    if unknown:
        raise LegacyMigrationError(
            "Legacy cache has unknown keys that require manual mapping: "
            + ", ".join(unknown)
        )
    return raw, hashlib.sha256(payload).hexdigest()


def _sha256_or_none(path: Path) -> str | None:
    return _sha256(path) if path.exists() else None


def _file_metadata(path: Path) -> tuple[int, int, int] | None:
    if not path.exists():
        return None
    metadata = path.stat()
    return (stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid)


def _state_paths(path: Path) -> tuple[Path, ...]:
    return (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    )


def _validate_path_layout(
    *,
    source_path: Path,
    config_path: Path,
    env_path: Path,
    state_path: Path,
) -> None:
    named_paths = {
        "source": source_path.resolve(),
        "config": config_path.resolve(),
        "env": env_path.resolve(),
        "state": state_path.resolve(),
    }
    if len(set(named_paths.values())) != len(named_paths):
        raise LegacyMigrationError(
            "Source, config, env, and state paths must all be distinct."
        )
    state_sidecars = {path.resolve() for path in _state_paths(state_path)[1:]}
    for label in ("source", "config", "env"):
        if named_paths[label] in state_sidecars:
            raise LegacyMigrationError(
                f"The {label} path cannot alias a SQLite state sidecar."
            )


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise LegacyMigrationError(f"Existing config is invalid TOML: {path}") from exc
    if not isinstance(raw, dict):
        raise LegacyMigrationError(f"Existing config root must be a TOML table: {path}")
    return raw


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise LegacyMigrationError(
                    f"Existing state database failed integrity check: {path}"
                )
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "state_kv" not in tables and tables:
                raise LegacyMigrationError(
                    f"Existing state database does not have Mitra's state_kv table: {path}"
                )
            if "state_kv" not in tables:
                return {}
            rows = conn.execute("SELECT key, value FROM state_kv").fetchall()
    except sqlite3.Error as exc:
        raise LegacyMigrationError(f"Could not read existing state database: {path}") from exc

    out: dict[str, Any] = {}
    for key, value in rows:
        try:
            out[str(key)] = json.loads(str(value))
        except json.JSONDecodeError as exc:
            raise LegacyMigrationError(
                f"Existing state key {key!r} does not contain valid JSON."
            ) from exc
    return out


def _merge_value(
    target: dict[str, Any],
    key: str,
    value: Any,
    *,
    path: str,
    prefer_legacy: bool,
    conflicts: list[str],
    migrated: list[str],
    unchanged: list[str],
) -> None:
    if key not in target:
        target[key] = value
        migrated.append(path)
        return
    if target[key] == value:
        unchanged.append(path)
        return
    if prefer_legacy:
        target[key] = value
        migrated.append(f"{path} (replaced existing value)")
        return
    conflicts.append(path)


def _unquote_env_value(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _get_env_value(text: str, key: str) -> str | None:
    found: str | None = None
    for line in text.splitlines():
        match = _ENV_ASSIGNMENT_RE.match(line)
        if not match or match.group("key") != key:
            continue
        if found is not None:
            raise LegacyMigrationError(f"Environment file defines {key} more than once.")
        found = _unquote_env_value(match.group("value"))
    return found


def _legacy_secret(legacy: dict[str, Any], key: str) -> str:
    value = legacy.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LegacyMigrationError(f"Legacy {key} value must be a string.")
    return value.strip()


def _coerce_bool(value: Any, *, path: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "on"}:
            return True
        if normalized in {"false", "no", "0", "off"}:
            return False
    raise LegacyMigrationError(f"Legacy {path} value is not a boolean.")


def _merge_env_value(
    text: str,
    key: str,
    value: str,
    *,
    prefer_legacy: bool,
    conflicts: list[str],
    migrated: list[str],
    unchanged: list[str],
) -> str:
    if "\r" in value or "\n" in value:
        raise LegacyMigrationError(f"Legacy value for {key} contains a newline.")
    lines = text.splitlines()
    found_index: int | None = None
    current = ""
    for index, line in enumerate(lines):
        match = _ENV_ASSIGNMENT_RE.match(line)
        if match and match.group("key") == key:
            if found_index is not None:
                raise LegacyMigrationError(f"Environment file defines {key} more than once.")
            found_index = index
            current = _unquote_env_value(match.group("value"))

    path = f"env.{key}"
    if found_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key}={value}")
        migrated.append(path)
    elif not current or current == value or prefer_legacy:
        if current == value:
            unchanged.append(path)
        else:
            lines[found_index] = f"{key}={value}"
            migrated.append(path)
    else:
        unchanged.append(f"{path} (preserved existing value)")
    return "\n".join(lines).rstrip() + "\n"


def _normalize_snowflake_list(value: Any, *, path: str) -> list[int]:
    if not isinstance(value, list):
        raise LegacyMigrationError(f"Legacy {path} value is not an array.")
    out: list[int] = []
    for index, item in enumerate(value):
        try:
            out.append(int(item))
        except (TypeError, ValueError) as exc:
            raise LegacyMigrationError(
                f"Legacy {path}[{index}] is not an integer ID."
            ) from exc
    return sorted(set(out))


def build_migration_plan(
    *,
    source_path: Path,
    config_path: Path,
    env_path: Path,
    state_path: Path,
    prefer_legacy: bool = False,
) -> MigrationPlan:
    _validate_path_layout(
        source_path=source_path,
        config_path=config_path,
        env_path=env_path,
        state_path=state_path,
    )
    legacy, source_sha256 = _load_legacy_cache(source_path)
    config_sha256 = _sha256_or_none(config_path)
    config = _load_config(config_path)
    if _sha256_or_none(config_path) != config_sha256:
        raise LegacyMigrationError("Existing config changed while it was being read.")
    env_sha256 = _sha256_or_none(env_path)
    try:
        env_text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    except UnicodeError as exc:
        raise LegacyMigrationError(f"Existing env file is not valid UTF-8: {env_path}") from exc
    if _sha256_or_none(env_path) != env_sha256:
        raise LegacyMigrationError("Existing env file changed while it was being read.")
    state_observed_paths = (state_path, Path(f"{state_path}-wal"), Path(f"{state_path}-journal"))
    state_sha256 = {path: _sha256_or_none(path) for path in state_observed_paths}
    existing_state = _read_state(state_path)
    if any(
        _sha256_or_none(path) != expected
        for path, expected in state_sha256.items()
    ):
        raise LegacyMigrationError("Existing state database changed while it was being read.")
    state_sha256[Path(f"{state_path}-shm")] = _sha256_or_none(
        Path(f"{state_path}-shm")
    )

    migrated: list[str] = []
    unchanged: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []
    conflicts: list[str] = []

    channel_value = legacy.get("channel_id")
    if channel_value is None:
        channel_value = legacy.get("channel")
    legacy_bot_keys = ("ip_poll_seconds", "admin_role_name", "ip_subscriber_role_name")
    bot: dict[str, Any] | None = None
    if channel_value is not None or any(key in legacy for key in legacy_bot_keys):
        raw_bot = config.setdefault("bot", {})
        if not isinstance(raw_bot, dict):
            raise LegacyMigrationError("Existing [bot] config is not a table.")
        bot = raw_bot
    if channel_value is not None:
        try:
            channel_value = int(channel_value)
        except (TypeError, ValueError) as exc:
            raise LegacyMigrationError("Legacy channel ID is not an integer.") from exc
        _merge_value(
            bot,
            "channel_id",
            channel_value,
            path="config.bot.channel_id",
            prefer_legacy=prefer_legacy,
            conflicts=conflicts,
            migrated=migrated,
            unchanged=unchanged,
        )

    for key in legacy_bot_keys:
        if key in legacy:
            if bot is None:  # pragma: no cover - guarded by legacy_bot_keys
                raise LegacyMigrationError("Could not prepare [bot] config.")
            _merge_value(
                bot,
                key,
                legacy[key],
                path=f"config.bot.{key}",
                prefer_legacy=prefer_legacy,
                conflicts=conflicts,
                migrated=migrated,
                unchanged=unchanged,
            )

    legacy_ups = legacy.get("ups")
    if legacy_ups is not None and not isinstance(legacy_ups, dict):
        raise LegacyMigrationError("Legacy ups value is not an object.")
    if isinstance(legacy_ups, dict):
        unknown_ups = sorted(set(legacy_ups) - _UPS_KEYS)
        if unknown_ups:
            raise LegacyMigrationError(
                "Legacy UPS config has unknown keys: " + ", ".join(unknown_ups)
            )
        ups = config.setdefault("ups", {})
        if not isinstance(ups, dict):
            raise LegacyMigrationError("Existing [ups] config is not a table.")
        for key in sorted(legacy_ups):
            _merge_value(
                ups,
                key,
                legacy_ups[key],
                path=f"config.ups.{key}",
                prefer_legacy=prefer_legacy,
                conflicts=conflicts,
                migrated=migrated,
                unchanged=unchanged,
            )

    nested_cloudflare = legacy.get("cloudflare")
    if nested_cloudflare is not None and not isinstance(nested_cloudflare, dict):
        raise LegacyMigrationError("Legacy cloudflare value is not an object.")
    nested_cloudflare = nested_cloudflare if isinstance(nested_cloudflare, dict) else {}
    unknown_cloudflare = sorted(set(nested_cloudflare) - _CLOUDFLARE_KEYS)
    if unknown_cloudflare:
        raise LegacyMigrationError(
            "Legacy Cloudflare config has unknown keys: "
            + ", ".join(unknown_cloudflare)
        )
    zone_id = nested_cloudflare.get("zone_id", legacy.get("zone_id"))
    record_ids = nested_cloudflare.get("record_ids", legacy.get("record_ids"))
    explicit_enabled = nested_cloudflare.get("enabled", legacy.get("enabled"))
    normalized_record_ids: list[str] | None = None
    if record_ids is not None:
        if not isinstance(record_ids, list):
            raise LegacyMigrationError("Legacy Cloudflare record_ids is not an array.")
        normalized_record_ids = [
            str(item).strip()
            for item in record_ids
            if item is not None and str(item).strip()
        ]
    cloudflare_enabled = (
        _coerce_bool(explicit_enabled, path="Cloudflare enabled")
        if explicit_enabled is not None
        else bool(str(zone_id or "").strip() and normalized_record_ids)
    )
    has_cloudflare_config = (
        zone_id is not None or record_ids is not None or explicit_enabled is not None
    )
    cloudflare: dict[str, Any] | None = None
    if has_cloudflare_config:
        raw_cloudflare = config.setdefault("cloudflare", {})
        if not isinstance(raw_cloudflare, dict):
            raise LegacyMigrationError("Existing [cloudflare] config is not a table.")
        cloudflare = raw_cloudflare
    if zone_id is not None and cloudflare is not None:
        _merge_value(
            cloudflare,
            "zone_id",
            str(zone_id).strip(),
            path="config.cloudflare.zone_id",
            prefer_legacy=prefer_legacy,
            conflicts=conflicts,
            migrated=migrated,
            unchanged=unchanged,
        )
    if record_ids is not None and cloudflare is not None:
        _merge_value(
            cloudflare,
            "record_ids",
            normalized_record_ids,
            path="config.cloudflare.record_ids",
            prefer_legacy=prefer_legacy,
            conflicts=conflicts,
            migrated=migrated,
            unchanged=unchanged,
        )
    if has_cloudflare_config and cloudflare is not None:
        _merge_value(
            cloudflare,
            "enabled",
            cloudflare_enabled,
            path="config.cloudflare.enabled",
            prefer_legacy=prefer_legacy,
            conflicts=conflicts,
            migrated=migrated,
            unchanged=unchanged,
        )

    cache_discord_token = _legacy_secret(legacy, "token")
    modern_discord_token = (
        _get_env_value(env_text, "DISCORD_APPLICATION_TOKEN") or ""
    ).strip()
    legacy_env_tokens = {
        key: value
        for key in ("MITRA_TOKEN", "DISCORD_TOKEN")
        if (value := (_get_env_value(env_text, key) or "").strip())
    }
    if len(set(legacy_env_tokens.values())) > 1:
        raise LegacyMigrationError(
            "MITRA_TOKEN and DISCORD_TOKEN contain different values; choose one before migration."
        )
    legacy_env_token = next(iter(legacy_env_tokens.values()), "")
    selected_discord_token = modern_discord_token or legacy_env_token or cache_discord_token
    merge_discord_token = (
        cache_discord_token or legacy_env_token or modern_discord_token
        if modern_discord_token
        else selected_discord_token
    )
    if merge_discord_token:
        env_text = _merge_env_value(
            env_text,
            "DISCORD_APPLICATION_TOKEN",
            merge_discord_token,
            prefer_legacy=prefer_legacy,
            conflicts=conflicts,
            migrated=migrated,
            unchanged=unchanged,
        )
        if legacy_env_token and not modern_discord_token:
            migrated.append("legacy Discord env key copied to DISCORD_APPLICATION_TOKEN")
    else:
        warnings.append("No Discord token was found in the legacy cache or target env file.")

    raw_legacy_api_token = nested_cloudflare.get("api_token", legacy.get("api_token"))
    if raw_legacy_api_token is not None and not isinstance(raw_legacy_api_token, str):
        raise LegacyMigrationError("Legacy api_token value must be a string.")
    legacy_api_token = str(raw_legacy_api_token or "").strip()
    if legacy_api_token:
        env_text = _merge_env_value(
            env_text,
            "CLOUDFLARE_API_TOKEN",
            legacy_api_token,
            prefer_legacy=prefer_legacy,
            conflicts=conflicts,
            migrated=migrated,
            unchanged=unchanged,
        )
    elif not (_get_env_value(env_text, "CLOUDFLARE_API_TOKEN") or "").strip():
        warnings.append(
            "Set CLOUDFLARE_API_TOKEN in the server env file before starting Mitra."
        )

    if (
        legacy.get("api_key")
        or legacy.get("email")
        or nested_cloudflare.get("api_key")
        or nested_cloudflare.get("email")
    ):
        skipped.append("legacy Cloudflare Global API Key/email (obsolete; retained in backup)")
    if "force_sync" in legacy or "commands_fingerprint" in legacy:
        skipped.append("obsolete slash-command sync cache (retained in backup)")
    if any(
        key in legacy
        for key in ("todo_channel_id", "todo_category_id", "todo_board_messages")
    ):
        skipped.append("legacy top-level todo layout (manual review; retained in backup)")
        warnings.append(
            "Legacy top-level todo fields need manual review and were not activated."
        )
    if "power_restart_notice" in legacy:
        skipped.append("stale power restart notice (transient; retained in backup)")

    normalized = normalize_storage_data(legacy)
    state_updates: dict[str, Any] = {}
    state_presence = {
        "admins": "admins" in legacy,
        "subscribers": "subscribers" in legacy,
        "ip": "ip" in legacy,
        "notifications": "notifications" in legacy,
        "updater": "updater" in legacy,
        "power_restart_notice": False,
        "todo": "todo" in legacy,
        "todo_config": "todo_config" in legacy,
    }
    for key, present in state_presence.items():
        if not present:
            continue
        value = normalized.get(key)
        if key in {"admins", "subscribers"}:
            value = _normalize_snowflake_list(legacy.get(key), path=key)
        if key == "ip" and value is not None:
            value = str(value).strip()
        if key in existing_state and existing_state[key] != value and not prefer_legacy:
            conflicts.append(f"state.{key}")
            continue
        if key in existing_state and existing_state[key] == value:
            unchanged.append(f"state.{key}")
            continue
        state_updates[key] = value
        migrated.append(f"state.{key}")

    try:
        FileConfigModel.model_validate(config)
    except Exception as exc:
        raise LegacyMigrationError(f"Migrated config would be invalid: {exc}") from exc

    if conflicts:
        raise LegacyMigrationError(
            "Migration found conflicting existing values and changed nothing: "
            + ", ".join(sorted(conflicts))
            + ". Make an independent backup and review every conflict before using "
            "--prefer-legacy."
        )

    target_paths = (config_path, env_path, *_state_paths(state_path))
    return MigrationPlan(
        source_path=source_path,
        source_sha256=source_sha256,
        config_path=config_path,
        env_path=env_path,
        state_path=state_path,
        config_data=config,
        env_text=env_text,
        state_updates=state_updates,
        target_sha256={
            config_path: config_sha256,
            env_path: env_sha256,
            **state_sha256,
        },
        target_metadata={path: _file_metadata(path) for path in target_paths},
        config_changed=any(item.startswith("config.") for item in migrated),
        env_changed=any(item.startswith("env.") for item in migrated),
        migrated=migrated,
        unchanged=unchanged,
        skipped=skipped,
        warnings=warnings,
    )


def _atomic_write_text(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_stat = path.stat() if path.exists() else None
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(contents)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        if existing_stat is not None:
            if hasattr(os, "chown"):
                os.chown(temp_path, existing_stat.st_uid, existing_stat.st_gid)
            temp_path.chmod(stat.S_IMODE(existing_stat.st_mode))
        _atomic_replace(temp_path, path)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _atomic_replace(source: Path, destination: Path) -> None:
    if os.name != "nt" or not destination.exists():
        os.replace(source, destination)
        return

    # ReplaceFileW preserves the destination's DACL and other filesystem
    # metadata. Unlike MoveFileEx/os.replace, this keeps Windows service-account
    # access intact while still replacing the file atomically.
    import ctypes

    replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    replace_file.restype = ctypes.c_int
    ctypes.set_last_error(0)
    replaced = replace_file(
        str(destination),
        str(source),
        None,
        0,
        None,
        None,
    )
    if not replaced:
        error_code = ctypes.get_last_error()
        raise OSError(error_code, ctypes.FormatError(error_code), str(destination))


def _atomic_copy(
    source: Path,
    destination: Path,
    *,
    metadata: tuple[int, int, int] | None,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".restore",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
        shutil.copy2(source, temp_path)
        if metadata is not None:
            mode, uid, gid = metadata
            if hasattr(os, "chown"):
                os.chown(temp_path, uid, gid)
            temp_path.chmod(mode)
        _atomic_replace(temp_path, destination)
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def _find_git_root(path: Path) -> Path | None:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _backup_candidates(plan: MigrationPlan) -> list[tuple[str, Path]]:
    return [
        ("cache.json", plan.source_path),
        (".env", plan.env_path),
        ("config.toml", plan.config_path),
        ("state.db", plan.state_path),
        ("state.db-wal", Path(f"{plan.state_path}-wal")),
        ("state.db-shm", Path(f"{plan.state_path}-shm")),
        ("state.db-journal", Path(f"{plan.state_path}-journal")),
    ]


def _create_backup(plan: MigrationPlan, backup_dir: Path) -> Path:
    backup_dir = backup_dir.resolve()
    git_root = _find_git_root(backup_dir.parent)
    if git_root is not None and backup_dir.is_relative_to(git_root):
        recovery_root = (git_root / ".recovery").resolve()
        if not backup_dir.is_relative_to(recovery_root):
            raise LegacyMigrationError(
                "A backup inside the Git worktree must be placed under .recovery."
            )
    if backup_dir.exists():
        if not backup_dir.is_dir() or any(backup_dir.iterdir()):
            raise LegacyMigrationError(f"Backup directory is not empty: {backup_dir}")
    else:
        backup_dir.mkdir(parents=True, mode=0o700)
    if os.name != "nt":
        backup_dir.chmod(0o700)

    manifest: list[dict[str, Any]] = []
    for backup_name, source in _backup_candidates(plan):
        if not source.exists():
            continue
        destination = backup_dir / backup_name
        shutil.copy2(source, destination)
        if os.name != "nt":
            destination.chmod(0o600)
        manifest.append(
            {
                "name": backup_name,
                "source": str(source.resolve()),
                "size": destination.stat().st_size,
                "sha256": _sha256(destination),
            }
        )
    if not any(item["name"] == "cache.json" for item in manifest):
        raise LegacyMigrationError("Backup did not contain the legacy cache; refusing to write.")
    _atomic_write_text(
        backup_dir / "manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return backup_dir


def _assert_inputs_unchanged(plan: MigrationPlan) -> None:
    if _sha256_or_none(plan.source_path) != plan.source_sha256:
        raise LegacyMigrationError(
            "Legacy cache changed after planning; retry with the bot stopped."
        )
    changed = [
        str(path)
        for path, expected in plan.target_sha256.items()
        if _sha256_or_none(path) != expected
    ]
    if changed:
        raise LegacyMigrationError(
            "Migration targets changed after planning; retry with the bot stopped: "
            + ", ".join(changed)
        )
    metadata_changed = [
        str(path)
        for path, expected in plan.target_metadata.items()
        if _file_metadata(path) != expected
    ]
    if metadata_changed:
        raise LegacyMigrationError(
            "Migration target metadata changed after planning; retry with the bot stopped: "
            + ", ".join(metadata_changed)
        )


def _write_state_updates(path: Path, updates: dict[str, Any]) -> None:
    if not updates:
        return
    payloads = [
        (key, json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        for key, value in updates.items()
    ]
    StateStore(path)
    with closing(sqlite3.connect(str(path))) as conn:
        conn.executemany(
            """
            INSERT INTO state_kv(key, value)
            VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            payloads,
        )
        conn.commit()


def _restore_targets(
    plan: MigrationPlan,
    backup_path: Path,
    written_targets: set[str],
) -> None:
    file_targets: list[tuple[Path, Path]] = []
    if "config" in written_targets:
        file_targets.append((plan.config_path, backup_path / "config.toml"))
    if "env" in written_targets:
        file_targets.append((plan.env_path, backup_path / ".env"))
    for target, backup in file_targets:
        if backup.exists():
            _atomic_copy(
                backup,
                target,
                metadata=plan.target_metadata.get(target),
            )
        else:
            target.unlink(missing_ok=True)

    if "state" in written_targets:
        for backup_name, target in _backup_candidates(plan)[3:]:
            backup = backup_path / backup_name
            if backup.exists():
                _atomic_copy(
                    backup,
                    target,
                    metadata=plan.target_metadata.get(target),
                )
            else:
                target.unlink(missing_ok=True)


def apply_migration(plan: MigrationPlan, *, backup_dir: Path) -> Path:
    backup_path = _create_backup(plan, backup_dir)
    _assert_inputs_unchanged(plan)

    written_targets: set[str] = set()
    try:
        if plan.config_changed:
            written_targets.add("config")
            _atomic_write_text(plan.config_path, tomli_w.dumps(plan.config_data))
        if plan.env_changed:
            written_targets.add("env")
            _atomic_write_text(plan.env_path, plan.env_text)
            if os.name != "nt":
                plan.env_path.chmod(0o600)
        if plan.state_updates:
            written_targets.add("state")
            _write_state_updates(plan.state_path, plan.state_updates)

        post_config = _load_config(plan.config_path)
        FileConfigModel.model_validate(post_config)
        if post_config != plan.config_data:
            raise LegacyMigrationError("Post-migration config verification failed.")
        if plan.env_changed and plan.env_path.read_text(encoding="utf-8") != plan.env_text:
            raise LegacyMigrationError("Post-migration environment verification failed.")
        post_state = _read_state(plan.state_path)
        for key, expected in plan.state_updates.items():
            if post_state.get(key) != expected:
                raise LegacyMigrationError(
                    f"Post-migration verification failed for state.{key}."
                )
        if _sha256_or_none(plan.source_path) != plan.source_sha256:
            raise LegacyMigrationError(
                "Legacy cache changed during migration; keep the backup and retry."
            )

        receipt = {
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "source_sha256": plan.source_sha256,
            "migrated": sorted(plan.migrated),
            "unchanged": sorted(plan.unchanged),
            "skipped": sorted(plan.skipped),
            "warnings": sorted(plan.warnings),
        }
        _atomic_write_text(
            backup_path / "migration-receipt.json",
            json.dumps(receipt, indent=2, sort_keys=True) + "\n",
        )
        return backup_path
    except BaseException as exc:
        if written_targets:
            try:
                _restore_targets(plan, backup_path, written_targets)
            except Exception as restore_exc:
                raise LegacyMigrationError(
                    "Migration failed and automatic restoration also failed. "
                    f"Keep the bot stopped and restore from {backup_path}: {restore_exc}"
                ) from exc
        raise LegacyMigrationError(
            f"Migration failed; original targets were restored from {backup_path}: {exc}"
        ) from exc


def _default_backup_dir(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return root.parent / f"{root.name}-migration-backups" / stamp


def _print_plan(plan: MigrationPlan, *, applied: bool, backup_dir: Path | None) -> None:
    print("Legacy cache migration " + ("completed." if applied else "dry run."))
    print(f"Source SHA256: {plan.source_sha256}")
    print(f"Target env file: {plan.env_path}")
    for label, items in (
        ("Migrated", plan.migrated),
        ("Already current", plan.unchanged),
        ("Not migrated", plan.skipped),
        ("Warnings", plan.warnings),
    ):
        if not items:
            continue
        print(f"{label}:")
        for item in sorted(items):
            print(f"  - {item}")
    if backup_dir is not None:
        print(f"Backup: {backup_dir}")
    if not applied:
        print("No files were changed. Re-run with --apply --confirm-bot-stopped.")


def _resolve_from_root(root: Path, value: Path | None, default_name: str) -> Path:
    selected = value if value is not None else Path(default_name)
    if not selected.is_absolute():
        selected = root / selected
    return selected.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate legacy cache.json into env/config.toml/state.db safely."
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="Legacy JSON path (default: cache.json under --root).",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-bot-stopped", action="store_true")
    parser.add_argument(
        "--prefer-legacy",
        action="store_true",
        help="Replace conflicting modern values with legacy values after backup.",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    source = _resolve_from_root(root, args.source, "cache.json")
    development_env = root / ".env"
    production_env = root / ".env.production"
    if args.env_file is None and development_env.exists() and production_env.exists():
        parser.error("both .env and .env.production exist; pass --env-file explicitly")
    if args.env_file is None and not development_env.exists() and production_env.exists():
        env_path = production_env.resolve()
    else:
        env_path = _resolve_from_root(root, args.env_file, ".env")
    config_path = _resolve_from_root(root, args.config, "config.toml")
    state_path = _resolve_from_root(root, args.state, "state.db")

    if args.apply and not args.confirm_bot_stopped:
        parser.error("--apply requires --confirm-bot-stopped")

    try:
        plan = build_migration_plan(
            source_path=source,
            config_path=config_path,
            env_path=env_path,
            state_path=state_path,
            prefer_legacy=bool(args.prefer_legacy),
        )
        backup_dir: Path | None = None
        if args.apply:
            backup_dir = (
                _resolve_from_root(root, args.backup_dir, ".recovery/migration")
                if args.backup_dir is not None
                else _default_backup_dir(root).resolve()
            )
            apply_migration(plan, backup_dir=backup_dir)
        _print_plan(plan, applied=bool(args.apply), backup_dir=backup_dir)
    except (LegacyMigrationError, OSError) as exc:
        parser.exit(2, f"Migration refused: {exc}\n")


if __name__ == "__main__":
    main()
