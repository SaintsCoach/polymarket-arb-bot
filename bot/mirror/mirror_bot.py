"""
MirrorBot — orchestrates AddressMonitor + PortfolioManager.

Wires the two subsystems together:
  - AddressMonitor fires on_position_opened / on_position_closed
  - PortfolioManager handles slot allocation and P&L tracking
  - Periodic price updater refreshes unrealized P&L every 30 s
"""

import logging
import threading
import time

import requests

from .address_monitor import AddressMonitor
from .portfolio import PortfolioManager

logger = logging.getLogger("arb_bot.mirror.bot")

PRICE_UPDATE_INTERVAL = 30.0  # seconds between bulk price refreshes


class MirrorBot:
    def __init__(self, event_bus=None, starting_balance: float = 20_000.0,
                 default_poll_interval: float = 30.0):
        self._bus = event_bus
        self._running = False
        self.start_ts: float = 0.0
        self._http = requests.Session()
        self._http.headers.update({"Accept": "application/json"})

        self.portfolio = PortfolioManager(
            event_bus=event_bus,
            starting_balance=starting_balance,
        )
        self.monitor = AddressMonitor(
            on_position_opened=self._on_opened,
            on_position_closed=self._on_closed,
            event_bus=event_bus,
            default_interval=default_poll_interval,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self.start_ts = time.time()
        self.monitor.start()
        threading.Thread(
            target=self._price_update_loop,
            daemon=True,
            name="mirror-price-updater",
        ).start()
        logger.info("MirrorBot started")
        self._emit_initial_state()

    def stop(self) -> None:
        self._running = False
        self.monitor.stop()
        logger.info("MirrorBot stopped")

    # ── Address management (proxy to monitor) ─────────────────────────────────

    def add_address(self, address: str, nickname: str,
                    poll_interval: float = None) -> dict:
        return self.monitor.add_address(address, nickname, poll_interval)

    def remove_address(self, address: str) -> bool:
        return self.monitor.remove_address(address)

    def update_address(self, address: str, nickname: str = None,
                       enabled: bool = None) -> bool:
        return self.monitor.update_address(address, nickname, enabled)

    def get_addresses(self) -> list[dict]:
        return self.monitor.get_addresses()

    # ── REST snapshot helpers (called by dashboard server) ───────────────────

    def snapshot(self) -> dict:
        return {
            "overview":   self.portfolio.get_overview(),
            "positions":  self.portfolio.get_positions(),
            "queue":      self.portfolio.get_queue(),
            "resolved":   self.portfolio.get_resolved(),
            "addresses":  self.monitor.get_addresses(),
        }

    # ── Callbacks from AddressMonitor ─────────────────────────────────────────

    def _on_opened(self, cfg, pos_data: dict) -> None:
        """Called when a watched address opens a new position."""
        title = pos_data.get("title", "?")[:55]
        logger.info("[mirror] %s opened → %s", cfg.nickname, title)
        self.portfolio.open_position(cfg, pos_data)

    def _on_closed(self, cfg, pos_data: dict) -> None:
        """Called when a watched address closes an existing position."""
        title = pos_data.get("title", "?")[:55]
        logger.info("[mirror] %s closed → %s", cfg.nickname, title)
        self.portfolio.close_position_by_token(cfg, pos_data)

    # ── Periodic price updater ────────────────────────────────────────────────

    def _price_update_loop(self) -> None:
        while self._running:
            time.sleep(PRICE_UPDATE_INTERVAL)
            if self._running:
                try:
                    self.portfolio.update_prices(self._http)
                except Exception as exc:
                    logger.warning("Price update loop error: %s", exc)

    def reset(self) -> None:
        """Clear portfolio and re-snapshot all addresses."""
        self.start_ts = time.time()
        self.portfolio.reset()
        self.monitor.reset_all()
        if self._bus:
            self._bus.publish("mirror_bot_start", {"ts": self.start_ts})
            self._bus.publish("mirror_overview",  self.portfolio.get_overview())
            self._bus.publish("mirror_positions", {"positions": []})
            self._bus.publish("mirror_queue",     {"queue": []})
        logger.info("MirrorBot reset — fresh baseline at %s", time.ctime(self.start_ts))

    # ── Initial state push ────────────────────────────────────────────────────

    def _emit_initial_state(self) -> None:
        """Push current state immediately on start so dashboard isn't blank."""
        if not self._bus:
            return
        snap = self.snapshot()
        self._bus.publish("mirror_bot_start",  {"ts": self.start_ts})
        self._bus.publish("mirror_overview",   snap["overview"])
        self._bus.publish("mirror_positions",  {"positions": snap["positions"]})
        self._bus.publish("mirror_queue",      {"queue": snap["queue"]})
        self._bus.publish("mirror_addresses",  {"addresses": snap["addresses"]})
