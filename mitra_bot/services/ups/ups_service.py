# mitra_bot/services/ups/ups_service.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .tripplite_client import TrippliteUPSClient
from .ups_log import UPSLogStore
from mitra_bot.services.power_service import execute_power_action


@dataclass
class UPSConfig:
    enabled: bool
    warn_time_to_empty_seconds: int
    critical_time_to_empty_seconds: int
    auto_shutdown_enabled: bool
    auto_shutdown_action: str
    auto_shutdown_delay_seconds: int
    auto_shutdown_force: bool


@dataclass
class UPSEvent:
    """
    Structured event returned by poll().
    The Discord task layer decides how to notify.
    """

    level: str  # "info", "warn", "critical"
    message: str


def _utc_ts_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _safe_bool(v: Any) -> Optional[bool]:
    if v is None:
        return None
    try:
        return bool(v)
    except Exception:
        return None


def _get_dict(d: Dict[str, Any], key: str) -> Dict[str, Any]:
    v = d.get(key)
    return v if isinstance(v, dict) else {}


def _fmt_seconds(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    try:
        s = max(0, int(seconds))
    except Exception:
        return "unknown"

    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


class UPSService:
    def __init__(
        self,
        *,
        client: TrippliteUPSClient,
        log_store: UPSLogStore,
        config: UPSConfig,
    ) -> None:
        self.client = client
        self.log_store = log_store
        self.config = config

        self._last_state: Optional[str] = None  # "line" | "battery"

    # --------------------------------------------------
    # Public poll method
    # --------------------------------------------------

    def poll(self) -> Optional[UPSEvent]:
        """
        Poll the UPS and return an optional UPSEvent
        if something important happened.
        """
        if not self.config.enabled:
            return None

        if not self.client.available:
            logging.warning("Tripplite library not available.")
            return None

        try:
            status = self.client.get_status()
        except Exception:
            logging.exception("Failed to poll UPS.")
            return None

        return self._process_status(status)

    # --------------------------------------------------
    # Internal logic
    # --------------------------------------------------

    def _process_status(self, status: Dict[str, Any]) -> Optional[UPSEvent]:
        """
        Interpret UPS status and detect state transitions.

        IMPORTANT:
        - Logging should match the original bot.py JSONL fields so your old graph works:
          time_to_empty_s, output_w, input_v, etc.
        - Also emits the newer convenience fields used elsewhere in the refactor.
        """

        # Support both:
        # - "legacy/raw" tripplite dict with nested status/input/output and "time to empty"
        # - "refactor" minimal dict with battery_percent/input_voltage/time_to_empty_seconds/on_battery
        status_flags = _get_dict(status, "status")
        inp = _get_dict(status, "input")
        outp = _get_dict(status, "output")

        # Flags (legacy)
        ac_present = _safe_bool(status_flags.get("ac present"))
        charging = _safe_bool(status_flags.get("charging"))
        discharging = _safe_bool(status_flags.get("discharging"))
        shutdown_imminent = _safe_bool(status_flags.get("shutdown imminent"))
        needs_replacement = _safe_bool(status_flags.get("needs replacement"))

        # Metrics (legacy)
        health_pct = _safe_float(status.get("health"))
        time_to_empty_s = _safe_float(status.get("time to empty"))

        input_v = _safe_float(inp.get("voltage"))
        input_hz = _safe_float(inp.get("frequency"))
        output_v = _safe_float(outp.get("voltage"))
        output_w = _safe_float(outp.get("power"))

        # Metrics (new/minimal fallbacks)
        battery_percent = status.get("battery_percent", None)

        # Prefer legacy extracted values; fall back to minimal keys if present
        if input_v is None:
            input_v = _safe_float(status.get("input_voltage"))

        # Prefer existing time_to_empty_s; fall back to refactor key
        if time_to_empty_s is None:
            time_to_empty_s = _safe_float(status.get("time_to_empty_seconds"))

        # Determine on_battery:
        # 1) if provided explicitly, use it
        # 2) else derive from ac_present if we have it
        # 3) else default False
        on_battery_val = status.get("on_battery")
        if on_battery_val is not None:
            on_battery = bool(on_battery_val)
        elif ac_present is not None:
            on_battery = ac_present is False
        else:
            on_battery = False

        current_state = "battery" if on_battery else "line"

        # ------------------------------------------------------------------
        # LOG ROW: match old bot.py keys (plus compatibility keys)
        # ------------------------------------------------------------------
        log_row: Dict[str, Any] = {
            "ts": _utc_ts_z(),
            # original keys used by old graphs
            "ac_present": ac_present,
            "charging": charging,
            "discharging": discharging,
            "shutdown_imminent": shutdown_imminent,
            "needs_replacement": needs_replacement,
            "health_pct": health_pct,
            "time_to_empty_s": time_to_empty_s,
            "input_v": input_v,
            "input_hz": input_hz,
            "output_v": output_v,
            "output_w": output_w,
            # newer keys (so other parts of refactor can use them)
            "battery_percent": battery_percent,
            "input_voltage": input_v,
            "time_to_empty_seconds": time_to_empty_s,
            "on_battery": on_battery,
        }

        self.log_store.append(log_row)

        # ------------------------------------------------------------------
        # Events: state transition detection
        # ------------------------------------------------------------------
        if self._last_state is None:
            self._last_state = current_state
            return None

        if current_state != self._last_state:
            self._last_state = current_state

            if current_state == "battery":
                return UPSEvent(
                    level="warn",
                    message=f"⚠️ UPS switched to battery power. Runtime: **{_fmt_seconds(time_to_empty_s)}**",
                )

            return UPSEvent(
                level="info",
                message="✅ Utility power restored.",
            )

        # ------------------------------------------------------------------
        # Threshold checks while on battery
        # ------------------------------------------------------------------
        if on_battery and time_to_empty_s is not None:
            return self._check_thresholds(int(time_to_empty_s))

        return None

    # --------------------------------------------------
    # Threshold logic
    # --------------------------------------------------

    def _check_thresholds(self, time_to_empty_seconds: int) -> Optional[UPSEvent]:
        """
        Evaluate warning / critical thresholds.
        """

        if time_to_empty_seconds <= self.config.critical_time_to_empty_seconds:
            self._handle_auto_shutdown()
            return UPSEvent(
                level="critical",
                message=f"🚨 UPS battery critical. Runtime: **{_fmt_seconds(time_to_empty_seconds)}**",
            )

        if time_to_empty_seconds <= self.config.warn_time_to_empty_seconds:
            return UPSEvent(
                level="warn",
                message=f"⚠️ UPS battery running low. Runtime: **{_fmt_seconds(time_to_empty_seconds)}**",
            )

        return None

    # --------------------------------------------------
    # Auto shutdown
    # --------------------------------------------------

    def _handle_auto_shutdown(self) -> None:
        if not self.config.auto_shutdown_enabled:
            return

        try:
            logging.warning(
                "Auto shutdown triggered: %s",
                self.config.auto_shutdown_action,
            )

            execute_power_action(
                self.config.auto_shutdown_action,
                delay_seconds=self.config.auto_shutdown_delay_seconds,
                force=self.config.auto_shutdown_force,
            )
        except Exception:
            logging.exception("Auto shutdown failed.")
