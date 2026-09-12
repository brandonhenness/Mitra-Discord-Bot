from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]

from mitra_bot import migrate_legacy_cache
from mitra_bot.migrate_legacy_cache import (
    LegacyMigrationError,
    apply_migration,
    build_migration_plan,
)
from mitra_bot.storage.state_store import StateStore


LEGACY_DISCORD_TOKEN = "synthetic-legacy-discord-token-for-tests-only"
LEGACY_GLOBAL_API_KEY = "synthetic-obsolete-global-api-key-for-tests-only"
LEGACY_EMAIL = "legacy-cloudflare-user@example.invalid"
MODERN_DISCORD_TOKEN = "synthetic-modern-discord-token-for-tests-only"
MODERN_CLOUDFLARE_TOKEN = "synthetic-modern-cloudflare-token-for-tests-only"
LEGACY_ENV_DISCORD_TOKEN = "synthetic-legacy-env-discord-token-for-tests-only"


def _legacy_cache(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "token": LEGACY_DISCORD_TOKEN,
        "channel": 123456789012345678,
        "ip": "203.0.113.42",
        "ip_poll_seconds": 300,
        "admin_role_name": "Legacy Admin",
        "ip_subscriber_role_name": "Legacy IP Subscriber",
        "api_key": LEGACY_GLOBAL_API_KEY,
        "email": LEGACY_EMAIL,
        "zone_id": "synthetic-zone-id",
        "record_ids": ["synthetic-record-one", "synthetic-record-two"],
        "force_sync": False,
        "commands_fingerprint": "synthetic-command-fingerprint",
        "ups": {
            "enabled": True,
            "poll_seconds": 45,
            "warn_time_to_empty_seconds": 720,
            "critical_time_to_empty_seconds": 240,
            "auto_shutdown_enabled": True,
            "auto_shutdown_action": "shutdown",
            "auto_shutdown_delay_seconds": 30,
            "auto_shutdown_force": True,
            "log_enabled": True,
            "log_file": "synthetic-ups-stats.jsonl",
            "graph_default_hours": 12,
            "timezone": "America/Los_Angeles",
        },
    }
    data.update(overrides)
    return data


def _write_cache(path: Path, data: dict[str, Any]) -> bytes:
    payload = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(payload)
    return payload


def _read_state(path: Path) -> tuple[str, dict[str, Any]]:
    with closing(sqlite3.connect(path)) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        rows = conn.execute("SELECT key, value FROM state_kv ORDER BY key").fetchall()
    return integrity, {str(key): json.loads(str(value)) for key, value in rows}


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def test_cli_dry_run_makes_no_writes_and_does_not_print_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "cache.json"
    source_bytes = _write_cache(source, _legacy_cache())

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mitra-migrate-cache",
            "--source",
            str(source),
            "--root",
            str(tmp_path),
        ],
    )

    migrate_legacy_cache.main()

    output = capsys.readouterr().out
    assert "Legacy cache migration dry run." in output
    assert "No files were changed." in output
    assert LEGACY_DISCORD_TOKEN not in output
    assert LEGACY_GLOBAL_API_KEY not in output
    assert LEGACY_EMAIL not in output
    assert source.read_bytes() == source_bytes
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "state.db").exists()
    assert not (tmp_path.parent / f"{tmp_path.name}-migration-backups").exists()


