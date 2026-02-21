#!/usr/bin/env python3
"""
Polymarket Sports Arbitrage Bot
================================
Entry point — loads config, wires components, starts the monitoring loop.

Usage:
    python main.py [--config path/to/config.yaml]
"""

import argparse
import logging
import signal
import sys

import yaml

from bot.client import PolymarketClient
from bot.logger import setup_logger
from bot.monitor import Monitor


def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    _validate_config(cfg)
    return cfg


def _validate_config(cfg: dict) -> None:
    """Raise ValueError for obviously wrong config values."""
    s = cfg.get("strategy", {})
    if s.get("min_profit_threshold_pct", 0) <= 0:
        raise ValueError("min_profit_threshold_pct must be > 0")
    if s.get("slippage_tolerance_pct", 0) < 0:
        raise ValueError("slippage_tolerance_pct must be >= 0")

    paper = cfg.get("paper_mode", {}).get("enabled", False)
    if not paper:
        pk = cfg.get("wallet", {}).get("private_key", "")
        if not pk or pk.startswith("0xYOUR"):
            raise ValueError("wallet.private_key is not configured in config.yaml")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Sports Arbitrage Bot")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log = setup_logger(
        log_dir=cfg["logging"]["log_dir"],
        level=cfg["logging"]["level"],
    )
    log.info("=" * 60)
    log.info("Polymarket Arb Bot starting")
    log.info(
        "Config: min_profit=%.1f%% | max_trade=%s USDC | max_risk=%s USDC | "
        "slippage=%.1f%% | min_liq=%s USDC | interval=%ds",
        cfg["strategy"]["min_profit_threshold_pct"],
        cfg["strategy"]["max_trade_size_usdc"],
        cfg["strategy"]["max_risk_per_trade_usdc"],
        cfg["strategy"]["slippage_tolerance_pct"],
        cfg["strategy"]["min_liquidity_usdc"],
        cfg["strategy"]["polling_interval_seconds"],
    )
    log.info("=" * 60)

    client = PolymarketClient(cfg)

    paper = cfg.get("paper_mode", {}).get("enabled", False)
    if paper:
        from bot.paper_trader import PaperTrader, TradeOutcome
        engine = PaperTrader(client, cfg)
        log.info("[PAPER MODE] Virtual balance: %.2f USDC", cfg["paper_mode"]["starting_balance_usdc"])
    else:
        from bot.executor import Executor, TradeOutcome
        engine = Executor(client, cfg)
        balance = client.get_usdc_balance()
        log.info("Wallet USDC balance: %.2f", balance)

    def on_opportunity(opp):
        result = engine.execute(opp)
        prefix = "[PAPER] " if paper else ""
        if result.outcome == TradeOutcome.SUCCESS:
            log.info(
                "%sTRADE SUCCESS | profit=%.4f USDC | market=%s",
                prefix, result.profit_usdc, opp.market_question[:60],
            )
            if paper:
                engine.print_summary()
        else:
            log.info(
                "%sTRADE %s | %s | %s",
                prefix, result.outcome.value, opp.market_question[:60], result.reason,
            )

    monitor = Monitor(client, cfg, on_opportunity=on_opportunity)

    def _shutdown(signum, frame):
        log.info("Shutdown signal received — stopping.")
        monitor.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    monitor.start()


if __name__ == "__main__":
    main()
