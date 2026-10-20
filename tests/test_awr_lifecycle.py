"""Offline checks of AWR boundaries; no Oracle report behavior is simulated as real."""
from dataclasses import dataclass, replace
import json
from pathlib import Path
import tempfile
import threading
import unittest

from balio.config import RunConfig
from balio.runner import run
from test_runner import FakeConnectionConfig, FakeDatabase


@dataclass(frozen=True)
class FakeAWRConfig:
    enabled: bool = True

    def validate(self):
        return self

    def public_dict(self):
        return {"enabled": self.enabled}


class TrackingDatabase(FakeDatabase):
    def __init__(self, events, **kwargs):
        super().__init__(**kwargs)
        self.events = events
        self.active_batches = 0
        self.event_lock = threading.Lock()

    def run_batch(self, connection, **kwargs):
        with self.event_lock:
            self.active_batches += 1
            self.events.append("batch_start")
        try:
            return super().run_batch(connection, **kwargs)
        finally:
            with self.event_lock:
                self.active_batches -= 1
                self.events.append("batch_end")


class FakeAWR:
    def __init__(self, events, database, failure=None, error_message="synthetic AWR failure",
                 final_status="completed"):
        self.events = events
        self.database = database
        self.failure = failure
        self.error_message = error_message
        self.final_status = final_status
        self.closed = False
        self.prepared_workers = None
        self.finished_workload_status = None
        self._result = {"status": "pending", "errors": [], "files": []}

    def _event(self, phase):
        self.events.append("awr_" + phase)
        if self.failure == phase:
            self._result["status"] = "failed"
            self._result["errors"].append(self.error_message)
            raise RuntimeError(self.error_message)

    def prepare(self, worker_details):
        self.prepared_workers = worker_details
        self._event("prepare")
        self._result["status"] = "prepared"

    def begin(self):
        self._event("begin")
        self._result["status"] = "started"

    def finish(self, run_status="completed"):
        if self.database.active_batches:
            raise AssertionError("AWR finish ran while a workload batch was active")
        self.finished_workload_status = run_status
        self._event("finish")
        self._result["status"] = self.final_status
        self._result["run_status"] = run_status
        if self.final_status == "partial":
            self._result["covers_run_end"] = False

    def result(self):
        return dict(self._result)

    def close(self):
        self.closed = True
        self.events.append("awr_close")


