"""Oracle connections, owned schema deployment, resumable seeding and batches.

SQL intentionally uses a 12.1 baseline. That is a compatibility target, not a
claim of certification against each Oracle release or managed service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
import sys
import threading
from typing import Any, Callable


MAGIC = "BALIO_DATASET_V1"
SCHEMA_VERSION = 1
MAX_ROWS = 2_147_483_646
MAX_SEED = 2_147_483_646
_INIT_LOCK = threading.Lock()
_INITIALIZED: tuple[str, str | None, str | None] | None = None
_PAYLOAD_ALPHABET = bytes(33 + (value % 94) for value in range(256))
_META_COLUMNS = (
    "magic", "schema_version", "status", "hot_rows", "cold_rows",
    "payload_bytes", "hot_seeded", "cold_seeded", "created_at", "updated_at",
)
_TABLES = ("BALIO_META", "BALIO_HOT", "BALIO_COLD")
_RESERVED = (*_TABLES, "BALIO_RUNNER", "BALIO_META_PK", "BALIO_HOT_PK", "BALIO_COLD_PK")

# Keep Oracle's literal names: blocks, requests, bytes and centiseconds are
# different units. Statistics absent on a release are absent, never fake zeros.
STAT_NAMES = (
    "session logical reads",
    "physical reads cache",
    "physical reads",
    "physical reads direct",
    "physical read IO requests",
    "physical read total IO requests",
    "physical read bytes",
    "physical read total bytes",
    "physical writes",
    "physical write IO requests",
    "physical write total IO requests",
    "physical write total bytes",
    "redo size",
    "db block changes",
    "user commits",
    "CPU used by this session",
)


@dataclass(frozen=True)
class ConnectionConfig:
    dsn: str
    user: str | None = None
    password: str | None = field(default=None, repr=False)
    mode: str = "thick"
    lib_dir: str | None = None
    config_dir: str | None = None
    external_auth: bool = False
    call_timeout_ms: int = 60_000
    wallet_location: str | None = None
    wallet_password: str | None = field(default=None, repr=False)


def _driver() -> Any:
    try:
        import oracledb
    except ImportError as exc:
        raise RuntimeError("Install BALIO with its python-oracledb dependency before connecting") from exc
    return oracledb


def _integer(value: Any, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def _percentage(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite percentage from 0 to 100")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise ValueError(f"{name} must be a finite percentage from 0 to 100")
    return result


def connect(config: ConnectionConfig) -> Any:
    """Connect remotely; Thick/Instant Client is the default transport.

    On Linux the shared-library search path must be set before Python starts.
    TNS_ADMIN/config_dir selects network configuration, not client libraries.
    Passwords and external-wallet credentials are deliberately not logged.
    """
    global _INITIALIZED
    if config.mode not in ("thick", "thin"):
        raise ValueError("mode must be thick or thin")
    if not config.dsn:
        raise ValueError("A database DSN, TNS alias or Easy Connect service is required")
    if not config.external_auth and (not config.user or config.password is None):
        raise ValueError("Supply user and password, or explicitly choose external authentication")
    if config.mode == "thin" and config.external_auth:
        raise ValueError("External authentication requires Thick mode")
    if config.mode == "thin" and config.lib_dir:
        raise ValueError("lib_dir applies only to Thick mode")
    if config.mode == "thick" and sys.platform.startswith("linux") and config.lib_dir:
        raise ValueError("On Linux configure ldconfig or LD_LIBRARY_PATH before starting BALIO; omit --lib-dir")
    _integer(config.call_timeout_ms, "call_timeout_ms", 0, 2_147_483_647)
    driver = _driver()
    identity = (config.mode, config.lib_dir, config.config_dir)
    with _INIT_LOCK:
        if _INITIALIZED is not None and _INITIALIZED != identity:
            raise RuntimeError("Oracle driver mode/client/network configuration cannot change in one process")
        if _INITIALIZED is None:
            if config.mode == "thick":
                kwargs = {}
                if config.lib_dir:
                    kwargs["lib_dir"] = config.lib_dir
                if config.config_dir:
                    kwargs["config_dir"] = config.config_dir
                driver.init_oracle_client(**kwargs)
            elif hasattr(driver, "enable_thin_mode"):
                driver.enable_thin_mode()
            elif not driver.is_thin_mode():
                raise RuntimeError("Thick mode is already enabled; start a new process for Thin mode")
            _INITIALIZED = identity
    kwargs = {"dsn": config.dsn, "externalauth": config.external_auth}
    if config.user:
        kwargs["user"] = config.user
    if config.password is not None:
        kwargs["password"] = config.password
    if config.mode == "thin" and config.config_dir:
        kwargs["config_dir"] = config.config_dir
    if config.wallet_location:
        kwargs["wallet_location"] = config.wallet_location
    if config.wallet_password is not None:
        kwargs["wallet_password"] = config.wallet_password
    conn = driver.connect(**kwargs)
    try:
        version = tuple(int(part) for part in str(conn.version).split(".")[:2])
        if version < (12, 1):
            raise RuntimeError(f"BALIO targets Oracle Database 12.1 or newer; connected version is {conn.version}")
        conn.autocommit = False
        conn.call_timeout = config.call_timeout_ms
        conn.module = "BALIO"
        return conn
    except BaseException:
        conn.close()
        raise


def database_details(conn: Any) -> dict[str, Any]:
    """Unprivileged runtime identity. Numeric version 23 can denote 26ai."""
    with conn.cursor() as cur:
        cur.execute("""SELECT USER, SYS_CONTEXT('USERENV', 'DB_NAME'),
                       SYS_CONTEXT('USERENV', 'SERVICE_NAME'),
                       SYS_CONTEXT('USERENV', 'INSTANCE_NAME'),
                       SYS_CONTEXT('USERENV', 'CON_NAME'),
                       SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME') FROM dual""")
        row = cur.fetchone()
    driver = _driver()
    result = dict(zip(("user", "database_name", "service_name", "instance_name", "container_name", "db_unique_name"), row))
    result.update(database_version=str(conn.version), driver_version=driver.__version__,
                  driver_mode="thin" if driver.is_thin_mode() else "thick")
    if not driver.is_thin_mode():
        result["client_version"] = ".".join(map(str, driver.clientversion()))
    return result


