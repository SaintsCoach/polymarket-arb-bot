"""
Football (soccer) live data feed via API-Football v3.
Polls /fixtures?live=all and diffs against last snapshot to emit LiveEvent objects.
"""

import logging
import time

import requests

from .base import BaseSportFeed
from ..models import LiveEvent

logger = logging.getLogger("arb_bot.datafeed.football")

FIXTURES_URL = "https://v3.football.api-sports.io/fixtures"


class RateLimitError(Exception):
    pass


class FootballFeed(BaseSportFeed):
    def __init__(self, api_key: str, bus=None):
        self._key = api_key
        self._bus = bus
        self._last_fixtures: dict = {}   # fixture_id → full fixture dict
        self._http = requests.Session()
        self._http.headers["x-apisports-key"] = api_key
        self._calls_remaining = 100
        self._last_call_ts = 0.0

    def sport_name(self) -> str:
        return "soccer"

    def poll(self) -> list:
        resp = self._http.get(
            FIXTURES_URL,
            params={"live": "all"},
            timeout=10,
        )
        self._calls_remaining = int(
            resp.headers.get("x-ratelimit-requests-remaining", 0)
        )
        self._last_call_ts = time.time()
        self._emit_api_status()

        if resp.status_code == 429:
            raise RateLimitError("API-Football rate limit exceeded")
        resp.raise_for_status()

        fixtures = resp.json().get("response", [])
        events = self._diff(fixtures)
        logger.info(
            "DataFeedBot poll: %d live fixtures, %d new events",
            len(fixtures),
            len(events),
        )
        return events

    def _diff(self, fixtures: list) -> list:
        new_events = []
        current: dict = {}

        for f in fixtures:
            fid = f["fixture"]["id"]
            current[fid] = f
            prev = self._last_fixtures.get(fid)

            if prev is None:
                # New fixture appearing in live feed
                new_events.append(self._make_event(f, "match_start"))
            else:
                ph = prev["goals"]["home"] or 0
                pa = prev["goals"]["away"] or 0
                ch = f["goals"]["home"] or 0
                ca = f["goals"]["away"] or 0

                if ch > ph or ca > pa:
                    new_events.append(self._make_event(f, "goal"))
                else:
                    # Check latest event entry for red card
                    prev_n = len(prev.get("events", []))
                    curr_evts = f.get("events", [])
                    if len(curr_evts) > prev_n:
                        latest = curr_evts[-1]
                        if (
                            latest.get("type") == "Card"
                            and latest.get("detail") == "Red Card"
                        ):
                            new_events.append(self._make_event(f, "red_card"))

        # Fixtures that disappeared from the live feed → match ended
        for fid in self._last_fixtures:
            if fid not in current:
                new_events.append(
                    self._make_event(self._last_fixtures[fid], "match_end")
                )

        self._last_fixtures = current
        return new_events

    def _make_event(self, f: dict, event_type: str) -> LiveEvent:
        teams = f.get("teams", {})
        goals = f.get("goals", {})
        status = f.get("fixture", {}).get("status", {})
        minute = status.get("elapsed") or 0

        return LiveEvent(
            fixture_id=f["fixture"]["id"],
            home_team=teams.get("home", {}).get("name", "Home"),
            away_team=teams.get("away", {}).get("name", "Away"),
            home_score=goals.get("home") or 0,
            away_score=goals.get("away") or 0,
            minute=minute,
            event_type=event_type,
            detected_at=time.time(),
            raw=f,
        )

    def _emit_api_status(self) -> None:
        if not self._bus:
            return
        self._bus.publish(
            "datafeed_api_status",
            {
                "calls_remaining": self._calls_remaining,
                "last_call_ts": self._last_call_ts,
                "health": "green" if self._calls_remaining > 20 else (
                    "yellow" if self._calls_remaining > 5 else "red"
                ),
            },
        )
