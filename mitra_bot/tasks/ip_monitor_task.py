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
from mitra_bot.storage.cache_store import get_cloudflare_config, load_ip


class CloudflareDNSUpdateConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    zone_id: str = ""
    record_ids: list[str] = Field(default_factory=list)
    api_token: str = ""
    api_key: str = ""
    email: str = ""

    @field_validator("enabled", mode="before")
    @classmethod
    def _coerce_enabled(cls, value: object) -> bool:
        # Treat null/missing config as enabled by default.
        if value is None:
            return True
        return bool(value)

    @field_validator("zone_id", "api_token", "api_key", "email", mode="before")
    @classmethod
    def _coerce_optional_str(cls, value: object) -> str:
        # Legacy cache entries may contain null for unset string fields.
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
        self.api_key = self.api_key.strip()
        self.email = self.email.strip()
        return self

    @property
    def has_auth(self) -> bool:
        return bool(self.api_token) or bool(self.api_key and self.email)


class IPMonitorTask:
    """
    Background loop that checks public IP and notifies subscribers on change.
    """

    def __init__(self, bot: discord.Bot, *, interval_seconds: int = 60) -> None:
        self.bot = bot
        self.interval_seconds = interval_seconds

        self._last_ip: Optional[str] = None

        # bind loop
        self.loop.change_interval(seconds=self.interval_seconds)

    async def start(self) -> None:
        # load last ip from cache
        self._last_ip = await load_ip()
        if not self._last_ip:
            logging.info("No cached IP found.")
        else:
            logging.info("Cached IP loaded: %s", self._last_ip)

        self.loop.start()

    async def _update_cloudflare_dns(self, ip: str) -> None:
        raw_cfg = get_cloudflare_config()
        if not raw_cfg:
            return
        cfg = CloudflareDNSUpdateConfig.model_validate(raw_cfg)

        if not cfg.enabled:
            logging.info("Cloudflare DNS update is disabled in cache config.")
            return

        if not cfg.zone_id:
            logging.warning("Cloudflare config is missing zone_id; skipping DNS update.")
            return

        if not cfg.record_ids:
            logging.warning("Cloudflare config has no record_ids; skipping DNS update.")
            return

        if not cfg.has_auth:
            logging.warning(
                "Cloudflare config needs api_token or api_key+email; skipping DNS update."
            )
            return

        ip_version = ipaddress.ip_address(ip).version

        service = CloudflareService(
            api_token=cfg.api_token or None,
            api_key=cfg.api_key or None,
            email=cfg.email or None,
        )
        records = await asyncio.to_thread(service.get_dns_records, cfg.zone_id)
        records_by_id = {str(r.get("id", "")): r for r in records}

        updated = 0
        for record_id in cfg.record_ids:
            record = records_by_id.get(record_id)
            if not record:
                logging.warning("Cloudflare record_id not found in zone: %s", record_id)
                continue

            record_type = str(record.get("type", "")).upper()
            if ip_version == 4 and record_type != "A":
                logging.info(
                    "Skipping record %s (%s): public IP is IPv4.",
                    record_id,
                    record_type,
                )
                continue
            if ip_version == 6 and record_type != "AAAA":
                logging.info(
                    "Skipping record %s (%s): public IP is IPv6.",
                    record_id,
                    record_type,
                )
                continue

            record_name = str(record.get("name", "")).strip()
            if not record_name:
                logging.warning(
                    "Skipping record %s: missing record name in Cloudflare response.",
                    record_id,
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

    @tasks.loop(seconds=60)
    async def loop(self) -> None:
        ip = get_public_ip()
        if not ip:
            return

        if self._last_ip is None:
            self._last_ip = ip
            return

        if ip == self._last_ip:
            return

        logging.info("Public IP changed: %s -> %s", self._last_ip, ip)
        self._last_ip = ip

        try:
            await self._update_cloudflare_dns(ip)
        except Exception:
            logging.exception("Failed to update Cloudflare DNS records.")

        # Find the IPCog and call its notifier
        cog = self.bot.get_cog("IPCog")
        if cog is None:
            logging.warning("IPCog not loaded; cannot notify subscribers.")
            return

        try:
            await cog.notify_ip_change(ip)  # type: ignore[attr-defined]
        except Exception:
            logging.exception("Failed to notify IP change.")

    @loop.before_loop
    async def before_loop(self) -> None:
        await self.bot.wait_until_ready()
