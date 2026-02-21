"""
40-slot portfolio manager for the Mirror Bot.

Lifecycle
---------
  open_position(cfg, pos_data)   → fills a slot or queues the trade
  close_position_by_token(cfg, pos_data) → frees a slot, drains queue
  update_prices(client)          → refreshes current_price for all open positions

Bus events emitted
------------------
  mirror_overview          – balance / pnl / slot summary
  mirror_positions         – list of all open positions
  mirror_queue             – list of queued trades
  mirror_resolved          – single resolved trade (on close)
  mirror_position_opened   – single position dict (on open)
  mirror_position_closed   – single resolved trade dict (on close)
"""

import logging
import time
import uuid
from typing import Optional

from .models import MirrorPosition, QueuedTrade, ResolvedTrade, WatchedAddress

logger = logging.getLogger("arb_bot.mirror.portfolio")

SLOTS            = 40
SLOT_SIZE_USDC   = 500.0
STARTING_BALANCE = 20_000.0

GAMMA_API        = "https://gamma-api.polymarket.com"


class PortfolioManager:
    def __init__(self, event_bus=None, starting_balance: float = STARTING_BALANCE):
        self._bus              = event_bus
        self._starting_balance = starting_balance
        self._balance          = starting_balance          # free cash
        self._realized_pnl: float = 0.0
        self._positions: dict[str, MirrorPosition] = {}   # token_id → position
        self._queue:     list[QueuedTrade]          = []
        self._resolved:  list[ResolvedTrade]        = []

    def reset(self) -> None:
        """Clear all portfolio state back to starting balance."""
        self._balance      = self._starting_balance
        self._realized_pnl = 0.0
        self._positions    = {}
        self._queue        = []
        self._resolved     = []
        self._emit_overview()
        self._emit_positions()
        self._emit_queue()

    # ── Public API ────────────────────────────────────────────────────────────

    def open_position(self, cfg: WatchedAddress, pos_data: dict) -> Optional[MirrorPosition]:
        """
        Attempt to open a mirrored position.
        If all 40 slots are taken, queue the trade instead.
        Returns the new MirrorPosition or None (if queued or duplicate).
        """
        token_id = pos_data.get("asset") or pos_data.get("token_id", "")
        if not token_id:
            logger.warning("open_position called with no token_id in pos_data")
            return None

        # Deduplicate – already open for this token
        if token_id in self._positions:
            return None

        # Check if this token is already queued
        if any(q.token_id == token_id for q in self._queue):
            return None

        entry_price = float(pos_data.get("curPrice") or pos_data.get("price", 0.5))

        if len(self._positions) >= SLOTS or self._balance < SLOT_SIZE_USDC:
            # Queue for later
            qt = QueuedTrade(
                id=str(uuid.uuid4())[:8],
                market_id=pos_data.get("conditionId", ""),
                market_question=pos_data.get("title", "Unknown market")[:100],
                token_id=token_id,
                outcome=pos_data.get("outcome", "Yes"),
                entry_price=entry_price,
                triggered_by=cfg.nickname,
                triggered_by_address=cfg.address,
                queued_at=time.time(),
            )
            self._queue.append(qt)
            logger.info("[%s] Queued trade — %s (queue size: %d)",
                        cfg.nickname, qt.market_question[:50], len(self._queue))
            self._emit_queue()
            return None

        # Open the position
        position = self._create_position(cfg, pos_data, token_id, entry_price)
        self._positions[token_id] = position
        self._balance -= SLOT_SIZE_USDC
        cfg.stats.trades_mirrored += 1

        logger.info("[%s] Opened position — %s @ %.3f  (slots: %d/%d)",
                    cfg.nickname, position.market_question[:50],
                    entry_price, len(self._positions), SLOTS)

        self._emit_position_opened(position)
        self._emit_positions()
        self._emit_overview()
        return position

    def close_position_by_token(self, cfg: WatchedAddress,
                                pos_data: dict) -> Optional[ResolvedTrade]:
        """
        Close the open position matching pos_data's token_id.
        Credits the slot back and drains the queue.
        Returns the ResolvedTrade or None if position was not found.
        """
        token_id = pos_data.get("asset") or pos_data.get("token_id", "")
        position = self._positions.pop(token_id, None)
        if not position:
            return None

        exit_price = float(pos_data.get("curPrice") or pos_data.get("price",
                           position.entry_price))
        pnl = (exit_price - position.entry_price) * position.shares
        result = "WIN" if pnl > 0.01 else ("LOSS" if pnl < -0.01 else "PUSH")

        resolved = ResolvedTrade(
            market_question=position.market_question,
            outcome=position.outcome,
            entry_price=position.entry_price,
            exit_price=exit_price,
            shares=position.shares,
            usdc_deployed=position.usdc_deployed,
            pnl_usdc=round(pnl, 4),
            duration_s=time.time() - position.opened_at,
            triggered_by=position.triggered_by,
            resolved_at=time.time(),
            result=result,
        )

        self._balance      += SLOT_SIZE_USDC + pnl
        self._realized_pnl += pnl
        self._resolved.insert(0, resolved)

        # Update address stats
        cfg.stats.total_pnl_usdc += pnl
        if result == "WIN":
            cfg.stats.wins   += 1
        elif result == "LOSS":
            cfg.stats.losses += 1

        logger.info("[%s] Closed %s — %s  pnl: %+.2f USDC",
                    cfg.nickname, position.market_question[:40], result, pnl)

        self._emit_position_closed(resolved)
        self._emit_positions()
        self._emit_overview()
        self._process_queue()
        return resolved

    def update_prices(self, http_session) -> None:
        """Refresh current_price on all open positions using Gamma API."""
        if not self._positions:
            return
        token_ids = list(self._positions.keys())
        try:
            # Batch by groups of 20 to stay within URL length limits
            for i in range(0, len(token_ids), 20):
                batch = token_ids[i:i + 20]
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
            logger.warning("Price update failed: %s", exc)

    # ── Getters ───────────────────────────────────────────────────────────────

    def get_overview(self) -> dict:
        total_deployed = len(self._positions) * SLOT_SIZE_USDC
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        return {
            "balance_usdc":    round(self._balance, 2),
            "realized_pnl":    round(self._realized_pnl, 4),
            "unrealized_pnl":  round(unrealized, 4),
            "total_pnl":       round(self._realized_pnl + unrealized, 4),
            "slots_used":      len(self._positions),
            "slots_total":     SLOTS,
            "queue_size":      len(self._queue),
            "total_deployed":  round(total_deployed, 2),
        }

    def get_positions(self) -> list[dict]:
        return [self._pos_to_dict(p) for p in self._positions.values()]

    def get_queue(self) -> list[dict]:
        return [self._queue_to_dict(q) for q in self._queue]

    def get_resolved(self, limit: int = 50) -> list[dict]:
        return [self._resolved_to_dict(r) for r in self._resolved[:limit]]

    # ── Internal ──────────────────────────────────────────────────────────────

    def _create_position(self, cfg: WatchedAddress, pos_data: dict,
                         token_id: str, entry_price: float) -> MirrorPosition:
        shares = SLOT_SIZE_USDC / entry_price if entry_price > 0 else 0.0
        return MirrorPosition(
            id=str(uuid.uuid4())[:8],
            market_id=pos_data.get("conditionId", ""),
            market_question=pos_data.get("title", "Unknown market")[:100],
            token_id=token_id,
            outcome=pos_data.get("outcome", "Yes"),
            entry_price=entry_price,
            current_price=entry_price,
            shares=round(shares, 4),
            usdc_deployed=SLOT_SIZE_USDC,
            opened_at=time.time(),
            triggered_by=cfg.nickname,
            triggered_by_address=cfg.address,
        )

    def _process_queue(self) -> None:
        """Drain the queue into newly freed slots."""
        while self._queue and len(self._positions) < SLOTS and self._balance >= SLOT_SIZE_USDC:
            qt = self._queue.pop(0)
            # Reconstruct a minimal pos_data dict from the queued trade
            pos_data = {
                "asset":       qt.token_id,
                "conditionId": qt.market_id,
                "title":       qt.market_question,
                "outcome":     qt.outcome,
                "curPrice":    qt.entry_price,
            }
            # Synthesize a dummy WatchedAddress to carry stats
            fake_cfg = WatchedAddress(
                address=qt.triggered_by_address,
                nickname=qt.triggered_by,
            )
            # We need to look up the real cfg to update stats properly.
            # For now open the position directly to avoid circular imports.
            entry_price = qt.entry_price
            position = self._create_position(fake_cfg, pos_data, qt.token_id, entry_price)
            self._positions[qt.token_id] = position
            self._balance -= SLOT_SIZE_USDC
            logger.info("Dequeued → opened %s @ %.3f  (queue remaining: %d)",
                        position.market_question[:50], entry_price, len(self._queue))
            self._emit_position_opened(position)

        self._emit_queue()
        self._emit_positions()
        self._emit_overview()

    def _update_market_prices(self, market: dict) -> None:
        """Update current_price for any positions matching market's token IDs."""
        import json
        try:
            raw_ids = market.get("clobTokenIds", "[]")
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            best_ask = market.get("bestAsk")
            best_bid = market.get("bestBid")
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
            self._bus.publish("mirror_overview", self.get_overview())

    def _emit_positions(self) -> None:
        if self._bus:
            self._bus.publish("mirror_positions", {"positions": self.get_positions()})

    def _emit_queue(self) -> None:
        if self._bus:
            self._bus.publish("mirror_queue", {"queue": self.get_queue()})

    def _emit_position_opened(self, pos: MirrorPosition) -> None:
        if self._bus:
            self._bus.publish("mirror_position_opened", self._pos_to_dict(pos))

    def _emit_position_closed(self, resolved: ResolvedTrade) -> None:
        if self._bus:
            self._bus.publish("mirror_position_closed", self._resolved_to_dict(resolved))

    # ── Serialisers ───────────────────────────────────────────────────────────

    def _pos_to_dict(self, p: MirrorPosition) -> dict:
        return {
            "id":                    p.id,
            "market_question":       p.market_question,
            "token_id":              p.token_id,
            "outcome":               p.outcome,
            "entry_price":           round(p.entry_price, 4),
            "current_price":         round(p.current_price, 4),
            "shares":                round(p.shares, 4),
            "usdc_deployed":         round(p.usdc_deployed, 2),
            "unrealized_pnl":        round(p.unrealized_pnl, 4),
            "unrealized_pnl_pct":    round(p.unrealized_pnl_pct, 2),
            "opened_at":             p.opened_at,
            "age_s":                 round(p.age_s, 0),
            "triggered_by":          p.triggered_by,
            "triggered_by_address":  p.triggered_by_address,
        }

    def _queue_to_dict(self, q: QueuedTrade) -> dict:
        return {
            "id":             q.id,
            "market_question": q.market_question,
            "token_id":       q.token_id,
            "outcome":        q.outcome,
            "entry_price":    round(q.entry_price, 4),
            "triggered_by":   q.triggered_by,
            "queued_at":      q.queued_at,
        }

    def _resolved_to_dict(self, r: ResolvedTrade) -> dict:
        return {
            "market_question": r.market_question,
            "outcome":         r.outcome,
            "entry_price":     round(r.entry_price, 4),
            "exit_price":      round(r.exit_price, 4),
            "shares":          round(r.shares, 4),
            "usdc_deployed":   round(r.usdc_deployed, 2),
            "pnl_usdc":        round(r.pnl_usdc, 4),
            "duration_s":      round(r.duration_s, 0),
            "triggered_by":    r.triggered_by,
            "resolved_at":     r.resolved_at,
            "result":          r.result,
        }
