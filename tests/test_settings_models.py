from __future__ import annotations

import unittest

from mitra_bot.models.settings_models import AppSettingsModel


class SettingsModelTests(unittest.TestCase):
    def test_channel_fallback_and_coercion(self) -> None:
        parsed = AppSettingsModel.model_validate({"channel": "1474199874982510800"})
        self.assertEqual(parsed.resolved_channel_id, 1474199874982510800)

    def test_channel_id_precedence(self) -> None:
        parsed = AppSettingsModel.model_validate(
            {
                "channel": "111",
                "channel_id": "222",
            }
        )
        self.assertEqual(parsed.resolved_channel_id, 222)

    def test_ups_defaults(self) -> None:
        parsed = AppSettingsModel.model_validate({})
        self.assertTrue(parsed.ups.enabled)
        self.assertEqual(parsed.ups.poll_seconds, 30)


if __name__ == "__main__":
    unittest.main()
