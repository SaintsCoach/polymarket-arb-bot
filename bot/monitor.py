"""
Market monitoring loop.

Performance strategy:
  1. Gamma pre-screen — use bestAsk/bestBid already in each market's Gamma
     response to estimate combined price with zero extra HTTP calls.
     Only markets where the estimate falls below the arb threshold proceed.
  2. Parallel order-book fetch — confirmed candidates get their actual order
     books fetched concurrently (ThreadPoolExecutor) to confirm the arb and
     get precise prices before execution.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from .arbitrage import ArbOpportunity, find_arb_opportunity, extract_market_token_ids
from .client import PolymarketClient

logger = logging.getLogger("arb_bot")
opp_log = logging.getLogger("arb_bot.opportunities")
error_log = logging.getLogger("arb_bot.errors")

# Candidates pass the Gamma pre-screen if their estimated combined ask is
# below this. Set slightly above the actual threshold to allow for estimation
# error (actual NO ask may differ from the implied estimate).
_PRESCREEN_BUFFER = 0.02


class Monitor:
    def __init__(
        self,
        client: PolymarketClient,
        cfg: dict,
        on_opportunity: Callable[[ArbOpportunity], None],
        event_bus=None,
    ):
        self._client = client
        self._strategy = cfg["strategy"]
        self._filters = cfg["filters"]
        self._on_opportunity = on_opportunity
        self._bus = event_bus
        self._running = False
        self._min_profit = self._strategy["min_profit_threshold_pct"] / 100
        self._prescreen_threshold = 1.0 - self._min_profit + _PRESCREEN_BUFFER

    def start(self) -> None:
        """Block and poll indefinitely until stop() is called."""
        self._running = True
        interval = self._strategy["polling_interval_seconds"]
        logger.info("Monitor started — polling every %ds", interval)
        while self._running:
            try:
                self._scan()
            except Exception as exc:
                error_log.error("Unexpected scan error: %s", exc, exc_info=True)
            time.sleep(interval)

    def stop(self) -> None:
        self._running = False

    # ── Internal ──────────────────────────────────────────────────────────────

    def _scan(self) -> None:
        t0 = time.time()
        markets = self._client.get_sports_markets(self._filters["sports_tags"])
        logger.info("Fetched %d unique sports markets", len(markets))

        # ── Step 1: Gamma pre-screen (zero extra API calls) ───────────────────
        candidates = [m for m in markets if self._gamma_prescreen(m)]
        scan_ms = int((time.time() - t0) * 1000)
        logger.info(
            "Pre-screen: %d/%d markets pass initial price estimate",
            len(candidates), len(markets),
        )

        if self._bus:
            self._bus.publish("scan", {
                "markets_total": len(markets),
                "candidates": len(candidates),
                "scan_ms": scan_ms,
            })
            if candidates:
                self._bus.publish("candidates", {
                    "markets": [
                        {
                            "question": m.get("question", "?")[:80],
                            "combined_est": round(
                                float(m.get("bestAsk") or 0) + (1.0 - float(m.get("bestBid") or 1)), 4
                            ),
                        }
                        for m in candidates
                    ]
                })

        if not candidates:
            return

        # ── Step 2: Confirm with real order books (parallel) ──────────────────
        with ThreadPoolExecutor(max_workers=min(len(candidates), 10)) as pool:
            futures = {pool.submit(self._check_market, m): m for m in candidates}
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    market = futures[future]
                    error_log.error(
                        "Market check error [%s]: %s",
                        market.get("conditionId", "?"), exc,
                    )

    def _gamma_prescreen(self, market: dict) -> bool:
        """
        Fast filter using data already in the Gamma response — no extra HTTP calls.

        YES ask  = market['bestAsk']   (the YES token's live best ask)
        NO ask   ≈ 1 - market['bestBid'] (implied: if someone pays bestBid for
                   YES, buying NO instead costs at most 1 - bestBid)

        If YES_ask + implied_NO_ask < threshold, worth a real order-book check.
        """
        try:
            best_ask = market.get("bestAsk")
            best_bid = market.get("bestBid")
            if best_ask is None or best_bid is None:
                return True  # no price data — include and let order book decide

            yes_ask = float(best_ask)
            implied_no_ask = 1.0 - float(best_bid)

            if not (0 < yes_ask < 1) or not (0 < implied_no_ask < 1):
                return False

            combined_est = yes_ask + implied_no_ask
            logger.debug(
                "  [pre] %s | YES_ask=%.4f impl_NO=%.4f combined_est=%.4f",
                market.get("question", "?")[:50], yes_ask, implied_no_ask, combined_est,
            )
            return combined_est < self._prescreen_threshold
        except (TypeError, ValueError):
            return True  # parse error — include to be safe

    def _check_market(self, market: dict) -> None:
        """Fetch real order books and trigger execution if arb confirmed."""
        yes_id, no_id = extract_market_token_ids(market)
        if not yes_id or not no_id:
            return

        yes_ask = self._client.get_best_ask(yes_id)
        no_ask = self._client.get_best_ask(no_id)

        if yes_ask is None or no_ask is None:
            return
        if not (0 < yes_ask < 1) or not (0 < no_ask < 1):
            return

        combined_pct = (yes_ask + no_ask) * 100
        logger.debug(
            "  [book] %s | YES=%.4f NO=%.4f combined=%.2f%%",
            market.get("question", "?")[:55], yes_ask, no_ask, combined_pct,
        )

        opp = find_arb_opportunity(
            market=market,
            yes_ask=yes_ask,
            no_ask=no_ask,
            max_trade_size_usdc=self._strategy["max_trade_size_usdc"],
            max_risk_per_trade_usdc=self._strategy["max_risk_per_trade_usdc"],
            min_profit_pct=self._strategy["min_profit_threshold_pct"],
        )

        if opp is not None:
            opp_log.info(
                "FOUND | combined=%.2f%% | profit=%.2f%% | est_profit=%.4f USDC | %s",
                opp.combined_pct,
                opp.expected_profit_pct,
                opp.estimated_profit_usdc,
                opp.market_question[:70],
            )
            if self._bus:
                self._bus.publish("opportunity", {
                    "question": opp.market_question[:80],
                    "yes_ask": opp.yes_ask,
                    "no_ask": opp.no_ask,
                    "combined_pct": round(opp.combined_pct, 3),
                    "profit_pct": round(opp.expected_profit_pct, 3),
                    "est_profit_usdc": round(opp.estimated_profit_usdc, 4),
                })
            self._on_opportunity(opp)