def test_apply_maps_data_backs_up_inputs_and_preserves_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cache.json"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    state_path = tmp_path / "state.db"
    backup_path = tmp_path / "migration-backup"

    cache = _legacy_cache(
        admins=[1002, "1001", 1002],
        subscribers=[2002, "2001", 2002],
        notifications={
            "guild_channels": {
                "3001": 3002,
                "invalid-guild": 3003,
            }
        },
        updater={
            "enabled": False,
            "check_on_startup": False,
            "check_interval_seconds": 3600,
            "github_repo": "example/mitra-test",
        },
        power_restart_notice={
            "action": "restart",
            "channel_id": 4001,
            "guild_id": 4002,
            "message_id": 4003,
            "requested_by_user_id": 1001,
            "delay_seconds": 15,
            "force": False,
        },
        todo={"4002": [{"legacy_unused_task": True}]},
        todo_config={
            "guilds": {
                "4002": {
                    "category_id": 4100,
                    "hub_channel_id": 4101,
                    "hub_message_id": 4102,
                }
            },
            "lists": {
                "4200": {
                    "guild_id": 4002,
                    "board_message_id": 4201,
                    "tasks": [
                        {
                            "id": 1,
                            "title": "Synthetic migration task",
                            "notes": "No real user data",
                            "status": "in_progress",
                            "done": False,
                            "assignee_ids": [1001],
                            "assignee_id": 1001,
                            "thread_id": 4202,
                            "created_by": 1002,
                            "created_at": "2026-01-02T03:04:05+00:00",
                        }
                    ],
                }
            },
        },
    )
    source_bytes = _write_cache(source, cache)

    original_config = (
        '[bot]\nadmin_role_name = "Legacy Admin"\n\n'
        '[operator_notes]\nowner = "synthetic-test"\n'
    ).encode("utf-8")
    config_path.write_bytes(original_config)
    original_env = b"# Existing non-secret deployment setting\nMITRA_ENABLE_MEMBERS_INTENT=true\n"
    env_path.write_bytes(original_env)
    existing_store = StateStore(state_path)
    existing_store.set_json("existing_only", {"preserve": True})
    original_state = state_path.read_bytes()

    plan = build_migration_plan(
        source_path=source,
        config_path=config_path,
        env_path=env_path,
        state_path=state_path,
    )
    returned_backup = apply_migration(plan, backup_dir=backup_path)

    assert returned_backup == backup_path
    assert source.read_bytes() == source_bytes
    assert (backup_path / "cache.json").read_bytes() == source_bytes
    assert (backup_path / "config.toml").read_bytes() == original_config
    assert (backup_path / ".env").read_bytes() == original_env
    assert (backup_path / "state.db").read_bytes() == original_state

    manifest = json.loads((backup_path / "manifest.json").read_text(encoding="utf-8"))
    assert {entry["name"] for entry in manifest} == {
        "cache.json",
        "config.toml",
        ".env",
        "state.db",
    }
    assert all(entry["size"] > 0 for entry in manifest)
    assert all(len(entry["sha256"]) == 64 for entry in manifest)
    receipt_text = (backup_path / "migration-receipt.json").read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)
    assert receipt["source_sha256"] == plan.source_sha256
    assert "legacy Cloudflare Global API Key/email (obsolete; retained in backup)" in receipt[
        "skipped"
    ]
    assert "obsolete slash-command sync cache (retained in backup)" in receipt["skipped"]
    assert "stale power restart notice (transient; retained in backup)" in receipt[
        "skipped"
    ]

    config_text = config_path.read_text(encoding="utf-8")
    config = tomllib.loads(config_text)
    assert config["bot"] == {
        "admin_role_name": "Legacy Admin",
        "channel_id": 123456789012345678,
        "ip_poll_seconds": 300,
        "ip_subscriber_role_name": "Legacy IP Subscriber",
    }
    assert config["operator_notes"] == {"owner": "synthetic-test"}
    assert config["ups"] == cache["ups"]
    assert config["cloudflare"] == {
        "enabled": True,
        "zone_id": "synthetic-zone-id",
        "record_ids": ["synthetic-record-one", "synthetic-record-two"],
    }

    env = _env_values(env_path)
    assert env["DISCORD_APPLICATION_TOKEN"] == LEGACY_DISCORD_TOKEN
    assert env["MITRA_ENABLE_MEMBERS_INTENT"] == "true"
    assert "CLOUDFLARE_API_TOKEN" not in env

    integrity, state = _read_state(state_path)
    assert integrity.lower() == "ok"
    assert state["existing_only"] == {"preserve": True}
    assert state["ip"] == "203.0.113.42"
    assert state["admins"] == [1001, 1002]
    assert state["subscribers"] == [2001, 2002]
    assert state["notifications"]["guild_channels"] == {"3001": "3002"}
    assert state["updater"]["enabled"] is False
    assert state["updater"]["check_on_startup"] is False
    assert state["updater"]["check_interval_seconds"] == 3600
    assert "power_restart_notice" not in state
    assert state["todo"] == {"4002": [{"legacy_unused_task": True}]}
    task = state["todo_config"]["lists"]["4200"]["tasks"][0]
    assert task["title"] == "Synthetic migration task"
    assert task["assignee_ids"] == ["1001"]
    assert task["thread_id"] == "4202"

    backup_integrity, backup_state = _read_state(backup_path / "state.db")
    assert backup_integrity.lower() == "ok"
    assert backup_state == {"existing_only": {"preserve": True}}

    serialized_state = json.dumps(state, sort_keys=True)
    manifest_text = json.dumps(manifest, sort_keys=True)
    non_env_targets = "\n".join(
        (config_text, serialized_state, receipt_text, manifest_text)
    )
    assert LEGACY_DISCORD_TOKEN not in non_env_targets
    assert LEGACY_GLOBAL_API_KEY not in non_env_targets
    assert LEGACY_EMAIL not in non_env_targets
    assert LEGACY_GLOBAL_API_KEY not in env_path.read_text(encoding="utf-8")
    assert LEGACY_EMAIL not in env_path.read_text(encoding="utf-8")


