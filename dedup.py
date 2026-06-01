import time
from models import Opportunity

DEFAULT_TTL = 120  # seconds


class Deduplicator:
    def __init__(self, ttl: int = DEFAULT_TTL):
        self._ttl = ttl
        self._seen: dict[tuple, float] = {}  # key -> expiry timestamp

    def is_new(self, opp: Opportunity) -> bool:
        """Return True if this opportunity hasn't been seen within the TTL window."""
        key = opp.dedup_key()
        now = time.time()
        self._evict(now)
        if key in self._seen:
            return False
        self._seen[key] = now + self._ttl
        return True

    def _evict(self, now: float):
        expired = [k for k, exp in self._seen.items() if exp <= now]
        for k in expired:
            del self._seen[k]
