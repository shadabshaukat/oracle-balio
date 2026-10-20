"""Keep workload operations, blocks, requests, and bytes distinct."""
import math

COUNTERS = (
    "session logical reads", "physical reads cache", "physical reads",
    "physical reads direct", "physical read IO requests", "physical read bytes",
    "physical read total IO requests", "physical read total bytes", "redo size",
    "db block changes", "user commits", "CPU used by this session",
)


def counter_delta(before, after):
    if before is None or after is None:
        return {name: None for name in COUNTERS}
    return {
        name: after[name] - before[name]
        if name in before and name in after and after[name] >= before[name] else None
        for name in COUNTERS
    }


def complete_sum(items, key):
    values = [item.get(key) for item in items]
    return sum(values) if values and all(v is not None for v in values) else None


def rates(counters, seconds):
    def rate(name):
        n = counters.get(name)
        return n / seconds if n is not None and seconds > 0 else None
    logical = counters.get("session logical reads")
    physical_cache = counters.get("physical reads cache")
    return {
        "logical_block_accesses_per_s": rate("session logical reads"),
        "physical_read_requests_per_s": rate("physical read IO requests"),
        "physical_read_total_requests_per_s": rate("physical read total IO requests"),
        "physical_read_blocks_per_s": rate("physical reads"),
        "buffered_physical_read_blocks_per_s": rate("physical reads cache"),
        "physical_read_bytes_per_s": rate("physical read bytes"),
        "redo_bytes_per_s": rate("redo size"),
        "session_cpu_seconds": counters["CPU used by this session"] / 100
        if counters.get("CPU used by this session") is not None else None,
        "physical_to_logical_pct": 100 * physical_cache / logical
        if physical_cache is not None and logical is not None and logical > 0 else None,
    }


def aggregate_samples(samples):
    if not samples:
        return {"measured_seconds": 0.0, "operations": 0, "operations_per_s": None,
                "counters": {k: None for k in COUNTERS}, "rates": rates({}, 0)}
    seconds = sum(s["measured_seconds"] for s in samples)
    counters = {name: complete_sum([s["counters"] for s in samples], name) for name in COUNTERS}
    counts = {name: sum(s[name] for s in samples) for name in
              ("operations", "hot_operations", "cold_operations", "read_operations", "update_operations", "commits", "batches")}
    return {"measured_seconds": seconds, **counts,
            "operations_per_s": counts["operations"] / seconds if seconds else None,
            "achieved_cold_operation_pct": 100 * counts["cold_operations"] / counts["operations"] if counts["operations"] else None,
            "counters": counters, "rates": rates(counters, seconds)}


class BatchHistogram:
    """Fixed log2 microsecond bins; quantiles are upper bounds, not per-row latency."""
    def __init__(self):
        self.bins = [0] * 40
        self.count = 0
        self.total_ms = 0.0
        self.max_ms = 0.0

    def add(self, seconds):
        micros = max(1.0, seconds * 1e6)
        index = min(39, max(0, math.ceil(math.log2(micros))))
        self.bins[index] += 1
        self.count += 1
        self.total_ms += seconds * 1000
        self.max_ms = max(self.max_ms, seconds * 1000)

    def export(self):
        def quantile(q):
            if not self.count:
                return None
            threshold = math.ceil(self.count * q)
            cumulative = 0
            for index, value in enumerate(self.bins):
                cumulative += value
                if cumulative >= threshold:
                    return 2**index / 1000
        return {"count": self.count,
                "mean_ms": self.total_ms / self.count if self.count else None,
                "max_ms": self.max_ms if self.count else None,
                "p50_upper_ms": quantile(.50), "p95_upper_ms": quantile(.95),
                "p99_upper_ms": quantile(.99), "log2_microsecond_bins": self.bins,
                "scope": "client-observed PL/SQL batch round trip; includes network and commits; not per-operation latency"}
