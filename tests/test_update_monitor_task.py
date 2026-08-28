from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from mitra_bot.tasks.update_monitor_task import UpdateMonitorTask


class UpdateMonitorTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_periodic_check_waits_the_configured_interval(self) -> None:
        bot = Mock()
        bot.wait_until_ready = AsyncMock()
        monitor = UpdateMonitorTask(bot, interval_seconds=300)

        with patch(
            "mitra_bot.tasks.update_monitor_task.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            await monitor.before_loop()

        bot.wait_until_ready.assert_awaited_once_with()
        sleep.assert_awaited_once_with(300)


if __name__ == "__main__":
    unittest.main()
