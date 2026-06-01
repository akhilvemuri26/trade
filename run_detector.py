import logging
import os
import time
from collections import defaultdict
from typing import Optional

from models import RunEvent, ScoreEvent, Sport

logger = logging.getLogger(__name__)

# (min_points, max_opponent_points, window_seconds) per sport
RUN_THRESHOLDS: dict[Sport, tuple[int, int, float]] = {
    Sport.NBA: (
        int(os.getenv("RUN_NBA_PTS", "8")),
        int(os.getenv("RUN_NBA_OPP", "2")),
        float(os.getenv("RUN_NBA_WINDOW", "180")),
    ),
    Sport.MLB: (
        int(os.getenv("RUN_MLB_PTS", "3")),
        int(os.getenv("RUN_MLB_OPP", "0")),
        float(os.getenv("RUN_MLB_WINDOW", "600")),
    ),
    Sport.NHL: (
        int(os.getenv("RUN_NHL_PTS", "2")),
        int(os.getenv("RUN_NHL_OPP", "0")),
        float(os.getenv("RUN_NHL_WINDOW", "300")),
    ),
    Sport.UFC: (
        int(os.getenv("RUN_UFC_PTS", "1")),
        int(os.getenv("RUN_UFC_OPP", "99")),
        float(os.getenv("RUN_UFC_WINDOW", "600")),
    ),
    Sport.TENNIS_ATP: (
        int(os.getenv("RUN_ATP_PTS", "3")),
        int(os.getenv("RUN_ATP_OPP", "0")),
        float(os.getenv("RUN_ATP_WINDOW", "600")),
    ),
    Sport.TENNIS_WTA: (
        int(os.getenv("RUN_WTA_PTS", "3")),
        int(os.getenv("RUN_WTA_OPP", "0")),
        float(os.getenv("RUN_WTA_WINDOW", "600")),
    ),
}

# Cooldown: don't fire another run event for the same team within this window
RUN_COOLDOWN = float(os.getenv("RUN_COOLDOWN", "120"))


class RunDetector:
    def __init__(self):
        # team -> list of (timestamp, points_scored)
        self._history: dict[str, list[tuple[float, int]]] = defaultdict(list)
        # opponent tracking: team -> opponent_name
        self._opponents: dict[str, str] = {}
        # team -> last run event timestamp (for cooldown)
        self._last_run: dict[str, float] = {}

    def _key(self, sport: Sport, team: str) -> str:
        return f"{sport.value}:{team}"

    def ingest(self, event: ScoreEvent) -> Optional[RunEvent]:
        """
        Process a score event and return a RunEvent if a run is detected.
        """
        key = self._key(event.sport, event.team)
        opp_key = self._key(event.sport, event.opponent)

        self._history[key].append((event.timestamp, event.delta))
        self._opponents[key] = event.opponent

        threshold = RUN_THRESHOLDS.get(event.sport)
        if not threshold:
            return None

        min_pts, max_opp, window = threshold
        now = event.timestamp

        # Prune old events outside the window
        self._history[key] = [
            (ts, pts) for ts, pts in self._history[key]
            if now - ts <= window
        ]
        self._history[opp_key] = [
            (ts, pts) for ts, pts in self._history[opp_key]
            if now - ts <= window
        ]

        team_scored = sum(pts for _, pts in self._history[key])
        opp_scored = sum(pts for _, pts in self._history[opp_key])

        if team_scored >= min_pts and opp_scored <= max_opp:
            # Check cooldown
            last = self._last_run.get(key, 0)
            if now - last < RUN_COOLDOWN:
                return None

            self._last_run[key] = now
            earliest = min(ts for ts, _ in self._history[key])

            run = RunEvent(
                sport=event.sport,
                team=event.team,
                points_scored=team_scored,
                opponent_scored=opp_scored,
                window_seconds=now - earliest,
                opponent=event.opponent,
            )
            logger.info(
                "RUN DETECTED: %s %s scored %d (opp %d) in %.0fs",
                event.sport.value, event.team, team_scored, opp_scored, run.window_seconds,
            )
            return run

        return None
