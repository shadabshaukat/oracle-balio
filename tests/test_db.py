"""Offline boundary/state-machine checks, not Oracle SQL integration tests."""
import copy
from datetime import datetime
import unittest
from unittest.mock import MagicMock

from balio import db


class SeedConnection:
    """Transactional test double for interruption/resume invariants."""
    def __init__(self, fail_insert_number=None):
        self.meta = dict(magic=db.MAGIC, schema_version=1, status="EMPTY", hot_rows=0,
                         cold_rows=0, payload_bytes=512, hot_seeded=0, cold_seeded=0,
                         created_at=datetime(2026, 1, 1), updated_at=datetime(2026, 1, 1))
        self.rows = {"hot": {}, "cold": {}}
        self.committed = copy.deepcopy((self.meta, self.rows))
        self.fail_insert_number = fail_insert_number
        self.insert_count = 0
        self.rollback_count = 0
        self.autocommit = False

    def cursor(self):
        return SeedCursor(self)

    def commit(self):
        self.committed = copy.deepcopy((self.meta, self.rows))

    def rollback(self):
        self.rollback_count += 1
        self.meta, self.rows = copy.deepcopy(self.committed)


class SeedCursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, *args, **binds):
        if sql.startswith("SELECT magic"):
            self.result = [tuple(self.conn.meta[key] for key in db._META_COLUMNS)]
        elif "FROM user_segments" in sql:
            self.result = []
        elif "SET status = 'SEEDING'" in sql:
            self.conn.meta.update(status="SEEDING", hot_rows=binds["hot"], cold_rows=binds["cold"],
                                  payload_bytes=binds["payload"])
        elif "SET status = 'READY'" in sql:
            self.conn.meta["status"] = "READY"
        elif "SET hot_seeded" in sql:
            self.conn.meta["hot_seeded"] = binds["last"]
        elif "SET cold_seeded" in sql:
            self.conn.meta["cold_seeded"] = binds["last"]
        else:
            raise AssertionError(f"Unexpected test SQL: {sql}")

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result

    def setinputsizes(self, *args):
        pass

    def executemany(self, sql, rows):
        self.conn.insert_count += 1
        pool = "hot" if "BALIO_HOT" in sql else "cold"
        for row_id, payload in rows:
            if row_id in self.conn.rows[pool]:
                raise AssertionError("Duplicate key during seed resume")
            self.conn.rows[pool][row_id] = payload
            if self.conn.insert_count == self.conn.fail_insert_number:
                raise ConnectionError("simulated disconnect during array insert")


class DatabaseTests(unittest.TestCase):
    def test_payloads_have_requested_ascii_bytes_and_are_reproducible(self):
        for width in (64, 511, 2000):
            hot = db.payload_for("hot", 1, width)
            self.assertEqual(len(hot.encode("utf-8")), width)
            self.assertEqual(hot, db.payload_for("hot", 1, width))
            self.assertNotEqual(hot, db.payload_for("hot", 2, width))
            self.assertNotEqual(hot, db.payload_for("cold", 1, width))
            self.assertGreater(len(set(hot)), 20)

    def test_seed_disconnect_preserves_committed_chunk_and_resumes(self):
        conn = SeedConnection(fail_insert_number=2)
        with self.assertRaises(ConnectionError):
            db.seed(conn, hot_rows=5, cold_rows=4, payload_bytes=64, batch_rows=2)
        self.assertEqual(conn.meta["hot_seeded"], 2)
        self.assertEqual(sorted(conn.rows["hot"]), [1, 2])
        self.assertEqual(conn.meta["status"], "SEEDING")
        self.assertEqual(conn.rollback_count, 1)
        conn.fail_insert_number = None
        result = db.seed(conn, hot_rows=5, cold_rows=4, payload_bytes=64, batch_rows=2)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(sorted(conn.rows["hot"]), list(range(1, 6)))
        self.assertEqual(sorted(conn.rows["cold"]), list(range(1, 5)))
        count = conn.insert_count
        db.seed(conn, hot_rows=5, cold_rows=4, payload_bytes=64, batch_rows=2)
        self.assertEqual(conn.insert_count, count, "Ready dataset must not be reseeded")

    def test_seed_mismatch_never_changes_committed_dataset(self):
        conn = SeedConnection()
        db.seed(conn, hot_rows=3, cold_rows=4, payload_bytes=64, batch_rows=2)
        expected = copy.deepcopy((conn.meta, conn.rows))
        with self.assertRaises(ValueError):
            db.seed(conn, hot_rows=3, cold_rows=5, payload_bytes=64)
        self.assertEqual((conn.meta, conn.rows), expected)

    def test_seed_rejects_unrecognized_marker(self):
        conn = SeedConnection()
        conn.meta["magic"] = "SOME_OTHER_TOOL"
        conn.commit()
        with self.assertRaisesRegex(RuntimeError, "ownership marker"):
            db.seed(conn, 3, 4, 64)
        self.assertEqual(conn.insert_count, 0)

    def test_batch_rejects_invalid_percentages_and_empty_active_pool(self):
        base = dict(operations=100, cold_pct=50, update_pct=0, seed=42,
                    worker_id=0, workers=4, hot_rows=100, cold_rows=100, commit_every=10)
        for change in ({"cold_pct": float("nan")}, {"update_pct": 101}, {"seed": 0},
                       {"worker_id": 4}, {"hot_rows": 0}, {"hot_rows": db.MAX_ROWS + 1},
                       {"update_pct": 10, "cold_rows": 3}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                db.run_batch(None, **(base | change))

    def test_snapshots_preserve_missing_counters_as_missing(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [("session logical reads", 123), ("physical reads cache", 7)]
        counters, error = db.snapshot(conn)
        self.assertIsNone(error)
        self.assertEqual(counters["physical reads cache"], 7)
        self.assertNotIn("physical read total IO requests", counters)
        cur.execute.side_effect = RuntimeError("view access unavailable")
        self.assertEqual(db.snapshot(conn), (None, "view access unavailable"))

    def test_passwords_are_not_in_connection_config_repr(self):
        config = db.ConnectionConfig(dsn="db", user="balio", password="top-secret", wallet_password="wallet-secret")
        self.assertNotIn("top-secret", repr(config))
        self.assertNotIn("wallet-secret", repr(config))


if __name__ == "__main__":
    unittest.main()