def test_conflicting_config_and_state_refuse_without_writes(tmp_path: Path) -> None:
    source = tmp_path / "cache.json"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    state_path = tmp_path / "state.db"
    backup_path = tmp_path / "backup"
    source_bytes = _write_cache(source, _legacy_cache())
    config_path.write_text("[bot]\nchannel_id = 999\n", encoding="utf-8")
    env_path.write_text("EXISTING_VALUE=keep\n", encoding="utf-8")
    StateStore(state_path).set_json("ip", "198.51.100.7")

    before = {
        source: source.read_bytes(),
        config_path: config_path.read_bytes(),
        env_path: env_path.read_bytes(),
        state_path: state_path.read_bytes(),
    }

    with pytest.raises(LegacyMigrationError) as exc_info:
        build_migration_plan(
            source_path=source,
            config_path=config_path,
            env_path=env_path,
            state_path=state_path,
        )

    message = str(exc_info.value)
    assert "config.bot.channel_id" in message
    assert "state.ip" in message
    assert source.read_bytes() == source_bytes
    assert {path: path.read_bytes() for path in before} == before
    assert not backup_path.exists()


@pytest.mark.parametrize(
    ("cache_update", "message"),
    [
        ({"unmapped_top_level_value": "synthetic"}, "unknown keys"),
        ({"ups": {"unmapped_ups_value": 1}}, "unknown keys"),
    ],
)
def test_unknown_keys_refuse_without_creating_targets(
    tmp_path: Path,
    cache_update: dict[str, Any],
    message: str,
) -> None:
    source = tmp_path / "cache.json"
    data = _legacy_cache()
    data.update(cache_update)
    source_bytes = _write_cache(source, data)

    with pytest.raises(LegacyMigrationError, match=message):
        build_migration_plan(
            source_path=source,
            config_path=tmp_path / "config.toml",
            env_path=tmp_path / ".env",
            state_path=tmp_path / "state.db",
        )

    assert source.read_bytes() == source_bytes
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "state.db").exists()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("admins", {"unexpected": "object"}),
        ("subscribers", [1234, "not-an-integer-id"]),
    ],
)
def test_malformed_snowflake_lists_are_refused(
    tmp_path: Path,
    key: str,
    value: Any,
) -> None:
    source = tmp_path / "cache.json"
    _write_cache(source, _legacy_cache(**{key: value}))

    with pytest.raises(LegacyMigrationError, match=key):
        build_migration_plan(
            source_path=source,
            config_path=tmp_path / "config.toml",
            env_path=tmp_path / ".env",
            state_path=tmp_path / "state.db",
        )

    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "state.db").exists()


@pytest.mark.parametrize(
    ("channel_id", "expected"),
    [(None, 123456789012345678), (987654321098765432, 987654321098765432)],
)
def test_channel_id_uses_legacy_fallback_and_new_key_precedence(
    tmp_path: Path,
    channel_id: int | None,
    expected: int,
) -> None:
    source = tmp_path / "cache.json"
    _write_cache(source, _legacy_cache(channel_id=channel_id))

    plan = build_migration_plan(
        source_path=source,
        config_path=tmp_path / "config.toml",
        env_path=tmp_path / ".env",
        state_path=tmp_path / "state.db",
    )

    assert plan.config_data["bot"]["channel_id"] == expected


