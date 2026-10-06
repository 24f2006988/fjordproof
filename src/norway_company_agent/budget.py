from __future__ import annotations

import threading
import time
from typing import Any


class BudgetGuard:
    """Thread-safe time and request budget guard for batch runs.

    Guarantees that the agent never overshoots time deadlines (e.g. 45 min)
    or outbound request caps (e.g. 2,000 or batch-scaled budgets), triggering
    graceful degradation so every single company receives a valid terminal envelope.
    """

    def __init__(
        self,
        max_runtime_seconds: float = 2400.0,
        max_requests: int = 18000,
        deadline: float | None = None,
    ) -> None:
        self.start_time = time.monotonic()
        self.max_runtime_seconds = float(max_runtime_seconds)
        self.max_requests = int(max_requests)
        self.deadline = deadline or (self.start_time + self.max_runtime_seconds)
        self._requests = 0
        self._lock = threading.Lock()

    def record_requests(self, count: int = 1) -> int:
        with self._lock:
            self._requests += count
            return self._requests

    @property
    def requests(self) -> int:
        with self._lock:
            return self._requests

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.start_time

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def is_exhausted(
        self,
        safety_margin_seconds: float | None = None,
        safety_margin_requests: int | None = None,
    ) -> bool:
        """Return True when remaining time or request headroom is within safety margin."""
        margin_sec = safety_margin_seconds if safety_margin_seconds is not None else min(180.0, self.max_runtime_seconds * 0.1)
        if self.remaining_seconds <= margin_sec:
            return True
        margin_req = safety_margin_requests if safety_margin_requests is not None else min(50, int(self.max_requests * 0.05))
        with self._lock:
            if self._requests >= (self.max_requests - margin_req):
                return True
        return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            reqs = self._requests
        return {
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "remaining_seconds": round(self.remaining_seconds, 1),
            "requests": reqs,
            "max_requests": self.max_requests,
            "is_exhausted": self.is_exhausted(),
        }
