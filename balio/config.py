"""Validated, credential-free workload parameters."""
from dataclasses import asdict, dataclass
import math


@dataclass(frozen=True)
class RunConfig:
    workers: int = 4
    duration: float = 60.0
    warmup: float = 15.0
    interval: float = 5.0
    batch_operations: int = 1000
    cold_pct: float = 50.0
    update_pct: float = 0.0
    commit_every: int = 100
    random_seed: int = 42
    target_physical_pct: float | None = None
    tolerance: float = 2.0
    controller_gain: float = 0.5
    controller_max_step: float = 10.0
    call_timeout_ms: int = 60000
    platform: str = "other"
    deployment_label: str = ""
    region: str = ""

    def validate(self):
        for name in ("duration", "warmup", "interval", "cold_pct", "update_pct",
                     "tolerance", "controller_gain", "controller_max_step"):
            if not math.isfinite(getattr(self, name)):
                raise ValueError(f"{name} must be finite")
        for name in ("workers", "batch_operations", "commit_every", "call_timeout_ms"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.duration <= 0 or self.interval <= 0 or self.warmup < 0:
            raise ValueError("duration and interval must be positive; warmup must be nonnegative")
        if not 0 <= self.cold_pct <= 100 or not 0 <= self.update_pct <= 100:
            raise ValueError("cold_pct and update_pct must be in [0, 100]")
        if self.target_physical_pct is not None and (
            not math.isfinite(self.target_physical_pct)
            or not 0 <= self.target_physical_pct <= 100
        ):
            raise ValueError("target_physical_pct must be in [0, 100]")
        if self.tolerance < 0 or self.controller_gain <= 0 or self.controller_max_step <= 0:
            raise ValueError("tolerance must be nonnegative; controller gain and step positive")
        if not 1 <= self.random_seed < 2147483647:
            raise ValueError("random_seed must be in [1, 2147483646]")
        if self.workers > 4096 or self.batch_operations > 1000000:
            raise ValueError("maximum workers=4096 and batch_operations=1000000")
        return self

    def public_dict(self):
        return asdict(self)


def sizing(hot_rows, cold_rows, payload_bytes):
    if hot_rows < 1 or cold_rows < 1:
        raise ValueError("hot_rows and cold_rows must be positive")
    if hot_rows > 2147483646 or cold_rows > 2147483646:
        raise ValueError("row count per table must be <=2147483646")
    if not 64 <= payload_bytes <= 2000:
        raise ValueError("payload_bytes must be in [64, 2000]")
    return {
        "hot_rows": hot_rows, "cold_rows": cold_rows, "payload_bytes": payload_bytes,
        "hot_payload_gib": hot_rows * payload_bytes / 2**30,
        "cold_payload_gib": cold_rows * payload_bytes / 2**30,
        "total_payload_gib": (hot_rows + cold_rows) * payload_bytes / 2**30,
        "note": "Payload only: excludes row/block overhead, indexes, undo, redo, and free space. Measure allocated segments after seeding. Cold data must exceed the effective buffer-cache budget to induce sustained physical reads.",
    }
