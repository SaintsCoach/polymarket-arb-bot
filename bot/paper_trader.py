"""
Paper trading engine.

Runs the same pre-trade checks as the real Executor (risk cap, balance,
liquidity, slippage) against live Polymarket prices, but simulates fills
instead of submitting real orders.

State (virtual balance, P&L, trade count) is persisted to
logs/paper_state.json so it survives restarts.
"""

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .arbitrage import ArbOpportunity
from .client import PolymarketClient

logger = logging.getLogger("arb_bot")
trade_log = logging.getLogger("arb_bot.trades")
error_log = logging.getLogger("arb_bot.errors")


class TradeOutcome(Enum):
    SUCCESS = "SUCCESS"
    ABORTED_RISK = "ABORTED_RISK"
    ABORTED_BALANCE = "ABORTED_BALANCE"
    ABORTED_LIQUIDITY = "ABORTED_LIQUIDITY"
    ABORTED_SLIPPAGE = "ABORTED_SLIPPAGE"
    ABORTED_ARB_EVAPORATED = "ABORTED_ARB_EVAPORATED"
    ERROR = "ERROR"


@dataclass
class TradeResult:
    outcome: TradeOutcome
    reason: str
    yes_fill_price: Optional[float] = None
    no_fill_price: Optional[float] = None
    profit_usdc: Optional[float] = None


