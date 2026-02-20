# mitra_bot/services/cloudflare_service.py
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

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
        *,
        api_key: Optional[str] = None,
        email: Optional[str] = None,
    ) -> None:
        self.api_token = (api_token or "").strip()
        self.api_key = (api_key or "").strip()
        self.email = (email or "").strip()

        has_token = bool(self.api_token)
        has_key_auth = bool(self.api_key and self.email)
        if not has_token and not has_key_auth:
            raise ValueError(
                "Cloudflare auth is missing. Provide api_token or api_key + email."
            )

    # --------------------------------------------------
    # Internal helpers
    # --------------------------------------------------

    @property
    def _headers(self) -> Dict[str, str]:
        headers: Dict[str, str] = {"Content-Type": "application/json"}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        else:
            headers["X-Auth-Key"] = self.api_key
            headers["X-Auth-Email"] = self.email
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

        response = requests.request(
            method,
            url,
            headers=self._headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )

        try:
            data = response.json()
        except Exception:
            logging.error("Cloudflare returned non-JSON response.")
            response.raise_for_status()
            raise

        if not data.get("success", False):
            logging.error("Cloudflare API error: %s", data)
            raise RuntimeError(f"Cloudflare API error: {data}")

        return data

    # --------------------------------------------------
    # Public API
    # --------------------------------------------------

    def get_zones(self) -> List[Dict[str, Any]]:
        """
        Return all zones available to the API token.
        """
        data = self._request("GET", "/zones")
        return data.get("result", [])

    def get_dns_records(self, zone_id: str) -> List[Dict[str, Any]]:
        """
        Return DNS records for a given zone.
        """
        data = self._request(
            "GET",
            f"/zones/{zone_id}/dns_records",
        )
        return data.get("result", [])

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
        Update an existing DNS record.

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
            "PUT",
            f"/zones/{zone_id}/dns_records/{record_id}",
            json_body=body,
        )

        logging.info(
            "Updated DNS record %s (%s) -> %s",
            name,
            record_type,
            content,
        )

        return data.get("result", {})
