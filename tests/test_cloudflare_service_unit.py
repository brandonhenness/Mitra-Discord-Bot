from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

from mitra_bot.services.cloudflare_service import CloudflareService


class CloudflareServiceUnitTests(unittest.TestCase):
    def test_requires_api_token(self) -> None:
        with self.assertRaises(ValueError):
            CloudflareService(api_token=None)

    @patch("mitra_bot.services.cloudflare_service.requests.request")
    def test_get_zones_sends_bearer_header(self, mock_request: Mock) -> None:
        mock_response = Mock()
        mock_response.json.return_value = {
            "success": True,
            "result": [{"id": "z1", "name": "example.com"}],
            "errors": [],
            "messages": [],
        }
        mock_request.return_value = mock_response

        svc = CloudflareService(api_token="token-123")
        zones = svc.get_zones()

        self.assertEqual(len(zones), 1)
        self.assertEqual(zones[0]["id"], "z1")
        kwargs = mock_request.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-123")

    @patch("mitra_bot.services.cloudflare_service.requests.request")
    def test_get_dns_records_returns_normalized_payload(self, mock_request: Mock) -> None:
        mock_response = Mock()
        mock_response.json.return_value = {
            "success": True,
            "result": [
                {
                    "id": "r1",
                    "type": "A",
                    "name": "home.example.com",
                    "content": "1.2.3.4",
                    "ttl": 1,
                    "proxied": False,
                }
            ],
            "result_info": {"page": 1, "total_pages": 1},
            "errors": [],
            "messages": [],
        }
        mock_request.return_value = mock_response

        svc = CloudflareService(api_token="token-123")
        records = svc.get_dns_records("zone-1")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "home.example.com")
        self.assertEqual(records[0]["content"], "1.2.3.4")
        self.assertEqual(
            mock_request.call_args.kwargs["params"],
            {"page": 1, "per_page": 100},
        )

    def test_get_dns_records_fetches_all_reported_pages(self) -> None:
        svc = CloudflareService(api_token="token-123")
        record_one = {
            "id": "r1",
            "type": "A",
            "name": "one.example.com",
            "content": "1.1.1.1",
            "ttl": 1,
            "proxied": False,
        }
        record_two = {
            "id": "r2",
            "type": "A",
            "name": "two.example.com",
            "content": "2.2.2.2",
            "ttl": 1,
            "proxied": False,
        }

        with patch.object(
            svc,
            "_request",
            side_effect=[
                {
                    "success": True,
                    "result": [record_one],
                    "result_info": {"total_pages": 2},
                },
                {
                    "success": True,
                    "result": [record_two],
                    "result_info": {"total_pages": 2},
                },
            ],
        ) as mock_api:
            records = svc.get_dns_records("zone-1")

        self.assertEqual([record["id"] for record in records], ["r1", "r2"])
        self.assertEqual(
            mock_api.call_args_list,
            [
                call(
                    "GET",
                    "/zones/zone-1/dns_records",
                    params={"page": 1, "per_page": 100},
                ),
                call(
                    "GET",
                    "/zones/zone-1/dns_records",
                    params={"page": 2, "per_page": 100},
                ),
            ],
        )

    @patch("mitra_bot.services.cloudflare_service.requests.request")
    def test_update_dns_record_uses_partial_update_and_preserves_options(
        self, mock_request: Mock
    ) -> None:
        mock_response = Mock()
        mock_response.json.return_value = {
            "success": True,
            "result": {
                "id": "r1",
                "type": "A",
                "name": "home.example.com",
                "content": "5.6.7.8",
                "ttl": 300,
                "proxied": True,
            },
            "errors": [],
            "messages": [],
        }
        mock_request.return_value = mock_response

        svc = CloudflareService(api_token="token-123")
        result = svc.update_dns_record(
            "zone-1",
            "r1",
            name="home.example.com",
            record_type="A",
            content="5.6.7.8",
            ttl=300,
            proxied=True,
        )

        self.assertEqual(result["content"], "5.6.7.8")
        self.assertEqual(mock_request.call_args.args[0], "PATCH")
        self.assertEqual(
            mock_request.call_args.kwargs["json"],
            {
                "type": "A",
                "name": "home.example.com",
                "content": "5.6.7.8",
                "ttl": 300,
                "proxied": True,
            },
        )

    @patch("mitra_bot.services.cloudflare_service.requests.request")
    def test_update_dns_record_raises_on_api_error(self, mock_request: Mock) -> None:
        mock_response = Mock()
        mock_response.json.return_value = {
            "success": False,
            "result": None,
            "errors": [{"code": 1000, "message": "bad"}],
            "messages": [],
        }
        mock_request.return_value = mock_response

        svc = CloudflareService(api_token="token-123")
        with self.assertRaises(RuntimeError):
            svc.update_dns_record(
                "zone-1",
                "record-1",
                name="home.example.com",
                record_type="A",
                content="1.2.3.4",
            )


if __name__ == "__main__":
    unittest.main()