def _metadata(cur: Any, for_update: bool = False) -> dict[str, Any]:
    query = "SELECT " + ", ".join(_META_COLUMNS) + " FROM BALIO_META WHERE singleton = 1"
    if for_update:
        query += " FOR UPDATE NOWAIT"
    cur.execute(query)
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("BALIO_META has no ownership marker; refusing to modify this schema")
    result = dict(zip(_META_COLUMNS, row))
    if result["magic"] != MAGIC or int(result["schema_version"]) != SCHEMA_VERSION:
        raise RuntimeError("Unrecognized BALIO ownership marker/schema version; refusing to modify it")
    for key in ("schema_version", "hot_rows", "cold_rows", "payload_bytes", "hot_seeded", "cold_seeded"):
        result[key] = int(result[key])
    for key in ("created_at", "updated_at"):
        if hasattr(result[key], "isoformat"):
            result[key] = result[key].isoformat()
    return result


def dataset_info(conn: Any) -> dict[str, Any]:
    """Read persisted targets and seed progress without scanning workload data."""
    with conn.cursor() as cur:
        result = _metadata(cur)
    sizes = segment_sizes(conn)
    result["segment_bytes"] = sizes.get("allocated_bytes")
    result["segment_details"] = sizes
    return result


def segment_sizes(conn: Any) -> dict[str, Any]:
    """Allocated workload table/index sizes, excluding metadata; not cache size."""
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT segment_name, segment_type, SUM(bytes), SUM(blocks), SUM(extents)
                             FROM user_segments
                            WHERE segment_name IN ('BALIO_HOT', 'BALIO_COLD', 'BALIO_HOT_PK', 'BALIO_COLD_PK')
                            GROUP BY segment_name, segment_type ORDER BY segment_name""")
            rows = cur.fetchall()
        segments = [{"name": r[0], "type": r[1], "bytes": int(r[2]), "blocks": int(r[3]), "extents": int(r[4])}
                    for r in rows]
        return {"available": True, "allocated_bytes": sum(r["bytes"] for r in segments), "segments": segments}
    except Exception as exc:
        return {"available": False, "error": str(exc), "segments": []}


def _schema_statements() -> list[tuple[str, str, str]]:
    source = Path(__file__).with_name("sql").joinpath("schema.sql").read_text(encoding="utf-8")
    statements = []
    for chunk in re.split(r"(?m)^\s*/\s*$", source):
        chunk = chunk.strip()
        if not chunk:
            continue
        marker = re.search(r"-- BALIO_OBJECT: (\w+) (TABLE|PACKAGE BODY|PACKAGE)\s*$", chunk, re.MULTILINE)
        if marker is None:
            raise RuntimeError("Invalid bundled schema SQL: missing object marker")
        name, kind = marker.groups()
        if kind == "TABLE":
            chunk = chunk.rstrip(";")
        statements.append((name, kind, chunk))
    return statements


def _validate_table(cur: Any, table: str) -> None:
    cur.execute("SELECT column_name, data_type, data_length, nullable FROM user_tab_columns WHERE table_name = :name ORDER BY column_id",
                name=table)
    columns = cur.fetchall()
    actual = [(row[0], row[1]) for row in columns]
    expected = [("ID", "NUMBER"), ("PAYLOAD", "VARCHAR2"), ("TOUCH", "NUMBER")]
    if table == "BALIO_META":
        expected = [("SINGLETON", "NUMBER"), ("MAGIC", "VARCHAR2"), ("SCHEMA_VERSION", "NUMBER"),
                    ("STATUS", "VARCHAR2"), ("HOT_ROWS", "NUMBER"), ("COLD_ROWS", "NUMBER"),
                    ("PAYLOAD_BYTES", "NUMBER"), ("HOT_SEEDED", "NUMBER"), ("COLD_SEEDED", "NUMBER"),
                    ("CREATED_AT", "TIMESTAMP(6)"), ("UPDATED_AT", "TIMESTAMP(6)")]
    widths = {"MAGIC": 32, "STATUS": 12} if table == "BALIO_META" else {"PAYLOAD": 2000}
    invalid_shape = any(row[3] != "N" or (row[0] in widths and row[2] != widths[row[0]]) for row in columns)
    if actual != expected or invalid_shape:
        raise RuntimeError(f"{table} does not match the BALIO schema; refusing to alter it")
    cur.execute("""SELECT c.constraint_name, c.status, c.validated, cc.column_name
                     FROM user_constraints c JOIN user_cons_columns cc
                       ON cc.constraint_name = c.constraint_name AND cc.table_name = c.table_name
                    WHERE c.table_name = :name AND c.constraint_type = 'P'
                    ORDER BY cc.position""", name=table)
    key = "SINGLETON" if table == "BALIO_META" else "ID"
    if cur.fetchall() != [(table + "_PK", "ENABLED", "VALIDATED", key)]:
        raise RuntimeError(f"{table} primary key does not match the BALIO schema; refusing to use it")


def deploy(conn: Any, payload_bytes: int = 512) -> dict[str, Any]:
    """Create only BALIO-owned objects. Re-running deploy preserves seeded rows.

    Oracle DDL implicitly commits. Run deployment on a dedicated idle connection.
    A valid metadata marker and package-source marker are required on redeploy.
    """
    _integer(payload_bytes, "payload_bytes", 64, 2000)
    statements = _schema_statements()
    with conn.cursor() as cur:
        placeholders = ",".join(f":n{i}" for i in range(len(_RESERVED)))
        cur.execute(f"SELECT object_name, object_type FROM user_objects WHERE object_name IN ({placeholders})",
                    {f"n{i}": value for i, value in enumerate(_RESERVED)})
        existing = {(row[0], row[1]) for row in cur.fetchall()}
        if existing and ("BALIO_META", "TABLE") not in existing:
            raise RuntimeError("Reserved BALIO names already exist without an ownership marker; choose a clean dedicated schema")
        if ("BALIO_META", "TABLE") in existing:
            _metadata(cur)
            for table in _TABLES:
                if (table, "TABLE") in existing:
                    _validate_table(cur, table)
            for kind in ("PACKAGE", "PACKAGE BODY"):
                if ("BALIO_RUNNER", kind) in existing:
                    cur.execute("SELECT COUNT(*) FROM user_source WHERE name = 'BALIO_RUNNER' AND type = :kind AND text LIKE '%BALIO_MANAGED_SCHEMA_1%'",
                                kind=kind)
                    if int(cur.fetchone()[0]) == 0:
                        raise RuntimeError("BALIO_RUNNER source is not recognized; refusing to replace it")
        allowed = {(table, "TABLE") for table in _TABLES} | {
            ("BALIO_RUNNER", "PACKAGE"), ("BALIO_RUNNER", "PACKAGE BODY"),
            ("BALIO_HOT_PK", "INDEX"), ("BALIO_COLD_PK", "INDEX"), ("BALIO_META_PK", "INDEX"),
        }
        if existing - allowed:
            raise RuntimeError("Reserved BALIO names have unexpected object types; choose a clean dedicated schema")
        for name, kind, sql in statements:
            if kind == "TABLE" and (name, kind) in existing:
                continue
            cur.execute(sql)
            if name == "BALIO_META":
                cur.execute("""INSERT INTO BALIO_META
                    (singleton, magic, schema_version, status, hot_rows, cold_rows, payload_bytes, hot_seeded, cold_seeded)
                    VALUES (1, :magic, :version, 'EMPTY', 0, 0, :payload, 0, 0)""",
                            magic=MAGIC, version=SCHEMA_VERSION, payload=payload_bytes)
                conn.commit()
        cur.execute("SELECT type, line, position, text FROM user_errors WHERE name = 'BALIO_RUNNER' ORDER BY type, sequence")
        errors = cur.fetchall()
        if errors:
            raise RuntimeError("BALIO package compilation failed: " + "; ".join(f"{r[0]} {r[1]}:{r[2]} {r[3]}" for r in errors))
    return dataset_info(conn)


def payload_for(pool: str, row_id: int, payload_bytes: int) -> str:
    """Deterministic, varied printable ASCII, exactly payload_bytes in AL32UTF8.

    Rows/pools use different XOF inputs. Printable ASCII has finite entropy;
    this avoids repeated filler but does not promise incompressible storage.
    """
    raw = hashlib.shake_256(f"BALIO1:{pool}:{row_id}".encode("ascii")).digest(payload_bytes)
    return raw.translate(_PAYLOAD_ALPHABET).decode("ascii")


def seed(conn: Any, hot_rows: int, cold_rows: int, payload_bytes: int,
         batch_rows: int = 1000, progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Conventional array-bound inserts; each data chunk/progress row is atomic.

    A disconnected or cancelled seed can be rerun with identical dimensions.
    The metadata row lock prevents concurrent seeders from inserting the same
    chunk. A busy lock fails promptly; it is safe to retry after the other seed.
    Targets cannot change in a partially seeded or ready dataset.
    """
    _integer(hot_rows, "hot_rows", 0, MAX_ROWS)
    _integer(cold_rows, "cold_rows", 0, MAX_ROWS)
    _integer(payload_bytes, "payload_bytes", 64, 2000)
    _integer(batch_rows, "batch_rows", 1, 100_000)
    if hot_rows + cold_rows == 0:
        raise ValueError("Seed at least one row in either pool")
    conn.autocommit = False
    try:
        while True:
            with conn.cursor() as cur:
                meta = _metadata(cur, for_update=True)
                if meta["status"] == "EMPTY":
                    cur.execute("""UPDATE BALIO_META SET status = 'SEEDING', hot_rows = :hot,
                                   cold_rows = :cold, payload_bytes = :payload, updated_at = SYSTIMESTAMP
                                   WHERE singleton = 1""", hot=hot_rows, cold=cold_rows, payload=payload_bytes)
                    meta.update(status="SEEDING", hot_rows=hot_rows, cold_rows=cold_rows, payload_bytes=payload_bytes)
                if (meta["hot_rows"], meta["cold_rows"], meta["payload_bytes"]) != (hot_rows, cold_rows, payload_bytes):
                    raise ValueError("Seed dimensions differ from this dataset; resume using its original dimensions or use a new dedicated schema")
                if meta["status"] not in ("SEEDING", "READY"):
                    raise RuntimeError("Unexpected BALIO seed state")
                if not (0 <= meta["hot_seeded"] <= hot_rows and 0 <= meta["cold_seeded"] <= cold_rows):
                    raise RuntimeError("BALIO seed metadata is inconsistent")
                if meta["hot_seeded"] == hot_rows and meta["cold_seeded"] == cold_rows:
                    if meta["status"] != "READY":
                        cur.execute("UPDATE BALIO_META SET status = 'READY', updated_at = SYSTIMESTAMP WHERE singleton = 1")
                    conn.commit()
                    result = dataset_info(conn)
                    if progress:
                        progress(result)
                    return result
                if meta["status"] == "READY":
                    raise RuntimeError("READY dataset has incomplete seed metadata")
                pool = "hot" if meta["hot_seeded"] < hot_rows else "cold"
                target = hot_rows if pool == "hot" else cold_rows
                first = meta[f"{pool}_seeded"] + 1
                last = min(target, first + batch_rows - 1)
                # pool is an internal two-value selection, never user SQL text.
                rows = [(i, payload_for(pool, i, payload_bytes)) for i in range(first, last + 1)]
                with conn.cursor() as insert_cur:
                    insert_cur.setinputsizes(None, payload_bytes)
                    insert_cur.executemany(f"INSERT INTO BALIO_{pool.upper()} (id, payload, touch) VALUES (:1, :2, 0)", rows)
                cur.execute(f"UPDATE BALIO_META SET {pool}_seeded = :last, updated_at = SYSTIMESTAMP WHERE singleton = 1", last=last)
                conn.commit()
            if progress:
                # Avoid extra data-dictionary queries on every inserted chunk.
                progress({**meta, f"{pool}_seeded": last})
    except BaseException:
        conn.rollback()
        raise


