from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from mitra_bot.discord_app.cogs.ip_cog import IPCog
from mitra_bot.tasks.ip_monitor_task import IPMonitorTask


class IPMonitorTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = Mock()
        self.monitor = IPMonitorTask(self.bot, interval_seconds=60)

    async def test_initial_ip_is_fetched_off_loop_and_persisted(self) -> None:
        event_loop_thread = threading.get_ident()
        worker_threads: list[int] = []
        self.monitor._update_cloudflare_dns = AsyncMock(return_value=0)

        def fake_get_public_ip() -> str:
            worker_threads.append(threading.get_ident())
            return "1.2.3.4"

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_public_ip",
                side_effect=fake_get_public_ip,
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.save_ip",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await self.monitor.loop()

        self.assertEqual(self.monitor._last_ip, "1.2.3.4")
        self.monitor._update_cloudflare_dns.assert_awaited_once_with("1.2.3.4")
        mock_save.assert_awaited_once_with("1.2.3.4")
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], event_loop_thread)
        self.bot.get_cog.assert_not_called()

    async def test_unchanged_stored_ip_reconciles_cloudflare_once(self) -> None:
        self.monitor._last_ip = "1.2.3.4"
        self.monitor._update_cloudflare_dns = AsyncMock(return_value=1)

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_public_ip",
                return_value="1.2.3.4",
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.save_ip",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await self.monitor.loop()
            await self.monitor.loop()

        self.monitor._update_cloudflare_dns.assert_awaited_once_with("1.2.3.4")
        mock_save.assert_not_awaited()
        self.bot.get_cog.assert_not_called()

    async def test_unchanged_stored_ip_retries_startup_reconciliation(self) -> None:
        self.monitor._last_ip = "1.2.3.4"
        self.monitor._update_cloudflare_dns = AsyncMock(
            side_effect=[RuntimeError("temporary failure"), 1]
        )

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_public_ip",
                return_value="1.2.3.4",
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.save_ip",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await self.monitor.loop()
            self.assertFalse(self.monitor._cloudflare_reconciled)
            await self.monitor.loop()

        self.assertTrue(self.monitor._cloudflare_reconciled)
        self.assertEqual(self.monitor._update_cloudflare_dns.await_count, 2)
        mock_save.assert_not_awaited()
        self.bot.get_cog.assert_not_called()

    async def test_initial_ip_is_not_persisted_until_cloudflare_succeeds(self) -> None:
        self.monitor._update_cloudflare_dns = AsyncMock(
            side_effect=RuntimeError("temporary failure")
        )

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_public_ip",
                return_value="1.2.3.4",
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.save_ip",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await self.monitor.loop()

        self.assertIsNone(self.monitor._last_ip)
        self.assertFalse(self.monitor._cloudflare_reconciled)
        mock_save.assert_not_awaited()
        self.bot.get_cog.assert_not_called()

    async def test_cloudflare_failure_keeps_old_ip_and_retries(self) -> None:
        self.monitor._last_ip = "1.1.1.1"
        self.monitor._update_cloudflare_dns = AsyncMock(
            side_effect=RuntimeError("Cloudflare unavailable")
        )

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_public_ip",
                return_value="2.2.2.2",
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.save_ip",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await self.monitor.loop()
            await self.monitor.loop()

        self.assertEqual(self.monitor._last_ip, "1.1.1.1")
        self.assertEqual(self.monitor._update_cloudflare_dns.await_count, 2)
        mock_save.assert_not_awaited()
        self.bot.get_cog.assert_not_called()

    async def test_notification_failure_does_not_duplicate_or_block_ip_commit(self) -> None:
        self.monitor._last_ip = "1.1.1.1"
        self.monitor._update_cloudflare_dns = AsyncMock(return_value=1)
        ip_cog = Mock()
        ip_cog.notify_ip_change = AsyncMock(return_value=False)
        self.bot.get_cog.return_value = ip_cog

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_public_ip",
                return_value="2.2.2.2",
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.save_ip",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await self.monitor.loop()
            await self.monitor.loop()

        self.assertEqual(self.monitor._last_ip, "2.2.2.2")
        self.monitor._update_cloudflare_dns.assert_awaited_once_with("2.2.2.2")
        ip_cog.notify_ip_change.assert_awaited_once_with("2.2.2.2")
        mock_save.assert_awaited_once_with("2.2.2.2")

    async def test_persist_failure_retries_before_sending_notification(self) -> None:
        self.monitor._last_ip = "1.1.1.1"
        self.monitor._update_cloudflare_dns = AsyncMock(return_value=1)
        ip_cog = Mock()
        ip_cog.notify_ip_change = AsyncMock(return_value=True)
        self.bot.get_cog.return_value = ip_cog

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_public_ip",
                return_value="2.2.2.2",
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.save_ip",
                new_callable=AsyncMock,
                side_effect=[OSError("disk unavailable"), None],
            ) as mock_save,
        ):
            await self.monitor.loop()
            self.assertEqual(self.monitor._last_ip, "1.1.1.1")
            ip_cog.notify_ip_change.assert_not_awaited()

            await self.monitor.loop()

        self.assertEqual(self.monitor._last_ip, "2.2.2.2")
        self.assertEqual(self.monitor._update_cloudflare_dns.await_count, 2)
        self.assertEqual(mock_save.await_count, 2)
        ip_cog.notify_ip_change.assert_awaited_once_with("2.2.2.2")


class CloudflareIPUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.monitor = IPMonitorTask(Mock(), interval_seconds=60)
        self.config = {
            "enabled": True,
            "zone_id": "zone-1",
            "record_ids": ["ipv4", "ipv6", "alias"],
            "api_token": "token-123",
        }

    async def test_ipv4_update_preserves_existing_record_options(self) -> None:
        records = [
            {
                "id": "ipv4",
                "type": "A",
                "name": "home.example.com",
                "content": "1.1.1.1",
                "ttl": 300,
                "proxied": True,
            },
            {
                "id": "ipv6",
                "type": "AAAA",
                "name": "home.example.com",
                "content": "2001:db8::1",
                "ttl": 1,
                "proxied": False,
            },
            {
                "id": "alias",
                "type": "CNAME",
                "name": "alias.example.com",
                "content": "home.example.com",
                "ttl": 1,
                "proxied": False,
            },
        ]
        updated_records = [
            {
                **records[0],
                "content": "5.6.7.8",
            }
        ]
        config = dict(self.config)
        config["record_ids"] = ["ipv4"]

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_cloudflare_config",
                return_value=config,
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.CloudflareService"
            ) as service_cls,
        ):
            service = service_cls.return_value
            service.get_dns_records.side_effect = [records, updated_records]
            updated = await self.monitor._update_cloudflare_dns("5.6.7.8")

        self.assertEqual(updated, 1)
        self.assertEqual(
            service.get_dns_records.call_args_list,
            [call("zone-1"), call("zone-1")],
        )
        self.assertEqual(
            service.update_dns_record.call_args_list,
            [
                call(
                    "zone-1",
                    "ipv4",
                    name="home.example.com",
                    record_type="A",
                    content="5.6.7.8",
                    ttl=300,
                    proxied=True,
                )
            ],
        )

    async def test_injected_dotenv_token_is_used_without_process_env(self) -> None:
        monitor = IPMonitorTask(
            Mock(),
            interval_seconds=60,
            cloudflare_api_token="dotenv-token",
        )
        config = dict(self.config)
        config.pop("api_token")
        config["record_ids"] = ["ipv4"]
        record = {
            "id": "ipv4",
            "type": "A",
            "name": "home.example.com",
            "content": "1.1.1.1",
            "ttl": 1,
            "proxied": False,
        }

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_cloudflare_config",
                return_value=config,
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.CloudflareService"
            ) as service_cls,
        ):
            service_cls.return_value.get_dns_records.side_effect = [
                [record],
                [{**record, "content": "5.6.7.8"}],
            ]
            await monitor._update_cloudflare_dns("5.6.7.8")

        service_cls.assert_called_once_with(api_token="dotenv-token")

    async def test_update_requires_matching_cloudflare_readback(self) -> None:
        config = dict(self.config)
        config["record_ids"] = ["ipv4"]
        old_record = {
            "id": "ipv4",
            "type": "A",
            "name": "home.example.com",
            "content": "1.1.1.1",
            "ttl": 1,
            "proxied": False,
        }

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_cloudflare_config",
                return_value=config,
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.CloudflareService"
            ) as service_cls,
        ):
            service = service_cls.return_value
            service.get_dns_records.side_effect = [[old_record], [old_record]]
            with self.assertRaisesRegex(RuntimeError, "readback"):
                await self.monitor._update_cloudflare_dns("5.6.7.8")

        service.update_dns_record.assert_called_once()
        self.assertEqual(service.get_dns_records.call_count, 2)

    async def test_missing_configured_record_is_a_retryable_failure(self) -> None:
        config = dict(self.config)
        config["record_ids"] = ["missing"]

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_cloudflare_config",
                return_value=config,
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.CloudflareService"
            ) as service_cls,
        ):
            service = service_cls.return_value
            service.get_dns_records.return_value = []
            with self.assertRaisesRegex(RuntimeError, "missing"):
                await self.monitor._update_cloudflare_dns("5.6.7.8")

        service.update_dns_record.assert_not_called()

    async def test_incompatible_configured_record_type_fails_before_writes(self) -> None:
        config = dict(self.config)
        config["record_ids"] = ["ipv6"]
        record = {
            "id": "ipv6",
            "type": "AAAA",
            "name": "home.example.com",
            "content": "2001:db8::1",
            "ttl": 1,
            "proxied": False,
        }

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_cloudflare_config",
                return_value=config,
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.CloudflareService"
            ) as service_cls,
        ):
            service = service_cls.return_value
            service.get_dns_records.return_value = [record]
            with self.assertRaisesRegex(RuntimeError, "must be A records"):
                await self.monitor._update_cloudflare_dns("5.6.7.8")

        service.update_dns_record.assert_not_called()

    async def test_record_already_at_target_ip_is_not_rewritten(self) -> None:
        config = dict(self.config)
        config["record_ids"] = ["ipv4"]
        record = {
            "id": "ipv4",
            "type": "A",
            "name": "home.example.com",
            "content": "5.6.7.8",
            "ttl": 300,
            "proxied": True,
        }

        with (
            patch(
                "mitra_bot.tasks.ip_monitor_task.get_cloudflare_config",
                return_value=config,
            ),
            patch(
                "mitra_bot.tasks.ip_monitor_task.CloudflareService"
            ) as service_cls,
        ):
            service = service_cls.return_value
            service.get_dns_records.return_value = [record]
            updated = await self.monitor._update_cloudflare_dns("5.6.7.8")

        self.assertEqual(updated, 0)
        service.update_dns_record.assert_not_called()


class IPCogNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_fetches_public_ip_off_event_loop(self) -> None:
        bot = Mock()
        cog = IPCog(bot)
        ctx = Mock()
        ctx.defer = AsyncMock()
        ctx.respond = AsyncMock()
        event_loop_thread = threading.get_ident()
        worker_threads: list[int] = []

        def fake_get_public_ip() -> str:
            worker_threads.append(threading.get_ident())
            return "5.6.7.8"

        with patch(
            "mitra_bot.discord_app.cogs.ip_cog.get_public_ip",
            side_effect=fake_get_public_ip,
        ):
            await IPCog.status.callback(cog, ctx)

        ctx.defer.assert_awaited_once_with(ephemeral=True)
        ctx.respond.assert_awaited_once()
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], event_loop_thread)

    async def test_legacy_channel_delivery_failure_is_reported(self) -> None:
        bot = Mock()
        bot.peer_service = None  # Standalone bot, not a mesh follower.
        bot.guilds = []
        bot.state = SimpleNamespace(
            channel_id=123,
            ip_subscriber_role_name="Mitra IP Subscriber",
        )
        cog = IPCog(bot)

        with patch(
            "mitra_bot.discord_app.cogs.ip_cog.Notifier.send_to_channel",
            new_callable=AsyncMock,
            return_value=False,
        ) as mock_send:
            delivered = await cog.notify_ip_change("5.6.7.8")

        self.assertFalse(delivered)
        mock_send.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
