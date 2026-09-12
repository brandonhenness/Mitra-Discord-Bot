"""Deployment verification for local multi-account DNS assignments."""
from mitra_bot.cloudflare_config import select_targets
from mitra_bot.peer_config import load_peer_config
from mitra_bot.services.cloudflare_auth import target_token
from mitra_bot.services.cloudflare_service import CloudflareService
from mitra_bot.services.ip_service import get_public_ip
import ipaddress


def verify_targets(targets, selected_secret, *, write=False):
    peer = load_peer_config()
    node_id = peer.node_id if peer.enabled else "local"
    selected = select_targets(targets, node_id)
    ip = get_public_ip() if write and selected else None
    if write and selected and (not ip or ipaddress.ip_address(ip).version != 4):
        raise RuntimeError("Cannot verify DNS writes without this server's public IPv4")
    count = 0
    for target in selected:
        token = target_token(target) if target.oauth_profile else selected_secret(target.token_env)
        if not token:
            raise RuntimeError("Missing credential for target " + target.name)
        service = CloudflareService(api_token=token)
        records = {r["id"]: r for r in service.get_dns_records(target.zone_id)}
        if any(record not in records or records[record]["type"] != "A" for record in target.record_ids):
            raise RuntimeError("A configured A record is missing or has an incompatible type")
        if write:
            for record_id in target.record_ids:
                record = records[record_id]
                service.update_dns_record(target.zone_id, record_id, name=record["name"], record_type="A",
                    content=ip, ttl=record.get("ttl", 1), proxied=bool(record.get("proxied", False)))
            readback = {r["id"]: r for r in service.get_dns_records(target.zone_id)}
            if any(readback.get(record, {}).get("content") != ip for record in target.record_ids):
                raise RuntimeError("DNS write readback mismatch")
        count += len(target.record_ids)
    return count