def run_batch(conn: Any, operations: int, cold_pct: float, update_pct: float,
              seed: int, worker_id: int, workers: int, hot_rows: int, cold_rows: int,
              commit_every: int) -> dict[str, int]:
    """Execute operations in one server call, preserving the returned RNG state.

    Percentages are selection probabilities, not exact per-batch proportions.
    Writes commit at commit_every updates or batch end. Reads never commit.
    Counters describe operations, not Oracle logical blocks or storage I/Os.
    """
    _integer(operations, "operations", 1, 1_000_000)
    cold_pct = _percentage(cold_pct, "cold_pct")
    update_pct = _percentage(update_pct, "update_pct")
    _integer(seed, "seed", 1, MAX_SEED)
    _integer(workers, "workers", 1, 1_000_000)
    _integer(worker_id, "worker_id", 0, workers - 1)
    _integer(hot_rows, "hot_rows", 0, MAX_ROWS)
    _integer(cold_rows, "cold_rows", 0, MAX_ROWS)
    _integer(commit_every, "commit_every", 1, 2_147_483_647)
    for active, count in ((cold_pct < 100, hot_rows), (cold_pct > 0, cold_rows)):
        if active and count == 0:
            raise ValueError("Every active pool must contain rows")
        if active and update_pct > 0 and count < workers:
            raise ValueError("Every pool used for updates needs at least one row per worker")
    with conn.cursor() as cur:
        rng = cur.var(int)
        rng.setvalue(0, seed)
        outs = [cur.var(int) for _ in range(6)]
        try:
            cur.callproc("BALIO_RUNNER.RUN_BATCH", [operations, cold_pct, update_pct, rng,
                         worker_id, workers, hot_rows, cold_rows, commit_every, *outs])
        except BaseException:
            # Earlier commits in this batch remain durable on any subsequent
            # error. The caller must fail/mark the run incomplete, not retry it.
            conn.rollback()
            raise
        values = [int(value.getvalue()) for value in outs]
    result = dict(zip(("hot_ops", "cold_ops", "read_ops", "update_ops", "commits", "checksum"), values))
    result.update(ops=operations, seed=int(rng.getvalue()))
    return result


def snapshot(conn: Any) -> tuple[dict[str, int] | None, str | None]:
    """Same-session counters, or (None, reason) when grants are unavailable.

    Snapshots have small measurement overhead. Session write counters do not
    attribute asynchronous DBWR/LGWR writes to this client workload.
    """
    try:
        with conn.cursor() as cur:
            binds = {f"n{i}": name for i, name in enumerate(STAT_NAMES)}
            placeholders = ",".join(f":n{i}" for i in range(len(STAT_NAMES)))
            cur.execute(f"""SELECT n.name, s.value FROM v$mystat s
                             JOIN v$statname n ON n.statistic# = s.statistic#
                            WHERE n.name IN ({placeholders})""", binds)
            counters = {str(name): int(value) for name, value in cur.fetchall()}
        if not counters:
            return None, "Session-statistics query returned no recognized counters"
        return counters, None
    except Exception as exc:
        return None, str(exc)
