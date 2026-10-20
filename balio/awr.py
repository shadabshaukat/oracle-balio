"""Optional, remotely generated AWR reports streamed to the client filesystem.

Caller owns workload barriers: prepare before warmup, begin after warmup and
before measurement, finish after every workload worker has stopped. AWR covers
database/container activity, including unrelated sessions and background work.
This module never enables packs, changes snapshot schedules, or grants access.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
from typing import Any

from . import db


class AwrError(RuntimeError):
    """An explicit AWR collection/export failure with credential-safe text."""


@dataclass(frozen=True)
class AwrConfig:
    mode: str = "snapshots"
    formats: tuple[str, ...] = ("html", "text")
    scope: str = "instance"

    def validate(self):
        if self.mode not in ("snapshots", "existing"):
            raise ValueError("AWR mode must be snapshots or existing")
        if self.scope not in ("instance", "global"):
            raise ValueError("AWR scope must be instance or global")
        if not self.formats or any(x not in ("html", "text") for x in self.formats):
            raise ValueError("AWR formats must contain html and/or text")
        if len(set(self.formats)) != len(self.formats):
            raise ValueError("AWR formats must not contain duplicates")
        return self


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _date(value):
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


class AwrCapture:
    """A dedicated reporting connection, never a workload worker connection."""

    def __init__(self, connection_config, config: AwrConfig, output_dir, api=None):
        self.connection_config = connection_config
        self.config = config.validate()
        self.output_dir = Path(output_dir) / "awr"
        self.api = api or db
        self.connection = None
        self._instances: list[int] = []
        self._view = "DBA_HIST_SNAPSHOT"
        self._begin_rows = {}
        self._end_rows = {}
        self._data = {
            "enabled": True, "mode": config.mode, "scope": config.scope,
            "formats": list(config.formats), "status": "new", "reports": [],
            "errors": [], "warnings": [],
            "attribution": "Database/container AWR includes all activity, not only BALIO.",
        }

    def _redact(self, value):
        text = str(value)
        for name in ("password", "wallet_password", "dsn"):
            secret = getattr(self.connection_config, name, None)
            if secret:
                text = text.replace(str(secret), "[REDACTED]")
        return text

    def _fail(self, stage, exc):
        message = self._redact(f"AWR {stage}: {exc}")
        self._data["status"] = "failed"
        self._data["errors"].append(message)
        raise AwrError(message) from None

    def _rows(self, sql, binds=None):
        with self.connection.cursor() as cur:
            cur.execute(sql, binds or {})
            return cur.fetchall()

    def _one(self, sql, binds=None):
        rows = self._rows(sql, binds)
        if len(rows) != 1:
            raise AwrError("Expected exactly one database identity row")
        return rows[0]

    def _identity(self):
        row = self._one("""
            SELECT SYS_CONTEXT('USERENV', 'DB_NAME'),
                   SYS_CONTEXT('USERENV', 'DB_UNIQUE_NAME'),
                   SYS_CONTEXT('USERENV', 'CON_NAME'),
                   SYS_CONTEXT('USERENV', 'CON_ID') FROM dual""")
        return dict(zip(("database_name", "db_unique_name", "container_name", "container_id"), row))

    def _local_dbid(self, is_pdb):
        try:
            value = self._one("SELECT DBMS_WORKLOAD_REPOSITORY.LOCAL_AWR_DBID() FROM dual")[0]
            source = "DBMS_WORKLOAD_REPOSITORY.LOCAL_AWR_DBID"
        except Exception:
            # LOCAL_AWR_DBID is not in the 12.1/12.2 public package contract.
            if is_pdb:
                value = self._one("""
                    SELECT local_awrdbid FROM dba_hist_wr_settings
                    WHERE con_id = TO_NUMBER(SYS_CONTEXT('USERENV', 'CON_ID'))""")[0]
                source = "DBA_HIST_WR_SETTINGS.LOCAL_AWRDBID"
            else:
                value = self._one("SELECT dbid FROM v$database")[0]
                source = "V$DATABASE.DBID"
        value = int(value)
        if value <= 0:
            raise AwrError("Local AWR DBID is not positive")
        return value, source

    def prepare(self, worker_details):
        """Read-only DB preflight. No snapshots are created until begin()."""
        if self._data["status"] != "new":
            raise AwrError("AWR prepare must be called exactly once")
        try:
            if not worker_details:
                raise AwrError("Worker database identities are required")
            self.connection = self.api.connect(self.connection_config)
            self.connection.module = "BALIO"
            self.connection.action = "awr-reporting"
            identity = self._identity()
            identity["container_id"] = int(identity["container_id"] or 0)
            is_pdb = identity["container_id"] > 1
            version = tuple(int(x) for x in str(self.connection.version).split(".")[:2])
            if is_pdb and version < (12, 2):
                raise AwrError("Oracle 12.1 PDB AWR requires a reporting connection to its CDB root")
            for worker in worker_details:
                for key in ("database_name", "db_unique_name"):
                    if not worker.get(key) or not identity.get(key):
                        raise AwrError(f"Cannot verify reporting/workload identity: missing {key}")
                    if str(worker[key]).upper() != str(identity[key]).upper():
                        raise AwrError(f"Reporting connection does not match workload {key}")
                if is_pdb and worker.get("container_name") != identity["container_name"]:
                    raise AwrError("Reporting PDB does not match workload container")
                if not worker.get("instance_name"):
                    raise AwrError("Worker instance_name is required for AWR attribution")
            dbid, source = self._local_dbid(is_pdb)
            self._view = "AWR_PDB_SNAPSHOT" if is_pdb else "DBA_HIST_SNAPSHOT"
            rows = self._rows("SELECT instance_number, instance_name FROM gv$instance")
            instances = {str(name): int(number) for number, name in rows}
            names = sorted({str(w["instance_name"]) for w in worker_details})
            if any(name not in instances for name in names):
                raise AwrError("Not all workload instances are visible through reporting GV$INSTANCE")
            self._instances = sorted({instances[name] for name in names})
            if not self._instances or any(x <= 0 for x in self._instances):
                raise AwrError("Invalid workload instance mapping")
            # Verify local-snapshot visibility without reading arbitrary history.
            self._rows(f"""SELECT snap_id, instance_number, startup_time,
                            begin_interval_time, end_interval_time, error_count
                            FROM {self._view} WHERE dbid = :dbid AND 1 = 0""", {"dbid": dbid})
            procedures = {str(row[0]) for row in self._rows("""
                SELECT procedure_name FROM all_procedures
                WHERE owner = 'SYS' AND object_name = 'DBMS_WORKLOAD_REPOSITORY'""")}
            prefix = "AWR_GLOBAL_REPORT_" if self.config.scope == "global" else "AWR_REPORT_"
            required = {prefix + fmt.upper() for fmt in self.config.formats}
            if self.config.mode == "snapshots":
                required.add("CREATE_SNAPSHOT")
            if not required.issubset(procedures):
                raise AwrError("Required AWR APIs are not visible: " + ", ".join(sorted(required - procedures)))
            # A new private directory keeps reports separate from earlier runs.
            self.output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
            self._data.update(status="prepared", database=identity, dbid=dbid,
                              dbid_source=source, snapshot_view=self._view,
                              instances=[{"number": instances[name], "name": name} for name in names],
                              prepared_at_utc=_utc())
            if not is_pdb and any(w.get("container_name") != identity["container_name"] for w in worker_details):
                self._data["warnings"].append("Reporting from CDB root includes activity from other containers.")
            if self.config.mode == "existing":
                self._data["warnings"].append(
                    "Existing mode uses the latest available common snapshots; the ending snapshot may precede workload stop. It does not promise full run coverage.")
            return self.result()
        except Exception as exc:
            self._fail("preflight", exc)

    def _snapshot_rows(self, snap_id):
        rows = self._rows(f"""
            SELECT snap_id, instance_number, startup_time, begin_interval_time,
                   end_interval_time, error_count FROM {self._view}
            WHERE dbid = :dbid AND snap_id = :snap_id""",
            {"dbid": self._data["dbid"], "snap_id": snap_id})
        selected = {}
        for row in rows:
            number = int(row[1])
            if number in self._instances:
                if number in selected:
                    raise AwrError("Ambiguous snapshot rows for local DBID and instance")
                if int(row[5] or 0):
                    raise AwrError(f"Snapshot {snap_id} has collection errors on instance {number}")
                selected[number] = {
                    "snap_id": int(row[0]), "instance_number": number,
                    "startup_time": _date(row[2]), "begin_interval_time": _date(row[3]),
                    "end_interval_time": _date(row[4]),
                }
        if set(selected) != set(self._instances):
            raise AwrError(f"Snapshot {snap_id} is missing one or more participating instances")
        return selected

    def _take_or_select(self):
        if self.config.mode == "snapshots":
            with self.connection.cursor() as cur:
                snap_id = int(cur.callfunc("DBMS_WORKLOAD_REPOSITORY.CREATE_SNAPSHOT", int, ["TYPICAL"]))
        else:
            binds = {"dbid": self._data["dbid"], "count": len(self._instances)}
            for i, number in enumerate(self._instances):
                binds[f"i{i}"] = number
            placeholders = ",".join(f":i{i}" for i in range(len(self._instances)))
            row = self._one(f"""
                SELECT MAX(snap_id) FROM (
                  SELECT snap_id FROM {self._view}
                  WHERE dbid = :dbid AND instance_number IN ({placeholders})
                    AND NVL(error_count, 0) = 0
                  GROUP BY snap_id HAVING COUNT(DISTINCT instance_number) = :count)""", binds)
            if row[0] is None:
                raise AwrError("No common AWR snapshot exists for the participating instances")
            snap_id = int(row[0])
        if snap_id <= 0:
            raise AwrError("AWR returned an invalid snapshot ID")
        return snap_id, self._snapshot_rows(snap_id)

    def begin(self):
        if self._data["status"] != "prepared":
            raise AwrError("AWR begin requires successful prepare")
        try:
            self._data["begin_requested_at_utc"] = _utc()
            snap_id, self._begin_rows = self._take_or_select()
            self._data.update(status="started", begin_snap_id=snap_id,
                              begin_snapshots=list(self._begin_rows.values()), begun_at_utc=_utc())
            return self.result()
        except Exception as exc:
            self._fail("begin", exc)

    def _write_report(self, instance, fmt):
        global_report = self.config.scope == "global"
        prefix = "AWR_GLOBAL_REPORT_" if global_report else "AWR_REPORT_"
        procedure = prefix + fmt.upper()
        inst_param = ",".join(map(str, self._instances)) if global_report else instance
        label = "global_" + "_".join(map(str, self._instances)) if global_report else f"instance_{instance}"
        extension = "txt" if fmt == "text" else "html"
        filename = (f"awr_{self._data['dbid']}_{label}_"
                    f"{self._data['begin_snap_id']}_{self._data['end_snap_id']}.{extension}")
        target = self.output_dir / filename
        fd, tmp = tempfile.mkstemp(prefix=".awr-", suffix=".partial", dir=self.output_dir)
        count = 0
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output:
                with self.connection.cursor() as cur:
                    cur.arraysize = 100
                    cur.execute(f"""SELECT output FROM TABLE(
                        DBMS_WORKLOAD_REPOSITORY.{procedure}(:dbid, :instances, :bid, :eid, 0))""",
                        {"dbid": self._data["dbid"], "instances": inst_param,
                         "bid": self._data["begin_snap_id"], "eid": self._data["end_snap_id"]})
                    for row in cur:
                        if row[0] is not None:
                            line = str(row[0])
                            output.write(line)
                            if not line.endswith("\n"):
                                output.write("\n")
                            count += len(line)
                if count == 0:
                    raise AwrError(f"{procedure} returned an empty report")
                output.flush()
                os.fsync(output.fileno())
            # Hard linking publishes a fully written file atomically and refuses
            # overwrite; mkstemp creates it mode 0600 regardless of umask.
            os.link(tmp, target)
        finally:
            Path(tmp).unlink(missing_ok=True)
        return {"format": fmt, "scope": self.config.scope,
                "instances": self._instances if global_report else [instance],
                "path": str(target.resolve()), "bytes": target.stat().st_size}

    def finish(self, run_status="completed"):
        if self._data["status"] != "started":
            raise AwrError("AWR finish requires successful begin")
        try:
            self._data.update(run_status=run_status, end_requested_at_utc=_utc())
            identity = self._identity()
            if any(str(identity[key]) != str(self._data["database"][key])
                   for key in ("database_name", "db_unique_name", "container_name")):
                raise AwrError("Reporting database/container identity changed during run")
            if self._local_dbid(int(identity["container_id"] or 0) > 1)[0] != self._data["dbid"]:
                raise AwrError("Local AWR DBID changed during run")
            snap_id, self._end_rows = self._take_or_select()
            self._data.update(end_snap_id=snap_id, end_snapshots=list(self._end_rows.values()))
            if snap_id <= self._data["begin_snap_id"]:
                raise AwrError("No newer ending AWR snapshot is available; use manual snapshots or a longer run")
            for instance in self._instances:
                before, after = self._begin_rows[instance], self._end_rows[instance]
                if before["startup_time"] != after["startup_time"]:
                    raise AwrError(f"Instance {instance} restarted between AWR snapshots")
                if before["end_interval_time"] >= after["end_interval_time"]:
                    raise AwrError(f"Snapshot time did not advance on instance {instance}")
            self._data["coverage"] = ("bracketed_with_snapshot_overhead"
                                      if self.config.mode == "snapshots"
                                      else "latest_available_not_exact")
            self._data["covers_run_end"] = self.config.mode == "snapshots"
            self._data["warnings"].append(
                "AWR intervals include snapshot boundary overhead and unrelated workload; compare recorded snapshot times with the measurement interval.")
            targets = [None] if self.config.scope == "global" else self._instances
            for instance in targets:
                for fmt in self.config.formats:
                    self._data["reports"].append(self._write_report(instance, fmt))
            if self.config.mode == "existing":
                self._data["warnings"].append(
                    "Partial coverage only: no post-run snapshot was created. Generate a new report after the next automatic snapshot to include the run end.")
            self._data.update(status="complete" if self.config.mode == "snapshots" else "partial",
                              finished_at_utc=_utc())
            return self.result()
        except Exception as exc:
            self._fail("finish", exc)

    def result(self):
        """Credential-free, JSON-serializable metadata, including partial output."""
        return deepcopy(self._data)

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            finally:
                self.connection = None