def test_existing_modern_env_secrets_are_preserved(tmp_path: Path) -> None:
    source = tmp_path / "cache.json"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    state_path = tmp_path / "state.db"
    backup_path = tmp_path / "backup"
    legacy_cloudflare_token = "synthetic-legacy-cloudflare-token-for-tests-only"
    _write_cache(
        source,
        _legacy_cache(
            cloudflare={
                "api_token": legacy_cloudflare_token,
                "zone_id": "synthetic-zone-id",
                "record_ids": ["synthetic-record-one"],
            }
        ),
    )
    original_env = (
        "# Preserve this deployment file and its modern credentials\n"
        f'export DISCORD_APPLICATION_TOKEN="{MODERN_DISCORD_TOKEN}"\n'
        f"CLOUDFLARE_API_TOKEN='{MODERN_CLOUDFLARE_TOKEN}'\n"
        "UNRELATED_SETTING=keep\n"
    )
    env_path.write_text(original_env, encoding="utf-8")

    plan = build_migration_plan(
        source_path=source,
        config_path=config_path,
        env_path=env_path,
        state_path=state_path,
    )
    apply_migration(plan, backup_dir=backup_path)

    env = _env_values(env_path)
    assert env["DISCORD_APPLICATION_TOKEN"] == MODERN_DISCORD_TOKEN
    assert env["CLOUDFLARE_API_TOKEN"] == MODERN_CLOUDFLARE_TOKEN
    assert env["UNRELATED_SETTING"] == "keep"
    assert LEGACY_DISCORD_TOKEN not in env_path.read_text(encoding="utf-8")
    assert legacy_cloudflare_token not in env_path.read_text(encoding="utf-8")
    assert "env.DISCORD_APPLICATION_TOKEN (preserved existing value)" in plan.unchanged
    assert "env.CLOUDFLARE_API_TOKEN (preserved existing value)" in plan.unchanged
    assert (backup_path / ".env").read_text(encoding="utf-8") == original_env


@pytest.mark.parametrize("legacy_key", ["MITRA_TOKEN", "DISCORD_TOKEN"])
def test_legacy_env_discord_token_is_promoted(
    tmp_path: Path,
    legacy_key: str,
) -> None:
    source = tmp_path / "cache.json"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    state_path = tmp_path / "state.db"
    backup_path = tmp_path / "backup"
    _write_cache(source, _legacy_cache(token=None))
    original_env = (
        "# Legacy deployment credential\n"
        f'{legacy_key}="{LEGACY_ENV_DISCORD_TOKEN}"\n'
        "UNRELATED_SETTING=keep\n"
    )
    env_path.write_text(original_env, encoding="utf-8")

    plan = build_migration_plan(
        source_path=source,
        config_path=config_path,
        env_path=env_path,
        state_path=state_path,
    )
    apply_migration(plan, backup_dir=backup_path)

    env = _env_values(env_path)
    assert env[legacy_key] == LEGACY_ENV_DISCORD_TOKEN
    assert env["DISCORD_APPLICATION_TOKEN"] == LEGACY_ENV_DISCORD_TOKEN
    assert env["UNRELATED_SETTING"] == "keep"
    assert "legacy Discord env key copied to DISCORD_APPLICATION_TOKEN" in plan.migrated
    assert (backup_path / ".env").read_text(encoding="utf-8") == original_env


def test_cli_root_defaults_source_and_selects_env_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "cache.json"
    source_bytes = _write_cache(source, _legacy_cache(token=None))
    production_env = tmp_path / ".env.production"
    production_env_text = (
        f"DISCORD_APPLICATION_TOKEN={MODERN_DISCORD_TOKEN}\n"
        f"CLOUDFLARE_API_TOKEN={MODERN_CLOUDFLARE_TOKEN}\n"
    )
    production_env.write_text(production_env_text, encoding="utf-8")

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mitra-migrate-cache",
            "--root",
            str(tmp_path),
        ],
    )

    migrate_legacy_cache.main()

    output = capsys.readouterr().out
    assert "Legacy cache migration dry run." in output
    assert f"Target env file: {production_env.resolve()}" in output
    assert LEGACY_DISCORD_TOKEN not in output
    assert MODERN_DISCORD_TOKEN not in output
    assert MODERN_CLOUDFLARE_TOKEN not in output
    assert source.read_bytes() == source_bytes
    assert production_env.read_text(encoding="utf-8") == production_env_text
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "state.db").exists()


