"""
40-slot portfolio manager for the DataFeed Bot.
Follows the same pattern as bot/mirror/portfolio.py.

Bus events emitted
------------------
  datafeed_overview          – balance / pnl / slot summary
  datafeed_positions         – list of all open positions
  datafeed_position_opened   – single position dict (on open)
  datafeed_position_closed   – single resolved trade dict (on close)
"""

import json
import logging
import time
import uuid
from typing import Optional

from .models import DataFeedPosition, DFOpportunity, ResolvedDFTrade

logger = logging.getLogger("arb_bot.datafeed.portfolio")

SLOTS          = 40
SLOT_SIZE_USDC = 500.0
GAMMA_API      = "https://gamma-api.polymarket.com"


class DataFeedPortfolio:
    def __init__(self, event_bus=None, starting_balance: float = 20_000.0):
        self._bus              = event_bus
        self._starting_balance = starting_balance
        self._balance: float   = starting_balance
        self._realized_pnl: float = 0.0
        self._positions: dict  = {}   # token_id → DataFeedPosition
        self._resolved: list   = []   # list of ResolvedDFTrade (newest first)

    def reset(self) -> None:
        self._balance      = self._starting_balance
        self._realized_pnl = 0.0
        self._positions    = {}
        self._resolved     = []
        self._emit_overview()
        self._emit_positions()

    # ── Public API ────────────────────────────────────────────────────────────

    def open_position(self, opp: DFOpportunity) -> Optional[DataFeedPosition]:
        """
        Attempt to open a paper position for the given opportunity.
        Returns the new DataFeedPosition or None (if duplicate or slots full).
        """
        token_id = opp.token_id
        if not token_id:
            return None

        # Deduplicate
        if token_id in self._positions:
            return None

        if len(self._positions) >= SLOTS or self._balance < SLOT_SIZE_USDC:
            logger.info(
                "DataFeedPortfolio: slot/balance limit — skipping opportunity %s",
                opp.market_question[:50],
            )
            return None

        entry_price = opp.market_price
        shares = SLOT_SIZE_USDC / entry_price if entry_price > 0 else 0.0

        pos = DataFeedPosition(
            id=str(uuid.uuid4())[:8],
            market_question=opp.market_question,
            token_id=token_id,
            outcome=opp.outcome,
            entry_price=entry_price,
            current_price=entry_price,
            shares=round(shares, 4),
            usdc_deployed=SLOT_SIZE_USDC,
            opened_at=time.time(),
            source_event=opp.source_event,
            fixture_id=opp.fixture_id,
        )

        self._positions[token_id] = pos
        self._balance -= SLOT_SIZE_USDC

        logger.info(
            "DataFeedPortfolio: opened %s %s @ %.3f  edge=%.1f%%  (slots: %d/%d)",
            pos.outcome,
            pos.market_question[:50],
            entry_price,
            opp.edge_pct,
            len(self._positions),
            SLOTS,
        )

        self._emit_position_opened(pos)
        self._emit_positions()
        self._emit_overview()
        return pos

    def close_position_by_token(self, token_id: str, exit_price: float) -> Optional[ResolvedDFTrade]:
        pos = self._positions.pop(token_id, None)
        if not pos:
            return None

        pnl    = (exit_price - pos.entry_price) * pos.shares
        result = "WIN" if pnl > 0.01 else ("LOSS" if pnl < -0.01 else "PUSH")

        resolved = ResolvedDFTrade(
            market_question=pos.market_question,
            outcome=pos.outcome,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            usdc_deployed=pos.usdc_deployed,
            pnl_usdc=round(pnl, 4),
            duration_s=time.time() - pos.opened_at,
            source_event=pos.source_event,
            resolved_at=time.time(),
            result=result,
        )

        self._balance      += SLOT_SIZE_USDC + pnl
        self._realized_pnl += pnl
        self._resolved.insert(0, resolved)
        if len(self._resolved) > 50:
            self._resolved = self._resolved[:50]

        logger.info(
            "DataFeedPortfolio: closed %s — %s  pnl: %+.2f USDC",
            pos.market_question[:40],
            result,
            pnl,
        )

        self._emit_position_closed(resolved)
        self._emit_positions()
        self._emit_overview()
        return resolved

    def close_resolved_markets(self, http_session) -> None:
        """
        For each open position, check Gamma API; if market is inactive,
        close the position at the current outcomePrices value.
        """
        if not self._positions:
            return
        to_close = []
        for token_id, pos in list(self._positions.items()):
            try:
                resp = http_session.get(
                    f"{GAMMA_API}/markets",
                    params={"clobTokenIds": token_id},
                    timeout=10,
                )
                resp.raise_for_status()
                markets = resp.json()
                if not isinstance(markets, list) or not markets:
                    continue
                mkt = markets[0]
                if not mkt.get("active", True):
                    # Market resolved — get outcome price
                    raw_prices = mkt.get("outcomePrices", "[0.5,0.5]")
                    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
                    exit_price = float(prices[0]) if prices else 0.5
                    to_close.append((token_id, exit_price))
            except Exception as exc:
                logger.warning("close_resolved_markets: %s", exc)

        for token_id, exit_price in to_close:
            self.close_position_by_token(token_id, exit_price)

    def update_prices(self, http_session) -> None:
        """Refresh current_price for all open positions via Gamma API."""
        if not self._positions:
            return
        token_ids = list(self._positions.keys())
        try:
            for i in range(0, len(token_ids), 20):
                batch     = token_ids[i:i + 20]
                ids_param = ",".join(batch)
                resp = http_session.get(
                    f"{GAMMA_API}/markets",
                    params={"clobTokenIds": ids_param},
                    timeout=10,
                )
                resp.raise_for_status()
                markets = resp.json() if isinstance(resp.json(), list) else []
                for mkt in markets:
                    self._update_market_prices(mkt)
            self._emit_positions()
            self._emit_overview()
        except Exception as exc:
            logger.warning("DataFeedPortfolio price update failed: %s", exc)

    # ── Getters ───────────────────────────────────────────────────────────────

    def get_overview(self) -> dict:
        total_deployed = len(self._positions) * SLOT_SIZE_USDC
        unrealized     = sum(p.unrealized_pnl for p in self._positions.values())
        return {
            "balance_usdc":   round(self._balance, 2),
            "realized_pnl":   round(self._realized_pnl, 4),
            "unrealized_pnl": round(unrealized, 4),
            "total_pnl":      round(self._realized_pnl + unrealized, 4),
            "slots_used":     len(self._positions),
            "slots_total":    SLOTS,
            "total_deployed": round(total_deployed, 2),
        }

    def get_positions(self) -> list:
        return [self._pos_to_dict(p) for p in self._positions.values()]

    def get_resolved(self, limit: int = 50) -> list:
        return [self._resolved_to_dict(r) for r in self._resolved[:limit]]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _update_market_prices(self, market: dict) -> None:
        try:
            raw_ids  = market.get("clobTokenIds", "[]")
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            best_ask  = market.get("bestAsk")
            best_bid  = market.get("bestBid")
            if best_ask is None and best_bid is None:
                return
            price = float(best_ask or best_bid)
            for tid in token_ids:
                if tid in self._positions:
                    self._positions[tid].current_price = price
        except Exception:
            pass

    # ── Emitters ──────────────────────────────────────────────────────────────

    def _emit_overview(self) -> None:
        if self._bus:
            self._bus.publish("datafeed_overview", self.get_overview())

    def _emit_positions(self) -> None:
        if self._bus:
            self._bus.publish("datafeed_positions", {"positions": self.get_positions()})

    def _emit_position_opened(self, pos: DataFeedPosition) -> None:
        if self._bus:
            self._bus.publish("datafeed_position_opened", self._pos_to_dict(pos))

    def _emit_position_closed(self, resolved: ResolvedDFTrade) -> None:
        if self._bus:
            self._bus.publish("datafeed_position_closed", self._resolved_to_dict(resolved))

    # ── Serialisers ───────────────────────────────────────────────────────────

    def _pos_to_dict(self, p: DataFeedPosition) -> dict:
        return {
            "id":               p.id,
            "market_question":  p.market_question,
            "token_id":         p.token_id,
            "outcome":          p.outcome,
            "entry_price":      round(p.entry_price, 4),
            "current_price":    round(p.current_price, 4),
            "shares":           round(p.shares, 4),
            "usdc_deployed":    round(p.usdc_deployed, 2),
            "unrealized_pnl":   round(p.unrealized_pnl, 4),
            "unrealized_pnl_pct": round(p.unrealized_pnl_pct, 2),
            "opened_at":        p.opened_at,
            "age_s":            round(p.age_s, 0),
            "source_event":     p.source_event,
            "fixture_id":       p.fixture_id,
        }

    def _resolved_to_dict(self, r: ResolvedDFTrade) -> dict:
        return {
            "market_question": r.market_question,
            "outcome":         r.outcome,
            "entry_price":     round(r.entry_price, 4),
            "exit_price":      round(r.exit_price, 4),
            "shares":          round(r.shares, 4),
            "usdc_deployed":   round(r.usdc_deployed, 2),
            "pnl_usdc":        round(r.pnl_usdc, 4),
            "duration_s":      round(r.duration_s, 0),
            "source_event":    r.source_event,
            "resolved_at":     r.resolved_at,
            "result":          r.result,
        }
