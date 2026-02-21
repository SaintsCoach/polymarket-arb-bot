"""
DataFeedBot — polls API-Football for live soccer events, detects scoring/red-card
events before Polymarket reprices, and paper-trades the edge.

Architecture mirrors MirrorBot:
  - _poll_loop  (daemon thread): fetch live fixtures → diff → detect opportunities
  - _price_loop (daemon thread): update prices + close resolved markets every 30s
  - EventBus: publishes datafeed_* events consumed by the dashboard
"""

import dataclasses
import logging
import threading
import time

import requests

from .feeds.football import FootballFeed
from .market_matcher import MarketMatcher
from .opportunity_detector import OpportunityDetector
from .portfolio import DataFeedPortfolio

logger = logging.getLogger("arb_bot.datafeed")


class DataFeedBot:
    def __init__(
        self,
        event_bus,
        api_key: str,
        starting_balance: float = 20_000.0,
        poll_interval: float    = 30.0,
        min_edge_pct: float     = 3.0,
        entry_window_s: float   = 45.0,
    ):
        self._bus      = event_bus
        self._running  = False
        self.start_ts  = 0.0
        self._interval = poll_interval
        self._http     = requests.Session()
        self.feed      = FootballFeed(api_key, bus=event_bus)
        self.matcher   = MarketMatcher(self._http)
        self.detector  = OpportunityDetector(min_edge_pct, entry_window_s)
        self.portfolio = DataFeedPortfolio(event_bus, starting_balance)

    def start(self) -> None:
        self._running = True
        self.start_ts = time.time()
        self._emit_initial_state()
        threading.Thread(
            target=self._poll_loop, daemon=True, name="datafeed-poller"
        ).start()
        threading.Thread(
            target=self._price_loop, daemon=True, name="datafeed-prices"
        ).start()
        logger.info("DataFeedBot started (poll_interval=%.0fs)", self._interval)

    def stop(self) -> None:
        self._running = False
        logger.info("DataFeedBot stopped")

    def reset(self) -> None:
        self.start_ts = time.time()
        self.portfolio.reset()
        if self._bus:
            self._bus.publish("datafeed_start", {"ts": self.start_ts})
        logger.info("DataFeedBot reset")

    def snapshot(self) -> dict:
        return {
            "overview":  self.portfolio.get_overview(),
            "positions": self.portfolio.get_positions(),
            "resolved":  self.portfolio.get_resolved(),
            "start_ts":  self.start_ts,
        }

    # ── Loops ─────────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            try:
                events = self.feed.poll()
                for evt in events:
                    self._bus.publish("datafeed_live_event", self._event_to_dict(evt))
                    market = self.matcher.find_market(evt)
                    if market:
                        opp = self.detector.evaluate(evt, market)
                        if opp:
                            self._bus.publish(
                                "datafeed_opportunity", dataclasses.asdict(opp)
                            )
                            self.portfolio.open_position(opp)
            except Exception as exc:
                logger.error("DataFeedBot poll error: %s", exc)
            time.sleep(self._interval)

    def _price_loop(self) -> None:
        while self._running:
            time.sleep(30)
            try:
                self.portfolio.update_prices(self._http)
                self.portfolio.close_resolved_markets(self._http)
            except Exception as exc:
                logger.warning("DataFeedBot price update error: %s", exc)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _emit_initial_state(self) -> None:
        if not self._bus:
            return
        snap = self.snapshot()
        self._bus.publish("datafeed_start",     {"ts": self.start_ts})
        self._bus.publish("datafeed_overview",  snap["overview"])
        self._bus.publish("datafeed_positions", {"positions": snap["positions"]})

    def _event_to_dict(self, evt) -> dict:
        return {
            "fixture_id":  evt.fixture_id,
            "home_team":   evt.home_team,
            "away_team":   evt.away_team,
            "home_score":  evt.home_score,
            "away_score":  evt.away_score,
            "minute":      evt.minute,
            "event_type":  evt.event_type,
            "detected_at": evt.detected_at,
        }
