import asyncio
import logging
from typing import AsyncIterator

import httpx

from models import ScoreEvent, Sport

logger = logging.getLogger(__name__)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

# (sport enum, ESPN path, poll interval seconds)
FEEDS: list[tuple[Sport, str, int]] = [
    (Sport.NBA, "basketball/nba", 30),
    (Sport.MLB, "baseball/mlb", 30),
    (Sport.NHL, "hockey/nhl", 10),
    (Sport.UFC, "mma/ufc", 10),
    (Sport.TENNIS_ATP, "tennis/atp", 30),
    (Sport.TENNIS_WTA, "tennis/wta", 30),
]


def _parse_scores(sport: Sport, data: dict) -> dict[str, int]:
    """Return {team_name: score} for all in-progress games."""
    scores: dict[str, int] = {}
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            state = comp.get("status", {}).get("type", {}).get("state", "")
            if state != "in":
                continue
            for competitor in comp.get("competitors", []):
                name = (
                    competitor.get("team", {}).get("displayName")
                    or competitor.get("team", {}).get("name")
                    or ""
                )
                raw_score = competitor.get("score", "0")
                try:
                    score = int(float(raw_score))
                except (ValueError, TypeError):
                    score = 0
                if name:
                    scores[name] = score
    return scores


class ScoreFeed:
    def __init__(self):
        # {sport: {team: last_known_score}}
        self._last: dict[Sport, dict[str, int]] = {s: {} for s, _, _ in FEEDS}
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=10)
        return self

    async def __aexit__(self, *_):
        if self._client:
            await self._client.aclose()

    async def _fetch(self, sport: Sport, path: str) -> list[ScoreEvent]:
        try:
            resp = await self._client.get(f"{ESPN_BASE}/{path}/scoreboard")
            resp.raise_for_status()
            current = _parse_scores(sport, resp.json())
        except Exception as e:
            logger.warning("ESPN fetch failed for %s: %s", path, e)
            return []

        events: list[ScoreEvent] = []
        last = self._last[sport]

        for team, score in current.items():
            prev = last.get(team)
            if prev is not None and score > prev:
                opponent = next(
                    (t for t in current if t != team), ""
                )
                events.append(
                    ScoreEvent(
                        sport=sport,
                        team=team,
                        old_score=prev,
                        new_score=score,
                        opponent=opponent,
                    )
                )

        self._last[sport] = current
        return events

    async def stream(self) -> AsyncIterator[ScoreEvent]:
        """Yield ScoreEvents as scores change across all sports."""
        async def poll_feed(sport: Sport, path: str, interval: int, queue: asyncio.Queue):
            while True:
                for event in await self._fetch(sport, path):
                    await queue.put(event)
                await asyncio.sleep(interval)

        queue: asyncio.Queue[ScoreEvent] = asyncio.Queue()
        tasks = [
            asyncio.create_task(poll_feed(sport, path, interval, queue))
            for sport, path, interval in FEEDS
        ]

        try:
            while True:
                yield await queue.get()
        finally:
            for t in tasks:
                t.cancel()