class AWRLifecycle(unittest.TestCase):
    def execute(self, *, failure=None, warmup=0, worker_failure=None,
                interrupt=False, enabled=True, separate_connection=False,
                error_message="synthetic AWR failure", final_status="completed"):
        events = []
        api = TrackingDatabase(events, failing_worker=worker_failure, interrupt=interrupt)
        collector = FakeAWR(events, api, failure=failure, error_message=error_message,
                            final_status=final_status)
        connection = FakeConnectionConfig()
        awr_connection = replace(connection, dsn="reporting-db.example/testpdb",
                                 password="separate-report-password")
        factory_calls = []

        def factory(connection_config, awr_config, output_dir):
            factory_calls.append((connection_config, awr_config, Path(output_dir)))
            return collector

        workload = RunConfig(workers=2, duration=.045, warmup=warmup,
                             interval=.015, batch_operations=100)
        with tempfile.TemporaryDirectory(prefix="balio-awr-lifecycle-") as directory:
            result = run(
                connection, workload, directory, api=api,
                awr_config=FakeAWRConfig() if enabled else None,
                awr_connection_config=awr_connection if separate_connection else None,
                awr_factory=factory,
            )
            persisted = json.loads((Path(directory) / "results.json").read_text())
            self.assertEqual(result, persisted)
        self.assertTrue(all(c.closed for c in api.connections))
        return result, collector, events, api, factory_calls, connection, awr_connection

    def test_disabled_collection_has_no_collector_side_effects(self):
        result, collector, events, _, calls, _, _ = self.execute(enabled=False)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls, [])
        self.assertFalse(collector.closed)
        self.assertFalse(any(event.startswith("awr_") for event in events))

    def test_prepare_precedes_warmup_and_snapshots_bracket_measured_work(self):
        result, collector, events, api, calls, connection, _ = self.execute(warmup=.025)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["workload_status"], "completed")
        self.assertEqual(result["awr"]["status"], "completed")
        self.assertEqual(calls[0][0], connection)
        self.assertEqual(len(collector.prepared_workers), 2)
        self.assertLess(events.index("awr_prepare"), events.index("batch_start"))
        first_snapshot = events.index("awr_begin")
        self.assertIn("batch_end", events[:first_snapshot])
        self.assertIn("batch_start", events[first_snapshot:])
        last_batch = max(i for i, event in enumerate(events) if event == "batch_end")
        self.assertGreater(events.index("awr_finish"), last_batch)
        self.assertGreater(events.index("awr_close"), events.index("awr_finish"))
        self.assertTrue(collector.closed)
        self.assertEqual(collector.finished_workload_status, "completed")
        self.assertGreater(api.batch_calls, result["summary"]["batches"])

    def test_separate_reporting_connection_is_passed_only_to_collector(self):
        result, _, _, api, calls, connection, awr_connection = self.execute(separate_connection=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(calls[0][0], awr_connection)
        self.assertTrue(all(c.config.dsn == connection.dsn for c in api.connections))

    def test_preflight_failure_prevents_warmup_and_measured_work(self):
        result, collector, events, api, _, _, _ = self.execute(failure="prepare", warmup=.025)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(api.batch_calls, 0)
        self.assertNotIn("awr_begin", events)
        self.assertNotIn("awr_finish", events)
        self.assertTrue(collector.closed)

    def test_begin_failure_prevents_measured_work(self):
        result, collector, events, api, _, _, _ = self.execute(failure="begin")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(api.batch_calls, 0)
        self.assertNotIn("awr_finish", events)
        self.assertTrue(collector.closed)

    def test_finish_failure_retains_completed_work_but_fails_requested_collection(self):
        result, collector, events, _, _, _, _ = self.execute(failure="finish")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["workload_status"], "completed")
        self.assertEqual(result["awr"]["status"], "failed")
        self.assertGreater(result["summary"]["operations"], 0)
        self.assertGreater(result["sample_count"], 0)
        self.assertEqual(events.count("awr_finish"), 1)
        self.assertTrue(result["errors"])
        self.assertTrue(collector.closed)

    def test_worker_failure_still_requests_end_snapshot_and_marks_report_scope(self):
        result, collector, events, _, _, _, _ = self.execute(worker_failure=0)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["workload_status"], "failed")
        self.assertEqual(collector.finished_workload_status, "failed")
        self.assertEqual(events.count("awr_finish"), 1)
        self.assertTrue(collector.closed)

    def test_partial_report_does_not_satisfy_requested_run_collection(self):
        result, collector, _, _, _, _, _ = self.execute(final_status="partial")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["workload_status"], "completed")
        self.assertEqual(result["awr"]["status"], "partial")
        self.assertFalse(result["awr"]["covers_run_end"])
        self.assertGreater(result["summary"]["operations"], 0)
        self.assertTrue(result["errors"])
        self.assertTrue(collector.closed)

    def test_interrupt_still_finishes_collection_after_cancellation(self):
        result, collector, events, api, _, _, _ = self.execute(worker_failure=0, interrupt=True)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["workload_status"], "interrupted")
        self.assertEqual(collector.finished_workload_status, "interrupted")
        self.assertEqual(events.count("awr_finish"), 1)
        self.assertTrue(any(c.cancelled for c in api.connections))
        self.assertTrue(collector.closed)

    def test_collection_errors_redact_workload_and_reporting_credentials(self):
        workload_connection = FakeConnectionConfig()
        error = (
            f"synthetic failure {workload_connection.password} {workload_connection.dsn} "
            "separate-report-password reporting-db.example/testpdb"
        )
        result, _, _, _, _, connection, awr_connection = self.execute(
            failure="finish", separate_connection=True, error_message=error)
        serialized = json.dumps(result)
        for secret in (connection.password, connection.dsn, awr_connection.password, awr_connection.dsn):
            self.assertNotIn(secret, serialized)
        self.assertIn("<redacted>", serialized)


if __name__ == "__main__":
    unittest.main()
