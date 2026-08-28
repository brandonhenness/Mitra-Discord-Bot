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
    record_ids = [
        str(record_id).strip()
        for record_id in cfg.get("record_ids", [])
        if str(record_id).strip()
    ]

    if not token:
        pytest.skip("Missing CLOUDFLARE_API_TOKEN")
    if not zone_id:
        pytest.skip("Missing [cloudflare].zone_id in config.toml")
    if not record_ids:
        pytest.fail("Missing [cloudflare].record_ids in config.toml")

    svc = CloudflareService(api_token=token)
    records = svc.get_dns_records(zone_id)
    assert isinstance(records, list)

    records_by_id = {str(record.get("id") or ""): record for record in records}
    missing_ids = [
        record_id for record_id in record_ids if record_id not in records_by_id
    ]
    assert not missing_ids, f"Configured Cloudflare record IDs were not found: {missing_ids}"

    unsupported = {
        record_id: str(records_by_id[record_id].get("type") or "")
        for record_id in record_ids
        if str(records_by_id[record_id].get("type") or "").upper()
        != "A"
    }
    assert not unsupported, (
        "Configured Cloudflare records must be A records because the public-IP "
        f"monitor is IPv4-only: {unsupported}"
    )

