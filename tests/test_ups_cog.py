from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock, patch

from mitra_bot.discord_app.cogs.ups_cog import UPSCog


class UPSCogAdminGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_denial_is_awaited_by_all_ups_commands(self) -> None:
        cog = object.__new__(UPSCog)

        command_calls = (
            (UPSCog.monitoring.callback, (True,)),
            (UPSCog.timezone.callback, ("UTC",)),
            (UPSCog.status.callback, (None,)),
        )

        for callback, args in command_calls:
            with self.subTest(command=callback.__name__):
                ctx = Mock()
                ctx.respond = AsyncMock()
                denial_response = AsyncMock()

                with patch(
                    "mitra_bot.discord_app.cogs.ups_cog.ensure_admin",
                    return_value=denial_response(),
                ):
                    await callback(cog, ctx, *args)

                denial_response.assert_awaited_once_with()
                ctx.respond.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
