"""
Opportunity detection: maps a LiveEvent + matched markets to DFOpportunity objects
when fair-value probability differs from market price by at least min_edge_pct.

Supports:
  - game_winner  — lookup table (WIN_PROB_TABLE)
  - over_under   — Poisson model
  - btts         — static/pass-through (no model yet)
"""

import logging
import math
import time

from .models import DFOpportunity, MarketType, MatchedMarket

logger = logging.getLogger("arb_bot.datafeed.detector")

# ── Win probability table ─────────────────────────────────────────────────────
# (goal_diff, time_band): (home_win, draw, away_win)
_BASE = (0.45, 0.27, 0.28)

WIN_PROB_TABLE: dict = {
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

_RED_CARD_HOME_PENALTY = 0.12
_RED_CARD_AWAY_PENALTY = 0.12

# ── Poisson O/U model ─────────────────────────────────────────────────────────
GOALS_PER_MIN = 2.6 / 90.0


def _poisson_pmf(k: int, lam: float) -> float:
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def p_over(line: float, current_goals: int, minutes_remaining: float) -> float:
    """
    Probability that total goals will exceed `line` given current state.

    "Over 2.5" settles True when total goals >= 3 (i.e. int(line) + 1).
    "Over 3.0" settles True when total goals >= 4.
    """
    needed_total = int(line) + 1        # minimum total goals to win the over
    needed       = needed_total - current_goals
    if needed <= 0:
        return 1.0
    if minutes_remaining <= 0:
        return 0.0
    lam = GOALS_PER_MIN * minutes_remaining
    prob_fewer = sum(_poisson_pmf(k, lam) for k in range(needed))
    return max(0.0, min(1.0, 1.0 - prob_fewer))


class OpportunityDetector:
    def __init__(self, min_edge_pct: float = 3.0, entry_window_s: float = 45.0):
        self._min_edge    = min_edge_pct / 100.0
        self._entry_window = entry_window_s

    # ── Public: batch evaluate ────────────────────────────────────────────────

    def evaluate_all(self, event, markets: list) -> list:
        """
        Evaluate a LiveEvent against all matched markets.
        Returns list[DFOpportunity] (may be empty).
        """
        if event.event_type not in ("goal", "red_card"):
            return []
        age = time.time() - event.detected_at
        if age > self._entry_window:
            return []

        results = []
        for market in markets:
            if market.market_type == MarketType.GAME_WINNER:
                opp = self._evaluate_winner(event, market)
            elif market.market_type == MarketType.OVER_UNDER:
                opp = self._evaluate_ou(event, market)
            else:
                opp = None   # BTTS: no model yet
            if opp is not None:
                results.append(opp)
        return results

    # ── Legacy single-market API ──────────────────────────────────────────────

    def evaluate(self, event, market: dict) -> "DFOpportunity | None":
        """
        Evaluate a LiveEvent against a raw Polymarket market dict.
        Kept for backward compatibility.
        """
        if event.event_type not in ("goal", "red_card"):
            return None
        age = time.time() - event.detected_at
        if age > self._entry_window:
            return None

        fair_home_win = self._fair_value_winner(event)
        if fair_home_win is None:
            return None

        token_id, market_price = self._get_market_price(market)
        if token_id is None or market_price is None:
            return None

        edge = fair_home_win - market_price
        if abs(edge) < self._min_edge:
            return None

        outcome = "Yes" if edge > 0 else "No"
        effective_fv = fair_home_win if outcome == "Yes" else (1.0 - fair_home_win)

        return DFOpportunity(
            fixture_id=event.fixture_id,
            market_id=market.get("id") or market.get("conditionId", ""),
            market_question=(market.get("question") or market.get("title", ""))[:100],
            token_id=token_id,
            outcome=outcome,
            fair_value=round(effective_fv, 4),
            market_price=round(market_price, 4),
            edge_pct=round(abs(edge) * 100, 2),
            source_event=self._describe_event(event),
            detected_at=event.detected_at,
            market_type="game_winner",
        )

    # ── Game-winner evaluator ─────────────────────────────────────────────────

    def _evaluate_winner(self, event, market: MatchedMarket) -> "DFOpportunity | None":
        fair_home_win = self._fair_value_winner(event)
        if fair_home_win is None:
            return None

        market_price = market.current_price
        edge = fair_home_win - market_price
        if abs(edge) < self._min_edge:
            return None

        outcome      = "Yes" if edge > 0 else "No"
        effective_fv = fair_home_win if outcome == "Yes" else (1.0 - fair_home_win)

        return DFOpportunity(
            fixture_id=event.fixture_id,
            market_id=market.market_id,
            market_question=market.question,
            token_id=market.token_id,
            outcome=outcome,
            fair_value=round(effective_fv, 4),
            market_price=round(market_price, 4),
            edge_pct=round(abs(edge) * 100, 2),
            source_event=self._describe_event(event),
            detected_at=event.detected_at,
            market_type="game_winner",
        )

    # ── Over/Under evaluator ──────────────────────────────────────────────────

    def _evaluate_ou(self, event, market: MatchedMarket) -> "DFOpportunity | None":
        if market.ou_line is None:
            return None

        current_goals     = event.home_score + event.away_score
        minutes_remaining = max(0, 90 - event.minute)
        fair_over         = p_over(market.ou_line, current_goals, minutes_remaining)

        market_price = market.current_price
        edge         = fair_over - market_price
        if abs(edge) < self._min_edge:
            return None

        outcome      = "Yes" if edge > 0 else "No"
        effective_fv = fair_over if outcome == "Yes" else (1.0 - fair_over)

        return DFOpportunity(
            fixture_id=event.fixture_id,
            market_id=market.market_id,
            market_question=market.question,
            token_id=market.token_id,
            outcome=outcome,
            fair_value=round(effective_fv, 4),
            market_price=round(market_price, 4),
            edge_pct=round(abs(edge) * 100, 2),
            source_event=self._describe_event(event),
            detected_at=event.detected_at,
            market_type="over_under",
            ou_line=market.ou_line,
        )

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _fair_value_winner(self, event) -> "float | None":
        goal_diff = max(-2, min(2, event.home_score - event.away_score))
        time_band = "first_half" if event.minute <= 45 else "second_half"
        probs     = WIN_PROB_TABLE.get((goal_diff, time_band))
        if probs is None:
            return None
        home_win, draw, away_win = probs
        if event.event_type == "red_card":
            if event.home_score <= event.away_score:
                home_win = max(0.01, home_win - _RED_CARD_HOME_PENALTY)
            else:
                home_win = min(0.99, home_win + _RED_CARD_AWAY_PENALTY)
        return home_win

    def _get_market_price(self, market: dict) -> "tuple[str | None, float | None]":
        import json as _json
        try:
            raw_ids   = market.get("clobTokenIds", "[]")
            token_ids = _json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            if not token_ids:
                return None, None
            token_id  = token_ids[0]
            best_ask  = market.get("bestAsk")
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
