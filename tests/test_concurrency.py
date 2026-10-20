"""Controlled thread tests verify load timing, not real Oracle throughput."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from balio.config import RunConfig
from balio.runner import run
from test_runner import FakeConnectionConfig, FakeDatabase


class CoordinatedDatabase(FakeDatabase):
    def __init__(self, *, slow_snapshot=False, raise_snapshot=False, require_overlap=False):
        super().__init__()
        self.slow_snapshot = slow_snapshot
        self.raise_snapshot = raise_snapshot
        self.require_overlap = require_overlap
        self.initial_snapshots = set()
        self.seen_snapshots = set()
        self.early_batches = []
        self.active = 0
        self.max_active = 0
        self.first_batches = threading.Barrier(2, timeout=1)
        self.process_cpu = 0.0

    def connect(self, config):
        connection = super().connect(config)
        with self.lock:
            self.process_cpu += 10  # Expensive setup must not be measured as load CPU.
        return connection

    def snapshot(self, connection):
        with self.lock:
            first = connection.action not in self.seen_snapshots
            self.seen_snapshots.add(connection.action)
        if first:
            if self.raise_snapshot and connection.action == "worker-0":
                raise RuntimeError("synthetic starting-snapshot failure")
            if self.slow_snapshot and connection.action == "worker-1":
                time.sleep(.025)
            with self.lock:
                self.initial_snapshots.add(connection.action)
        return super().snapshot(connection)

    def run_batch(self, connection, **kwargs):
        with self.lock:
            if self.initial_snapshots != {"worker-0", "worker-1"}:
                self.early_batches.append(connection.action)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.require_overlap and connection.batches == 0:
                # A serial driver cannot pass this rendezvous.
                self.first_batches.wait()
            result = super().run_batch(connection, **kwargs)
            with self.lock:
                self.process_cpu += .002
            return result
        finally:
            with self.lock:
                self.active -= 1

    def cpu_clock(self):
        with self.lock:
            return self.process_cpu


class ConcurrencyContracts(unittest.TestCase):
    def execute(self, api, *, warmup=0):
        config = RunConfig(workers=2, duration=.08, warmup=warmup,
                           interval=.02, batch_operations=100, call_timeout_ms=100)
        with tempfile.TemporaryDirectory(prefix="balio-concurrency-") as directory:
            result = run(FakeConnectionConfig(), config, directory, api=api)
            samples_path = Path(directory) / "samples.jsonl"
            samples = [json.loads(line) for line in samples_path.read_text().splitlines()]
        self.assertTrue(all(connection.closed for connection in api.connections))
        return result, samples

    def test_slow_initial_snapshot_does_not_allow_one_worker_to_start_early(self):
        api = CoordinatedDatabase(slow_snapshot=True)
        result, samples = self.execute(api)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(api.early_batches, [])
        self.assertGreater(result["summary"]["operations"], 0)
        self.assertTrue(all(sample["worker_start_skew_ms"] >= 0 for sample in samples))
        self.assertEqual(result["client"]["max_worker_start_skew_ms"],
                         max(sample["worker_start_skew_ms"] for sample in samples))

    def test_two_workers_have_independent_connections_and_overlapping_batches(self):
        api = CoordinatedDatabase(require_overlap=True)
        result, _ = self.execute(api)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(api.max_active, 2)
        workers = [connection for connection in api.connections if connection.action.startswith("worker-")]
        self.assertEqual(len(workers), 2)
        self.assertEqual({connection.action for connection in workers}, {"worker-0", "worker-1"})
        self.assertTrue(all(connection.batches > 0 for connection in workers))
        self.assertEqual(result["client"]["requested_workers"], 2)
        self.assertEqual(result["client"]["connected_workers"], 2)

    def test_snapshot_exception_releases_waiting_worker_and_prevents_work(self):
        api = CoordinatedDatabase(raise_snapshot=True)
        started = time.monotonic()
        result, _ = self.execute(api)
        elapsed = time.monotonic() - started
        self.assertEqual(result["status"], "failed")
        self.assertEqual(api.batch_calls, 0)
        self.assertTrue(any("starting-snapshot failure" in error for error in result["errors"]))
        # The runner's gate timeout exceeds five seconds; abort must release it promptly.
        self.assertLess(elapsed, 2)

    def test_process_cpu_measurement_excludes_connection_setup_and_warmup(self):
        api = CoordinatedDatabase()
        with patch("balio.runner.time.process_time", side_effect=api.cpu_clock):
            result, _ = self.execute(api, warmup=.025)
        self.assertEqual(result["status"], "completed")
        self.assertGreater(api.batch_calls, result["summary"]["batches"])
        expected_measured_cpu = .002 * result["summary"]["batches"]
        self.assertAlmostEqual(result["client"]["measured_phase_cpu_seconds"], expected_measured_cpu)
        self.assertGreater(result["client"]["cpu_percent_of_one_core"], 0)
        self.assertIn("not Oracle server CPU", result["client"]["note"])


if __name__ == "__main__":
    unittest.main()
