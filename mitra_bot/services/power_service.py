# mitra_bot/services/power_service.py
from __future__ import annotations

import logging
import os
import subprocess
from typing import List


def build_power_command(
    action: str,
    *,
    delay_seconds: int = 0,
    force: bool = False,
) -> List[str]:
    """
    Build the appropriate OS shutdown command.

    action:
        - "shutdown"
        - "restart"
        - "cancel"
    """

    if os.name != "nt":
        raise RuntimeError("Power actions are currently only implemented for Windows.")

    if action == "cancel":
        return ["shutdown", "/a"]

    if action not in {"shutdown", "restart"}:
        raise ValueError("Invalid power action.")

    cmd = ["shutdown"]

    if action == "shutdown":
        cmd.append("/s")
    elif action == "restart":
        cmd.append("/r")

    cmd.append("/t")
    cmd.append(str(max(0, int(delay_seconds))))

    if force:
        cmd.append("/f")

    return cmd


def execute_power_action(
    action: str,
    *,
    delay_seconds: int = 0,
    force: bool = False,
) -> str:
    """
    Execute a power action and return a human-readable status message.
    """

    cmd = build_power_command(
        action,
        delay_seconds=delay_seconds,
        force=force,
    )

    logging.info("Executing power action: %s", " ".join(cmd))

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        logging.exception("Power action failed.")
        raise RuntimeError(f"Power action failed: {e}") from e

    if action == "cancel":
        return "Shutdown/restart has been canceled."

    if action == "shutdown":
        return f"System shutdown scheduled in {delay_seconds} seconds."

    if action == "restart":
        return f"System restart scheduled in {delay_seconds} seconds."

    return "Power action executed."
