"""
Polls Polymarket wallet addresses for position changes.

Features:
  - Configurable per-address poll interval with random jitter
  - Exponential backoff on failures (1s → 2s → 4s → 8s → 16s, max 32s)
  - 429 rate-limit detection → 60-second pause for that address
  - Consecutive-failure tracking; address flagged "stale" after 5 failures
  - Last-known position cache so state is preserved across failed polls
"""

import logging
import os
import json
import random
import threading
import time
from typing import Callable, Optional

import requests

from .models import WatchedAddress, AddressStats

logger = logging.getLogger("arb_bot.mirror.monitor")

DATA_API  = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

MAX_RETRIES          = 5
BASE_DELAY           = 1.0
MAX_DELAY            = 32.0
RATE_LIMIT_PAUSE     = 60.0
MAX_FAILURES_STALE   = 5
PERSIST_PATH         = "logs/mirror_addresses.json"


class RateLimitError(Exception):
    pass


class AddressMonitor:
    def __init__(
        self,
        on_position_opened: Callable,
        on_position_closed: Callable,
        event_bus=None,
        default_interval: float = 30.0,
    ):
        self._on_opened   = on_position_opened
        self._on_closed   = on_position_closed
        self._bus         = event_bus
        self._default_interval = default_interval
        self._addresses: dict[str, WatchedAddress] = {}
        self._lock    = threading.Lock()
        self._running = False
        self._http    = requests.Session()
        self._http.headers.update({"Accept": "application/json"})
        self._load_persisted()

    # ── Public API ────────────────────────────────────────────────────────────

    def add_address(self, address: str, nickname: str,
                    poll_interval: Optional[float] = None) -> dict:
        key = address.lower()
        with self._lock:
            if key in self._addresses:
                self._addresses[key].nickname = nickname
            else:
                self._addresses[key] = WatchedAddress(
                    address=key,
                    nickname=nickname,
                    poll_interval=poll_interval or self._default_interval,
                )
        self._persist()
        self._emit_address_list()
        logger.info("Watching %s (%s)", address[:12], nickname)
        return self._addr_to_dict(self._addresses[key])

    def remove_address(self, address: str) -> bool:
        key = address.lower()
        with self._lock:
            existed = key in self._addresses
            self._addresses.pop(key, None)
        if existed:
            self._persist()
            self._emit_address_list()
            logger.info("Removed address %s", address[:12])
        return existed

    def update_address(self, address: str, nickname: Optional[str] = None,
                       enabled: Optional[bool] = None) -> bool:
        key = address.lower()
        with self._lock:
            cfg = self._addresses.get(key)
            if not cfg:
                return False
            if nickname is not None:
                cfg.nickname = nickname
            if enabled is not None:
                cfg.enabled = enabled
        self._persist()
        self._emit_address_list()
        return True

    def get_addresses(self) -> list[dict]:
        with self._lock:
            return [self._addr_to_dict(a) for a in self._addresses.values()]

    def start(self) -> None:
        self._running = True
        threading.Thread(target=self._poll_loop, daemon=True,
                         name="mirror-poller").start()
        logger.info("AddressMonitor started")

    def stop(self) -> None:
        self._running = False

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while self._running:
            with self._lock:
                addrs = list(self._addresses.values())

            for cfg in addrs:
                if not cfg.enabled:
                    continue
                if cfg.is_rate_limited:
                    continue
                jitter   = random.uniform(0, 5)
                next_due = cfg.last_poll_ts + cfg.poll_interval + jitter
                if time.time() < next_due:
                    continue
                self._poll_address(cfg)

            time.sleep(1)

    def _poll_address(self, cfg: WatchedAddress) -> None:
        cfg.last_poll_ts = time.time()
        try:
            positions = self._fetch_positions(cfg.address)
            self._process_positions(cfg, positions)
            cfg.last_successful_poll_ts = time.time()
            cfg.consecutive_failures    = 0
            self._emit_address_status(cfg)

        except RateLimitError:
            cfg.rate_limited_until  = time.time() + RATE_LIMIT_PAUSE
            cfg.consecutive_failures += 1
            logger.warning("Rate limited on %s (%s) — pausing %ds",
                           cfg.address[:12], cfg.nickname, RATE_LIMIT_PAUSE)
            if self._bus:
                self._bus.publish("mirror_api_event", {
                    "kind":       "rate_limited",
                    "address":    cfg.address,
                    "nickname":   cfg.nickname,
                    "resume_at":  cfg.rate_limited_until,
                    "ts":         time.time(),
                })
            self._emit_address_status(cfg)

        except Exception as exc:
            cfg.consecutive_failures += 1
            logger.error("Poll failed for %s (%s) attempt %d: %s",
                         cfg.address[:12], cfg.nickname,
                         cfg.consecutive_failures, exc)
            if self._bus:
                self._bus.publish("mirror_api_event", {
                    "kind":                "poll_error",
                    "address":             cfg.address,
                    "nickname":            cfg.nickname,
                    "consecutive_failures": cfg.consecutive_failures,
                    "error":               str(exc),
                    "stale":               cfg.is_stale,
                    "ts":                  time.time(),
                })
            self._emit_address_status(cfg)

    def _fetch_positions(self, address: str) -> list[dict]:
        """Fetch active (non-redeemable) positions with exponential backoff.

        Uses redeemable=false so we only see open markets, and limit=500 so
        we never silently truncate a whale with many active positions.
        Raises RateLimitError or the last Exception on total failure.
        """
        delay    = BASE_DELAY
        last_exc = None

        for attempt in range(MAX_RETRIES):
            try:
                resp = self._http.get(
                    f"{DATA_API}/positions",
                    params={
                        "user":        address,
                        "sizeThreshold": 0.01,
                        "redeemable":  "false",   # skip resolved markets
                        "limit":       500,        # whale may have many active positions
                    },
                    timeout=10,
                )
                if resp.status_code == 429:
                    raise RateLimitError()
                resp.raise_for_status()
                data = resp.json()
                positions = data if isinstance(data, list) else data.get("positions", [])
                logger.debug("[%s] API returned %d active positions", address[:12], len(positions))
                return positions

            except RateLimitError:
                raise
            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    logger.warning("[%s] Fetch attempt %d failed: %s — retrying in %.1fs",
                                   address[:12], attempt + 1, exc, delay)
                    if self._bus:
                        self._bus.publish("mirror_api_event", {
                            "kind":    "retry",
                            "address": address,
                            "attempt": attempt + 1,
                            "delay_s": delay,
                            "error":   str(exc),
                            "ts":      time.time(),
                        })
                    time.sleep(delay)
                    delay = min(delay * 2, MAX_DELAY)

        raise last_exc

    def _process_positions(self, cfg: WatchedAddress,
                           positions: list[dict]) -> None:
        new_map = {p["asset"]: p for p in positions if p.get("asset")}
        cfg.last_poll_count = len(new_map)

        logger.info("[%s] Poll: %d active positions fetched (initialized=%s, baseline=%d)",
                    cfg.nickname, len(new_map), cfg.is_initialized, len(cfg.last_positions))

        if not cfg.is_initialized:
            cfg.last_positions = new_map
            cfg.is_initialized = True
            cfg.last_poll_new    = 0
            cfg.last_poll_closed = 0
            logger.info("[%s] Baseline snapshot: %d positions (not mirrored)",
                        cfg.nickname, len(new_map))
            self._emit_poll_debug(cfg, new_map, opened=[], closed=[])
            return  # no callbacks on first poll

        old_map = cfg.last_positions
        opened_ids  = [tid for tid in new_map if tid not in old_map]
        closed_ids  = [tid for tid in old_map if tid not in new_map]

        logger.info("[%s] Diff: %d new, %d closed  (prev=%d, curr=%d)",
                    cfg.nickname, len(opened_ids), len(closed_ids),
                    len(old_map), len(new_map))

        cfg.last_poll_new    = len(opened_ids)
        cfg.last_poll_closed = len(closed_ids)

        for token_id in opened_ids:
            pos = new_map[token_id]
            logger.info("[%s] opened → %s  asset=%s  price=%s",
                        cfg.nickname, pos.get("title", "?")[:55],
                        token_id[:16], pos.get("curPrice"))
            try:
                self._on_opened(cfg, pos)
            except Exception as exc:
                logger.error("[%s] on_opened error for %s: %s",
                             cfg.nickname, pos.get("title", "?")[:40], exc)

        for token_id in closed_ids:
            pos = old_map[token_id]
            logger.info("[%s] closed → %s  asset=%s",
                        cfg.nickname, pos.get("title", "?")[:55], token_id[:16])
            try:
                self._on_closed(cfg, pos)
            except Exception as exc:
                logger.error("[%s] on_closed error for %s: %s",
                             cfg.nickname, pos.get("title", "?")[:40], exc)

        cfg.last_positions = new_map
        self._emit_poll_debug(cfg, new_map,
                              opened=[new_map[t] for t in opened_ids],
                              closed=[old_map[t] for t in closed_ids])

    def reset_all(self) -> None:
        with self._lock:
            for cfg in self._addresses.values():
                cfg.is_initialized = False
                cfg.last_positions = {}

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist(self) -> None:
        try:
            os.makedirs(os.path.dirname(PERSIST_PATH), exist_ok=True)
            with self._lock:
                data = [
                    {"address": cfg.address, "nickname": cfg.nickname,
                     "enabled": cfg.enabled}
                    for cfg in self._addresses.values()
                ]
            with open(PERSIST_PATH, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            logger.error("Failed to persist addresses: %s", exc)

    def _load_persisted(self) -> None:
        try:
            if not os.path.exists(PERSIST_PATH):
                return
            with open(PERSIST_PATH) as f:
                saved = json.load(f)
            for entry in saved:
                addr = entry["address"].lower()
                self._addresses[addr] = WatchedAddress(
                    address=addr,
                    nickname=entry.get("nickname", addr[:8]),
                    enabled=entry.get("enabled", True),
                    poll_interval=self._default_interval,
                )
            logger.info("Loaded %d persisted addresses", len(self._addresses))
        except Exception as exc:
            logger.warning("Could not load persisted addresses: %s", exc)

    # ── Emitters ──────────────────────────────────────────────────────────────

    def _emit_poll_debug(self, cfg: WatchedAddress, current_map: dict,
                         opened: list, closed: list) -> None:
        if not self._bus:
            return
        self._bus.publish("mirror_poll_debug", {
            "address":       cfg.address,
            "nickname":      cfg.nickname,
            "ts":            time.time(),
            "initialized":   cfg.is_initialized,
            "fetched":       len(current_map),
            "baseline_size": len(cfg.last_positions),
            "new_count":     len(opened),
            "closed_count":  len(closed),
            "opened": [{"title": p.get("title","?")[:60],
                        "asset": p.get("asset","")[:20],
                        "price": p.get("curPrice")} for p in opened],
            "closed": [{"title": p.get("title","?")[:60],
                        "asset": p.get("asset","")[:20]} for p in closed],
        })

    def _emit_address_status(self, cfg: WatchedAddress) -> None:
        if self._bus:
            self._bus.publish("mirror_address_status", self._addr_to_dict(cfg))

    def _emit_address_list(self) -> None:
        if self._bus:
            self._bus.publish("mirror_addresses",
                              {"addresses": self.get_addresses()})

    def _addr_to_dict(self, cfg: WatchedAddress) -> dict:
        return {
            "address":               cfg.address,
            "nickname":              cfg.nickname,
            "enabled":               cfg.enabled,
            "health":                cfg.health,
            "consecutive_failures":  cfg.consecutive_failures,
            "is_stale":              cfg.is_stale,
            "is_rate_limited":       cfg.is_rate_limited,
            "rate_limited_until":    cfg.rate_limited_until,
            "last_poll_ts":          cfg.last_poll_ts,
            "last_successful_poll_ts": cfg.last_successful_poll_ts,
            "last_poll_count":       cfg.last_poll_count,
            "last_poll_new":         cfg.last_poll_new,
            "last_poll_closed":      cfg.last_poll_closed,
            "stats": {
                "trades_mirrored": cfg.stats.trades_mirrored,
                "wins":            cfg.stats.wins,
                "losses":          cfg.stats.losses,
                "total_pnl_usdc":  round(cfg.stats.total_pnl_usdc, 4),
                "win_rate":        round(cfg.stats.win_rate, 1),
            },
        }