class PaperTrader:
    def __init__(self, client: PolymarketClient, cfg: dict, event_bus=None):
        self._client = client
        self._strategy = cfg["strategy"]
        self._max_trade = self._strategy["max_trade_size_usdc"]
        self._max_risk = self._strategy["max_risk_per_trade_usdc"]
        self._slippage_pct = self._strategy["slippage_tolerance_pct"]
        self._min_liquidity = self._strategy["min_liquidity_usdc"]
        self._bus = event_bus

        state_path = os.path.join(cfg["logging"]["log_dir"], "paper_state.json")
        starting_balance = cfg.get("paper_mode", {}).get("starting_balance_usdc", 10000.0)
        self._state_path = state_path
        self._state = self._load_state(starting_balance)

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_state(self, starting_balance: float) -> dict:
        if os.path.exists(self._state_path):
            with open(self._state_path) as f:
                state = json.load(f)
            logger.info(
                "[PAPER] Resuming — balance=%.2f USDC | total_profit=%.4f USDC | "
                "trades=%d | opportunities_seen=%d",
                state["balance_usdc"],
                state["total_profit_usdc"],
                state["trades_executed"],
                state["opportunities_seen"],
            )
            return state
        logger.info("[PAPER] Starting fresh — virtual balance=%.2f USDC", starting_balance)
        return {
            "balance_usdc": starting_balance,
            "total_profit_usdc": 0.0,
            "trades_executed": 0,
            "trades_aborted": 0,
            "opportunities_seen": 0,
        }

    def _save_state(self) -> None:
        with open(self._state_path, "w") as f:
            json.dump(self._state, f, indent=2)

    # ── Execution ─────────────────────────────────────────────────────────────

    def execute(self, opp: ArbOpportunity) -> TradeResult:
        self._state["opportunities_seen"] += 1

        # ── 1. Risk cap ───────────────────────────────────────────────────────
        total_cost = opp.yes_cost_usdc + opp.no_cost_usdc
        if total_cost > self._max_risk:
            return self._abort(
                TradeOutcome.ABORTED_RISK,
                f"Cost {total_cost:.2f} USDC > max risk {self._max_risk:.2f} USDC",
                opp,
            )

        # ── 2. Virtual balance check ──────────────────────────────────────────
        if self._state["balance_usdc"] < total_cost:
            return self._abort(
                TradeOutcome.ABORTED_BALANCE,
                f"Paper balance {self._state['balance_usdc']:.2f} < cost {total_cost:.2f} USDC",
                opp,
            )

        # ── 3. Liquidity check (against real order book) ──────────────────────
        yes_liq = self._client.get_available_liquidity_usdc(
            opp.yes_token_id, opp.yes_ask, opp.yes_cost_usdc
        )
        if yes_liq < self._min_liquidity:
            return self._abort(
                TradeOutcome.ABORTED_LIQUIDITY,
                f"YES liquidity {yes_liq:.2f} < min {self._min_liquidity:.2f} USDC",
                opp,
            )

        no_liq = self._client.get_available_liquidity_usdc(
            opp.no_token_id, opp.no_ask, opp.no_cost_usdc
        )
        if no_liq < self._min_liquidity:
            return self._abort(
                TradeOutcome.ABORTED_LIQUIDITY,
                f"NO liquidity {no_liq:.2f} < min {self._min_liquidity:.2f} USDC",
                opp,
            )

        # ── 4. Slippage check (re-fetch live prices) ──────────────────────────
        live_yes = self._client.get_best_ask(opp.yes_token_id)
        live_no = self._client.get_best_ask(opp.no_token_id)

        if live_yes is None or live_no is None:
            return self._abort(TradeOutcome.ERROR, "Could not re-fetch live prices", opp)

        yes_slip = abs(live_yes - opp.yes_ask) / opp.yes_ask * 100
        no_slip = abs(live_no - opp.no_ask) / opp.no_ask * 100

        if yes_slip > self._slippage_pct:
            return self._abort(
                TradeOutcome.ABORTED_SLIPPAGE,
                f"YES moved {yes_slip:.2f}% (tolerance {self._slippage_pct}%)",
                opp,
            )
        if no_slip > self._slippage_pct:
            return self._abort(
                TradeOutcome.ABORTED_SLIPPAGE,
                f"NO moved {no_slip:.2f}% (tolerance {self._slippage_pct}%)",
                opp,
            )

        # ── 5. Verify arb still exists at live prices ─────────────────────────
        if live_yes + live_no >= 1.0:
            return self._abort(
                TradeOutcome.ABORTED_ARB_EVAPORATED,
                f"Arb gone: live combined = {(live_yes + live_no) * 100:.2f}%",
                opp,
            )

        # ── 6. Simulate fill ──────────────────────────────────────────────────
        shares = min(
            self._max_trade / live_yes,
            self._max_trade / live_no,
            self._max_risk / (live_yes + live_no),
        )
        cost = shares * (live_yes + live_no)
        # One side always pays out 1 USDC/share at settlement
        profit = shares * (1.0 - live_yes - live_no)

        self._state["balance_usdc"] -= cost
        self._state["balance_usdc"] += shares   # winning-side payout (locked in)
        self._state["total_profit_usdc"] += profit
        self._state["trades_executed"] += 1
        self._save_state()

        trade_log.info(
            "[PAPER] SUCCESS | %s | YES@%.4f NO@%.4f | shares=%.4f | "
            "cost=%.2f | profit=%.4f USDC | balance=%.2f | cumulative_profit=%.4f",
            opp.market_question[:60],
            live_yes, live_no,
            shares, cost, profit,
            self._state["balance_usdc"],
            self._state["total_profit_usdc"],
        )
        if self._bus:
            self._bus.publish("trade", {
                "outcome": "SUCCESS",
                "question": opp.market_question[:80],
                "yes_fill": round(live_yes, 4),
                "no_fill": round(live_no, 4),
                "profit_usdc": round(profit, 4),
                "cumulative_profit": round(self._state["total_profit_usdc"], 4),
                "balance": round(self._state["balance_usdc"], 2),
                "reason": None,
            })
            self._bus.publish("stats", dict(self._state))
        return TradeResult(
            outcome=TradeOutcome.SUCCESS,
            reason="Simulated fill at live prices",
            yes_fill_price=live_yes,
            no_fill_price=live_no,
            profit_usdc=profit,
        )

    def print_summary(self) -> None:
        s = self._state
        logger.info(
            "[PAPER] Summary — balance=%.2f USDC | profit=%.4f USDC | "
            "trades=%d | aborted=%d | opps_seen=%d",
            s["balance_usdc"], s["total_profit_usdc"],
            s["trades_executed"], s["trades_aborted"], s["opportunities_seen"],
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _abort(self, outcome: TradeOutcome, reason: str, opp: ArbOpportunity) -> TradeResult:
        self._state["trades_aborted"] += 1
        self._save_state()
        trade_log.info(
            "[PAPER] ABORTED [%s] | %s | %s",
            outcome.value, opp.market_question[:60], reason,
        )
        if self._bus:
            self._bus.publish("trade", {
                "outcome": outcome.value,
                "question": opp.market_question[:80],
                "yes_fill": None,
                "no_fill": None,
                "profit_usdc": None,
                "cumulative_profit": round(self._state["total_profit_usdc"], 4),
                "balance": round(self._state["balance_usdc"], 2),
                "reason": reason,
            })
            self._bus.publish("stats", dict(self._state))
        return TradeResult(outcome=outcome, reason=reason)
