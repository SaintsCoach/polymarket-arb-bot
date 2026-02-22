"""Data models for the DataFeed Bot."""

import time
from dataclasses import dataclass, field
from enum import Enum


class MarketType(Enum):
    GAME_WINNER = "game_winner"
    OVER_UNDER  = "over_under"
    BOTH_TEAMS  = "btts"


@dataclass
class LiveEvent:
    fixture_id: int
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    minute: int           # 0–90+
    event_type: str       # "goal" | "red_card" | "match_start" | "match_end"
    detected_at: float    # time.time()
    raw: dict = field(default_factory=dict)
    source: str = "api_football"  # "api_football" | "sportradar"


@dataclass
class MatchedMarket:
    market_id: str
    question: str
    market_type: MarketType
    token_id: str        # Yes token
    token_id_no: str     # No token
    current_price: float
    ou_line: float | None  # only for OVER_UNDER
    outcome: str


@dataclass
class DFOpportunity:
    fixture_id: int
    market_id: str
    market_question: str
    token_id: str
    outcome: str          # "Yes" | "No"
    fair_value: float
    market_price: float
    edge_pct: float
    source_event: str     # e.g. "goal 1-0 min 23"
    detected_at: float
    market_type: str = "game_winner"   # "game_winner" | "over_under" | "btts"
    ou_line: float | None = None


@dataclass
class EdgeMeasurement:
    event_id: str
    event_type: str
    latency_s: float        # seconds: detection → price move
    price_at_detection: float
    price_after_move: float
    price_delta: float
    detected_at: float
    price_moved_at: float
    feed_source: str        # "api_football" | "sportradar"


@dataclass
class DataFeedPosition:
    id: str
    market_question: str
    token_id: str
    outcome: str
    entry_price: float
    current_price: float
    shares: float
    usdc_deployed: float
    opened_at: float
    source_event: str
    fixture_id: int

    @property
    def unrealized_pnl(self) -> float:
        return (self.current_price - self.entry_price) * self.shares

    @property
    def unrealized_pnl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return (self.current_price - self.entry_price) / self.entry_price * 100

    @property
    def age_s(self) -> float:
        return time.time() - self.opened_at


@dataclass
class ResolvedDFTrade:
    market_question: str
    outcome: str
    entry_price: float
    exit_price: float
    shares: float
    usdc_deployed: float
    pnl_usdc: float
    duration_s: float
    source_event: str
    resolved_at: float
    result: str   # "WIN" | "LOSS" | "PUSH"
