"""
RN1 Trade History Analyzer.

Fetches the last N trades and current positions for a watched address via the
Polymarket Data API, computes position-sizing, entry-price, market-type, and
timing statistics, and persists the result to logs/rn1_analysis.json.

Usage (standalone):
    python -m bot.mirror.rn1_analysis 0x2005d16a84ceefa912d4e380cd32e7ff827875ea

Called from the dashboard via /api/mirror/rn1-analysis.
"""

import json
import logging
import math
import os
import statistics
import time
from pathlib import Path

import requests

logger = logging.getLogger("arb_bot.mirror.rn1")

DATA_API  = "https://data-api.polymarket.com"
CACHE_FILE = Path("logs/rn1_analysis.json")
CACHE_TTL  = 300   # seconds — refresh at most every 5 min

# ── Market category keywords ──────────────────────────────────────────────────
_CATEGORIES = {
    "Soccer":     ["soccer", "la liga", "premier league", "champions league",
                   "bundesliga", "serie a", "ligue 1", "copa", "euro", "fifa",
                   "o/u", "over/under", "btts", "both teams", "mallorca",
                   "barcelona", "real madrid", "chelsea", "arsenal", "liverpool",
                   "manchester", "psg", "juventus", "inter", "milan", "ajax",
                   "atletico", "dortmund", "porto", "celtic", "rangers"],
    "Basketball": ["nba", "basketball", "lakers", "celtics", "warriors",
                   "bulls", "nets", "heat", "bucks", "76ers", "knicks"],
    "American Football": ["nfl", "super bowl", "touchdown", "quarterback",
                          "patriots", "chiefs", "cowboys", "eagles", "rams"],
    "Baseball":   ["mlb", "baseball", "world series", "yankees", "dodgers",
                   "red sox", "cubs", "astros"],
    "MMA/Boxing": ["ufc", "boxing", "mma", "fight", "knockout", "championship bout"],
    "Politics":   ["election", "president", "congress", "senate", "vote",
                   "trump", "biden", "harris", "democrat", "republican",
                   "governor", "mayor", "primary", "referendum", "ballot"],
    "Crypto":     ["bitcoin", "btc", "ethereum", "eth", "crypto", "coin",
                   "defi", "nft", "token", "price", "market cap"],
    "Other":      [],   # catch-all
}


def _categorize(title: str) -> str:
    t = title.lower()
    for cat, keywords in _CATEGORIES.items():
        if cat == "Other":
            continue
        if any(kw in t for kw in keywords):
            return cat
    return "Other"


