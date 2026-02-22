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
from bot.crypto_arb import CryptoArbBot
from bot.datafeed import DataFeedBot
from bot.mirror import MirrorBot
from bot.monitor import Monitor
from bot.paper_trader import PaperTrader
from dashboard.server import app, set_event_bus, set_mirror_bot, set_datafeed_bot, set_crypto_arb_bot


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

    # ── DataFeed Bot ──────────────────────────────────────────────────────────
    df_cfg = cfg.get("datafeed_mode", {})
    if df_cfg.get("enabled", False):
        datafeed = DataFeedBot(
            event_bus=bus,
            api_key=df_cfg.get("api_football_key", ""),
            starting_balance=float(df_cfg.get("starting_balance_usdc", 20_000.0)),
            poll_interval=float(df_cfg.get("poll_interval_seconds", 15.0)),
            min_edge_pct=float(df_cfg.get("min_edge_pct", 3.0)),
            entry_window_s=float(df_cfg.get("entry_window_seconds", 45)),
            sportradar_key=df_cfg.get("sportradar_api_key", ""),
            sportradar_poll=float(df_cfg.get("sportradar_poll_seconds", 30.0)),
            edge_tracker_poll_s=float(df_cfg.get("edge_tracker_poll_s", 3.0)),
            edge_price_move_threshold=float(df_cfg.get("edge_price_move_threshold", 0.02)),
            mirror_bot=mirror,
        )
        set_datafeed_bot(datafeed)
        threading.Thread(target=datafeed.start, daemon=True, name="datafeed").start()

    # ── Crypto Arb Bot ────────────────────────────────────────────────────────
    ca_cfg = cfg.get("crypto_arb_mode", {})
    if ca_cfg.get("enabled", False):
        crypto_arb = CryptoArbBot(
            event_bus=bus,
            starting_balance=float(ca_cfg.get("starting_balance_usdc", 20_000.0)),
            scan_interval=float(ca_cfg.get("scan_interval_seconds", 35.0)),
            min_profit_pct=float(ca_cfg.get("min_profit_pct", 0.5)),
            max_position_usdc=float(ca_cfg.get("max_position_usdc", 500.0)),
            max_position_pct=float(ca_cfg.get("max_position_pct", 0.02)),
            min_volume_usdc=float(ca_cfg.get("min_24h_volume_usdc", 100_000.0)),
            order_book_depth=int(ca_cfg.get("order_book_depth", 10)),
            min_book_age_s=float(ca_cfg.get("min_order_book_age_s", 60.0)),
        )
        set_crypto_arb_bot(crypto_arb)
        threading.Thread(target=crypto_arb.start, daemon=True, name="crypto-arb").start()

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
