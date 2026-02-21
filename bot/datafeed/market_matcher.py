"""
Fuzzy-matches a LiveEvent's team names against active Polymarket soccer markets.
Caches the market list for 5 minutes to avoid excessive API calls.
"""

import difflib
import logging
import time

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

    def find_market(self, event) -> dict | None:
        """
        Given a LiveEvent, try to find a matching active Polymarket soccer market.
        Returns the market dict (including clobTokenIds, bestAsk) or None.
        """
        markets = self._get_markets()
        if not markets:
            return None

        home = _normalize(event.home_team)
        away = _normalize(event.away_team)

        best_market = None
        best_score = 0.0

        for mkt in markets:
            title = _normalize(mkt.get("question") or mkt.get("title") or "")
            score = self._score(title, home, away)
            if score > best_score:
                best_score = score
                best_market = mkt

        if best_score >= 0.5:
            logger.debug(
                "Matched '%s vs %s' → '%s' (score=%.2f)",
                event.home_team,
                event.away_team,
                best_market.get("question", "")[:60],
                best_score,
            )
            return best_market

        return None

    def _score(self, title: str, home: str, away: str) -> float:
        """Compute a match score between a market title and two team names."""
        # SequenceMatcher ratio against each team name
        ratio_home = difflib.SequenceMatcher(None, title, home).ratio()
        ratio_away = difflib.SequenceMatcher(None, title, away).ratio()

        # Word overlap: count shared words
        title_words = set(title.split())
        home_words = set(home.split())
        away_words = set(away.split())
        overlap = len((home_words | away_words) & title_words)
        total_words = len(home_words | away_words)
        word_score = overlap / total_words if total_words > 0 else 0.0

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
            self._cache = data if isinstance(data, list) else []
            self._cache_ts = now
            logger.debug("MarketMatcher: fetched %d soccer markets", len(self._cache))
        except Exception as exc:
            logger.warning("MarketMatcher: failed to fetch markets: %s", exc)

        return self._cache
