"""
Opportunity detection: maps a LiveEvent + Polymarket market to a DFOpportunity
when fair-value probability differs from market price by at least min_edge_pct.

WIN_PROB_TABLE keys: (goal_diff, time_band)
  goal_diff: home_score - away_score, clipped to [-2, 2]
  time_band: "first_half" (minute <= 45) or "second_half"
Values: (home_win_prob, draw_prob, away_win_prob)
"""

import json
import logging
import time

from .models import DFOpportunity

logger = logging.getLogger("arb_bot.datafeed.detector")

GAMMA_API = "https://gamma-api.polymarket.com"

# Pre-match base rate (used for match_start events as a sanity check)
_BASE = (0.45, 0.27, 0.28)

WIN_PROB_TABLE: dict = {
    # (goal_diff, time_band): (home_win, draw, away_win)
    (-2, "first_half"):  (0.08, 0.14, 0.78),
    (-2, "second_half"): (0.04, 0.08, 0.88),
    (-1, "first_half"):  (0.20, 0.28, 0.52),
    (-1, "second_half"): (0.12, 0.20, 0.68),
    (0,  "first_half"):  (0.40, 0.30, 0.30),
    (0,  "second_half"): (0.35, 0.38, 0.27),
    (1,  "first_half"):  (0.62, 0.24, 0.14),
    (1,  "second_half"): (0.72, 0.20, 0.08),
    (2,  "first_half"):  (0.80, 0.12, 0.08),
    (2,  "second_half"): (0.90, 0.06, 0.04),
}

# Red card roughly shifts win probabilities by ±10–15% depending on which team
_RED_CARD_HOME_PENALTY = 0.12   # home team gets red → subtract from home_win
_RED_CARD_AWAY_PENALTY = 0.12   # away team gets red → add to home_win


class OpportunityDetector:
    def __init__(self, min_edge_pct: float = 3.0, entry_window_s: float = 45.0):
        self._min_edge = min_edge_pct / 100.0
        self._entry_window = entry_window_s

    def evaluate(self, event, market: dict) -> "DFOpportunity | None":
        """
        Evaluate a LiveEvent against a matched Polymarket market.
        Returns a DFOpportunity or None if no actionable edge found.
        """
        # Only trade on goal and red_card events within the entry window
        if event.event_type not in ("goal", "red_card"):
            return None

        age = time.time() - event.detected_at
        if age > self._entry_window:
            return None

        # Compute fair-value for home win
        fair_home_win = self._fair_value(event)
        if fair_home_win is None:
            return None

        # Get market price (bestAsk for Yes token on first clobTokenId)
        token_id, market_price = self._get_market_price(market)
        if token_id is None or market_price is None:
            return None

        edge = fair_home_win - market_price
        if abs(edge) < self._min_edge:
            return None

        outcome = "Yes" if edge > 0 else "No"
        # For "No", the effective fair value is 1 - fair_home_win
        effective_fv = fair_home_win if outcome == "Yes" else (1.0 - fair_home_win)
        edge_pct = abs(edge) * 100

        source = self._describe_event(event)

        return DFOpportunity(
            fixture_id=event.fixture_id,
            market_id=market.get("id") or market.get("conditionId", ""),
            market_question=(market.get("question") or market.get("title", ""))[:100],
            token_id=token_id,
            outcome=outcome,
            fair_value=round(effective_fv, 4),
            market_price=round(market_price, 4),
            edge_pct=round(edge_pct, 2),
            source_event=source,
            detected_at=event.detected_at,
        )

    def _fair_value(self, event) -> "float | None":
        goal_diff = max(-2, min(2, event.home_score - event.away_score))
        time_band = "first_half" if event.minute <= 45 else "second_half"
        probs = WIN_PROB_TABLE.get((goal_diff, time_band))
        if probs is None:
            return None

        home_win, draw, away_win = probs

        # Adjust for red card (simple heuristic — we don't know which team)
        if event.event_type == "red_card":
            # Assume the trailing team is more likely to get a red (frustration)
            if event.home_score <= event.away_score:
                home_win = max(0.01, home_win - _RED_CARD_HOME_PENALTY)
            else:
                home_win = min(0.99, home_win + _RED_CARD_AWAY_PENALTY)

        return home_win

    def _get_market_price(self, market: dict) -> "tuple[str | None, float | None]":
        """Extract token_id and bestAsk from a Gamma market dict."""
        try:
            raw_ids = market.get("clobTokenIds", "[]")
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            if not token_ids:
                return None, None
            token_id = token_ids[0]

            best_ask = market.get("bestAsk")
            if best_ask is None:
                return None, None
            return token_id, float(best_ask)
        except Exception:
            return None, None

    def _describe_event(self, event) -> str:
        if event.event_type == "goal":
            return f"goal {event.home_score}-{event.away_score} min {event.minute}"
        if event.event_type == "red_card":
            return f"red card min {event.minute} ({event.home_score}-{event.away_score})"
        return f"{event.event_type} min {event.minute}"
