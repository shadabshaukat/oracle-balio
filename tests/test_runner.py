"""Functional fake-backend tests; these do not claim Oracle integration coverage."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from balio.config import RunConfig
from balio.metrics import COUNTERS
from balio.runner import run


@dataclass(frozen=True)
class FakeConnectionConfig:
    dsn: str = "private-db.example/testpdb"
    password: str = "test-only-secret"
    call_timeout_ms: int = 60000


class FakeConnection:
    def __init__(self, config):
        self.config = config
        self.module = None
        self.action = ""
        self.closed = False
        self.cancelled = False
        self.batches = 0
        self.stats = dict.fromkeys(COUNTERS, 0)

    def close(self):
        self.closed = True

    def cancel(self):
        self.cancelled = True


class FakeDatabase:
    """Every operation does 4 logical reads; a cold operation reads 2 blocks.

    One request reads both physical blocks. This intentionally makes request IOPS
    different from physical blocks/s and keeps the control observation auditable.
    """
    def __init__(self, missing_worker=None, lose_stats_after_batch=False,
                 failing_worker=None, interrupt=False, hot_rows=100, cold_rows=10000):
        self.connections = []
        self.lock = threading.Lock()
        self.missing_worker = missing_worker
        self.lose_stats_after_batch = lose_stats_after_batch
        self.failing_worker = failing_worker
        self.interrupt = interrupt
        self.batch_calls = 0
        self.hot_rows = hot_rows
        self.cold_rows = cold_rows

    def connect(self, config):
        connection = FakeConnection(config)
        with self.lock:
            self.connections.append(connection)
        return connection

    def dataset_info(self, connection):
        return {"status": "READY", "hot_rows": self.hot_rows, "cold_rows": self.cold_rows}

    def database_details(self, connection):
        return {"database_version": "23.26.0.0.0", "instance": "fake"}

    def snapshot(self, connection):
        missing = connection.action == f"worker-{self.missing_worker}"
        if missing and (not self.lose_stats_after_batch or connection.batches > 0):
            return None, "ORA-00942: synthetic statistics access denied"
        return dict(connection.stats), None

    def run_batch(self, connection, **kwargs):
        time.sleep(.002)
        with self.lock:
            self.batch_calls += 1
        if kwargs["worker_id"] == self.failing_worker:
            if self.interrupt:
                raise KeyboardInterrupt()
            raise RuntimeError(f"synthetic failure at {connection.config.dsn} password={connection.config.password}")
        operations = kwargs["operations"]
        cold = round(operations * kwargs["cold_pct"] / 100)
        updates = round(operations * kwargs["update_pct"] / 100)
        connection.batches += 1
        connection.stats["session logical reads"] += operations * 4
        connection.stats["physical reads cache"] += cold * 2
        connection.stats["physical reads"] += cold * 2
        connection.stats["physical read IO requests"] += cold
        connection.stats["physical read total IO requests"] += cold
        connection.stats["physical read bytes"] += cold * 2 * 8192
        connection.stats["physical read total bytes"] += cold * 2 * 8192
        return {"seed": kwargs["seed"], "checksum": operations, "ops": operations,
                "cold_ops": cold, "hot_ops": operations - cold,
                "read_ops": operations - updates, "update_ops": updates,
                "commits": 0}


class RunnerContracts(unittest.TestCase):
    def execute(self, api, **overrides):
        parameters = dict(workers=2, duration=.045, warmup=0, interval=.015,
                          batch_operations=100, cold_pct=50)
        parameters.update(overrides)
        config = RunConfig(**parameters)
        with tempfile.TemporaryDirectory(prefix="balio-test-") as directory:
            document = run(FakeConnectionConfig(), config, directory, api=api)
            persisted = json.loads((Path(directory) / "results.json").read_text())
            self.assertEqual(document, persisted)
            if document["sample_count"]:
                records = [json.loads(line) for line in (Path(directory) / "samples.jsonl").read_text().splitlines()]
                self.assertEqual(len(records), document["sample_count"])
            self.assertTrue(all(c.closed for c in api.connections))
            self.assertEqual((Path(directory) / "results.json").stat().st_mode & 0o777, 0o600)
            return document

    def test_controlled_run_measures_known_ratio_and_preserves_request_units(self):
        result = self.execute(FakeDatabase(), target_physical_pct=25)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["errors"], [])
        self.assertGreater(result["summary"]["operations"], 0)
        self.assertEqual(result["summary"]["rates"]["physical_to_logical_pct"], 25)
        self.assertEqual(result["target_assessment"]["state"], "aggregate_within_tolerance")
        observed = result["summary"]["rates"]
        self.assertAlmostEqual(observed["physical_read_blocks_per_s"],
                               observed["physical_read_requests_per_s"] * 2)
        self.assertTrue(result["telemetry_available"])

    def test_fixed_mix_runs_without_complete_statistics(self):
        result = self.execute(FakeDatabase(missing_worker=1))
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["summary"]["operations"], 0)
        self.assertFalse(result["telemetry_available"])
        self.assertIsNone(result["summary"]["rates"]["physical_to_logical_pct"])
        self.assertIsNone(result["summary"]["rates"]["physical_read_requests_per_s"])

    def test_warmup_is_excluded_and_run_timeout_reaches_every_connection(self):
        api = FakeDatabase()
        result = self.execute(api, warmup=.025, call_timeout_ms=1234)
        self.assertEqual(result["status"], "completed")
        self.assertGreater(api.batch_calls, result["summary"]["batches"])
        self.assertEqual(result["summary"]["operations"], result["summary"]["batches"] * 100)
        self.assertTrue(all(c.config.call_timeout_ms == 1234 for c in api.connections))
        self.assertGreater(result["wall_seconds_including_setup_and_warmup"],
                           result["summary"]["measured_phase_wall_seconds"])
        self.assertGreaterEqual(result["summary"]["operations_per_s"],
                                result["summary"]["operations_per_wall_second"])

    def test_read_only_workers_can_share_single_hot_row(self):
        result = self.execute(FakeDatabase(hot_rows=1), cold_pct=0)
        self.assertEqual(result["status"], "completed")
        self.assertGreater(result["summary"]["operations"], 0)

    def test_inactive_pool_does_not_need_one_row_per_updater(self):
        result = self.execute(FakeDatabase(hot_rows=1), cold_pct=100, update_pct=10)
        self.assertEqual(result["status"], "completed")

    def test_active_update_pool_needs_one_row_per_worker(self):
        api = FakeDatabase(hot_rows=1)
        result = self.execute(api, cold_pct=0, update_pct=10)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(api.batch_calls, 0)

    def test_feedback_refuses_missing_worker_statistics_before_workload(self):
        api = FakeDatabase(missing_worker=1)
        result = self.execute(api, target_physical_pct=25)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(api.batch_calls, 0)
        self.assertEqual(result["sample_count"], 0)
        self.assertNotEqual(result["target_assessment"]["state"], "aggregate_within_tolerance")

    def test_feedback_stops_when_statistics_disappear_during_run(self):
        result = self.execute(FakeDatabase(missing_worker=1, lose_stats_after_batch=True),
                              target_physical_pct=25)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["errors"])
        self.assertIsNone(result["summary"]["rates"]["physical_to_logical_pct"])
        self.assertNotEqual(result["target_assessment"]["state"], "aggregate_within_tolerance")

    def test_failed_worker_marks_run_incomplete_and_redacts_credentials(self):
        result = self.execute(FakeDatabase(failing_worker=0), target_physical_pct=25)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["errors"])
        self.assertNotIn(result["target_assessment"]["state"],
                         {"aggregate_within_tolerance", "target_unmet"})
        text = json.dumps(result)
        self.assertNotIn(FakeConnectionConfig().password, text)
        self.assertNotIn(FakeConnectionConfig().dsn, text)
        self.assertIn("<redacted>", text)

    def test_interrupt_cancels_connections_and_never_reports_success(self):
        api = FakeDatabase(failing_worker=0, interrupt=True)
        result = self.execute(api, target_physical_pct=25)
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(any(c.cancelled for c in api.connections))
        self.assertNotEqual(result["target_assessment"]["state"], "aggregate_within_tolerance")

    def test_existing_run_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="balio-test-") as directory:
            result_path = Path(directory) / "results.json"
            result_path.write_text('{"original": true}')
            api = FakeDatabase()
            with self.assertRaises(ValueError):
                run(FakeConnectionConfig(), RunConfig(), directory, api=api)
            self.assertEqual(json.loads(result_path.read_text()), {"original": True})
            self.assertEqual(api.connections, [])


if __name__ == "__main__":
    unittest.main()
