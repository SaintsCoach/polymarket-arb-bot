"""Polymarket API wrapper — CLOB (trading) + Gamma (market discovery)."""

import logging
from typing import Optional

import requests

logger = logging.getLogger("arb_bot.client")


class PolymarketClient:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._strategy = cfg["strategy"]
        self._clob_host = cfg["clob_host"]
        self._gamma_host = cfg["gamma_host"]
        self._paper_mode = cfg.get("paper_mode", {}).get("enabled", False)

        self._http = requests.Session()
        self._http.headers.update({"Accept": "application/json"})

        if self._paper_mode:
            self.clob = None
            logger.info("Paper mode — skipping CLOB auth, using public endpoints only")
        else:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=cfg["api"]["key"],
                api_secret=cfg["api"]["secret"],
                api_passphrase=cfg["api"]["passphrase"],
            )
            self.clob = ClobClient(
                host=cfg["clob_host"],
                key=cfg["wallet"]["private_key"],
                chain_id=cfg["chain_id"],
                creds=creds,
                funder=cfg["wallet"]["address"],
            )

    # ── Market discovery (Gamma API) ──────────────────────────────────────────

    def get_sports_markets(self, tags: list[str]) -> list[dict]:
        """Return deduplicated active binary sports markets matching any of the given tags."""
        markets: list[dict] = []
        for tag in tags:
            try:
                resp = self._http.get(
                    f"{self._gamma_host}/markets",
                    params={"tag": tag, "active": "true", "closed": "false", "limit": 100},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                batch = data if isinstance(data, list) else data.get("markets", [])
                markets.extend(batch)
            except Exception as exc:
                logging.getLogger("arb_bot.errors").error(
                    "Gamma API error for tag '%s': %s", tag, exc
                )

        # Deduplicate by conditionId
        seen: set[str] = set()
        unique: list[dict] = []
        for m in markets:
            cid = m.get("conditionId") or m.get("condition_id", "")
            if cid and cid not in seen:
                seen.add(cid)
                unique.append(m)
        return unique

    # ── Order book (CLOB API) ─────────────────────────────────────────────────

    def get_order_book(self, token_id: str) -> Optional[dict]:
        """Return the order book for a token, or None on error.

        In paper mode uses the public CLOB REST endpoint directly.
        In live mode delegates to the authenticated py-clob-client.
        Response is normalised to a dict with 'asks' and 'bids' lists,
        each entry being a dict with 'price' and 'size' string keys.
        """
        if self._paper_mode:
            try:
                resp = self._http.get(
                    f"{self._clob_host}/book",
                    params={"token_id": token_id},
                    timeout=10,
                )
                resp.raise_for_status()
                return resp.json()  # already {"asks": [...], "bids": [...]}
            except Exception as exc:
                logger.error("Public order book fetch failed for token %s: %s", token_id, exc)
                return None
        else:
            try:
                return self.clob.get_order_book(token_id)
            except Exception as exc:
                logger.error("Order book fetch failed for token %s: %s", token_id, exc)
                return None

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Return the lowest ask price for a token (what you pay to BUY it)."""
        book = self.get_order_book(token_id)
        if book is None:
            return None
        asks = book.asks if hasattr(book, "asks") else book.get("asks", [])
        if not asks:
            return None
        try:
            return min(float(a.price if hasattr(a, "price") else a["price"]) for a in asks)
        except Exception as exc:
            logger.error("Error parsing asks for %s: %s", token_id, exc)
            return None

    def get_available_liquidity_usdc(
        self, token_id: str, max_price: float, target_usdc: float
    ) -> float:
        """
        Sum USDC value of all ask levels at or below max_price, up to target_usdc.
        Returns the total USDC of fillable liquidity — compare against required trade size.
        """
        book = self.get_order_book(token_id)
        if book is None:
            return 0.0
        asks = book.asks if hasattr(book, "asks") else book.get("asks", [])
        if not asks:
            return 0.0

        total = 0.0
        for ask in sorted(asks, key=lambda a: float(a.price if hasattr(a, "price") else a["price"])):
            price = float(ask.price if hasattr(ask, "price") else ask["price"])
            size = float(ask.size if hasattr(ask, "size") else ask["size"])
            if price > max_price:
                break
            total += price * size
            if total >= target_usdc:
                return total  # early exit — we have enough
        return total

    # ── Order placement ───────────────────────────────────────────────────────

    def place_fok_order(
        self,
        token_id: str,
        price: float,
        shares: float,
        side: str = "BUY",
    ) -> dict:
        """
        Place a Fill-or-Kill limit order.

        Args:
            token_id: CLOB token ID
            price:    limit price in USDC (0–1)
            shares:   number of outcome shares to buy/sell
            side:     "BUY" or "SELL"

        Returns dict with keys:
            filled    bool
            order_id  str | None
            fill_price float | None
            reason    str | None  (set on failure)
        """
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=round(shares, 4),
            side=side,
            fee_rate_bps=self._strategy.get("fee_rate_bps", 0),
            nonce=0,
            expiration=0,
        )
        try:
            signed = self.clob.create_order(order_args)
            resp = self.clob.post_order(signed, OrderType.FOK)

            # Normalise response — py-clob-client may return dict or object
            if hasattr(resp, "__dict__"):
                resp = vars(resp)

            success = resp.get("success", False) or resp.get("status") == "matched"
            return {
                "filled": bool(success),
                "order_id": resp.get("orderID") or resp.get("order_id"),
                "fill_price": float(resp.get("price", price)),
                "reason": None if success else (resp.get("errorMsg") or "FOK not filled"),
            }
        except Exception as exc:
            logger.error("place_fok_order failed (token=%s, side=%s): %s", token_id, side, exc)
            return {"filled": False, "order_id": None, "fill_price": None, "reason": str(exc)}

    def place_gtc_sell(self, token_id: str, price: float, shares: float) -> dict:
        """Place a Good-Till-Cancelled sell order (used for emergency hedging)."""
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=round(shares, 4),
            side="SELL",
            fee_rate_bps=self._strategy.get("fee_rate_bps", 0),
            nonce=0,
            expiration=0,
        )
        try:
            signed = self.clob.create_order(order_args)
            resp = self.clob.post_order(signed, OrderType.GTC)
            if hasattr(resp, "__dict__"):
                resp = vars(resp)
            return {"ok": True, "raw": resp}
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}

    # ── Account ───────────────────────────────────────────────────────────────

    def get_usdc_balance(self) -> float:
        """Return the wallet's current USDC balance on the CLOB."""
        try:
            bal = self.clob.get_balance_allowance(params={"asset_type": "USDC", "signature_type": 0})
            if hasattr(bal, "__dict__"):
                bal = vars(bal)
            return float(bal.get("balance", 0.0))
        except Exception as exc:
            logging.getLogger("arb_bot.errors").error("Balance fetch failed: %s", exc)
            return 0.0
