# mitra_bot/services/cloudflare_service.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from pydantic import ValidationError
import requests

from mitra_bot.models.cloudflare_models import (
    CloudflareAPIEnvelope,
    CloudflareDNSRecord,
    CloudflareZone,
)

BASE_URL = "https://api.cloudflare.com/client/v4"


class CloudflareService:
    """
    Thin wrapper around Cloudflare API for:
      - listing zones
      - listing DNS records
      - updating a DNS record
    """

    def __init__(
        self,
        api_token: Optional[str] = None,
    ) -> None:
        self.api_token = (api_token or "").strip()
        if not self.api_token:
            raise ValueError("Cloudflare auth is missing. Provide api_token.")

    # --------------------------------------------------
    # Internal helpers
    # --------------------------------------------------

    @property
    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        timeout: int = 20,
    ) -> Dict[str, Any]:
        url = f"{BASE_URL}{endpoint}"

        logging.debug("Cloudflare %s %s", method, url)

        try:
            response = requests.request(
                method, url, headers=self._headers, params=params,
                json=json_body, timeout=timeout,
            )
        except requests.RequestException:
            raise RuntimeError("Cloudflare request failed; check connectivity and account authorization.") from None

        try:
            raw = response.json()
        except Exception:
            raise RuntimeError("Cloudflare returned a non-JSON response.") from None

        try:
            data = CloudflareAPIEnvelope.model_validate(raw)
        except ValidationError:
            raise RuntimeError("Cloudflare returned an invalid API response.") from None

        if not data.success:
            codes = [str(item.get("code", "unknown")) for item in data.errors]
            raise RuntimeError("Cloudflare API error (codes: " + ", ".join(codes) + "). Check token scope and zone access.")

        return data.model_dump(mode="json")

    # --------------------------------------------------
    # Public API
    # --------------------------------------------------

    def get_zones(self) -> List[Dict[str, Any]]:
        """
        Return all zones available to the API token.
        """
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            data = self._request("GET", "/zones", params={"page": page, "per_page": 50})
            for raw in data.get("result", []):
                try:
                    out.append(CloudflareZone.model_validate(raw).model_dump(mode="json"))
                except ValidationError:
                    logging.debug("Skipping invalid Cloudflare zone payload")
            if page >= int((data.get("result_info") or {}).get("total_pages", 1)):
                break
            page += 1
        return out

    def create_dns_record(self, zone_id: str, *, name: str, content: str, proxied: bool = False):
        data = self._request("POST", f"/zones/{zone_id}/dns_records", json_body={
            "type": "A", "name": name, "content": content, "ttl": 1, "proxied": proxied,
        })
        return CloudflareDNSRecord.model_validate(data["result"]).model_dump(mode="json")

    def get_dns_records(self, zone_id: str) -> List[Dict[str, Any]]:
        """
        Return DNS records for a given zone.

        Cloudflare paginates this endpoint. Fetch every reported page so a
        configured record ID cannot be missed merely because it is not on the
        first page.
        """
        out: List[Dict[str, Any]] = []
        page = 1
        while True:
            data = self._request(
                "GET",
                f"/zones/{zone_id}/dns_records",
                params={"page": page, "per_page": 100},
            )
            raw_records = data.get("result", [])
            if not isinstance(raw_records, list):
                raise RuntimeError("Unexpected Cloudflare DNS records response format.")

            for raw in raw_records:
                try:
                    out.append(
                        CloudflareDNSRecord.model_validate(raw).model_dump(mode="json")
                    )
                except ValidationError:
                    logging.debug(
                        "Skipping invalid Cloudflare DNS record payload: %s", raw
                    )

            result_info = data.get("result_info")
            if not isinstance(result_info, dict):
                break
            try:
                total_pages = max(1, int(result_info.get("total_pages", 1)))
            except (TypeError, ValueError):
                total_pages = 1
            if page >= total_pages:
                break
            page += 1
        return out

    def update_dns_record(
        self,
        zone_id: str,
        record_id: str,
        *,
        name: str,
        record_type: str,
        content: str,
        ttl: int = 1,
        proxied: bool = False,
    ) -> Dict[str, Any]:
        """
        Partially update an existing DNS record without replacing metadata such
        as comments or tags that Mitra does not manage.

        ttl=1 means "automatic" in Cloudflare.
        """
        body = {
            "type": record_type,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
        }

        data = self._request(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{record_id}",
            json_body=body,
        )

        logging.info(
            "Updated DNS record %s (%s) -> %s",
            name,
            record_type,
            content,
        )

        result = data.get("result", {})
        try:
            return CloudflareDNSRecord.model_validate(result).model_dump(mode="json")
        except ValidationError:
            logging.debug("Returning unvalidated Cloudflare update payload: %s", result)
            return result if isinstance(result, dict) else {}
