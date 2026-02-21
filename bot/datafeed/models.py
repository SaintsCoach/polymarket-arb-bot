"""Data models for the DataFeed Bot."""

import time
from dataclasses import dataclass, field


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
