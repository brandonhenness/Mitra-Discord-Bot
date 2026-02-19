# mitra_bot/services/ip_service.py
from __future__ import annotations

import logging
from typing import Optional

import requests


IPIFY_URL = "https://api.ipify.org"


def get_public_ip(timeout: int = 10) -> Optional[str]:
    """
    Fetch the current public IPv4 address using ipify.

    Returns:
        str IP address on success
        None on failure
    """
    try:
        logging.debug("Requesting public IP from %s", IPIFY_URL)
        response = requests.get(IPIFY_URL, timeout=timeout)
        response.raise_for_status()

        ip = response.text.strip()
        if not ip:
            logging.error("Received empty IP response from ipify.")
            return None

        logging.info("Current public IP detected: %s", ip)
        return ip

    except Exception:
        logging.exception("Failed to fetch public IP.")
        return None
