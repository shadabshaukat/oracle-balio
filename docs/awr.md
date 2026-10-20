# AWR reports

[Back to the project README](../README.md)

BALIO can take AWR snapshots around the measured workload on the remote Oracle
database and save HTML and text reports on the Linux host running BALIO.
It calls `DBMS_WORKLOAD_REPOSITORY` through `python-oracledb` and Oracle Net.
SQL*Plus, SSH, server filesystem access, and an Oracle `DIRECTORY` object are
not required.

AWR is **off by default**. Enable it only when the database or cloud service
entitlement and the reporting account's permissions allow the requested AWR
operations. See [access and entitlement](#access-and-entitlement).

## Contents

- [Choose the Oracle accounts](#choose-the-oracle-accounts)
- [Run with a separate reporting account](#run-with-a-separate-reporting-account)
- [Reuse the workload account](#reuse-the-workload-account)
- [Options](#options)
- [Local files and run status](#local-files-and-run-status)
- [Snapshot timing and report scope](#snapshot-timing-and-report-scope)
- [Oracle releases, PDBs, and managed services](#oracle-releases-pdbs-and-managed-services)
- [Troubleshooting](#troubleshooting)
- [Access and entitlement](#access-and-entitlement)

## Choose the Oracle accounts

The workload account owns the benchmark tables and package. The reporting
account creates snapshots and generates AWR reports. These can be different
Oracle database users.

| Purpose | Where to enter the username | Where to enter its password |
| --- | --- | --- |
| Benchmark workload | `BALIO_USER` or `--user` | `BALIO_PASSWORD`, or the environment variable named by `--password-env` |
| Separate AWR reporting account | `--awr-user` | `BALIO_AWR_PASSWORD`, or the environment variable named by `--awr-password-env` |

**`BALIO_AWR_PASSWORD` must contain the Oracle database password for the exact
user passed to `--awr-user`.** It is not the Linux login password, an OCI
password, a wallet password, or the workload user's password unless that same
database user is intentionally used for reporting.

`AWR_REPORTER` in the examples below is an **example account name**, not an
Oracle built-in user. Ask your DBA for an existing account authorized to run
the required AWR operations, and replace `AWR_REPORTER` with that username.
BALIO does not create an AWR reporting account or grant AWR privileges.

On Autonomous Database, a service may allow these operations through `ADMIN`.
If your service permits this, use `--awr-user ADMIN` and enter **ADMIN's
database password** into `BALIO_AWR_PASSWORD`. Availability depends on the
service and its permissions. Keep the workload in its dedicated benchmark
schema. BALIO does not provide a `SYSDBA` connection mode.

## Run with a separate reporting account

Before running this example, install BALIO and deploy and seed the workload
schema as described in the [README](../README.md). Use Bash on the Linux client.
Replace the example host, service, and account names with your actual values.

### 1. Set the workload connection

```bash
export BALIO_DSN='dbhost.example:1521/benchmark_pdb'
export BALIO_USER='BALIO'

read -r -s -p 'Database password for Oracle user BALIO: ' BALIO_PASSWORD
echo
export BALIO_PASSWORD
```

Type the password for the workload schema at the hidden prompt. For a TNS
alias, set `BALIO_DSN` to that alias and configure `TNS_ADMIN` as described in
the README. An mTLS wallet does not itself replace the database username and
password.

### 2. Enter the reporting account's password

This example assumes your DBA has provided an account named `AWR_REPORTER`.
Change the prompt and the `--awr-user` value if the actual username differs.

```bash
read -r -s -p 'Database password for Oracle user AWR_REPORTER: ' BALIO_AWR_PASSWORD
echo
export BALIO_AWR_PASSWORD
```

### 3. Run and save the reports locally

```bash
balio run --workers 16 --duration 900 --warmup 120 \
  --awr snapshots --awr-format both --awr-scope instance \
  --awr-user AWR_REPORTER \
  --awr-password-env BALIO_AWR_PASSWORD \
  --output runs/workers-16
```

Use a fresh output directory for every run. After all commands that need these
credentials have finished, clear them from the shell:

```bash
unset BALIO_PASSWORD BALIO_AWR_PASSWORD
```

The password options take **environment variable names**, never literal
passwords. For example, `--awr-password-env REPORTING_DB_PASSWORD` tells BALIO
to read the exported variable `REPORTING_DB_PASSWORD`.

When `--awr-user` is set, `--awr-password-env` defaults to
`BALIO_AWR_PASSWORD`; specifying it above makes that mapping explicit. If the
selected password variable is unset or empty, BALIO prompts for the password
without echoing it in an interactive terminal. Noninteractive runs must supply
the exported variable. BALIO does not load `.env` files automatically.

There is no `BALIO_AWR_USER` setting: select the reporting username with
`--awr-user`. Setting `BALIO_AWR_PASSWORD` alone does not select a separate
reporting account.

## Reuse the workload account

If your DBA has authorized the workload account to perform the required AWR
operations, omit both `--awr-user` and `--awr-password-env`:

```bash
balio run --workers 16 --duration 900 --warmup 120 \
  --awr snapshots --awr-format both \
  --output runs/workload-account-awr
```

BALIO opens a separate reporting connection using the workload username and
password. In this case, it **does not read `BALIO_AWR_PASSWORD`**, even if that
variable is set.

## Options

| Option | Default | Behavior |
| --- | --- | --- |
| `--awr off` | `off` | Do not collect AWR reports. |
| `--awr snapshots` | — | Create snapshots before and after the measured workload. Bare `--awr` also selects this mode. |
| `--awr existing` | — | Use available snapshots; any exported report has partial coverage. See below. |
| `--awr-format both\|html\|text` | `both` | Write HTML, text, or both report formats. |
| `--awr-scope instance\|global` | `instance` | Write per-instance reports or a RAC global report for the participating instances. |
| `--awr-user USER` | Workload user | Use this Oracle database reporting account. |
| `--awr-password-env VARIABLE` | See credential rules above | Read the reporting password from this environment variable. |
| `--awr-dsn DSN` | Workload DSN | Connect the reporting account through another service in the same database. |
| `--awr-timeout-ms MILLISECONDS` | `180000` | Set the reporting connection's round-trip timeout. |

`--awr-dsn` changes only the reporting endpoint; it does not select a different
username or password. The reporting connection inherits the workload
connection's client mode and Oracle Net configuration.

For example, add `--awr-dsn 'dbhost.example:1521/cdb_reporting_service'` to the
separate-account run command when your DBA provides a reporting service in the
matching CDB root. A reporting connection into a different database or an
unrelated PDB is rejected.

### Existing snapshots

`--awr existing` does not create manual snapshots. It selects the latest common
available snapshot at each boundary:

- If no newer snapshot appears during the run, AWR collection fails.
- If a newer snapshot is available, BALIO can export a report, but records
  `awr.status: partial` and `awr.covers_run_end: false`.

The automatic ending snapshot may precede workload stop. To cover the full
run, use the recorded beginning snapshot and a later, post-run snapshot to
generate a report through an authorized reporting account or the service's
AWR interface. BALIO does not silently fall back from `snapshots` to
`existing`.

## Local files and run status

Reports are written under `<output>/awr/` on the **client running BALIO**.
For the first example, the output has this shape; angle-bracketed values are
replaced with the database and snapshot identifiers:

```text
runs/workers-16/
├── results.json
├── samples.jsonl
└── awr/
    ├── awr_<dbid>_instance_<number>_<begin_snap_id>_<end_snap_id>.html
    └── awr_<dbid>_instance_<number>_<begin_snap_id>_<end_snap_id>.txt
```

RAC global filenames use `global_<instance_numbers>` in place of
`instance_<number>`, for example `global_1_2`. Open the HTML file in a browser
or the text file in a text viewer. No additional SQL*Plus spool or file transfer
step is needed.

`results.json` records the AWR status, database ID, reporting container,
participating instances, snapshot IDs and timestamps, report paths, and errors
under `awr`. It records the measured workload timestamps under `measurement`.

| Result field | Interpretation |
| --- | --- |
| `workload_status` | Whether the workload completed, failed, or was interrupted. |
| `awr.status: complete` | The requested snapshot-mode collection and exports completed. |
| `awr.status: partial` | Existing snapshots produced a report that does not establish complete run coverage. |
| `awr.status: failed` | Requested AWR collection or report export failed. Inspect the recorded errors. |
| `status` | Overall result, including whether the requested AWR collection completed. |

Requested AWR collection that is partial or fails causes an overall failed
result and a nonzero exit status, even when the workload itself completed.
The workload measurements and any successfully exported reports are retained.

Report files are created with mode `0600` inside a new `0700` directory.
Incomplete file transfers are removed, and existing report files are never
overwritten.

## Snapshot timing and report scope

In `snapshots` mode, BALIO:

1. Connects the workers and verifies the reporting identity, APIs, and snapshot views.
2. Warms up the workload and pauses the workers at the measurement boundary.
3. Creates the beginning AWR snapshot.
4. Executes the measured workload.
5. Stops and joins all workload workers.
6. Creates the ending AWR snapshot and streams reports to local files.

Snapshot creation and report generation are outside the measured workload
interval. Snapshot boundary overhead, final batch completion, other sessions,
and background database work can still appear in the AWR interval.
**AWR describes activity in the reporting scope, not only BALIO sessions.**
Compare the recorded snapshot interval with BALIO's measurement interval when
interpreting rates.

`--awr-scope instance` produces each requested format for every database
instance used by the workers. `--awr-scope global` requests a RAC global report
containing the participating instance numbers. An instance missing from either
boundary, snapshot collection errors, a changed DBID, or an instance restart
makes AWR collection fail explicitly.

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
fallback to `DBA_HIST_WR_SETTINGS.LOCAL_AWRDBID`.

For a 12.1 PDB workload, use a reporting connection to the matching CDB root.
A CDB-root report includes broader CDB activity. See Oracle's
[multitenant AWR guidance](https://docs.oracle.com/en/database/oracle/oracle-database/12.2/tgdba/gathering-database-statistics.html),
[AWR_PDB view description](https://docs.oracle.com/en/database/oracle/oracle-database/12.2/refrn/awr_pdb_-views.html),
and [local repository settings](https://docs.oracle.com/en/database/oracle/oracle-database/12.2/refrn/DBA_HIST_WR_SETTINGS.html).

Autonomous Database and other managed services can restrict package operations,
catalog views, or CDB-root access. Support is capability-based: a platform label
cannot guarantee manual snapshots or every report format. Oracle documents
downloadable reports in
[Dedicated Autonomous Performance Hub](https://docs.oracle.com/en/cloud/paas/autonomous-database/dedicated/adbaa/observe-autonomous-ai-database-on-dedicated-exadata-1.html)
and [AWR Explorer](https://docs.oracle.com/en-us/iaas/database-management/doc/use-awr-explorer-analyze-database-performance.html).
These UI capabilities do not establish that an arbitrary SQL account can invoke
every AWR API.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Missing password in a noninteractive run | Export the variable named by `--awr-password-env`; its default is `BALIO_AWR_PASSWORD` when `--awr-user` is set. |
| Reporting login fails | Confirm that `--awr-user` names the intended database user and that the selected password variable contains that user's password. Check the service or TNS alias. |
| Required AWR APIs or views are unavailable | Have the DBA check reporting privileges, snapshot permissions, and the managed service's supported operations. A successful workload login does not establish AWR access. |
| Reporting database or container does not match | Point `--awr-dsn` at the workload's database and the same PDB, or its matching CDB root. |
| Oracle 12.1 PDB requires CDB-root reporting | Use an authorized reporting account and service for that PDB's CDB root. |
| No newer ending snapshot in `existing` mode | The run did not span an available newer snapshot. Use authorized manual snapshots or plan around the automatic snapshot interval. |
| Workload completed but the command failed | Inspect `results.json`: requested AWR collection may be partial or failed. Workload measurements remain available. |
| Output already exists | Choose a fresh `--output` directory. Prior report files are never overwritten. |

## Access and entitlement

Enable AWR only where your database or cloud service entitlement permits
Oracle Diagnostics Pack/AWR use. API accessibility or
`CONTROL_MANAGEMENT_PACK_ACCESS` settings do not establish entitlement.
BALIO does not change that setting or the snapshot schedule. See Oracle's
[licensing information](https://docs.oracle.com/en/database/oracle/oracle-database/26/dblic/Licensing-Information.html).

The reporting account needs permission to execute the requested
`DBMS_WORKLOAD_REPOSITORY` operations and read the relevant snapshot and
instance views. Oracle's [reporting privilege documentation](https://docs.oracle.com/en/database/oracle/oracle-database/26/tgdba/generating-automatic-workload-repository-reports.html)
lists `ADVISOR`, `SELECT_CATALOG_ROLE`, and package execution privileges for
reporting. Snapshot creation can need additional administrative permission.
Use the DBA-approved account supported by your service; the reporting
privileges alone are not a promise that snapshot creation will succeed.

BALIO's AWR paths have offline protocol and file-safety tests. They still
require live verification against the specific database release, RAC topology,
container configuration, and managed service before claiming compatibility.