def _percentile(sorted_vals: list, pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = max(0, min(len(sorted_vals) - 1, int(math.ceil(len(sorted_vals) * pct / 100)) - 1))
    return sorted_vals[idx]


# ── Fetch helpers ─────────────────────────────────────────────────────────────

def _fetch_activity(session: requests.Session, address: str, limit: int = 500) -> list:
    """
    Fetch trade activity. Tries /activity then /trades as fallback.
    Returns list of raw trade dicts.
    """
    for path in ("/activity", "/trades"):
        try:
            resp = session.get(
                f"{DATA_API}{path}",
                params={"user": address, "limit": limit},
                timeout=15,
            )
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                # Some endpoints wrap in {"data": [...]}
                return data.get("data") or data.get("activities") or data.get("trades") or []
        except Exception as exc:
            logger.debug("Activity fetch %s failed: %s", path, exc)
    return []


def _fetch_positions(session: requests.Session, address: str,
                     redeemable: bool = False) -> list:
    """Fetch active or redeemable positions."""
    try:
        params = {
            "user": address,
            "sizeThreshold": "0.01",
            "limit": 500,
        }
        if redeemable:
            params["redeemable"] = "true"
        else:
            params["redeemable"] = "false"

        resp = session.get(f"{DATA_API}/positions", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.debug("Positions fetch failed: %s", exc)
        return []


# ── Field extraction helpers (defensive for varied API shapes) ────────────────

def _usdc_size(trade: dict) -> float:
    """Extract USDC amount from a trade record."""
    for key in ("usdcSize", "usdc_size", "amount", "cashAmount", "dollarValue"):
        v = trade.get(key)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    # Fallback: shares × price
    price = float(trade.get("price", 0) or 0)
    size  = float(trade.get("size", 0) or 0)
    if price > 0 and size > 0:
        return round(price * size, 4)
    return 0.0


def _price(trade: dict) -> float:
    for key in ("price", "avgPrice", "executedPrice"):
        v = trade.get(key)
        if v is not None:
            try:
                p = float(v)
                if 0 < p <= 1:
                    return p
            except (ValueError, TypeError):
                pass
    return 0.0


def _side(trade: dict) -> str:
    """BUY or SELL."""
    s = str(trade.get("side") or trade.get("type") or "").upper()
    if "BUY" in s or "LONG" in s:
        return "BUY"
    if "SELL" in s or "SHORT" in s or "REDEEM" in s:
        return "SELL"
    return "BUY"


def _outcome(trade: dict) -> str:
    o = str(trade.get("outcome") or "").strip()
    return o if o in ("Yes", "No") else "Yes"


def _title(trade: dict) -> str:
    for key in ("title", "market", "question", "marketTitle"):
        v = trade.get(key)
        if v:
            return str(v)
    return "Unknown"


def _ts(trade: dict) -> float:
    """Return Unix timestamp."""
    for key in ("timestamp", "ts", "createdAt", "created_at", "time"):
        v = trade.get(key)
        if v is None:
            continue
        try:
            f = float(v)
            # If looks like milliseconds (> year 2100 in seconds), convert
            if f > 4_000_000_000:
                f /= 1000
            return f
        except (ValueError, TypeError):
            pass
    return time.time()


# ── Core analysis ─────────────────────────────────────────────────────────────

def analyze(address: str, session: requests.Session | None = None) -> dict:
    """
    Fetch trade history + positions for `address` and return a stats dict.
    Result is also persisted to CACHE_FILE.
    """
    if session is None:
        session = requests.Session()
        session.headers["Accept"] = "application/json"

    logger.info("[rn1] Fetching trade history for %s …", address[:12])
    t0 = time.time()

    activity   = _fetch_activity(session, address, limit=500)
    positions  = _fetch_positions(session, address, redeemable=False)
    redeemable = _fetch_positions(session, address, redeemable=True)

    logger.info("[rn1] Fetched: %d activity, %d positions, %d redeemable (%.1fs)",
                len(activity), len(positions), len(redeemable), time.time() - t0)

    # ── Filter to BUY trades only for sizing stats ────────────────────────────
    buys = [t for t in activity if _side(t) == "BUY"]

    usdc_sizes = sorted([s for t in buys if (s := _usdc_size(t)) > 0])
    prices     = [p for t in buys if (p := _price(t)) > 0]
    outcomes   = [_outcome(t) for t in buys]
    titles     = [_title(t) for t in buys]
    timestamps = sorted([_ts(t) for t in buys])

    # ── Position sizing ───────────────────────────────────────────────────────
    sizing: dict = {}
    if usdc_sizes:
        sizing = {
            "count":      len(usdc_sizes),
            "min":        round(usdc_sizes[0], 2),
            "max":        round(usdc_sizes[-1], 2),
            "mean":       round(statistics.mean(usdc_sizes), 2),
            "median":     round(statistics.median(usdc_sizes), 2),
            "p25":        round(_percentile(usdc_sizes, 25), 2),
            "p75":        round(_percentile(usdc_sizes, 75), 2),
            "p95":        round(_percentile(usdc_sizes, 95), 2),
            "total_usdc": round(sum(usdc_sizes), 2),
            # Bucketed distribution
            "buckets": {
                "<$50":    sum(1 for s in usdc_sizes if s < 50),
                "$50-100": sum(1 for s in usdc_sizes if 50 <= s < 100),
                "$100-250":sum(1 for s in usdc_sizes if 100 <= s < 250),
                "$250-500":sum(1 for s in usdc_sizes if 250 <= s < 500),
                "$500+":   sum(1 for s in usdc_sizes if s >= 500),
            },
        }

    # ── Entry price distribution ──────────────────────────────────────────────
    price_dist: dict = {}
    if prices:
        price_dist = {
            "mean":    round(statistics.mean(prices), 4),
            "median":  round(statistics.median(prices), 4),
            "buckets": {
                "<30%":   sum(1 for p in prices if p < 0.30),
                "30-50%": sum(1 for p in prices if 0.30 <= p < 0.50),
                "50-70%": sum(1 for p in prices if 0.50 <= p < 0.70),
                "70-90%": sum(1 for p in prices if 0.70 <= p < 0.90),
                ">90%":   sum(1 for p in prices if p >= 0.90),
            },
        }

    # ── Yes/No split ─────────────────────────────────────────────────────────
    yes_count = outcomes.count("Yes")
    no_count  = outcomes.count("No")
    total_out = yes_count + no_count
    outcome_split = {
        "yes_count": yes_count,
        "no_count":  no_count,
        "yes_pct":   round(yes_count / total_out * 100, 1) if total_out else 0,
        "no_pct":    round(no_count  / total_out * 100, 1) if total_out else 0,
    }

    # ── Market category distribution ──────────────────────────────────────────
    cat_counts: dict[str, int] = {}
    for title in titles:
        cat = _categorize(title)
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    # Sort by count desc, limit to top 6
    category_dist = dict(sorted(cat_counts.items(), key=lambda x: -x[1])[:8])

    # ── Win rate from redeemable positions ────────────────────────────────────
    win_rate: dict = {}
    if redeemable:
        # Redeemable = market resolved in our favour (value > 0)
        wins   = len(redeemable)
        # Active positions we still hold
        active = len(positions)
        win_rate = {
            "redeemable_wins": wins,
            "active_positions": active,
        }

    # ── Timing ────────────────────────────────────────────────────────────────
    timing: dict = {}
    if timestamps:
        now_day    = int(time.time() // 86400)
        days_active: dict[int, int] = {}
        for ts in timestamps:
            day = int(ts // 86400)
            days_active[day] = days_active.get(day, 0) + 1
        last_30_days = sum(v for k, v in days_active.items() if k >= now_day - 30)
        timing = {
            "first_trade_ts":   timestamps[0],
            "last_trade_ts":    timestamps[-1],
            "days_with_trades": len(days_active),
            "trades_last_30d":  last_30_days,
            "avg_trades_per_day": round(last_30_days / 30, 1),
            "most_active_day_count": max(days_active.values()) if days_active else 0,
        }

    result = {
        "address":       address,
        "fetched_at":    time.time(),
        "raw_activity":  len(activity),
        "buy_trades":    len(buys),
        "sell_trades":   len(activity) - len(buys),
        "sizing":        sizing,
        "price_dist":    price_dist,
        "outcome_split": outcome_split,
        "category_dist": category_dist,
        "win_rate":      win_rate,
        "timing":        timing,
    }

    # Persist cache
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(result, indent=2))
        logger.info("[rn1] Analysis saved to %s", CACHE_FILE)
    except Exception as exc:
        logger.warning("[rn1] Could not write cache: %s", exc)

    return result


def load_cached() -> dict | None:
    """Return cached analysis if it exists and is fresh, else None."""
    try:
        if not CACHE_FILE.exists():
            return None
        age = time.time() - CACHE_FILE.stat().st_mtime
        if age > CACHE_TTL:
            return None
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return None


# ── Standalone entry point ────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    addr = sys.argv[1] if len(sys.argv) > 1 else "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
    result = analyze(addr)
    print(json.dumps(result, indent=2))
