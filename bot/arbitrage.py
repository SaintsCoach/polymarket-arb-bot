"""Arbitrage opportunity detection and profit calculation."""

import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class ArbOpportunity:
    market_id: str
    market_question: str
    yes_token_id: str
    no_token_id: str
    yes_ask: float           # best ask price for YES shares (0–1)
    no_ask: float            # best ask price for NO shares (0–1)
    combined_pct: float      # (yes_ask + no_ask) * 100  — below 100 means arb exists
    expected_profit_pct: float  # profit as % of capital deployed
    shares: float            # number of shares to buy on each side
    yes_cost_usdc: float     # USDC to spend on YES side
    no_cost_usdc: float      # USDC to spend on NO side
    estimated_profit_usdc: float


def find_arb_opportunity(
    market: dict,
    yes_ask: float,
    no_ask: float,
    max_trade_size_usdc: float,
    max_risk_per_trade_usdc: float,
    min_profit_pct: float,
) -> Optional[ArbOpportunity]:
    """
    Return an ArbOpportunity if:
      - combined ask price < 1.0 (arbitrage exists)
      - expected profit % >= min_profit_pct

    Shares on each side are the same so that exactly one side pays out 1 USDC/share.
    Trade size is capped by both max_trade_size_usdc (per side) and max_risk_per_trade_usdc (total).
    """
    combined = yes_ask + no_ask

    # No arb if combined >= 1 (market is fairly priced or overpriced)
    if combined >= 1.0:
        return None

    # Profit per pair of shares = 1.0 - combined (one side always wins)
    # Profit % = profit / cost = (1 - combined) / combined * 100
    profit_pct = (1.0 - combined) / combined * 100

    if profit_pct < min_profit_pct:
        return None

    # Compute trade size: equal shares on both sides
    # Max shares limited by per-side cap and total risk cap
    max_by_yes_side = max_trade_size_usdc / yes_ask
    max_by_no_side = max_trade_size_usdc / no_ask
    max_by_risk = max_risk_per_trade_usdc / combined  # total cost per pair = combined

    shares = min(max_by_yes_side, max_by_no_side, max_by_risk)

    yes_cost = shares * yes_ask
    no_cost = shares * no_ask
    profit = shares * (1.0 - combined)

    cid = market.get("conditionId") or market.get("condition_id", "unknown")
    question = market.get("question", "Unknown market")
    yes_id, no_id = extract_market_token_ids(market)

    return ArbOpportunity(
        market_id=cid,
        market_question=question,
        yes_token_id=yes_id,
        no_token_id=no_id,
        yes_ask=yes_ask,
        no_ask=no_ask,
        combined_pct=combined * 100,
        expected_profit_pct=profit_pct,
        shares=shares,
        yes_cost_usdc=yes_cost,
        no_cost_usdc=no_cost,
        estimated_profit_usdc=profit,
    )


def extract_market_token_ids(market: dict) -> tuple[str, str]:
    """
    Extract (yes_token_id, no_token_id) from a Gamma API market dict.

    Handles two formats:
      - CLOB format:  market["tokens"] = [{"outcome": "Yes", "token_id": "..."}, ...]
      - Gamma format: market["clobTokenIds"] = '["id1","id2"]'  (JSON string)
                      market["outcomes"]     = '["Yes","No"]'   (JSON string)
    """
    # ── CLOB-style tokens list ────────────────────────────────────────────────
    tokens = market.get("tokens")
    if tokens and isinstance(tokens, list) and isinstance(tokens[0], dict):
        yes_id = no_id = ""
        for t in tokens:
            outcome = (t.get("outcome") or "").strip().lower()
            tid = t.get("token_id") or t.get("tokenId") or t.get("id", "")
            if outcome in ("yes", "1"):
                yes_id = tid
            elif outcome in ("no", "0"):
                no_id = tid
        return yes_id, no_id

    # ── Gamma API format ──────────────────────────────────────────────────────
    ids_raw = market.get("clobTokenIds", "[]")
    outcomes_raw = market.get("outcomes", '["Yes","No"]')

    try:
        ids: list[str] = json.loads(ids_raw) if isinstance(ids_raw, str) else list(ids_raw)
        outcomes: list[str] = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else list(outcomes_raw)
    except (json.JSONDecodeError, TypeError):
        return "", ""

    if len(ids) < 2:
        return "", ""

    yes_id = no_id = ""
    for i, outcome in enumerate(outcomes):
        if i >= len(ids):
            break
        o = str(outcome).strip().lower()
        if o in ("yes", "1"):
            yes_id = ids[i]
        elif o in ("no", "0"):
            no_id = ids[i]

    # Fallback: assume first = YES, second = NO
    if not yes_id:
        yes_id = ids[0]
    if not no_id and len(ids) > 1:
        no_id = ids[1]

    return yes_id, no_id
