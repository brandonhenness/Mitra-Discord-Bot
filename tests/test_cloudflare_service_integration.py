from __future__ import annotations

import os

import pytest

from mitra_bot.services.cloudflare_service import CloudflareService
from mitra_bot.storage.storage_store import get_cloudflare_config


@pytest.mark.integration
def test_cloudflare_integration_get_dns_records() -> None:
    if os.getenv("RUN_CLOUDFLARE_INTEGRATION") != "1":
        pytest.skip("Set RUN_CLOUDFLARE_INTEGRATION=1 to run live Cloudflare integration tests.")

    cfg = get_cloudflare_config()
    token = str(cfg.get("api_token") or "").strip()
    zone_id = str(cfg.get("zone_id") or "").strip()

    if not token:
        pytest.skip("Missing CLOUDFLARE_API_TOKEN")
    if not zone_id:
        pytest.skip("Missing [cloudflare].zone_id in config.toml")

    svc = CloudflareService(api_token=token)
    records = svc.get_dns_records(zone_id)
    assert isinstance(records, list)

