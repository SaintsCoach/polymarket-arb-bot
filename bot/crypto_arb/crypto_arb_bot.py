"""
CryptoArbBot — Coinbase ↔ Kraken cross-exchange arbitrage scanner.

Runs as a daemon thread inside the dashboard process, publishing scan results,
opportunities, trades and health events to the shared EventBus so they stream
to the browser in real time.

Architecture mirrors DataFeedBot:
  - _discover_loop: one-shot pair discovery at startup
  - _scan_loop:     repeated order-book scan every scan_interval seconds
  - EventBus events (prefix  arb_):
      arb_start           – bot started / reset
      arb_overview        – stats bar update
      arb_scan_result     – per-scan batch of pair data (bid/ask, spread)
      arb_opportunity     – positive-spread detection card
      arb_trade           – paper trade executed
      arb_exchange_health – coinbase / kraken API up/down status
      arb_top_pairs       – sorted list of pairs by opportunity count
      arb_pnl             – cumulative P&L data point for the chart
"""

import logging
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeout

import ccxt

logger = logging.getLogger("arb_bot.crypto_arb")

# ── Constants ─────────────────────────────────────────────────────────────────
CONCURRENCY   = 5      # concurrent order-book threads per exchange
KRAKEN_RENAMES = {"XBT": "BTC", "XDG": "DOGE"}


