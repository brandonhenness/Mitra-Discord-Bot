from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from mitra_bot.storage.storage_store import (
    read_storage_with_defaults,
    write_storage_json,
)
from mitra_bot.storage.config_store import ensure_config_file, get_config_path
from mitra_bot.storage.state_store import (
    get_state_path,
    get_state_store,
    reset_state_store_for_tests,
)


class ConfigStateTests(unittest.TestCase):
    def test_ensure_config_file_creates_defaults(self) -> None:
        tmp = tempfile.mkdtemp()
        old_cwd = Path.cwd()
        os.chdir(tmp)
        old_config = os.environ.get("MITRA_CONFIG_PATH")
        old_state = os.environ.get("MITRA_STATE_PATH")
        try:
            os.environ["MITRA_CONFIG_PATH"] = str(Path(tmp) / "config.toml")
            os.environ["MITRA_STATE_PATH"] = str(Path(tmp) / "state.db")
            cfg = ensure_config_file()
            self.assertTrue(get_config_path().exists())
            self.assertIn("bot", cfg)
            self.assertIn("ups", cfg)
            self.assertIn("cloudflare", cfg)
        finally:
            if old_config is None:
                os.environ.pop("MITRA_CONFIG_PATH", None)
            else:
                os.environ["MITRA_CONFIG_PATH"] = old_config
            if old_state is None:
                os.environ.pop("MITRA_STATE_PATH", None)
            else:
                os.environ["MITRA_STATE_PATH"] = old_state
            reset_state_store_for_tests()
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_storage_roundtrip_persists_to_state_db(self) -> None:
        tmp = tempfile.mkdtemp()
        old_cwd = Path.cwd()
        os.chdir(tmp)
        old_config = os.environ.get("MITRA_CONFIG_PATH")
        old_state = os.environ.get("MITRA_STATE_PATH")
        try:
            os.environ["MITRA_CONFIG_PATH"] = str(Path(tmp) / "config.toml")
            os.environ["MITRA_STATE_PATH"] = str(Path(tmp) / "state.db")
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
        finally:
            if old_config is None:
                os.environ.pop("MITRA_CONFIG_PATH", None)
            else:
                os.environ["MITRA_CONFIG_PATH"] = old_config
            if old_state is None:
                os.environ.pop("MITRA_STATE_PATH", None)
            else:
                os.environ["MITRA_STATE_PATH"] = old_state
            reset_state_store_for_tests()
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
