# AWR reports on the Linux client

BALIO can create AWR boundary snapshots on the remote database and stream HTML
and text reports back to the Linux host running BALIO. Reports are written below
the run output directory in `awr/`; there is no SSH, server filesystem copy,
Oracle DIRECTORY object, or database-server Oracle Home requirement. The
Python driver calls Oracle's reporting table functions over the same Oracle Net
transport used by Instant Client.

## Run with AWR

Assuming the usual BALIO connection environment is already configured:

```bash
balio run --workers 16 --duration 900 --warmup 120 \
  --awr snapshots --awr-format both --awr-scope instance \
  --output runs/workers-16
```

For a separate reporting account, keep the workload in its benchmark schema and
provide the reporting password in an environment variable:

```bash
read -r -s -p 'AWR reporting password: ' BALIO_AWR_PASSWORD; echo
export BALIO_AWR_PASSWORD
balio run --workers 16 --duration 900 --warmup 120 \
  --awr snapshots --awr-user BENCH_REPORTER \
  --awr-dsn 'dbhost.example:1521/reporting_service' \
  --awr-password-env BALIO_AWR_PASSWORD \
  --output runs/workers-16
unset BALIO_AWR_PASSWORD
```

Use a fresh output directory for each run. The reporting connection must reach
the same database as the workers. A CDB-root reporting connection may be used
for workers in a PDB; the report then includes the broader CDB activity. A
reporting connection into another PDB or another database is rejected.

The run order is:

1. Connect workers and verify the reporting identity, APIs, and snapshot views.
2. Warm up the workload and pause workers at the measurement boundary.
3. Create the beginning AWR snapshot.
4. Start measured workload execution.
5. Stop and join all workload workers.
6. Create the ending AWR snapshot and stream reports to local files.

Snapshot and report generation are outside the measured workload interval.
Snapshot boundary overhead, final batch completion, other sessions, and
background database work can still appear in the AWR interval. AWR is not a
per-BALIO-session report. Compare the recorded snapshot interval with BALIO's
measurement interval before interpreting the rates.

## Scope and metadata

`--awr-scope instance` produces each requested format for every database instance
used by the workers. `--awr-scope global` requests a RAC global report containing
the participating instance numbers. An instance missing from either boundary,
snapshot collection errors, changed DBID, or an instance restart makes AWR
collection fail explicitly.

The JSON summary records measurement UTC timestamps, AWR status, DBID and its source, reporting container,
participating instances, snapshot IDs and timestamps, workload status, output
paths, and errors. Completed report files remain listed if a later export fails.
Report files are created with mode `0600` inside a new `0700` directory.
Incomplete transfers are removed; existing report files are never overwritten.

## Oracle releases, PDBs, and managed services

Oracle documents the `CREATE_SNAPSHOT` function and HTML/text report functions
in the [12.1 package reference](https://docs.oracle.com/database/121/ARPLS/d_workload_repos.htm),
[19c package reference](https://docs.oracle.com/en/database/oracle/oracle-database/19/arpls/DBMS_WORKLOAD_REPOSITORY.html),
and [26ai package reference](https://docs.oracle.com/en/database/oracle/oracle-database/26/arpls/DBMS_WORKLOAD_REPOSITORY.html).
BALIO uses these APIs directly; `awrrpt.sql` is not copied or required locally.

PDB-local snapshots are available from 12.2. BALIO uses `AWR_PDB_SNAPSHOT` in a
PDB so it does not accidentally select CDB-root history from the default
`DBA_HIST` view. It resolves the local AWR DBID with
`DBMS_WORKLOAD_REPOSITORY.LOCAL_AWR_DBID()` when available, with a 12.2 PDB
fallback to `DBA_HIST_WR_SETTINGS.LOCAL_AWRDBID`. For a 12.1 PDB workload, use
a reporting connection to the matching CDB root. See Oracle's
[multitenant AWR guidance](https://docs.oracle.com/en/database/oracle/oracle-database/12.2/tgdba/gathering-database-statistics.html),
[AWR_PDB view description](https://docs.oracle.com/en/database/oracle/oracle-database/12.2/refrn/awr_pdb_-views.html),
and [local repository settings](https://docs.oracle.com/en/database/oracle/oracle-database/12.2/refrn/DBA_HIST_WR_SETTINGS.html).

Autonomous Database and other managed services can restrict package operations,
catalog views, or CDB-root access. Their support is capability-based: a platform
label cannot guarantee manual snapshots or all report formats. Oracle documents
downloadable reports in
[Dedicated Autonomous Performance Hub](https://docs.oracle.com/en/cloud/paas/autonomous-database/dedicated/adbaa/observe-autonomous-ai-database-on-dedicated-exadata-1.html)
and [AWR Explorer](https://docs.oracle.com/en-us/iaas/database-management/doc/use-awr-explorer-analyze-database-performance.html).
These UI capabilities do not prove that an arbitrary SQL account can invoke
every AWR API.

`--awr existing` avoids manual snapshot creation. It selects the latest common
available snapshot at each boundary. If no newer snapshot appears during the
run, it fails clearly. If a newer snapshot is available, it can export a useful
**partial** report, but records `status: partial` and `covers_run_end: false`.
BALIO does not call this complete coverage: the automatic snapshot may precede
workload stop. Export a later report with the recorded beginning snapshot and a
post-run ending snapshot using a reporting account or the service's AWR UI.
There is no silent fallback from manual snapshots to this mode.

## Access and entitlement

AWR is optional and off by default. Enable it only where your database or cloud
service entitlement permits Oracle Diagnostics Pack/AWR use; API accessibility
or `CONTROL_MANAGEMENT_PACK_ACCESS` settings do not establish entitlement.
BALIO never changes that setting or the snapshot schedule. See Oracle's
[licensing information](https://docs.oracle.com/en/database/oracle/oracle-database/26/dblic/Licensing-Information.html).

The reporting account needs permission to execute the requested
`DBMS_WORKLOAD_REPOSITORY` operations and read the relevant snapshot and
instance views. Oracle's [reporting privilege documentation](https://docs.oracle.com/en/database/oracle/oracle-database/26/tgdba/generating-automatic-workload-repository-reports.html)
lists `ADVISOR`, `SELECT_CATALOG_ROLE`, and package execution privileges for
reporting. Snapshot creation can need additional administrative permission;
use the DBA-approved account supported by your service. BALIO does not grant
these privileges to the workload schema.

These paths have offline protocol and file-safety tests. They still require
live verification against the specific database release, RAC topology,
container configuration, and managed service before claiming compatibility.
