#!/usr/bin/env python3
"""
Polymarket Arb Bot — Dashboard mode
=====================================
Runs the arb bot + mirror bot in background threads and serves the live
dashboard at http://localhost:8080

Usage:
    python main_dashboard.py [--config config.yaml] [--port 8080]
"""

import argparse
import logging
import signal
import sys
import threading
import time

import uvicorn
import yaml

from bot.client import PolymarketClient
from bot.events import EventBus
from bot.logger import setup_logger
from bot.mirror import MirrorBot
from bot.monitor import Monitor
from bot.paper_trader import PaperTrader
from dashboard.server import app, set_event_bus, set_mirror_bot


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _stats_emitter(engine: PaperTrader, bus: EventBus) -> None:
    """Emit periodic stats updates so the dashboard stays fresh even between trades."""
    while True:
        time.sleep(3)
        try:
            bus.publish("stats", dict(engine._state))
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Arb Bot — Dashboard")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--port",   type=int, default=8080)
    parser.add_argument("--host",   default="127.0.0.1")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log = setup_logger(cfg["logging"]["log_dir"], cfg["logging"]["level"])

    paper = cfg.get("paper_mode", {}).get("enabled", False)
    if not paper:
        pk = cfg.get("wallet", {}).get("private_key", "")
        if not pk or pk.startswith("0xYOUR"):
            log.error("Configure wallet credentials in config.yaml before running live.")
            sys.exit(1)

    log.info("=" * 60)
    log.info("Polymarket Arb Bot — Dashboard mode")
    log.info("Open http://%s:%d in your browser", args.host, args.port)
    log.info("=" * 60)

    # ── Wiring ───────────────────────────────────────────────────────────────
    bus = EventBus()
    set_event_bus(bus)

    client  = PolymarketClient(cfg)
    engine  = PaperTrader(client, cfg, event_bus=bus)
    monitor = Monitor(client, cfg, on_opportunity=engine.execute, event_bus=bus)

    # ── Mirror Bot ────────────────────────────────────────────────────────────
    mirror_cfg = cfg.get("mirror_mode", {})
    mirror = MirrorBot(
        event_bus=bus,
        starting_balance=float(mirror_cfg.get("starting_balance_usdc", 20_000.0)),
        default_poll_interval=float(mirror_cfg.get("poll_interval_seconds", 30.0)),
    )
    set_mirror_bot(mirror)

    # Pre-load addresses from config
    for entry in mirror_cfg.get("watched_addresses", []):
        mirror.add_address(
            entry["address"],
            entry.get("nickname", entry["address"][:8]),
        )

    # ── Bot thread ────────────────────────────────────────────────────────────
    def _bot():
        bus.publish("bot_start", {"paper_mode": paper})
        try:
            monitor.start()
        except Exception as exc:
            log.error("Bot thread crashed: %s", exc, exc_info=True)

    threading.Thread(target=_bot, daemon=True, name="bot").start()

    # ── Mirror bot thread ─────────────────────────────────────────────────────
    threading.Thread(target=mirror.start, daemon=True, name="mirror").start()

    # ── Periodic stats thread ─────────────────────────────────────────────────
    threading.Thread(
        target=_stats_emitter, args=(engine, bus), daemon=True, name="stats"
    ).start()

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    def _stop(sig, frame):
        log.info("Shutting down.")
        monitor.stop()
        mirror.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT,  _stop)
    signal.signal(signal.SIGTERM, _stop)

    # ── FastAPI server (blocks until killed) ──────────────────────────────────
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",   # suppress uvicorn's own access logs
    )


if __name__ == "__main__":
    main()
