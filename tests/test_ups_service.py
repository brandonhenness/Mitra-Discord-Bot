from __future__ import annotations

import unittest
from unittest.mock import Mock

from mitra_bot.services.ups.ups_service import UPSConfig, UPSService


def _config() -> UPSConfig:
    return UPSConfig(
        enabled=True,
        warn_time_to_empty_seconds=600,
        critical_time_to_empty_seconds=180,
        auto_shutdown_enabled=False,
        auto_shutdown_action="shutdown",
        auto_shutdown_delay_seconds=0,
        auto_shutdown_force=False,
    )


class UPSServiceTests(unittest.TestCase):
    def test_connected_ups_poll_processes_and_logs_status(self) -> None:
        client = Mock()
        client.available = True
        client.get_status.return_value = {
            "status": {
                "ac present": True,
                "charging": True,
                "discharging": False,
            },
            "input": {"voltage": 119.8, "frequency": 60.0},
            "output": {"voltage": 120.1, "power": 85.0},
            "health": 100,
            "time to empty": 1800,
        }
        log_store = Mock()
        service = UPSService(client=client, log_store=log_store, config=_config())

        event = service.poll()

        self.assertIsNone(event)
        client.get_status.assert_called_once_with()
        log_store.append.assert_called_once()
        row = log_store.append.call_args.args[0]
        self.assertTrue(row["ac_present"])
        self.assertFalse(row["on_battery"])
        self.assertEqual(row["time_to_empty_s"], 1800)
        self.assertEqual(row["input_v"], 119.8)
        self.assertEqual(row["output_w"], 85.0)

    def test_poll_emits_event_when_connected_ups_switches_to_battery(self) -> None:
        client = Mock()
        client.available = True
        client.get_status.side_effect = [
            {
                "status": {"ac present": True},
                "time to empty": 1800,
            },
            {
                "status": {"ac present": False},
                "time to empty": 900,
            },
        ]
        log_store = Mock()
        service = UPSService(client=client, log_store=log_store, config=_config())

        self.assertIsNone(service.poll())
        event = service.poll()

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.level, "warn")
        self.assertIn("switched to battery power", event.message)
        self.assertEqual(log_store.append.call_count, 2)


if __name__ == "__main__":
    unittest.main()
