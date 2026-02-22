"""
Sportradar trial HTTP polling feed.
Soccer: schedules/live/summaries  (30s poll)
NBA:    games/{date}/schedule   (30s poll)

Emits: match_start, goal, red_card, match_end, game_start, game_end
"""

import datetime
import logging
import time

import requests

from .base import BaseSportFeed
from ..models import LiveEvent

logger = logging.getLogger("arb_bot.datafeed.sportradar")

SOCCER_LIVE = "https://api.sportradar.us/soccer/trial/v4/en/schedules/live/summaries.json"
NBA_LIVE    = "https://api.sportradar.us/nba/trial/v8/en/games/{date}/schedule.json"


class SportradarFeed(BaseSportFeed):
    def __init__(self, api_key: str, bus=None):
        self._key  = api_key
        self._bus  = bus
        self._http = requests.Session()
        self._http.params = {"api_key": api_key}   # type: ignore[assignment]
        self._calls_remaining = 1000
        self._last_call_ts    = 0.0

        # diff state
        self._last_soccer: dict = {}  # match_id → summary dict
        self._last_nba:    dict = {}  # game_id  → summary dict

    def sport_name(self) -> str:
        return "soccer+nba"

    def poll(self, watched_sports: set | None = None) -> list:
        events: list = []
        if watched_sports is None or "soccer" in watched_sports:
            events.extend(self._poll_soccer())
        if watched_sports is not None and "nba" in watched_sports:
            events.extend(self._poll_nba())
        return events

    # ── Soccer ────────────────────────────────────────────────────────────────

    def _poll_soccer(self) -> list:
        if not self._key:
            return []
        try:
            resp = self._http.get(SOCCER_LIVE, timeout=12)
            self._track_rate_limit(resp, "soccer")
            if resp.status_code == 403:
                logger.warning("[df-sportradar] 403 Forbidden — check trial key")
                return []
            if resp.status_code == 429:
                logger.warning("[df-sportradar] rate limited")
                self._emit_api_status("yellow")
                return []
            resp.raise_for_status()
            data     = resp.json()
            summaries = data.get("summaries", [])
            events   = self._diff_soccer(summaries)
            logger.info("[df-sportradar] poll: %d fixtures, %d events",
                        len(summaries), len(events))
            return events
        except Exception as exc:
            logger.warning("[df-sportradar] soccer poll error: %s", exc)
            return []

    def _diff_soccer(self, summaries: list) -> list:
        new_events: list = []
        current: dict    = {}

        for s in summaries:
            sport_event = s.get("sport_event") or {}
            status      = s.get("sport_event_status") or {}
            match_id    = sport_event.get("id", "")
            if not match_id:
                continue
            current[match_id] = s

            competitors = sport_event.get("competitors", [])
            home = next((c.get("name", "Home") for c in competitors
                         if c.get("qualifier") == "home"), "Home")
            away = next((c.get("name", "Away") for c in competitors
                         if c.get("qualifier") == "away"), "Away")
            home_score = status.get("home_score", 0) or 0
            away_score = status.get("away_score", 0) or 0
            minute     = (status.get("clock") or {}).get("played", "0:00").split(":")[0]
            try:
                minute = int(minute)
            except (ValueError, AttributeError):
                minute = 0
            match_status = status.get("status", "")

            prev = self._last_soccer.get(match_id)

            if prev is None:
                if match_status in ("live", "inprogress"):
                    new_events.append(self._make_soccer_event(
                        match_id, home, away, home_score, away_score, minute,
                        "match_start", s
                    ))
            else:
                prev_status = (prev.get("sport_event_status") or {})
                ph = prev_status.get("home_score", 0) or 0
                pa = prev_status.get("away_score", 0) or 0
                if home_score > ph or away_score > pa:
                    new_events.append(self._make_soccer_event(
                        match_id, home, away, home_score, away_score, minute,
                        "goal", s
                    ))

        # Disappeared matches → ended
        for mid in self._last_soccer:
            if mid not in current:
                s    = self._last_soccer[mid]
                st   = s.get("sport_event_status") or {}
                evt  = s.get("sport_event") or {}
                comp = evt.get("competitors", [])
                h    = next((c.get("name", "Home") for c in comp
                             if c.get("qualifier") == "home"), "Home")
                a    = next((c.get("name", "Away") for c in comp
                             if c.get("qualifier") == "away"), "Away")
                new_events.append(self._make_soccer_event(
                    mid, h, a,
                    st.get("home_score", 0) or 0,
                    st.get("away_score", 0) or 0,
                    90, "match_end", s
                ))

        self._last_soccer = current
        return new_events

    def _make_soccer_event(self, match_id, home, away, hs, as_, min_,
                            event_type, raw) -> LiveEvent:
        # Use a stable integer fixture_id derived from the string ID
        try:
            fid = int(match_id.split(":")[-1])
        except (ValueError, AttributeError):
            fid = hash(match_id) & 0xFFFFFF

        return LiveEvent(
            fixture_id=fid,
            home_team=home,
            away_team=away,
            home_score=hs,
            away_score=as_,
            minute=min_,
            event_type=event_type,
            detected_at=time.time(),
            raw=raw,
            source="sportradar",
        )

    # ── NBA ───────────────────────────────────────────────────────────────────

    def _poll_nba(self) -> list:
        if not self._key:
            return []
        today = datetime.date.today().strftime("%Y/%m/%d")
        url   = NBA_LIVE.format(date=today)
        try:
            resp = self._http.get(url, timeout=12)
            self._track_rate_limit(resp, "nba")
            if resp.status_code in (403, 429):
                return []
            resp.raise_for_status()
            data   = resp.json()
            games  = data.get("games", [])
            events = self._diff_nba(games)
            logger.info("[df-sportradar] nba poll: %d games, %d events",
                        len(games), len(events))
            return events
        except Exception as exc:
            logger.warning("[df-sportradar] nba poll error: %s", exc)
            return []

    def _diff_nba(self, games: list) -> list:
        new_events: list = []
        current: dict    = {}

        for g in games:
            gid    = g.get("id", "")
            status = g.get("status", "")
            home   = g.get("home", {}).get("name", "Home")
            away   = g.get("away", {}).get("name", "Away")
            hpts   = g.get("home_points", 0) or 0
            apts   = g.get("away_points", 0) or 0
            if not gid:
                continue
            current[gid] = g

            prev = self._last_nba.get(gid)
            if prev is None:
                if status in ("inprogress", "halftime"):
                    new_events.append(self._make_nba_event(
                        gid, home, away, hpts, apts, "game_start", g
                    ))
            else:
                # score change = scoring event (use as proxy for "goal")
                if hpts != prev.get("home_points", 0) or apts != prev.get("away_points", 0):
                    new_events.append(self._make_nba_event(
                        gid, home, away, hpts, apts, "goal", g
                    ))

        for gid in self._last_nba:
            if gid not in current:
                g = self._last_nba[gid]
                new_events.append(self._make_nba_event(
                    gid,
                    g.get("home", {}).get("name", "Home"),
                    g.get("away", {}).get("name", "Away"),
                    g.get("home_points", 0) or 0,
                    g.get("away_points", 0) or 0,
                    "game_end", g
                ))

        self._last_nba = current
        return new_events

    def _make_nba_event(self, gid, home, away, hpts, apts,
                        event_type, raw) -> LiveEvent:
        try:
            fid = int(gid.split(":")[-1])
        except (ValueError, AttributeError):
            fid = hash(gid) & 0xFFFFFF

        return LiveEvent(
            fixture_id=fid,
            home_team=home,
            away_team=away,
            home_score=hpts,
            away_score=apts,
            minute=0,
            event_type=event_type,
            detected_at=time.time(),
            raw=raw,
            source="sportradar",
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _track_rate_limit(self, resp, source_label: str) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is not None:
            try:
                self._calls_remaining = int(remaining)
            except ValueError:
                pass
        self._last_call_ts = time.time()
        self._emit_api_status(
            "green" if self._calls_remaining > 50 else (
                "yellow" if self._calls_remaining > 10 else "red"
            )
        )

    def _emit_api_status(self, health: str = "green") -> None:
        if not self._bus:
            return
        self._bus.publish("datafeed_api_status", {
            "source":          "sportradar",
            "calls_remaining": self._calls_remaining,
            "last_call_ts":    self._last_call_ts,
            "health":          health,
        })
