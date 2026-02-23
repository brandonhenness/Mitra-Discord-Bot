# mitra_bot/services/ups/tripplite_client.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Optional dependency: tripplite
try:
    from tripplite import Battery  # type: ignore
    TRIPPLITE_AVAILABLE = True
except Exception:
    Battery = None  # type: ignore
    TRIPPLITE_AVAILABLE = False


class UPSRawStatusModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Dict[str, Any] = Field(default_factory=dict)
    input: Dict[str, Any] = Field(default_factory=dict)
    output: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, value: Any) -> Dict[str, Any]:
        data = dict(value) if isinstance(value, dict) else {}
        if not isinstance(data.get("status"), dict):
            data["status"] = {}
        if not isinstance(data.get("input"), dict):
            data["input"] = {}
        if not isinstance(data.get("output"), dict):
            data["output"] = {}
        return data


class TrippliteUPSClient:
    """
    A small wrapper around tripplite.Battery that:
      - opens lazily
      - retries on OSError by closing/reopening the handle
    """

    def __init__(self) -> None:
        self._battery: Optional[Any] = None  # Battery instance

    @property
    def available(self) -> bool:
        return TRIPPLITE_AVAILABLE

    def open(self) -> None:
        if not TRIPPLITE_AVAILABLE:
            raise RuntimeError("tripplite library is not installed.")

        if self._battery is None:
            try:
                self._battery = Battery()
                self._battery.open()
            except OSError as exc:
                if _is_no_ups_connected_error(exc):
                    raise RuntimeError("No UPS connected.") from exc
                raise

    def close(self) -> None:
        if self._battery is None:
            return
        try:
            self._battery.close()
        except Exception:
            pass
        finally:
            self._battery = None

    def get_status(self) -> Dict[str, Any]:
        """
        Returns the dict from tripplite.Battery.get()
        """
        self.open()
        assert self._battery is not None

        try:
            raw = self._battery.get()
            return UPSRawStatusModel.model_validate(raw).model_dump(mode="json")
        except OSError as e:
            logging.warning("UPS read error (OSError). Reopening connection. %s", e)

            # Reopen and retry once
            try:
                self._battery.close()
            except Exception:
                pass

            self._battery = Battery()
            self._battery.open()
            raw = self._battery.get()
            return UPSRawStatusModel.model_validate(raw).model_dump(mode="json")


def _is_no_ups_connected_error(exc: Exception) -> bool:
    text = str(exc).strip().lower()
    if not text:
        return False
    markers = (
        "no ups connected",
        "could not find any connected tripplite devices",
        "could not find any connected",
        "no connected",
        "device not found",
        "cannot find",
    )
    return any(marker in text for marker in markers)
