from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

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
            "errors": [],
            "messages": [],
        }
        mock_request.return_value = mock_response

        svc = CloudflareService(api_token="token-123")
        records = svc.get_dns_records("zone-1")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "home.example.com")
        self.assertEqual(records[0]["content"], "1.2.3.4")

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
