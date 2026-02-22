"""
DataFeedBot — polls API-Football + Sportradar for live sports events, detects
scoring/red-card events before Polymarket reprices, and paper-trades the edge.

Threads
-------
  _football_loop  : poll API-Football every 15s
  _sportradar_loop: poll Sportradar every 30s
  _price_loop     : update prices + close resolved markets every 30s
  _edge_loop      : EdgeTracker.poll_pending() every 3s

MirrorBot reference is used to build the RN1 team watchlist (lowers the market
matching threshold for games RN1 is currently trading).
"""

import dataclasses
import logging
import threading
import time

import requests

from .edge_tracker import EdgeTracker
from .feeds.football import FootballFeed
from .feeds.sportradar import SportradarFeed
from .market_matcher import MarketMatcher
from .opportunity_detector import OpportunityDetector
from .portfolio import DataFeedPortfolio

logger = logging.getLogger("arb_bot.datafeed")

# Dedup cache TTL — suppress duplicate events for the same match + event type
DEDUP_TTL_S = 90.0


class DataFeedBot:
    def __init__(
        self,
        event_bus,
        api_key: str,
        starting_balance: float = 20_000.0,
        poll_interval: float    = 15.0,
        min_edge_pct: float     = 3.0,
        entry_window_s: float   = 45.0,
        sportradar_key: str     = "",
        sportradar_poll: float  = 30.0,
        edge_tracker_poll_s: float         = 3.0,
        edge_price_move_threshold: float   = 0.02,
        mirror_bot=None,
    ):
        self._bus             = event_bus
        self._running         = False
        self.start_ts         = 0.0
        self._interval        = poll_interval
        self._sr_interval     = sportradar_poll
        self._edge_poll_s     = edge_tracker_poll_s
        self._mirror_bot      = mirror_bot
        self._http            = requests.Session()

        self.feed_football   = FootballFeed(api_key, bus=event_bus)
        self.feed_sportradar = SportradarFeed(sportradar_key, bus=event_bus)
        self.matcher         = MarketMatcher(self._http)
        self.detector        = OpportunityDetector(min_edge_pct, entry_window_s)
        self.portfolio       = DataFeedPortfolio(event_bus, starting_balance)
        self.edge_tracker    = EdgeTracker(event_bus)

        # Set custom threshold on tracker
        self.edge_tracker.PRICE_MOVE_THRESHOLD = edge_price_move_threshold

        # Dedup cache: "{home}_{away}_{event_type}_{minute}" → timestamp
        self._seen_events: dict[str, float] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self.start_ts = time.time()
        self._emit_initial_state()

        threading.Thread(
            target=self._football_loop, daemon=True, name="datafeed-football"
        ).start()
        threading.Thread(
            target=self._sportradar_loop, daemon=True, name="datafeed-sportradar"
        ).start()
        threading.Thread(
            target=self._price_loop, daemon=True, name="datafeed-prices"
        ).start()
        threading.Thread(
            target=self._edge_loop, daemon=True, name="datafeed-edge"
        ).start()

        logger.info(
            "DataFeedBot started (football=%.0fs, sportradar=%.0fs, edge=%.0fs)",
            self._interval, self._sr_interval, self._edge_poll_s,
        )

    def stop(self) -> None:
        self._running = False
        logger.info("DataFeedBot stopped")

    def reset(self) -> None:
        self.start_ts = time.time()
        self.portfolio.reset()
        self._seen_events.clear()
        if self._bus:
            self._bus.publish("datafeed_start", {"ts": self.start_ts})
        logger.info("DataFeedBot reset")

    def snapshot(self) -> dict:
        return {
            "overview":          self.portfolio.get_overview(),
            "positions":         self.portfolio.get_positions(),
            "resolved":          self.portfolio.get_resolved(),
            "start_ts":          self.start_ts,
            "edge_measurements": self.edge_tracker.get_measurements(),
            "edge_stats":        self.edge_tracker.get_stats(),
        }

    # ── Loops ──────────────────────────────────────────────────────────────────

    def _football_loop(self) -> None:
        while self._running:
            try:
                events = self.feed_football.poll()
                for evt in events:
                    self._handle_event(evt)
            except Exception as exc:
                logger.error("[df-football] poll error: %s", exc)
            time.sleep(self._interval)

    def _sportradar_loop(self) -> None:
        while self._running:
            try:
                events = self.feed_sportradar.poll(
                    watched_sports={"soccer"}  # expand to {"soccer","nba"} when needed
                )
                for evt in events:
                    self._handle_event(evt)
            except Exception as exc:
                logger.error("[df-sportradar] poll error: %s", exc)
            time.sleep(self._sr_interval)

    def _price_loop(self) -> None:
        while self._running:
            time.sleep(30)
            try:
                self.portfolio.update_prices(self._http)
                self.portfolio.close_resolved_markets(self._http)
            except Exception as exc:
                logger.warning("DataFeedBot price update error: %s", exc)

    def _edge_loop(self) -> None:
        while self._running:
            time.sleep(self._edge_poll_s)
            try:
                self.edge_tracker.poll_pending()
            except Exception as exc:
                logger.debug("[edge] poll error: %s", exc)

    # ── Event handling ─────────────────────────────────────────────────────────

    def _handle_event(self, evt) -> None:
        """Process one LiveEvent: dedup → publish → match markets → detect → open."""
        dedup_key = (
            f"{evt.home_team.lower()}_{evt.away_team.lower()}"
            f"_{evt.event_type}_{evt.minute}"
        )
        now = time.time()

        # Expire old dedup entries
        expired = [k for k, ts in self._seen_events.items()
                   if now - ts > DEDUP_TTL_S]
        for k in expired:
            del self._seen_events[k]

        if dedup_key in self._seen_events:
            return   # duplicate across feeds
        self._seen_events[dedup_key] = now

        # Publish to dashboard
        if self._bus:
            self._bus.publish("datafeed_live_event", self._event_to_dict(evt))

        # Only look for opportunities on goal/red_card
        if evt.event_type not in ("goal", "red_card"):
            return

        # Primary: match against Mirror Bot's known active positions
        rn1_positions = self._get_rn1_positions()
        markets       = self.matcher.find_markets_from_positions(evt, rn1_positions)

        # Fallback: Gamma API (covers non-RN1 markets)
        if not markets:
            rn1_teams = self._get_rn1_teams()
            markets   = self.matcher.find_all_markets(evt, rn1_teams=rn1_teams)

        opps = self.detector.evaluate_all(evt, markets)

        for opp in opps:
            if self._bus:
                self._bus.publish("datafeed_opportunity", dataclasses.asdict(opp))
            pos = self.portfolio.open_position(opp)
            if pos:
                self.edge_tracker.track(evt, opp)

    # ── RN1 watchlist ──────────────────────────────────────────────────────────

    def _get_rn1_teams(self) -> set:
        """
        Extract team names from MirrorBot's current open positions so we can
        lower the market-matching threshold for games RN1 is trading.
        """
        if self._mirror_bot is None:
            return set()
        try:
            snap      = self._mirror_bot.snapshot()
            positions = snap.get("positions", [])
            teams: set = set()
            for pos in positions:
                question = (pos.get("title") or pos.get("market_question") or "").lower()
                # Crude extraction: every word of 4+ chars is a candidate team token
                words = [w for w in question.split() if len(w) >= 4
                         and w not in {"will", "beat", "wins", "over", "draw"}]
                teams.update(words)
            return teams
        except Exception:
            return set()

    def _get_rn1_positions(self) -> list:
        """Return the raw Mirror Bot position dicts (confirmed-active markets)."""
        if self._mirror_bot is None:
            return []
        try:
            snap = self._mirror_bot.snapshot()
            return snap.get("positions", [])
        except Exception:
            return []

    # ── Helpers ────────────────────────────────────────────────────────────────

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
            "source":      getattr(evt, "source", "api_football"),
        }
