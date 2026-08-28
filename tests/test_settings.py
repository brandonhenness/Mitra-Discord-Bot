from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from mitra_bot.settings import load_settings


class SettingsTests(unittest.TestCase):
    def test_load_settings_uses_discord_application_token(self) -> None:
        tmp = tempfile.mkdtemp()
        old_cwd = Path.cwd()
        os.chdir(tmp)
        old_token = os.environ.get("DISCORD_APPLICATION_TOKEN")
        old_config = os.environ.get("MITRA_CONFIG_PATH")
        try:
            os.environ["DISCORD_APPLICATION_TOKEN"] = "test-token"
            os.environ["MITRA_CONFIG_PATH"] = str(Path(tmp) / "config.toml")

            settings = load_settings(interactive_token=False)
            self.assertEqual(settings.token, "test-token")
            self.assertEqual(settings.ip_poll_seconds, 900)
            self.assertFalse(Path(os.environ["MITRA_CONFIG_PATH"]).exists())
        finally:
            if old_token is None:
                os.environ.pop("DISCORD_APPLICATION_TOKEN", None)
            else:
                os.environ["DISCORD_APPLICATION_TOKEN"] = old_token
            if old_config is None:
                os.environ.pop("MITRA_CONFIG_PATH", None)
            else:
                os.environ["MITRA_CONFIG_PATH"] = old_config
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_settings_requires_discord_application_token(self) -> None:
        tmp = tempfile.mkdtemp()
        old_cwd = Path.cwd()
        os.chdir(tmp)
        old_token = os.environ.get("DISCORD_APPLICATION_TOKEN")
        old_config = os.environ.get("MITRA_CONFIG_PATH")
        try:
            os.environ.pop("DISCORD_APPLICATION_TOKEN", None)
            os.environ["MITRA_CONFIG_PATH"] = str(Path(tmp) / "config.toml")
            with self.assertRaises(RuntimeError):
                load_settings(interactive_token=False)
        finally:
            if old_token is None:
                os.environ.pop("DISCORD_APPLICATION_TOKEN", None)
            else:
                os.environ["DISCORD_APPLICATION_TOKEN"] = old_token
            if old_config is None:
                os.environ.pop("MITRA_CONFIG_PATH", None)
            else:
                os.environ["MITRA_CONFIG_PATH"] = old_config
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_settings_reads_token_from_dotenv_file(self) -> None:
        tmp = tempfile.mkdtemp()
        old_cwd = Path.cwd()
        os.chdir(tmp)
        old_token = os.environ.get("DISCORD_APPLICATION_TOKEN")
        old_config = os.environ.get("MITRA_CONFIG_PATH")
        try:
            os.environ.pop("DISCORD_APPLICATION_TOKEN", None)
            os.environ["MITRA_CONFIG_PATH"] = str(Path(tmp) / "config.toml")
            Path(".env").write_text(
                "DISCORD_APPLICATION_TOKEN=dotenv-token\n",
                encoding="utf-8",
            )

            settings = load_settings(interactive_token=False)
            self.assertEqual(settings.token, "dotenv-token")
        finally:
            if old_token is None:
                os.environ.pop("DISCORD_APPLICATION_TOKEN", None)
            else:
                os.environ["DISCORD_APPLICATION_TOKEN"] = old_token
            if old_config is None:
                os.environ.pop("MITRA_CONFIG_PATH", None)
            else:
                os.environ["MITRA_CONFIG_PATH"] = old_config
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)

    def test_load_settings_reads_cloudflare_token_from_dotenv_file(self) -> None:
        tmp = tempfile.mkdtemp()
        old_cwd = Path.cwd()
        os.chdir(tmp)
        old_discord_token = os.environ.get("DISCORD_APPLICATION_TOKEN")
        old_cloudflare_token = os.environ.get("CLOUDFLARE_API_TOKEN")
        old_config = os.environ.get("MITRA_CONFIG_PATH")
        try:
            os.environ.pop("DISCORD_APPLICATION_TOKEN", None)
            os.environ.pop("CLOUDFLARE_API_TOKEN", None)
            os.environ["MITRA_CONFIG_PATH"] = str(Path(tmp) / "config.toml")
            Path(".env").write_text(
                "DISCORD_APPLICATION_TOKEN=dotenv-discord-token\n"
                "CLOUDFLARE_API_TOKEN=dotenv-cloudflare-token\n",
                encoding="utf-8",
            )

            settings = load_settings(interactive_token=False)

            self.assertEqual(
                settings.cloudflare_api_token,
                "dotenv-cloudflare-token",
            )
            self.assertNotIn("CLOUDFLARE_API_TOKEN", os.environ)
        finally:
            if old_discord_token is None:
                os.environ.pop("DISCORD_APPLICATION_TOKEN", None)
            else:
                os.environ["DISCORD_APPLICATION_TOKEN"] = old_discord_token
            if old_cloudflare_token is None:
                os.environ.pop("CLOUDFLARE_API_TOKEN", None)
            else:
                os.environ["CLOUDFLARE_API_TOKEN"] = old_cloudflare_token
            if old_config is None:
                os.environ.pop("MITRA_CONFIG_PATH", None)
            else:
                os.environ["MITRA_CONFIG_PATH"] = old_config
            os.chdir(old_cwd)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
