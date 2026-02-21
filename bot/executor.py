"""
Order execution engine.

Flow for each opportunity:
  1. Risk cap check (total USDC <= max_risk_per_trade)
  2. Balance check (wallet has enough USDC)
  3. Liquidity check (enough depth on each side)
  4. Slippage check (re-fetch live prices; abort if moved beyond tolerance)
  5. Place YES FOK order
  6. Place NO FOK order
  7. If YES filled but NO failed → emergency GTC sell to hedge YES exposure
"""

import logging
from dataclasses import dataclass
from enum import Enum

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
    FAILED_YES_NOT_FILLED = "FAILED_YES_NOT_FILLED"
    FAILED_NO_NOT_FILLED = "FAILED_NO_NOT_FILLED"   # YES was filled; hedge attempted
    ERROR = "ERROR"


@dataclass
class TradeResult:
    outcome: TradeOutcome
    reason: str
    yes_order_id: str | None = None
    no_order_id: str | None = None
    yes_fill_price: float | None = None
    no_fill_price: float | None = None
    profit_usdc: float | None = None


class Executor:
    def __init__(self, client: PolymarketClient, cfg: dict):
        self._client = client
        self._strategy = cfg["strategy"]
        self._max_trade = self._strategy["max_trade_size_usdc"]
        self._max_risk = self._strategy["max_risk_per_trade_usdc"]
        self._slippage_pct = self._strategy["slippage_tolerance_pct"]
        self._min_liquidity = self._strategy["min_liquidity_usdc"]

    def execute(self, opp: ArbOpportunity) -> TradeResult:
        # ── 1. Risk cap ───────────────────────────────────────────────────────
        total_cost = opp.yes_cost_usdc + opp.no_cost_usdc
        if total_cost > self._max_risk:
            return self._abort(
                TradeOutcome.ABORTED_RISK,
                f"Total cost {total_cost:.2f} USDC > max risk {self._max_risk:.2f} USDC",
                opp,
            )

        # ── 2. Balance check ──────────────────────────────────────────────────
        balance = self._client.get_usdc_balance()
        if balance < total_cost:
            return self._abort(
                TradeOutcome.ABORTED_BALANCE,
                f"Insufficient balance: have {balance:.2f}, need {total_cost:.2f} USDC",
                opp,
            )

        # ── 3. Liquidity check ────────────────────────────────────────────────
        yes_liq = self._client.get_available_liquidity_usdc(
            opp.yes_token_id, opp.yes_ask, opp.yes_cost_usdc
        )
        if yes_liq < self._min_liquidity:
            return self._abort(
                TradeOutcome.ABORTED_LIQUIDITY,
                f"YES liquidity {yes_liq:.2f} < minimum {self._min_liquidity:.2f} USDC",
                opp,
            )

        no_liq = self._client.get_available_liquidity_usdc(
            opp.no_token_id, opp.no_ask, opp.no_cost_usdc
        )
        if no_liq < self._min_liquidity:
            return self._abort(
                TradeOutcome.ABORTED_LIQUIDITY,
                f"NO liquidity {no_liq:.2f} < minimum {self._min_liquidity:.2f} USDC",
                opp,
            )

        # ── 4. Slippage / price-freshness check ───────────────────────────────
        live_yes = self._client.get_best_ask(opp.yes_token_id)
        live_no = self._client.get_best_ask(opp.no_token_id)

        if live_yes is None or live_no is None:
            return self._abort(
                TradeOutcome.ERROR,
                "Could not fetch live prices for slippage check",
                opp,
            )

        yes_slip = abs(live_yes - opp.yes_ask) / opp.yes_ask * 100
        no_slip = abs(live_no - opp.no_ask) / opp.no_ask * 100

        if yes_slip > self._slippage_pct:
            return self._abort(
                TradeOutcome.ABORTED_SLIPPAGE,
                f"YES price moved {yes_slip:.2f}% (tolerance {self._slippage_pct}%)",
                opp,
            )
        if no_slip > self._slippage_pct:
            return self._abort(
                TradeOutcome.ABORTED_SLIPPAGE,
                f"NO price moved {no_slip:.2f}% (tolerance {self._slippage_pct}%)",
                opp,
            )

        # Verify arb still profitable at live prices
        if live_yes + live_no >= 1.0:
            return self._abort(
                TradeOutcome.ABORTED_ARB_EVAPORATED,
                f"Arb gone: live combined = {(live_yes + live_no) * 100:.2f}%",
                opp,
            )

        exec_yes = live_yes
        exec_no = live_no
        # Recompute shares at live prices to stay within caps
        shares = min(
            self._max_trade / exec_yes,
            self._max_trade / exec_no,
            self._max_risk / (exec_yes + exec_no),
        )

        trade_log.info(
            "ATTEMPTING | %s | YES@%.4f NO@%.4f | shares=%.4f | cost=%.2f USDC | profit≈%.4f USDC",
            opp.market_question[:60],
            exec_yes,
            exec_no,
            shares,
            shares * (exec_yes + exec_no),
            shares * (1.0 - exec_yes - exec_no),
        )

        # ── 5. YES FOK order ──────────────────────────────────────────────────
        yes_resp = self._client.place_fok_order(
            token_id=opp.yes_token_id,
            price=exec_yes,
            shares=shares,
        )

        if not yes_resp["filled"]:
            trade_log.warning(
                "FAILED_YES | %s | reason: %s",
                opp.market_question[:60],
                yes_resp["reason"],
            )
            return TradeResult(
                outcome=TradeOutcome.FAILED_YES_NOT_FILLED,
                reason=f"YES FOK not filled: {yes_resp['reason']}",
            )

        # ── 6. NO FOK order ───────────────────────────────────────────────────
        no_resp = self._client.place_fok_order(
            token_id=opp.no_token_id,
            price=exec_no,
            shares=shares,
        )

        if not no_resp["filled"]:
            # YES filled, NO failed → we have unhedged directional exposure
            error_log.error(
                "PARTIAL FILL | YES filled (id=%s) but NO failed: %s | market: %s "
                "| Attempting emergency hedge.",
                yes_resp["order_id"],
                no_resp["reason"],
                opp.market_question[:60],
            )
            self._emergency_hedge(opp.yes_token_id, shares, exec_yes)
            return TradeResult(
                outcome=TradeOutcome.FAILED_NO_NOT_FILLED,
                reason=f"NO FOK not filled: {no_resp['reason']}. Emergency GTC sell placed.",
                yes_order_id=yes_resp["order_id"],
            )

        # ── 7. Success ────────────────────────────────────────────────────────
        actual_profit = shares * (1.0 - yes_resp["fill_price"] - no_resp["fill_price"])
        trade_log.info(
            "SUCCESS | %s | YES_id=%s NO_id=%s | fill_yes=%.4f fill_no=%.4f | profit=%.4f USDC",
            opp.market_question[:60],
            yes_resp["order_id"],
            no_resp["order_id"],
            yes_resp["fill_price"],
            no_resp["fill_price"],
            actual_profit,
        )
        return TradeResult(
            outcome=TradeOutcome.SUCCESS,
            reason="Both sides filled",
            yes_order_id=yes_resp["order_id"],
            no_order_id=no_resp["order_id"],
            yes_fill_price=yes_resp["fill_price"],
            no_fill_price=no_resp["fill_price"],
            profit_usdc=actual_profit,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _abort(self, outcome: TradeOutcome, reason: str, opp: ArbOpportunity) -> TradeResult:
        trade_log.info("ABORTED [%s] | %s | %s", outcome.value, opp.market_question[:60], reason)
        return TradeResult(outcome=outcome, reason=reason)

    def _emergency_hedge(self, yes_token_id: str, shares: float, buy_price: float) -> None:
        """
        Best-effort: place a GTC sell for the YES position slightly below cost
        to exit the unhedged exposure quickly.
        """
        sell_price = round(buy_price * 0.97, 4)  # 3% below cost — prioritise speed of exit
        result = self._client.place_gtc_sell(yes_token_id, sell_price, shares)
        if result["ok"]:
            error_log.warning(
                "Emergency hedge placed | token=%s | shares=%.4f | sell_price=%.4f",
                yes_token_id,
                shares,
                sell_price,
            )
        else:
            error_log.critical(
                "EMERGENCY HEDGE FAILED — MANUAL INTERVENTION REQUIRED | "
                "token=%s | shares=%.4f | err=%s",
                yes_token_id,
                shares,
                result.get("reason"),
            )
