# mitra_bot/tasks/ip_monitor_task.py
from __future__ import annotations

import asyncio
import ipaddress
import logging
from typing import Optional

import discord
from discord.ext import tasks
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mitra_bot.services.cloudflare_service import CloudflareService
from mitra_bot.services.ip_service import get_public_ip
from mitra_bot.storage.storage_store import get_cloudflare_config, load_ip, save_ip


class CloudflareDNSUpdateConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    zone_id: str = ""
    record_ids: list[str] = Field(default_factory=list)
    api_token: str = ""

    @field_validator("enabled", mode="before")
    @classmethod
    def _coerce_enabled(cls, value: object) -> bool:
        # Treat null/missing config as enabled by default.
        if value is None:
            return True
        return bool(value)

    @field_validator("zone_id", "api_token", mode="before")
    @classmethod
    def _coerce_optional_str(cls, value: object) -> str:
        # Normalize optional string fields.
        if value is None:
            return ""
        return str(value)

    @field_validator("record_ids", mode="before")
    @classmethod
    def _coerce_record_ids(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        return [str(value)]

    @model_validator(mode="after")
    def _normalize(self) -> "CloudflareDNSUpdateConfig":
        self.zone_id = self.zone_id.strip()
        self.record_ids = [str(x).strip() for x in self.record_ids if str(x).strip()]
        self.api_token = self.api_token.strip()
        return self

    @property
    def has_auth(self) -> bool:
        return bool(self.api_token)


class IPMonitorTask:
    """
    Background loop that checks public IP and notifies subscribers on change.
    """

    def __init__(
        self,
        bot: discord.Bot,
        *,
        interval_seconds: int = 60,
        cloudflare_api_token: Optional[str] = None,
    ) -> None:
        self.bot = bot
        self.interval_seconds = interval_seconds
        self.cloudflare_api_token = (cloudflare_api_token or "").strip()

        self._last_ip: Optional[str] = None
        self._cloudflare_reconciled = False

        # bind loop
        self.loop.change_interval(seconds=self.interval_seconds)

    async def start(self) -> None:
        # Load last observed IP from persistent state.
        self._last_ip = await load_ip()
        if not self._last_ip:
            logging.info("No stored IP found.")
        else:
            logging.info("Stored IP loaded: %s", self._last_ip)

        self.loop.start()

    async def _update_cloudflare_dns(self, ip: str) -> int:
        raw_cfg = dict(get_cloudflare_config())
        if self.cloudflare_api_token:
            # The secret is loaded centrally from .env/real environment by
            # AppSettings and injected in memory. It is never written to config.toml
            # or state.db.
            raw_cfg["api_token"] = self.cloudflare_api_token
        if not raw_cfg:
            return 0
        cfg = CloudflareDNSUpdateConfig.model_validate(raw_cfg)

        if not cfg.enabled:
            logging.info("Cloudflare DNS update is disabled in config.")
            return 0

        if not cfg.zone_id:
            raise RuntimeError("Cloudflare config is enabled but zone_id is missing.")

        if not cfg.record_ids:
            raise RuntimeError("Cloudflare config is enabled but record_ids is empty.")

        if not cfg.has_auth:
            raise RuntimeError(
                "Cloudflare config is enabled but CLOUDFLARE_API_TOKEN is missing."
            )

        ip_version = ipaddress.ip_address(ip).version

        service = CloudflareService(
            api_token=cfg.api_token or None,
        )
        records = await asyncio.to_thread(service.get_dns_records, cfg.zone_id)
        records_by_id = {str(r.get("id", "")): r for r in records}

        missing_record_ids = [
            record_id for record_id in cfg.record_ids if record_id not in records_by_id
        ]
        if missing_record_ids:
            missing = ", ".join(missing_record_ids)
            raise RuntimeError(
                f"Cloudflare record_id(s) not found in zone {cfg.zone_id}: {missing}"
            )

        expected_record_type = "A" if ip_version == 4 else "AAAA"
        incompatible_records = {
            record_id: str(records_by_id[record_id].get("type", "")).upper()
            for record_id in cfg.record_ids
            if str(records_by_id[record_id].get("type", "")).upper()
            != expected_record_type
        }
        if incompatible_records:
            rendered = ", ".join(
                f"{record_id} ({record_type or 'unknown'})"
                for record_id, record_type in incompatible_records.items()
            )
            raise RuntimeError(
                f"Cloudflare record_id(s) must be {expected_record_type} records "
                f"for public IP {ip}: {rendered}"
            )

        updated = 0
        for record_id in cfg.record_ids:
            record = records_by_id[record_id]

            record_type = str(record.get("type", "")).upper()
            record_name = str(record.get("name", "")).strip()
            if not record_name:
                raise RuntimeError(
                    f"Cloudflare record {record_id} is missing its record name."
                )

            if str(record.get("content", "")).strip() == ip:
                logging.debug(
                    "Cloudflare record %s already points to %s.", record_id, ip
                )
                continue

            ttl_raw = record.get("ttl", 1)
            try:
                ttl = int(ttl_raw)
            except Exception:
                ttl = 1

            proxied = bool(record.get("proxied", False))
            await asyncio.to_thread(
                service.update_dns_record,
                cfg.zone_id,
                record_id,
                name=record_name,
                record_type=record_type,
                content=ip,
                ttl=ttl,
                proxied=proxied,
            )
            updated += 1

        logging.info("Cloudflare DNS update complete. Updated %s record(s).", updated)
        return updated

    @tasks.loop(seconds=60)
    async def loop(self) -> None:
        ip = await asyncio.to_thread(get_public_ip)
        if not ip:
            return

        if self._last_ip is None:
            try:
                await self._update_cloudflare_dns(ip)
                await save_ip(ip)
            except Exception:
                logging.exception(
                    "Failed to establish initial public IP baseline; will retry."
                )
                return
            self._last_ip = ip
            self._cloudflare_reconciled = True
            logging.info("Stored initial public IP baseline: %s", ip)
            return

        if ip == self._last_ip:
            if not self._cloudflare_reconciled:
                try:
                    await self._update_cloudflare_dns(ip)
                except Exception:
                    logging.exception(
                        "Failed to reconcile Cloudflare DNS at startup; will retry."
                    )
                    return
                self._cloudflare_reconciled = True
            return

        logging.info("Public IP changed: %s -> %s", self._last_ip, ip)

        try:
            await self._update_cloudflare_dns(ip)
        except Exception:
            logging.exception(
                "Failed to update Cloudflare DNS records; IP change will be retried."
            )
            return

        # DNS is the authoritative operation. Commit the new baseline before
        # best-effort Discord delivery so a broken destination cannot cause
        # duplicate alerts to every healthy guild on each poll.
        try:
            await save_ip(ip)
        except Exception:
            logging.exception("Failed to persist changed public IP; will retry.")
            return

        self._last_ip = ip
        self._cloudflare_reconciled = True

        # Find the IPCog and call its notifier
        cog = self.bot.get_cog("IPCog")
        if cog is None:
            logging.warning("IPCog not loaded; cannot notify subscribers.")
            return

        try:
            delivered = await cog.notify_ip_change(ip)  # type: ignore[attr-defined]
        except Exception:
            logging.exception("Failed to notify IP change; DNS state was committed.")
            return

        if not delivered:
            logging.warning(
                "IP change notification was not delivered to every destination; "
                "DNS state was committed to avoid duplicate alerts."
            )

    @loop.before_loop
    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()