def test_cli_refuses_ambiguous_env_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_cache(tmp_path / "cache.json", _legacy_cache())
    (tmp_path / ".env").write_text("FIRST=keep\n", encoding="utf-8")
    (tmp_path / ".env.production").write_text("SECOND=keep\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["mitra-migrate-cache", "--root", str(tmp_path)],
    )

    with pytest.raises(SystemExit) as exc_info:
        migrate_legacy_cache.main()

    assert exc_info.value.code == 2
    assert "pass --env-file explicitly" in capsys.readouterr().err
    assert not (tmp_path / "config.toml").exists()
    assert not (tmp_path / "state.db").exists()


@pytest.mark.parametrize(
    ("record_ids", "expected_ids", "expected_enabled"),
    [
        ([None, "", "  ", "synthetic-record"], ["synthetic-record"], True),
        ([None, "", "  "], [], False),
    ],
)
def test_cloudflare_record_normalization_skips_null_and_blank_values(
    tmp_path: Path,
    record_ids: list[Any],
    expected_ids: list[str],
    expected_enabled: bool,
) -> None:
    source = tmp_path / "cache.json"
    _write_cache(source, _legacy_cache(record_ids=record_ids))

    plan = build_migration_plan(
        source_path=source,
        config_path=tmp_path / "config.toml",
        env_path=tmp_path / ".env",
        state_path=tmp_path / "state.db",
    )

    assert plan.config_data["cloudflare"]["record_ids"] == expected_ids
    assert plan.config_data["cloudflare"]["enabled"] is expected_enabled


