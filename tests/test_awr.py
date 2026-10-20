"""AWR protocol and local-export tests; no live Oracle compatibility claim."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import tempfile
import unittest

from balio.awr import AwrCapture, AwrConfig, AwrError
from balio.db import ConnectionConfig


START = datetime(2026, 10, 20, 10)
WORKER = {
    "database_name": "BENCH", "db_unique_name": "BENCH_UNIQUE",
    "container_name": "BENCHPDB", "instance_name": "bench1",
}
CONNECTION = ConnectionConfig("private-host.example/bench", user="reporter",
                              password="secret-password", wallet_password="wallet-secret")
PROCEDURES = [
    ("CREATE_SNAPSHOT",), ("AWR_REPORT_HTML",), ("AWR_REPORT_TEXT",),
    ("AWR_GLOBAL_REPORT_HTML",), ("AWR_GLOBAL_REPORT_TEXT",),
]


def snapshot(snap_id, instance=1, startup=START, error_count=0):
    return (snap_id, instance, startup, START + timedelta(minutes=snap_id - 1),
            START + timedelta(minutes=snap_id), error_count)


class FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, query, binds=None):
        conn = self.connection
        query = " ".join(query.upper().split())
        conn.queries.append((query, binds))
        if "SYS_CONTEXT('USERENV', 'DB_NAME')" in query:
            self.rows = [conn.identity]
        elif "LOCAL_AWR_DBID()" in query:
            if conn.missing_function:
                raise RuntimeError("PLS-00302 component LOCAL_AWR_DBID must be declared")
            self.rows = [(conn.dbid,)]
        elif "LOCAL_AWRDBID FROM DBA_HIST_WR_SETTINGS" in query:
            self.rows = [(conn.dbid,)]
        elif "SELECT DBID FROM V$DATABASE" in query:
            self.rows = [(conn.dbid,)]
        elif "FROM GV$INSTANCE" in query:
            self.rows = conn.instances
        elif "AND 1 = 0" in query:
            self.rows = []
        elif "FROM ALL_PROCEDURES" in query:
            self.rows = conn.procedures
        elif "SELECT MAX(SNAP_ID)" in query:
            self.rows = [(conn.ids.pop(0),)]
        elif "AND SNAP_ID = :SNAP_ID" in query:
            self.rows = conn.snapshots.get(binds["snap_id"], [])
        elif "FROM TABLE(" in query:
            self.rows = [(line,) for line in conn.report]
            conn.report_queries.append((query, binds))
        else:
            raise AssertionError(f"Unexpected fake SQL: {query}")

    def callfunc(self, name, return_type, params):
        self.connection.calls.append((name, return_type, params))
        if self.connection.snapshot_failure:
            raise RuntimeError(self.connection.snapshot_failure)
        return self.connection.ids.pop(0)

    def fetchall(self):
        return self.rows

    def __iter__(self):
        for i, row in enumerate(self.rows):
            if self.connection.report_failure and i == 1:
                raise RuntimeError(self.connection.report_failure)
            yield row


class FakeConnection:
    def __init__(self):
        self.version = "19.22.0.0.0"
        self.identity = ("BENCH", "BENCH_UNIQUE", "BENCHPDB", "3")
        self.dbid = 12345
        self.instances = [(1, "bench1"), (2, "bench2")]
        self.procedures = list(PROCEDURES)
        self.snapshots = {10: [snapshot(10, 1), snapshot(10, 2)],
                          11: [snapshot(11, 1), snapshot(11, 2)]}
        self.ids = [10, 11]
        self.queries = []
        self.calls = []
        self.report_queries = []
        self.report = ["<html>", "<body>AWR report</body>", "</html>"]
        self.report_failure = None
        self.snapshot_failure = None
        self.missing_function = False
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class FakeApi:
    def __init__(self, connection):
        self.connection = connection

    def connect(self, config):
        return self.connection


class AwrTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = FakeConnection()
        self.capture = AwrCapture(CONNECTION, AwrConfig(), self.root, FakeApi(self.conn))

    def tearDown(self):
        self.capture.close()
        self.temp.cleanup()

    def prepare(self, workers=None):
        return self.capture.prepare(workers or [WORKER])

    def test_preflight_is_read_only_and_private(self):
        info = self.prepare()
        self.assertEqual(info["status"], "prepared")
        self.assertEqual(self.conn.calls, [])
        self.assertEqual(info["snapshot_view"], "AWR_PDB_SNAPSHOT")
        self.assertEqual(os.stat(self.root / "awr").st_mode & 0o777, 0o700)
        self.assertNotIn(CONNECTION.password, str(info))
        self.assertNotIn(CONNECTION.dsn, str(info))

    def test_exports_both_formats_for_each_worker_instance(self):
        self.prepare([WORKER, {**WORKER, "instance_name": "bench2"}])
        self.capture.begin()
        result = self.capture.finish("completed")
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["covers_run_end"])
        self.assertEqual(len(result["reports"]), 4)
        self.assertEqual(len(self.conn.calls), 2)
        self.assertEqual(self.conn.calls[0][0], "DBMS_WORKLOAD_REPOSITORY.CREATE_SNAPSHOT")
        self.assertEqual(self.conn.calls[0][2], ["TYPICAL"])
        self.assertEqual({p["instances"][0] for p in result["reports"]}, {1, 2})
        for report in result["reports"]:
            path = Path(report["path"])
            self.assertIn("<body>AWR report</body>", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list((self.root / "awr").glob("*.partial")))

    def test_explicit_global_passes_only_worker_instances(self):
        self.capture = AwrCapture(CONNECTION, AwrConfig(scope="global"), self.root, FakeApi(self.conn))
        self.prepare([WORKER, {**WORKER, "instance_name": "bench2"}])
        self.capture.begin()
        result = self.capture.finish()
        self.assertEqual(len(result["reports"]), 2)
        for query, binds in self.conn.report_queries:
            self.assertIn("AWR_GLOBAL_REPORT_", query)
            self.assertEqual(binds["instances"], "1,2")
            self.assertEqual(binds["dbid"], 12345)

    def test_root_reporting_permitted_for_workload_pdb(self):
        self.conn.identity = ("BENCH", "BENCH_UNIQUE", "CDB$ROOT", "1")
        info = self.prepare()
        self.assertEqual(info["snapshot_view"], "DBA_HIST_SNAPSHOT")
        self.assertTrue(any("other containers" in w for w in info["warnings"]))

    def test_unrelated_database_rejected_before_any_snapshot(self):
        self.conn.identity = ("BENCH", "DIFFERENT_PRIMARY", "BENCHPDB", "3")
        with self.assertRaisesRegex(AwrError, "db_unique_name"):
            self.prepare()
        self.assertFalse(self.conn.calls)

    def test_missing_identity_rejected(self):
        with self.assertRaisesRegex(AwrError, "missing db_unique_name"):
            self.prepare([{k: v for k, v in WORKER.items() if k != "db_unique_name"}])

    def test_other_pdb_rejected(self):
        self.conn.identity = ("BENCH", "BENCH_UNIQUE", "OTHERPDB", "4")
        with self.assertRaisesRegex(AwrError, "does not match"):
            self.prepare()

    def test_unmapped_worker_instance_rejected(self):
        with self.assertRaisesRegex(AwrError, "Not all workload instances"):
            self.prepare([{**WORKER, "instance_name": "unrelated"}])

    def test_12_1_pdb_requires_root_connection(self):
        self.conn.version = "12.1.0.2.0"
        with self.assertRaisesRegex(AwrError, "CDB root"):
            self.prepare()

    def test_12_1_root_uses_database_dbid_when_local_function_missing(self):
        self.conn.version = "12.1.0.2.0"
        self.conn.identity = ("BENCH", "BENCH_UNIQUE", "CDB$ROOT", "1")
        self.conn.missing_function = True
        self.assertEqual(self.prepare()["dbid_source"], "V$DATABASE.DBID")

    def test_12_2_pdb_fallback_uses_local_awr_dbid_not_cdb_dbid(self):
        self.conn.version = "12.2.0.1.0"
        self.conn.missing_function = True
        self.assertEqual(self.prepare()["dbid_source"], "DBA_HIST_WR_SETTINGS.LOCAL_AWRDBID")
        self.assertFalse(any("SELECT DBID FROM V$DATABASE" in q for q, _ in self.conn.queries))

    def test_missing_api_fails_preflight(self):
        self.conn.procedures = [p for p in PROCEDURES if p[0] != "AWR_REPORT_TEXT"]
        with self.assertRaisesRegex(AwrError, "AWR_REPORT_TEXT"):
            self.prepare()

    def test_missing_instance_snapshot_fails(self):
        self.prepare([WORKER, {**WORKER, "instance_name": "bench2"}])
        self.conn.snapshots[10] = [snapshot(10, 1)]
        with self.assertRaisesRegex(AwrError, "missing one or more"):
            self.capture.begin()

    def test_snapshot_errors_are_not_silently_accepted(self):
        self.prepare()
        self.conn.snapshots[10] = [snapshot(10, 1, error_count=1)]
        with self.assertRaisesRegex(AwrError, "collection errors"):
            self.capture.begin()

    def test_restart_prevents_report(self):
        self.prepare()
        self.capture.begin()
        self.conn.snapshots[11] = [snapshot(11, 1, startup=START + timedelta(minutes=1))]
        with self.assertRaisesRegex(AwrError, "restarted"):
            self.capture.finish()
        self.assertFalse(self.conn.report_queries)

    def test_dbid_change_prevents_report(self):
        self.prepare()
        self.capture.begin()
        self.conn.dbid = 9876
        with self.assertRaisesRegex(AwrError, "DBID changed"):
            self.capture.finish()
        self.assertFalse(self.conn.report_queries)

    def test_existing_mode_is_explicitly_partial(self):
        self.capture = AwrCapture(CONNECTION, AwrConfig(mode="existing"), self.root, FakeApi(self.conn))
        self.prepare()
        self.capture.begin()
        result = self.capture.finish()
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["covers_run_end"])
        self.assertEqual(result["coverage"], "latest_available_not_exact")
        self.assertFalse(self.conn.calls)
        self.assertEqual(len(result["reports"]), 2)

    def test_existing_no_new_snapshot_fails_without_report(self):
        self.capture = AwrCapture(CONNECTION, AwrConfig(mode="existing"), self.root, FakeApi(self.conn))
        self.conn.ids = [10, 10]
        self.prepare()
        self.capture.begin()
        with self.assertRaisesRegex(AwrError, "No newer ending"):
            self.capture.finish()
        self.assertFalse(self.conn.report_queries)

    def test_partial_transfer_removed_and_credentials_redacted(self):
        self.prepare()
        self.capture.begin()
        self.conn.report_failure = f"ORA-99999 {CONNECTION.password} {CONNECTION.wallet_password} {CONNECTION.dsn}"
        with self.assertRaises(AwrError) as raised:
            self.capture.finish()
        for secret in (CONNECTION.password, CONNECTION.wallet_password, CONNECTION.dsn):
            self.assertNotIn(secret, str(raised.exception))
            self.assertNotIn(secret, str(self.capture.result()))
        self.assertEqual(list((self.root / "awr").iterdir()), [])

    def test_empty_reports_fail(self):
        self.prepare()
        self.capture.begin()
        self.conn.report = []
        with self.assertRaisesRegex(AwrError, "empty report"):
            self.capture.finish()
        self.assertEqual(list((self.root / "awr").iterdir()), [])

    def test_existing_file_is_never_overwritten(self):
        self.prepare()
        self.capture.begin()
        target = self.root / "awr" / "awr_12345_instance_1_10_11.html"
        target.write_text("existing")
        with self.assertRaises(AwrError):
            self.capture.finish()
        self.assertEqual(target.read_text(), "existing")
        self.assertEqual(len(list((self.root / "awr").iterdir())), 1)

    def test_existing_awr_directory_is_not_reused(self):
        (self.root / "awr").mkdir()
        with self.assertRaises(AwrError):
            self.prepare()
        self.assertFalse(self.conn.calls)

    def test_lifecycle_requires_successful_prepare_and_begin(self):
        with self.assertRaises(AwrError):
            self.capture.begin()
        with self.assertRaises(AwrError):
            self.capture.finish()
        self.prepare()
        with self.assertRaises(AwrError):
            self.capture.prepare([WORKER])
        self.capture.begin()
        with self.assertRaises(AwrError):
            self.capture.begin()

    def test_invalid_config_cannot_generate_sql(self):
        for config in (AwrConfig(mode="auto"), AwrConfig(scope="all;drop table x"),
                       AwrConfig(formats=()), AwrConfig(formats=("html", "html")),
                       AwrConfig(formats=("html;drop table x",))):
            with self.assertRaises(ValueError):
                config.validate()

    def test_failed_workload_can_still_get_diagnostic_report(self):
        self.prepare()
        self.capture.begin()
        result = self.capture.finish("failed")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["run_status"], "failed")

    def test_metadata_copy_and_close(self):
        info = self.prepare()
        info["warnings"].append("mutation")
        self.assertNotIn("mutation", self.capture.result()["warnings"])
        self.capture.close()
        self.capture.close()
        self.assertTrue(self.conn.closed)


if __name__ == "__main__":
    unittest.main()
