# mitra_bot/services/ups/tripplite_client.py
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

# Optional dependency: tripplite
try:
    from tripplite import Battery  # type: ignore
    TRIPPLITE_AVAILABLE = True
except Exception:
    Battery = None  # type: ignore
    TRIPPLITE_AVAILABLE = False


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
            self._battery = Battery()
            self._battery.open()

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
            return self._battery.get()
        except OSError as e:
            logging.warning("UPS read error (OSError). Reopening connection. %s", e)

            # Reopen and retry once
            try:
                self._battery.close()
            except Exception:
                pass

            self._battery = Battery()
            self._battery.open()
            return self._battery.get()
