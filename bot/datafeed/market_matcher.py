"""
Fuzzy-matches a LiveEvent's team names against active Polymarket soccer markets.
Caches the market list for 5 minutes to avoid excessive API calls.

Returns list[MatchedMarket] covering game_winner, over_under, and btts markets.
"""

import difflib
import json
import logging
import re
import time

from .models import MarketType, MatchedMarket

logger = logging.getLogger("arb_bot.datafeed.matcher")

GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
CACHE_TTL = 300  # seconds

# Common abbreviation expansions used in normalization
_ABBREV = {
    "man utd": "manchester united",
    "man city": "manchester city",
    "psg": "paris saint-germain",
    "inter": "inter milan",
    "atletico": "atletico madrid",
    "ac milan": "milan",
    "spurs": "tottenham",
    "bvb": "borussia dortmund",
}

_OU_REGEX   = re.compile(r"o/?u\s*(\d+\.?\d*)", re.IGNORECASE)
_BTTS_REGEX = re.compile(r"both\s+teams?\s+(to\s+)?score", re.IGNORECASE)


def _normalize(name: str) -> str:
    n = name.lower().strip()
    for abbr, full in _ABBREV.items():
        if n == abbr:
            return full
    return n


class MarketMatcher:
    def __init__(self, http_session):
        self._http = http_session
        self._cache: list = []
        self._cache_ts: float = 0.0

    # ── Legacy single-market API (kept for backward compat) ──────────────────

    def find_market(self, event) -> dict | None:
        """
        Return the best raw market dict for an event (game_winner only).
        Used by legacy callers; prefer find_all_markets() for new code.
        """
        markets = self._get_markets()
        if not markets:
            return None

        home = _normalize(event.home_team)
        away = _normalize(event.away_team)

        best_market = None
        best_score  = 0.0

        for mkt in markets:
            title = _normalize(mkt.get("question") or mkt.get("title") or "")
            score = self._score(title, home, away)
            if score > best_score:
                best_score  = score
                best_market = mkt

        if best_score >= 0.5:
            return best_market
        return None

    # ── New multi-market API ──────────────────────────────────────────────────

    def find_all_markets(self, event, rn1_teams: set | None = None) -> list:
        """
        Return all MatchedMarket objects for game_winner, over_under, and btts
        markets that match the event's teams.

        rn1_teams: if provided, lower the match threshold for teams in this set.
        """
        markets = self._get_markets()
        if not markets:
            return []

        home_norm = _normalize(event.home_team)
        away_norm = _normalize(event.away_team)

        # Determine match threshold
        rn1_boost = False
        if rn1_teams:
            hn = event.home_team.lower()
            an = event.away_team.lower()
            if any(t.lower() in hn or hn in t.lower() for t in rn1_teams) or \
               any(t.lower() in an or an in t.lower() for t in rn1_teams):
                rn1_boost = True

        threshold = 0.35 if rn1_boost else 0.50

        matched: list[MatchedMarket] = []
        for mkt in markets:
            title = _normalize(mkt.get("question") or mkt.get("title") or "")
            score = self._score(title, home_norm, away_norm)
            if score < threshold:
                continue

            mm = self._classify_market(mkt, title)
            if mm is not None:
                matched.append(mm)

        if matched:
            logger.debug(
                "find_all_markets '%s vs %s' → %d markets (boost=%s)",
                event.home_team, event.away_team, len(matched), rn1_boost,
            )
        return matched

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _classify_market(self, mkt: dict, norm_title: str) -> "MatchedMarket | None":
        """Determine MarketType and build a MatchedMarket, or return None."""
        token_id, token_id_no, price = self._extract_tokens(mkt)
        if token_id is None or price is None:
            return None

        market_id = mkt.get("id") or mkt.get("conditionId", "")
        question  = (mkt.get("question") or mkt.get("title", ""))[:120]

        # Over/Under
        ou_match = _OU_REGEX.search(norm_title)
        if ou_match:
            line = float(ou_match.group(1))
            return MatchedMarket(
                market_id=market_id,
                question=question,
                market_type=MarketType.OVER_UNDER,
                token_id=token_id,
                token_id_no=token_id_no or "",
                current_price=price,
                ou_line=line,
                outcome="Yes",
            )

        # BTTS
        if _BTTS_REGEX.search(norm_title):
            return MatchedMarket(
                market_id=market_id,
                question=question,
                market_type=MarketType.BOTH_TEAMS,
                token_id=token_id,
                token_id_no=token_id_no or "",
                current_price=price,
                ou_line=None,
                outcome="Yes",
            )

        # Default: game winner
        return MatchedMarket(
            market_id=market_id,
            question=question,
            market_type=MarketType.GAME_WINNER,
            token_id=token_id,
            token_id_no=token_id_no or "",
            current_price=price,
            ou_line=None,
            outcome="Yes",
        )

    def _extract_tokens(self, mkt: dict) -> "tuple[str|None, str|None, float|None]":
        try:
            raw_ids  = mkt.get("clobTokenIds", "[]")
            token_ids = json.loads(raw_ids) if isinstance(raw_ids, str) else raw_ids
            if not token_ids:
                return None, None, None
            token_id    = token_ids[0]
            token_id_no = token_ids[1] if len(token_ids) > 1 else None
            best_ask    = mkt.get("bestAsk")
            if best_ask is None:
                return None, None, None
            return token_id, token_id_no, float(best_ask)
        except Exception:
            return None, None, None

    def _score(self, title: str, home: str, away: str) -> float:
        """Compute a match score between a market title and two team names."""
        ratio_home = difflib.SequenceMatcher(None, title, home).ratio()
        ratio_away = difflib.SequenceMatcher(None, title, away).ratio()

        title_words = set(title.split())
        home_words  = set(home.split())
        away_words  = set(away.split())
        overlap     = len((home_words | away_words) & title_words)
        total_words = len(home_words | away_words)
        word_score  = overlap / total_words if total_words > 0 else 0.0

        return max(ratio_home, ratio_away) * 0.5 + word_score * 0.5

    def _get_markets(self) -> list:
        now = time.time()
        if self._cache and (now - self._cache_ts) < CACHE_TTL:
            return self._cache

        try:
            resp = self._http.get(
                GAMMA_MARKETS_URL,
                params={"active": "true", "tag": "Soccer", "limit": 200},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            self._cache    = data if isinstance(data, list) else []
            self._cache_ts = now
            logger.debug("MarketMatcher: fetched %d soccer markets", len(self._cache))
        except Exception as exc:
            logger.warning("MarketMatcher: failed to fetch markets: %s", exc)

        return self._cache
