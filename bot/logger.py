"""Structured logging — separate log files for opportunities, trades, and errors."""

import logging
import os
from pathlib import Path


def setup_logger(log_dir: str, level: str = "INFO") -> logging.Logger:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_level = getattr(logging, level.upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    def _file(filename: str) -> logging.FileHandler:
        h = logging.FileHandler(os.path.join(log_dir, filename))
        h.setFormatter(fmt)
        return h

    # Root bot logger → main.log + console
    root = logging.getLogger("arb_bot")
    root.setLevel(log_level)
    root.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    root.addHandler(_file("main.log"))

    # Specialised child loggers (inherit level from root)
    for name, filename in [
        ("arb_bot.opportunities", "opportunities.log"),
        ("arb_bot.trades", "trades.log"),
        ("arb_bot.errors", "errors.log"),
    ]:
        lg = logging.getLogger(name)
        lg.setLevel(log_level)
        lg.addHandler(_file(filename))

    return root
