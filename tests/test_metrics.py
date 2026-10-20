"""Numerical contracts: requests, blocks, time and counter coverage differ."""
import unittest

from balio.controller import MixController
from balio.metrics import COUNTERS, aggregate_samples, complete_sum, counter_delta, rates
from balio.runner import summarize_window


def counters(**overrides):
    values = dict.fromkeys(COUNTERS, 0)
    values.update(overrides)
    return values


def sample(logical, physical, seconds, operations):
    return {
        "measured_seconds": seconds,
        "counters": counters(**{"session logical reads": logical, "physical reads cache": physical}),
        "operations": operations, "hot_operations": operations, "cold_operations": 0,
        "read_operations": operations, "update_operations": 0, "commits": 0, "batches": 1,
    }


class MetricContracts(unittest.TestCase):
    def test_requests_are_not_blocks_and_ratio_excludes_direct_reads(self):
        observed = rates(counters(**{
            "session logical reads": 1000, "physical reads cache": 200,
            "physical reads": 300, "physical reads direct": 100,
            "physical read IO requests": 50, "physical read bytes": 1228800,
            "CPU used by this session": 200,
        }), 2)
        self.assertEqual(observed["physical_read_requests_per_s"], 25)
        self.assertEqual(observed["physical_read_blocks_per_s"], 150)
        self.assertEqual(observed["buffered_physical_read_blocks_per_s"], 100)
        self.assertEqual(observed["physical_to_logical_pct"], 20)
        self.assertEqual(observed["physical_read_bytes_per_s"], 614400)
        self.assertEqual(observed["session_cpu_seconds"], 2)

    def test_aggregate_weights_raw_counters_and_time(self):
        result = aggregate_samples([sample(100, 50, 2, 20), sample(900, 90, 8, 80)])
        self.assertEqual(result["rates"]["physical_to_logical_pct"], 14)
        self.assertEqual(result["operations_per_s"], 10)
        self.assertEqual(result["measured_seconds"], 10)

    def test_counter_reset_or_absence_is_unavailable_not_zero(self):
        before = counters(**{"session logical reads": 100, "physical reads cache": 20})
        after = counters(**{"session logical reads": 90, "physical reads cache": 30})
        del after["physical read IO requests"]
        delta = counter_delta(before, after)
        self.assertIsNone(delta["session logical reads"])
        self.assertIsNone(delta["physical read IO requests"])
        self.assertEqual(delta["physical reads cache"], 10)
        self.assertTrue(all(v is None for v in counter_delta(None, after).values()))
        self.assertIsNone(rates(delta, 5)["physical_to_logical_pct"])

    def test_one_missing_worker_invalidates_aggregate_metric(self):
        self.assertIsNone(complete_sum([{"read": 10}, {"read": None}], "read"))
        self.assertIsNone(complete_sum([{"read": 10}, {}], "read"))
        self.assertEqual(complete_sum([{"read": 10}, {"read": 0}], "read"), 10)
        self.assertIsNone(complete_sum([], "read"))
        missing = sample(100, 20, 1, 10)
        missing["counters"]["physical reads cache"] = None
        result = aggregate_samples([sample(100, 20, 1, 10), missing])
        self.assertIsNone(result["rates"]["physical_to_logical_pct"])

    def test_zero_logical_reads_never_claim_zero_physical_fraction(self):
        self.assertIsNone(rates(counters(), 1)["physical_to_logical_pct"])
        self.assertIsNone(rates(counters(), 0)["physical_read_requests_per_s"])
        self.assertIsNone(aggregate_samples([])["operations_per_s"])

    def test_concurrent_workers_use_shared_wall_interval(self):
        def worker(start, end, ops):
            return {"started": start, "ended": end, "worker": 0, "ops": ops,
                    "hot_ops": ops, "cold_ops": 0, "read_ops": ops,
                    "update_ops": 0, "commits": 0, "counters": counters(),
                    "batch_latency": {"count": 1}, "error": None}
        result = summarize_window([worker(0, 2, 100), worker(.5, 3, 200)], 1, 0)
        self.assertEqual(result["measured_seconds"], 3)
        self.assertEqual(result["operations_per_s"], 100)


class ControllerContracts(unittest.TestCase):
    def test_saturation_reports_unmet_and_can_recover(self):
        controller = MixController(target=80, cold_pct=95, gain=1, max_step=10)
        first = controller.step(20)
        self.assertEqual(first["next_cold_pct"], 100)
        self.assertEqual(first["state"], "saturated_target_unmet")
        self.assertEqual(controller.step(20)["saturated_intervals"], 2)
        recovered = controller.step(80)
        self.assertEqual(recovered["state"], "within_tolerance")
        self.assertEqual(controller.saturated_intervals, 0)

    def test_deadband_preserves_input_and_missing_measurement_fails(self):
        controller = MixController(target=20, cold_pct=40, tolerance=2)
        self.assertEqual(controller.step(21)["next_cold_pct"], 40)
        self.assertEqual(controller.step(0)["next_cold_pct"], 50)
        for unavailable in (None, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                controller.step(unavailable)


if __name__ == "__main__":
    unittest.main()
