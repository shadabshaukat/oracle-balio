"""Bounded feedback on observed buffered physical-block / logical-block ratio."""
from dataclasses import dataclass
import math


@dataclass
class MixController:
    target: float
    cold_pct: float
    tolerance: float = 2.0
    gain: float = 0.5
    max_step: float = 10.0
    saturated_intervals: int = 0

    def step(self, observed):
        if observed is None or not math.isfinite(observed):
            raise ValueError("Feedback requires complete per-worker Oracle counters")
        error = self.target - observed
        if abs(error) <= self.tolerance:
            self.saturated_intervals = 0
            return {"next_cold_pct": self.cold_pct, "state": "within_tolerance", "error_pp": error}
        delta = max(-self.max_step, min(self.max_step, self.gain * error))
        new_mix = max(0.0, min(100.0, self.cold_pct + delta))
        saturated = (new_mix == 100.0 and error > 0) or (new_mix == 0.0 and error < 0)
        self.saturated_intervals = self.saturated_intervals + 1 if saturated else 0
        self.cold_pct = new_mix
        return {
            "next_cold_pct": new_mix,
            "state": "saturated_target_unmet" if saturated else "adjusting",
            "error_pp": error,
            "saturated_intervals": self.saturated_intervals,
        }
