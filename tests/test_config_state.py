from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from mitra_bot.storage import storage_store
from mitra_bot.storage.storage_store import (
    get_ups_config,
    load_ip,
    load_subscribers,
    read_storage_with_defaults,
    set_notification_channel_id_for_guild,
    set_updater_config,
    write_storage_json,
)
from mitra_bot.storage.config_store import (
    ConfigFileError,
    ensure_config_file,
    get_config_path,
    read_config_dict,
    write_config_dict,
)
from mitra_bot.storage.state_store import (
    get_state_path,
    get_state_store,
    reset_state_store_for_tests,
)


class ConfigStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.old_cwd = Path.cwd()
        self.old_config = os.environ.get("MITRA_CONFIG_PATH")
        self.old_state = os.environ.get("MITRA_STATE_PATH")
        os.chdir(self.tmp)
        os.environ["MITRA_CONFIG_PATH"] = str(Path(self.tmp) / "config.toml")
        os.environ["MITRA_STATE_PATH"] = str(Path(self.tmp) / "state.db")
        reset_state_store_for_tests()

    def tearDown(self) -> None:
        if self.old_config is None:
            os.environ.pop("MITRA_CONFIG_PATH", None)
        else:
            os.environ["MITRA_CONFIG_PATH"] = self.old_config
        if self.old_state is None:
            os.environ.pop("MITRA_STATE_PATH", None)
        else:
            os.environ["MITRA_STATE_PATH"] = self.old_state
        reset_state_store_for_tests()
        os.chdir(self.old_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ensure_config_file_creates_defaults(self) -> None:
        cfg = ensure_config_file()
        self.assertTrue(get_config_path().exists())
        self.assertIn("bot", cfg)
        self.assertIn("ups", cfg)
        self.assertIn("cloudflare", cfg)

    def test_read_missing_config_returns_defaults_without_creating_file(self) -> None:
        cfg = read_config_dict()

        self.assertIn("bot", cfg)
        self.assertFalse(get_config_path().exists())

        snapshot = read_storage_with_defaults()
        self.assertEqual(snapshot["ip_poll_seconds"], 900)
        self.assertFalse(get_config_path().exists())

    def test_invalid_toml_raises_and_is_never_overwritten(self) -> None:
        path = get_config_path()
        invalid_contents = b"[bot\nip_poll_seconds = 60\n"
        path.write_bytes(invalid_contents)
        expected_path = re.escape(str(path))

        with self.assertRaisesRegex(ConfigFileError, expected_path):
            read_config_dict()
        with self.assertRaisesRegex(ConfigFileError, expected_path):
            ensure_config_file()
        with self.assertRaisesRegex(ConfigFileError, expected_path):
            write_config_dict({})
        with self.assertRaisesRegex(ConfigFileError, expected_path):
            load_subscribers()
        with self.assertRaisesRegex(ConfigFileError, expected_path):
            asyncio.run(load_ip())

        self.assertEqual(path.read_bytes(), invalid_contents)

    def test_invalid_config_values_are_never_overwritten(self) -> None:
        path = get_config_path()
        invalid_contents = b'[bot]\nip_poll_seconds = "never"\n'
        path.write_bytes(invalid_contents)

        with self.assertRaisesRegex(ConfigFileError, re.escape(str(path))):
            read_config_dict()
        with self.assertRaisesRegex(ConfigFileError, re.escape(str(path))):
            write_config_dict({"bot": {"ip_poll_seconds": 60}})

        self.assertEqual(path.read_bytes(), invalid_contents)

    def test_failed_atomic_replace_preserves_existing_config(self) -> None:
        path = get_config_path()
        write_config_dict({"bot": {"ip_poll_seconds": 60}})
        original_contents = path.read_bytes()

        with patch(
            "mitra_bot.storage.config_store.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated replace failure"):
                write_config_dict({"bot": {"ip_poll_seconds": 120}})

        self.assertEqual(path.read_bytes(), original_contents)
        self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_default_reads_do_not_rewrite_config_or_state(self) -> None:
        path = get_config_path()
        path.write_text(
            "# This comment proves the config was not regenerated.\n"
            "[bot]\n"
            "ip_poll_seconds = 60\n",
            encoding="utf-8",
        )
        store = get_state_store()
        store.set_json("ip", "203.0.113.5")
        original_config = path.read_bytes()
        original_state = store.read_all()

        snapshot = read_storage_with_defaults()
        ups = get_ups_config()

        self.assertEqual(snapshot["ip"], "203.0.113.5")
        self.assertTrue(ups["enabled"])
        self.assertEqual(path.read_bytes(), original_config)
        self.assertEqual(store.read_all(), original_state)

    def test_concurrent_mutations_preserve_unrelated_state(self) -> None:
        first_read_started = threading.Event()
        allow_first_write = threading.Event()
        second_read_started = threading.Event()
        call_count = 0
        call_count_lock = threading.Lock()
        errors: list[BaseException] = []
        original_read = storage_store.read_storage_with_defaults

        def delayed_read() -> dict:
            nonlocal call_count
            snapshot = original_read()
            with call_count_lock:
                call_count += 1
                current_call = call_count
            if current_call == 1:
                first_read_started.set()
                if not allow_first_write.wait(timeout=2):
                    raise TimeoutError("test did not release the first storage write")
            else:
                second_read_started.set()
            return snapshot

        def capture_errors(func, *args) -> None:
            try:
                func(*args)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch(
            "mitra_bot.storage.storage_store.read_storage_with_defaults",
            side_effect=delayed_read,
        ):
            updater_thread = threading.Thread(
                target=capture_errors,
                args=(set_updater_config, {"enabled": False}),
            )
            notification_thread = threading.Thread(
                target=capture_errors,
                args=(set_notification_channel_id_for_guild, 42, 99),
            )
            updater_thread.start()
            self.assertTrue(first_read_started.wait(timeout=1))
            notification_thread.start()
            try:
                self.assertFalse(second_read_started.wait(timeout=0.1))
            finally:
                allow_first_write.set()
                updater_thread.join(timeout=2)
                notification_thread.join(timeout=2)

        self.assertFalse(updater_thread.is_alive())
        self.assertFalse(notification_thread.is_alive())
        self.assertEqual(errors, [])
        snapshot = read_storage_with_defaults()
        self.assertFalse(snapshot["updater"]["enabled"])
        self.assertEqual(snapshot["notifications"]["guild_channels"]["42"], "99")

    def test_storage_roundtrip_persists_to_state_db(self) -> None:
        write_storage_json(
            {
                "channel_id": 123,
                "ip_poll_seconds": 60,
                "notifications": {"guild_channels": {"1": "2"}},
                "updater": {"enabled": False},
            }
        )
        out = read_storage_with_defaults()
        self.assertEqual(out["channel_id"], 123)
        self.assertEqual(out["ip_poll_seconds"], 60)
        self.assertEqual(out["notifications"]["guild_channels"]["1"], "2")
        self.assertFalse(out["updater"]["enabled"])
        self.assertTrue(get_config_path().exists())
        self.assertTrue(get_state_path().exists())
        self.assertEqual(get_state_store().get_meta("schema_version"), "1")


if __name__ == "__main__":
    unittest.main()
