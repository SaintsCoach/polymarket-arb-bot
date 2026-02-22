"""
EdgeTracker — measures the time between our event detection and Polymarket's
price response.

For each DFOpportunity we track, we poll the market price every 3s.
When the price moves ≥ PRICE_MOVE_THRESHOLD, we record the latency.
We give up after MAX_TRACK_WINDOW_S (2 minutes).

Emits
-----
  datafeed_edge_measurement  — per resolved track
  datafeed_edge_stats        — summary stats every 60s
"""

import logging
import statistics
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger("arb_bot.datafeed.edge")

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class PendingEdge:
    event_id: str            # "{fixture_id}_{event_type}_{minute}"
    event_type: str
    event_ts: float
    token_id: str
    market_price_at_detection: float
    expected_direction: str  # "Yes" | "No"
    fixture_id: int
    feed_source: str         # "api_football" | "sportradar"


class EdgeTracker:
    PRICE_MOVE_THRESHOLD = 0.02   # 2-cent move = market repriced
    MAX_TRACK_WINDOW_S   = 120.0  # give up after 2 minutes
    POLL_INTERVAL_S      = 3.0

    def __init__(self, event_bus=None):
        self._bus       = event_bus
        self._pending:  dict[str, PendingEdge] = {}  # event_id → PendingEdge
        self._measurements: list = []
        self._http      = requests.Session()
        self._last_stats_emit = time.time()

    def track(self, event, opp) -> None:
        """Register an opportunity for edge-latency tracking."""
        event_id = f"{event.fixture_id}_{event.event_type}_{event.minute}"
        if event_id in self._pending:
            return  # already tracking this event
        self._pending[event_id] = PendingEdge(
            event_id=event_id,
            event_type=event.event_type,
            event_ts=event.detected_at,
            token_id=opp.token_id,
            market_price_at_detection=opp.market_price,
            expected_direction=opp.outcome,
            fixture_id=event.fixture_id,
            feed_source=getattr(event, "source", "api_football"),
        )
        logger.debug("[edge] tracking %s (token %s, price %.3f)",
                     event_id, opp.token_id, opp.market_price)

    def poll_pending(self, http=None) -> None:
        """
        Check all pending edges against current market prices.
        Called from the edge_loop thread every POLL_INTERVAL_S seconds.
        """
        if not self._pending:
            return

        now     = time.time()
        expired = [eid for eid, p in self._pending.items()
                   if now - p.event_ts > self.MAX_TRACK_WINDOW_S]
        for eid in expired:
            logger.debug("[edge] expired without price move: %s", eid)
            del self._pending[eid]

        if not self._pending:
            return

        token_ids = list({p.token_id for p in self._pending.values()})
        try:
            resp = self._http.get(
                f"{GAMMA_API}/markets",
                params={"clobTokenIds": ",".join(token_ids)},
                timeout=8,
            )
            resp.raise_for_status()
            markets = resp.json() if isinstance(resp.json(), list) else []
        except Exception as exc:
            logger.debug("[edge] price poll error: %s", exc)
            return

        price_map: dict[str, float] = {}
        for mkt in markets:
            try:
                import json as _json
                raw_ids = mkt.get("clobTokenIds", "[]")
                tids    = _json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
                ask     = mkt.get("bestAsk") or mkt.get("bestBid")
                if ask is not None:
                    for tid in tids:
                        price_map[tid] = float(ask)
            except Exception:
                pass

        resolved = []
        for eid, pending in self._pending.items():
            current_price = price_map.get(pending.token_id)
            if current_price is None:
                continue
            delta = abs(current_price - pending.market_price_at_detection)
            if delta >= self.PRICE_MOVE_THRESHOLD:
                latency = time.time() - pending.event_ts
                m = {
                    "event_id":           pending.event_id,
                    "event_type":         pending.event_type,
                    "latency_s":          round(latency, 2),
                    "price_at_detection": round(pending.market_price_at_detection, 4),
                    "price_after_move":   round(current_price, 4),
                    "price_delta":        round(current_price - pending.market_price_at_detection, 4),
                    "detected_at":        pending.event_ts,
                    "price_moved_at":     time.time(),
                    "feed_source":        pending.feed_source,
                }
                self._measurements.append(m)
                if len(self._measurements) > 200:
                    self._measurements = self._measurements[-200:]
                logger.info(
                    "[edge] %s → price moved in %.1fs (delta %+.3f)  [%s]",
                    pending.event_type, latency,
                    current_price - pending.market_price_at_detection,
                    pending.feed_source,
                )
                if self._bus:
                    self._bus.publish("datafeed_edge_measurement", m)
                resolved.append(eid)

        for eid in resolved:
            del self._pending[eid]

        # Emit summary stats every 60s
        if time.time() - self._last_stats_emit >= 60:
            stats = self.get_stats()
            if self._bus and stats["total_tracked"] > 0:
                self._bus.publish("datafeed_edge_stats", stats)
            self._last_stats_emit = time.time()

    def get_measurements(self) -> list:
        return list(self._measurements)

    def get_stats(self) -> dict:
        if not self._measurements:
            return {
                "total_tracked": 0,
                "avg_latency_s": None,
                "p50_latency_s": None,
                "p95_latency_s": None,
            }
        latencies = [m["latency_s"] for m in self._measurements]
        latencies_sorted = sorted(latencies)
        n   = len(latencies_sorted)
        p50 = latencies_sorted[n // 2]
        p95 = latencies_sorted[min(int(n * 0.95), n - 1)]
        return {
            "total_tracked": n,
            "avg_latency_s": round(statistics.mean(latencies), 2),
            "p50_latency_s": round(p50, 2),
            "p95_latency_s": round(p95, 2),
        }
