import asyncio
import random
from dataclasses import dataclass

@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.8
    max_delay: float = 8.0

async def with_retry(fn, policy: RetryPolicy = RetryPolicy(), retryable=(429, 500, 502, 503, 504)):
    last = None
    for attempt in range(policy.attempts):
        try:
            return await fn()
        except Exception as exc:
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status not in retryable:
                raise
            if attempt == policy.attempts - 1:
                raise
            delay = min(policy.max_delay, policy.base_delay * (2 ** attempt))
            await asyncio.sleep(delay + random.random() * 0.25)
    raise last
