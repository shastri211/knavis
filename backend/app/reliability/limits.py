import time
from collections import defaultdict, deque

class RateLimiter:
    def __init__(self, limit=30, window_seconds=60):
        self.limit = limit
        self.window = window_seconds
        self.events = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.time()
        q = self.events[key]
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True