class CryptoArbBot:
    def __init__(
        self,
        event_bus,
        starting_balance: float = 20_000.0,
        scan_interval:    float = 35.0,
        min_profit_pct:   float = 0.5,
        max_position_usdc: float = 500.0,
        max_position_pct:  float = 0.02,
        min_volume_usdc:  float = 100_000.0,
        max_volume_usdc:  float = float("inf"),
        order_book_depth: int   = 10,
        min_book_age_s:   float = 60.0,
        cb_taker_fee:     float = 0.006,
        cb_maker_fee:     float = 0.004,
        kr_taker_fee:     float = 0.0026,
        kr_maker_fee:     float = 0.0016,
    ):
        self._bus              = event_bus
        self._starting_balance = starting_balance
        self._interval         = scan_interval
        self._min_profit       = min_profit_pct
        self._max_pos_usdc     = max_position_usdc
        self._max_pos_pct      = max_position_pct
        self._min_vol          = min_volume_usdc
        self._max_vol          = max_volume_usdc
        self._depth            = order_book_depth
        self._max_age          = min_book_age_s
        self._fees             = {
            "coinbase": {"taker": cb_taker_fee, "maker": cb_maker_fee},
            "kraken":   {"taker": kr_taker_fee, "maker": kr_maker_fee},
        }

        # State
        self._running       = False
        self.start_ts       = 0.0
        self._pairs: list   = []
        self._scan_count    = 0
        self._opp_count     = 0
        self._trade_count   = 0
        self._balance       = starting_balance
        self._realized_pnl  = 0.0
        self._trades: list  = []
        self._opportunities: list = []
        self._top_pairs: dict = defaultdict(int)   # pair → opp count
        self._pnl_history: list = []               # [{ts, pnl}]
        self._last_scan_pairs: list = []           # last scan pair data for hydration
        self._exchange_health = {"coinbase": True, "kraken": True}

        # CCXT clients
        self._cb = ccxt.coinbaseadvanced({"enableRateLimit": True, "timeout": 8_000})
        self._cb.rateLimit = 200
        self._kr = ccxt.kraken({"enableRateLimit": True, "timeout": 8_000})
        self._kr.rateLimit = 1_000

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        self.start_ts = time.time()
        self._emit_initial_state()
        threading.Thread(target=self._discover_then_scan, daemon=True,
                         name="crypto-arb-scan").start()
        logger.info("CryptoArbBot started (interval=%.0fs)", self._interval)

    def stop(self) -> None:
        self._running = False

    def reset(self) -> None:
        self._balance       = self._starting_balance
        self._realized_pnl  = 0.0
        self._trades        = []
        self._opportunities = []
        self._top_pairs     = defaultdict(int)
        self._pnl_history   = []
        self._scan_count    = 0
        self._opp_count     = 0
        self._trade_count   = 0
        self.start_ts       = time.time()
        self._emit_overview()
        self._bus.publish("arb_start", {"ts": self.start_ts})
        self._bus.publish("arb_trades",    {"trades": []})
        self._bus.publish("arb_opportunities", {"opportunities": []})
        self._bus.publish("arb_top_pairs", {"pairs": []})
        self._bus.publish("arb_pnl",       {"history": []})

    def snapshot(self) -> dict:
        return {
            "overview":       self._get_overview(),
            "trades":         list(self._trades[-100:]),
            "opportunities":  list(self._opportunities[-50:]),
            "scan_pairs":     list(self._last_scan_pairs),
            "exchange_health": self._exchange_health,
            "top_pairs":      self._get_top_pairs(),
            "pnl_history":    list(self._pnl_history),
            "start_ts":       self.start_ts,
        }

    # ── Discovery + Scan loop ─────────────────────────────────────────────────

    def _discover_then_scan(self) -> None:
        try:
            self._pairs = self._discover_pairs()
        except Exception as exc:
            logger.error("Pair discovery failed: %s", exc)
            self._pairs = []

        while self._running:
            try:
                self._do_scan()
            except Exception as exc:
                logger.error("Scan error: %s", exc)
            if self._running:
                time.sleep(self._interval)

    def _discover_pairs(self) -> list:
        logger.info("CryptoArbBot: loading markets…")
        cb_markets = self._cb.load_markets()
        kr_markets = self._kr.load_markets()

        cb_syms = {s for s, m in cb_markets.items()
                   if m.get("active") and "/" in s and m.get("type", "spot") == "spot"}
        kr_syms = {s for s, m in kr_markets.items()
                   if m.get("active") and "/" in s}

        kr_norm: dict = {}
        for sym in kr_syms:
            norm = sym
            for old, new in KRAKEN_RENAMES.items():
                norm = norm.replace(old, new)
            kr_norm[norm] = sym

        common = cb_syms & set(kr_norm.keys())
        logger.info("CryptoArbBot: %d common pairs before volume filter", len(common))

        # Bulk tickers for volume filter
        try:
            cb_tickers = self._cb.fetch_tickers()
        except Exception:
            cb_tickers = {}
        try:
            kr_tickers = self._kr.fetch_tickers()
        except Exception:
            kr_tickers = {}

        sweet_spot = []   # $min_vol – $max_vol on both sides (illiquid target)
        above_cap  = []   # one or both sides > max_vol (HFT-covered, include as fallback)

        for sym in sorted(common):
            cb_vol = float((cb_tickers.get(sym) or {}).get("quoteVolume") or 0)
            kr_sym = kr_norm.get(sym, sym)
            kr_tick = kr_tickers.get(kr_sym) or kr_tickers.get(sym) or {}
            kr_vol  = float(kr_tick.get("quoteVolume") or 0)
            if cb_vol < self._min_vol or kr_vol < self._min_vol:
                continue   # too illiquid to fill
            if cb_vol <= self._max_vol and kr_vol <= self._max_vol:
                sweet_spot.append(sym)
            else:
                above_cap.append(sym)

        # Sweet-spot pairs first so the scan evaluates them before timeout
        qualified = sweet_spot + above_cap
        logger.info(
            "CryptoArbBot: %d qualified pairs (%d sweet-spot $%dk-$%dk, %d high-vol)",
            len(qualified), len(sweet_spot),
            int(self._min_vol / 1000), int(self._max_vol / 1000),
            len(above_cap),
        )
        self._bus.publish("arb_overview", self._get_overview())
        return qualified

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _do_scan(self) -> None:
        if not self._pairs:
            return

        books_cb: dict = {}
        books_kr: dict = {}
        health_cb = True
        health_kr = True

        import threading as _t
        sem_cb = _t.Semaphore(CONCURRENCY)
        sem_kr = _t.Semaphore(CONCURRENCY)

        def fetch_one(ex_name, sym, sem):
            with sem:
                try:
                    client = self._cb if ex_name == "coinbase" else self._kr
                    raw = client.fetch_order_book(sym, self._depth)
                    ts  = (raw.get("timestamp") or time.time() * 1000) / 1000
                    return ex_name, sym, raw["bids"][:self._depth], raw["asks"][:self._depth], ts, True
                except Exception as exc:
                    logger.debug("[%s] %s: %s", ex_name, sym, exc)
                    return ex_name, sym, [], [], time.time(), False

        scan_timeout = max(60.0, len(self._pairs) * 2.0)
        futures: dict = {}

        with ThreadPoolExecutor(max_workers=CONCURRENCY * 2) as pool:
            for sym in self._pairs:
                if not self._running:
                    return
                futures[pool.submit(fetch_one, "coinbase", sym, sem_cb)] = ("coinbase", sym)
                futures[pool.submit(fetch_one, "kraken",   sym, sem_kr)] = ("kraken",   sym)

            try:
                for fut in as_completed(futures, timeout=scan_timeout):
                    try:
                        ex_name, sym, bids, asks, ts, ok = fut.result()
                        if ex_name == "coinbase":
                            if ok:
                                books_cb[sym] = (bids, asks, ts)
                            else:
                                health_cb = False
                        else:
                            if ok:
                                books_kr[sym] = (bids, asks, ts)
                            else:
                                health_kr = False
                    except Exception:
                        pass
            except FutureTimeout:
                pass

        # Update health
        self._exchange_health = {"coinbase": health_cb, "kraken": health_kr}
        self._bus.publish("arb_exchange_health", self._exchange_health)

        self._scan_count += 1

        # Evaluate all pairs
        scan_pairs = []
        now = time.time()

        for sym in self._pairs:
            cb_data = books_cb.get(sym)
            kr_data = books_kr.get(sym)
            if not cb_data or not kr_data:
                continue

            cb_bids, cb_asks, cb_ts = cb_data
            kr_bids, kr_asks, kr_ts = kr_data

            if not cb_bids or not cb_asks or not kr_bids or not kr_asks:
                continue
            if now - cb_ts > self._max_age or now - kr_ts > self._max_age:
                continue

            cb_best_ask = float(cb_asks[0][0])
            cb_best_bid = float(cb_bids[0][0])
            kr_best_ask = float(kr_asks[0][0])
            kr_best_bid = float(kr_bids[0][0])

            # Check both directions
            for buy_ex, buy_ask, buy_bk_a, sell_ex, sell_bid, sell_bk_b in [
                ("coinbase", cb_best_ask, cb_asks, "kraken",   kr_best_bid, kr_bids),
                ("kraken",   kr_best_ask, kr_asks, "coinbase", cb_best_bid, cb_bids),
            ]:
                if sell_bid <= buy_ask:
                    continue

                raw_spread = (sell_bid - buy_ask) / buy_ask * 100
                # taker buy + maker sell
                fee_pct = (self._fees[buy_ex]["taker"] + self._fees[sell_ex]["maker"]) * 100

                # VWAP walk
                pos = min(self._balance * self._max_pos_pct, self._max_pos_usdc)
                buy_vwap, buy_fill   = self._vwap_buy(buy_bk_a,  pos)
                sell_vwap, sell_fill = self._vwap_sell(sell_bk_b, pos)
                actual = min(buy_fill, sell_fill, pos)
                if actual < 10:
                    continue

                slip_buy  = abs(buy_vwap  - buy_ask)  / buy_ask  * 100 if buy_ask  else 0
                slip_sell = abs(sell_vwap - sell_bid) / sell_bid * 100 if sell_bid else 0
                slip_pct  = slip_buy + slip_sell
                net       = raw_spread - fee_pct - slip_pct
                est_profit = actual * net / 100

                # quality_score: raw / fee ratio — >1.0 means spread exceeds fee cost
                quality = round(raw_spread / fee_pct, 4) if fee_pct else 0.0

                pair_data = {
                    "sym":      sym,
                    "buy_ex":   buy_ex,
                    "sell_ex":  sell_ex,
                    "buy_ask":  round(buy_ask,  8),
                    "sell_bid": round(sell_bid, 8),
                    "cb_ask":   round(cb_best_ask, 8),
                    "cb_bid":   round(cb_best_bid, 8),
                    "kr_ask":   round(kr_best_ask, 8),
                    "kr_bid":   round(kr_best_bid, 8),
                    "raw_pct":  round(raw_spread, 4),
                    "fee_pct":  round(fee_pct,    4),
                    "slip_pct": round(slip_pct,   4),
                    "net_pct":  round(net,        4),
                    "est_usd":  round(est_profit, 4),
                    "quality":  quality,
                    "ts":       now,
                }
                scan_pairs.append(pair_data)

                if net >= self._min_profit:
                    self._handle_opportunity(pair_data)

        # Sort by quality score descending (raw/fee ratio)
        scan_pairs.sort(key=lambda x: x.get("quality", 0), reverse=True)
        self._last_scan_pairs = scan_pairs

        # Log every scan — top 5 by quality so we can track progress toward threshold
        if scan_pairs:
            top5 = scan_pairs[:5]
            summary = "  ".join(
                f"{p['sym']}(q={p['quality']:.3f} net={p['net_pct']:+.3f}%)"
                for p in top5
            )
            logger.info("[scan #%d/%d pairs] best quality: %s",
                        self._scan_count, len(self._pairs), summary)
        else:
            logger.info("[scan #%d] no positive-spread pairs found this cycle", self._scan_count)

        # Top 10 by quality for dashboard quality panel
        quality_top10 = [
            {"sym": p["sym"], "quality": p["quality"],
             "raw_pct": p["raw_pct"], "fee_pct": p["fee_pct"],
             "net_pct": p["net_pct"], "buy_ex": p["buy_ex"], "sell_ex": p["sell_ex"]}
            for p in scan_pairs[:10]
        ]
        self._bus.publish("arb_quality_pairs", {"pairs": quality_top10,
                                                "scan_count": self._scan_count})

        # Emit scan result (top 30 by quality for the live feed)
        self._bus.publish("arb_scan_result", {"pairs": scan_pairs[:30],
                                              "scan_count": self._scan_count,
                                              "total_pairs": len(self._pairs)})
        self._emit_overview()

    # ── Opportunity + Trade ───────────────────────────────────────────────────

    def _handle_opportunity(self, p: dict) -> None:
        self._opp_count += 1
        self._top_pairs[p["sym"]] += 1
        opp = dict(p, opp_id=str(uuid.uuid4())[:8], detected_at=time.time())
        self._opportunities.append(opp)
        if len(self._opportunities) > 200:
            self._opportunities = self._opportunities[-200:]
        self._bus.publish("arb_opportunity", opp)
        self._bus.publish("arb_top_pairs",   {"pairs": self._get_top_pairs()})
        self._execute_paper_trade(p)

    def _execute_paper_trade(self, p: dict) -> None:
        pos    = min(self._balance * self._max_pos_pct, self._max_pos_usdc)
        pos    = min(pos, self._balance)
        if pos < 10:
            return

        buy_fee  = pos * self._fees[p["buy_ex"]]["taker"]
        qty      = (pos - buy_fee) / p["buy_ask"] if p["buy_ask"] else 0
        proceeds = qty * p["sell_bid"]
        sell_fee = proceeds * self._fees[p["sell_ex"]]["maker"]
        net_usdc = proceeds - sell_fee
        pnl      = net_usdc - pos

        self._balance      += pnl
        self._realized_pnl += pnl
        self._trade_count  += 1

        trade = {
            "id":        str(uuid.uuid4())[:8],
            "sym":       p["sym"],
            "buy_ex":    p["buy_ex"],
            "sell_ex":   p["sell_ex"],
            "buy_price": p["buy_ask"],
            "sell_price":p["sell_bid"],
            "pos_usdc":  round(pos, 2),
            "pnl_usdc":  round(pnl, 4),
            "net_pct":   p["net_pct"],
            "ts":        time.time(),
        }
        self._trades.append(trade)
        if len(self._trades) > 500:
            self._trades = self._trades[-500:]

        self._pnl_history.append({"ts": time.time(), "pnl": round(self._realized_pnl, 4)})
        if len(self._pnl_history) > 500:
            self._pnl_history = self._pnl_history[-500:]

        self._bus.publish("arb_trade", trade)
        self._bus.publish("arb_pnl",   {"history": self._pnl_history})
        logger.info("[PAPER] %s BUY %s SELL %s pnl=%+.4f", p["sym"], p["buy_ex"], p["sell_ex"], pnl)

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _vwap_buy(asks: list, usdc: float):
        remaining = usdc
        cost = qty = 0.0
        for level in asks:
            price, vol = float(level[0]), float(level[1])
            lv = price * vol
            if remaining <= lv:
                fq = remaining / price
                cost += fq * price; qty += fq; remaining = 0; break
            cost += lv; qty += vol; remaining -= lv
        if qty == 0:
            return float("inf"), 0.0
        return cost / qty, usdc - remaining

    @staticmethod
    def _vwap_sell(bids: list, usdc: float):
        remaining = usdc
        proceeds = qty = 0.0
        for level in bids:
            price, vol = float(level[0]), float(level[1])
            lv = price * vol
            if remaining <= lv:
                fq = remaining / price
                proceeds += fq * price; qty += fq; remaining = 0; break
            proceeds += lv; qty += vol; remaining -= lv
        if qty == 0:
            return 0.0, 0.0
        return proceeds / qty, usdc - remaining

    def _get_overview(self) -> dict:
        return {
            "balance":      round(self._balance, 2),
            "realized_pnl": round(self._realized_pnl, 4),
            "scan_count":   self._scan_count,
            "opp_count":    self._opp_count,
            "trade_count":  self._trade_count,
            "pair_count":   len(self._pairs),
            "start_ts":     self.start_ts,
        }

    def _get_top_pairs(self) -> list:
        return sorted(
            [{"sym": k, "count": v} for k, v in self._top_pairs.items()],
            key=lambda x: x["count"], reverse=True
        )[:10]

    def _emit_overview(self) -> None:
        self._bus.publish("arb_overview", self._get_overview())

    def _emit_initial_state(self) -> None:
        self._bus.publish("arb_start",           {"ts": self.start_ts})
        self._bus.publish("arb_overview",        self._get_overview())
        self._bus.publish("arb_exchange_health", self._exchange_health)
        self._bus.publish("arb_top_pairs",       {"pairs": []})
        self._bus.publish("arb_trades",          {"trades": []})
        self._bus.publish("arb_opportunities",   {"opportunities": []})
        self._bus.publish("arb_pnl",             {"history": []})