def test_nested_legacy_cloudflare_global_credentials_are_skipped(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cache.json"
    nested_global_key = "synthetic-nested-obsolete-global-key-for-tests-only"
    nested_email = "nested-legacy-cloudflare@example.invalid"
    _write_cache(
        source,
        _legacy_cache(
            api_key="",
            email="",
            cloudflare={
                "api_key": nested_global_key,
                "email": nested_email,
                "zone_id": "synthetic-nested-zone-id",
                "record_ids": ["synthetic-nested-record-id"],
            },
        ),
    )

    plan = build_migration_plan(
        source_path=source,
        config_path=tmp_path / "config.toml",
        env_path=tmp_path / ".env",
        state_path=tmp_path / "state.db",
    )

    assert (
        "legacy Cloudflare Global API Key/email (obsolete; retained in backup)"
        in plan.skipped
    )
    modern_targets = "\n".join(
        (
            json.dumps(plan.config_data, sort_keys=True),
            plan.env_text,
            json.dumps(plan.state_updates, sort_keys=True),
        )
    )
    assert nested_global_key not in modern_targets
    assert nested_email not in modern_targets
    assert plan.config_data["cloudflare"] == {
        "enabled": True,
        "zone_id": "synthetic-nested-zone-id",
        "record_ids": ["synthetic-nested-record-id"],
    }


def test_apply_failure_restores_all_targets_byte_for_byte(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "cache.json"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    state_path = tmp_path / "state.db"
    backup_path = tmp_path / "backup"
    source_bytes = _write_cache(source, _legacy_cache())
    config_path.write_text(
        '[bot]\nadmin_role_name = "Legacy Admin"\n',
        encoding="utf-8",
    )
    env_path.write_text("UNRELATED_SETTING=keep\n", encoding="utf-8")
    StateStore(state_path).set_json("existing_only", {"preserve": True})
    originals = {
        config_path: config_path.read_bytes(),
        env_path: env_path.read_bytes(),
        state_path: state_path.read_bytes(),
    }

    plan = build_migration_plan(
        source_path=source,
        config_path=config_path,
        env_path=env_path,
        state_path=state_path,
    )

    def fail_state_write(path: Path, updates: dict[str, Any]) -> None:
        assert path == state_path
        assert updates
        raise OSError("synthetic injected state write failure")

    monkeypatch.setattr(migrate_legacy_cache, "_write_state_updates", fail_state_write)

    with pytest.raises(
        LegacyMigrationError,
        match="original targets were restored",
    ):
        apply_migration(plan, backup_dir=backup_path)

    assert source.read_bytes() == source_bytes
    assert {path: path.read_bytes() for path in originals} == originals
    assert (backup_path / "config.toml").read_bytes() == originals[config_path]
    assert (backup_path / ".env").read_bytes() == originals[env_path]
    assert (backup_path / "state.db").read_bytes() == originals[state_path]
    assert not (backup_path / "migration-receipt.json").exists()
    integrity, state = _read_state(state_path)
    assert integrity.lower() == "ok"
    assert state == {"existing_only": {"preserve": True}}


def test_state_only_migration_does_not_rewrite_config_or_env(tmp_path: Path) -> None:
    source = tmp_path / "cache.json"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    state_path = tmp_path / "state.db"
    backup_path = tmp_path / "backup"
    _write_cache(source, {"ip": "203.0.113.77"})
    original_config = b'# preserve this comment\r\n[bot]\r\nchannel_id = 1234\r\n'
    original_env = b"# preserve CRLF and trailing blanks\r\nUNRELATED=keep\r\n\r\n"
    config_path.write_bytes(original_config)
    env_path.write_bytes(original_env)

    plan = build_migration_plan(
        source_path=source,
        config_path=config_path,
        env_path=env_path,
        state_path=state_path,
    )
    assert plan.config_changed is False
    assert plan.env_changed is False

    apply_migration(plan, backup_dir=backup_path)

    assert config_path.read_bytes() == original_config
    assert env_path.read_bytes() == original_env
    integrity, state = _read_state(state_path)
    assert integrity.lower() == "ok"
    assert state["ip"] == "203.0.113.77"


def test_aliasing_source_and_target_paths_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "cache.json"
    source_bytes = _write_cache(source, _legacy_cache())

    with pytest.raises(LegacyMigrationError, match="must all be distinct"):
        build_migration_plan(
            source_path=source,
            config_path=source,
            env_path=tmp_path / ".env",
            state_path=tmp_path / "state.db",
        )

    assert source.read_bytes() == source_bytes
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "state.db").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL behavior")
def test_windows_atomic_replacement_preserves_custom_dacls(tmp_path: Path) -> None:
    source = tmp_path / "cache.json"
    config_path = tmp_path / "config.toml"
    env_path = tmp_path / ".env"
    state_path = tmp_path / "state.db"
    _write_cache(source, _legacy_cache())
    config_path.write_text(
        '[bot]\nadmin_role_name = "Legacy Admin"\n',
        encoding="utf-8",
    )
    env_path.write_text("UNRELATED=keep\n", encoding="utf-8")

    restrict_acl = r"""
# Exercise this fixture without PowerShell module autoloading.
$PSModuleAutoLoadingPreference = "None"
$ErrorActionPreference = "Stop"
$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
# Change only the DACL under test. Assigning ownership can require privileges
# unavailable to the hosted runner even when it can edit file permissions.
# Direct .NET calls avoid PowerShell module discovery across pwsh/Windows
# PowerShell environments on hosted runners.
$security = [System.IO.File]::GetAccessControl($target, [System.Security.AccessControl.AccessControlSections]::Access)
$security.SetAccessRuleProtection($true, $false)
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $sid,
    [System.Security.AccessControl.FileSystemRights]::FullControl,
    [System.Security.AccessControl.AccessControlType]::Allow
)
[void]$security.AddAccessRule($rule)
[System.IO.File]::SetAccessControl($target, $security)
"""

    def powershell_literal(path: Path) -> str:
        return "'" + str(path).replace("'", "''") + "'"

    for path in (config_path, env_path):
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"$target = {powershell_literal(path)}\n{restrict_acl}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr

    before = {
        path: subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$PSModuleAutoLoadingPreference = 'None'; $ErrorActionPreference = 'Stop'; "
                f"[System.IO.File]::GetAccessControl({powershell_literal(path)}).GetSecurityDescriptorSddlForm("
                "[System.Security.AccessControl.AccessControlSections]::Access -bor "
                "[System.Security.AccessControl.AccessControlSections]::Owner -bor "
                "[System.Security.AccessControl.AccessControlSections]::Group)",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for path in (config_path, env_path)
    }

    plan = build_migration_plan(
        source_path=source,
        config_path=config_path,
        env_path=env_path,
        state_path=state_path,
    )
    apply_migration(plan, backup_dir=tmp_path / "backup")

    after = {
        path: subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$PSModuleAutoLoadingPreference = 'None'; $ErrorActionPreference = 'Stop'; "
                f"[System.IO.File]::GetAccessControl({powershell_literal(path)}).GetSecurityDescriptorSddlForm("
                "[System.Security.AccessControl.AccessControlSections]::Access -bor "
                "[System.Security.AccessControl.AccessControlSections]::Owner -bor "
                "[System.Security.AccessControl.AccessControlSections]::Group)",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        for path in (config_path, env_path)
    }
    assert after == before
