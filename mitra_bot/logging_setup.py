# mitra_bot/logging_setup.py
from __future__ import annotations

import logging
from typing import Optional


class LogColors:
    GRAY = "\x1b[90m"
    BRIGHT_BLUE = "\x1b[94m"
    YELLOW = "\x1b[33;1m"
    RED = "\x1b[31;1m"
    MAGENTA = "\x1b[35m"
    RESET = "\x1b[0m"
    CYAN = "\x1b[36;1m"


class CustomFormatter(logging.Formatter):
    FORMAT = (
        "[" + LogColors.GRAY + "%(asctime)s" + LogColors.RESET + "] "
        "[%(levelname)-8s" + LogColors.RESET + "] "
        "%(name)s" + LogColors.RESET + ": %(message)s"
    )

    COLOR_FORMAT = {
        logging.DEBUG: FORMAT.replace("%(levelname)-8s", LogColors.CYAN + "%(levelname)-8s").replace(
            "%(name)s", LogColors.MAGENTA + "%(name)s"
        ),
        logging.INFO: FORMAT.replace("%(levelname)-8s", LogColors.BRIGHT_BLUE + "%(levelname)-8s").replace(
            "%(name)s", LogColors.MAGENTA + "%(name)s"
        ),
        logging.WARNING: FORMAT.replace("%(levelname)-8s", LogColors.YELLOW + "%(levelname)-8s").replace(
            "%(name)s", LogColors.MAGENTA + "%(name)s"
        ),
        logging.ERROR: FORMAT.replace("%(levelname)-8s", LogColors.RED + "%(levelname)-8s").replace(
            "%(name)s", LogColors.MAGENTA + "%(name)s"
        ),
        logging.CRITICAL: FORMAT.replace("%(levelname)-8s", LogColors.RED + "%(levelname)-8s").replace(
            "%(name)s", LogColors.MAGENTA + "%(name)s"
        ),
    }

    def format(self, record: logging.LogRecord) -> str:
        log_fmt = self.COLOR_FORMAT.get(record.levelno, self.FORMAT)
        formatter = logging.Formatter(log_fmt, datefmt="%Y-%m-%d %H:%M:%S")
        return formatter.format(record)


def setup_logging(
    *,
    level: int = logging.INFO,
    logfile: str = "bot.log",
    add_file_handler: bool = True,
    stream: Optional[object] = None,
) -> None:
    """
    Configure root logging similarly to the original bot.py:
      - Console handler with colored formatter
      - Optional file handler with plain formatter

    Safe to call multiple times: it clears existing handlers first.
    """
    root_logger = logging.getLogger()

    # Prevent duplicate handlers if setup_logging is called more than once
    for h in list(root_logger.handlers):
        root_logger.removeHandler(h)

    root_logger.setLevel(level)

    console = logging.StreamHandler(stream)
    console.setFormatter(CustomFormatter())
    root_logger.addHandler(console)

    if add_file_handler:
        file_handler = logging.FileHandler(logfile)
        file_formatter = logging.Formatter(
            "[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)
