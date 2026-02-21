"""Data models for the Mirror Bot."""

import time
import uuid
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AddressStats:
    trades_mirrored: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usdc: float = 0.0

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total * 100) if total > 0 else 0.0


@dataclass
class WatchedAddress:
    address: str
    nickname: str
    enabled: bool = True
    poll_interval: float = 30.0
    last_poll_ts: float = 0.0
    last_successful_poll_ts: float = 0.0
    consecutive_failures: int = 0
    rate_limited_until: Optional[float] = None
    # token_id → raw position dict from Gamma API
    last_positions: dict = field(default_factory=dict)
    is_initialized: bool = False   # True after first baseline snapshot
    stats: AddressStats = field(default_factory=AddressStats)
    # Debug / diagnostics (updated every poll)
    last_poll_count:  int = 0   # positions returned by API last poll
    last_poll_new:    int = 0   # new positions detected
    last_poll_closed: int = 0   # closed positions detected

    @property
    def is_stale(self) -> bool:
        return self.consecutive_failures >= 5

    @property
    def is_rate_limited(self) -> bool:
        return self.rate_limited_until is not None and time.time() < self.rate_limited_until

    @property
    def health(self) -> str:
        if self.is_stale:
            return "stale"
        if self.is_rate_limited:
            return "rate_limited"
        return "ok"


@dataclass
class MirrorPosition:
    id: str
    market_id: str
    market_question: str
    token_id: str
    outcome: str            # "Yes" or "No"
    entry_price: float
    current_price: float
    shares: float
    usdc_deployed: float
    opened_at: float
    triggered_by: str       # nickname
    triggered_by_address: str

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
class QueuedTrade:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    market_id: str = ""
    market_question: str = ""
    token_id: str = ""
    outcome: str = ""
    entry_price: float = 0.5
    triggered_by: str = ""
    triggered_by_address: str = ""
    queued_at: float = field(default_factory=time.time)


@dataclass
class ResolvedTrade:
    market_question: str
    outcome: str
    entry_price: float
    exit_price: float
    shares: float
    usdc_deployed: float
    pnl_usdc: float
    duration_s: float
    triggered_by: str
    resolved_at: float
    result: str             # "WIN" | "LOSS" | "PUSH"
