"""Bounded controller catch-up; does not change protocol order or batch size."""
from dataclasses import dataclass


@dataclass
class LiveCatchUpPolicy:
    # Callers opt in explicitly; retain the fixed policy for A/B diagnostics.
    enabled: bool = False
    normal_seconds: float = 0.040
    quick_seconds: float = 0.001
    yield_seconds: float = 0.005
    max_quick_passes: int = 4
    work_budget_seconds: float = 0.020
    _passes: int = 0
    _work_seconds: float = 0.0

    def next_wait(self, pending_events: int, work_seconds: float) -> float:
        if not self.enabled or pending_events <= 0:
            self._passes = 0
            self._work_seconds = 0.0
            return self.normal_seconds
        self._passes += 1
        self._work_seconds += max(0.0, work_seconds)
        if (self._passes >= self.max_quick_passes
                or self._work_seconds >= self.work_budget_seconds):
            self._passes = 0
            self._work_seconds = 0.0
            return self.yield_seconds
        return self.quick_seconds
