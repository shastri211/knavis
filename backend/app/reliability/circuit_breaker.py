import time
from dataclasses import dataclass

@dataclass
class Circuit:
    failures: int = 0
    opened_at: float | None = None

class CircuitOpen(RuntimeError):
    pass

class CircuitBreaker:
    def __init__(self, threshold=5, recovery_seconds=30):
        self.threshold = threshold
        self.recovery_seconds = recovery_seconds
        self.state = {}

    def before_call(self, name):
        c = self.state.setdefault(name, Circuit())
        if c.opened_at is not None:
            if time.time() - c.opened_at < self.recovery_seconds:
                raise CircuitOpen(f"{name} temporarily unavailable")
            c.opened_at = None
            c.failures = 0

    def success(self, name):
        self.state[name] = Circuit()

    def failure(self, name):
        c = self.state.setdefault(name, Circuit())
        c.failures += 1
        if c.failures >= self.threshold:
            c.opened_at = time.time()
