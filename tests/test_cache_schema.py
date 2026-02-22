from __future__ import annotations

import unittest

from mitra_bot.storage.cache_schema import (
    normalize_cache_data,
    normalize_notifications_patch,
    normalize_power_restart_notice_patch,
    normalize_updater_patch,
)


class CacheSchemaTests(unittest.TestCase):
    def test_normalize_cache_data_converts_snowflakes_to_strings(self) -> None:
        out = normalize_cache_data(
            {
                "notifications": {"guild_channels": {1246058921739817030: 1474199874982510800}},
                "todo_config": {
                    "guilds": {
                        "1246058921739817030": {
                            "category_id": 1474301060297265247,
                            "hub_channel_id": 1474301061996216451,
                            "hub_message_id": 1474301064864862207,
                        }
                    },
                    "lists": {
                        "1474301110725513308": {
                            "guild_id": 1246058921739817030,
                            "board_message_id": 1474301112394711050,
                            "tasks": [
                                {
                                    "id": 1,
                                    "title": "x",
                                    "assignee_ids": [269280664048631818],
                                    "assignee_id": 269280664048631818,
                                    "thread_id": 1474301184964558842,
                                    "created_by": 269280664048631818,
                                }
                            ],
                        }
                    },
                },
            }
        )

        self.assertEqual(
            out["notifications"]["guild_channels"]["1246058921739817030"],
            "1474199874982510800",
        )
        task = out["todo_config"]["lists"]["1474301110725513308"]["tasks"][0]
        self.assertEqual(task["assignee_id"], "269280664048631818")
        self.assertEqual(task["thread_id"], "1474301184964558842")

    def test_notifications_patch_normalizes_ids(self) -> None:
        out = normalize_notifications_patch({"guild_channels": {123: 456}})
        self.assertEqual(out, {"guild_channels": {"123": "456"}})

    def test_restart_notice_patch_normalizes_ids(self) -> None:
        out = normalize_power_restart_notice_patch(
            {
                "channel_id": 1,
                "guild_id": 2,
                "message_id": 3,
                "requested_by_user_id": 4,
                "confirmed_by_user_id": 5,
            }
        )
        self.assertEqual(out["channel_id"], "1")
        self.assertEqual(out["guild_id"], "2")
        self.assertEqual(out["message_id"], "3")
        self.assertEqual(out["requested_by_user_id"], "4")
        self.assertEqual(out["confirmed_by_user_id"], "5")

    def test_updater_defaults_exist(self) -> None:
        out = normalize_cache_data({})
        self.assertIn("updater", out)
        self.assertTrue(out["updater"]["enabled"])
        self.assertTrue(out["updater"]["check_on_startup"])
        self.assertEqual(out["updater"]["check_interval_seconds"], 21600)

    def test_updater_patch_roundtrip(self) -> None:
        out = normalize_updater_patch(
            {
                "enabled": False,
                "check_on_startup": False,
                "check_interval_seconds": 3600,
                "github_repo": "owner/repo",
                "last_notified_version": "v1.2.3",
            }
        )
        self.assertFalse(out["enabled"])
        self.assertFalse(out["check_on_startup"])
        self.assertEqual(out["check_interval_seconds"], 3600)
        self.assertEqual(out["github_repo"], "owner/repo")
        self.assertEqual(out["last_notified_version"], "v1.2.3")


if __name__ == "__main__":
    unittest.main()
