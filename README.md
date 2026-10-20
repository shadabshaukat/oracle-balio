# BALIO

**Balanced Logical and physical I/O for Oracle**

A Linux command-line benchmark for generating logical and physical I/O pressure on a remote Oracle database. BALIO runs indexed point reads and small updates through Oracle Net and Instant Client. The database server executes the workload; the client host controls concurrency and collects results.

BALIO is an independent implementation inspired by SLOB's focus on database I/O. No SLOB code is incorporated. The name is a working project name and does not imply Oracle endorsement.

> **Development status — v0.2.0:** implementation and offline tests are available. Live Oracle validation is still required. The SQL compatibility target is **12.1, 12.2, 18c, 19c, 21c, 23ai and 26ai**; these are not yet tested or certified compatibility claims. See [validation status](docs/validation.md).

## Contents

- [Features](#features)
- [Install on Linux](#install-on-linux)
- [Oracle accounts and credentials](#oracle-accounts-and-credentials)
- [Quick start](#quick-start)
- [Size the dataset](#size-the-dataset)
- [Workload profiles and controls](#workload-profiles-and-controls)
- [Client concurrency](#client-concurrency)
- [AWR reports](#awr-reports)
- [Metrics and permissions](#metrics-and-permissions)
- [Autonomous, RAC and release considerations](#autonomous-rac-and-release-considerations)
- [Results and verification](#results-and-verification)
- [Documentation](#documentation)

## Features

- Remotely creates a dedicated benchmark user if an authorized administrative connection is supplied, then deploys its tables and package into that user.
- Seeds deterministic varied row payloads using array-bound inserts; interrupted seeding resumes from committed progress with the same dimensions.
- Uses a small **HOT** table and a large **COLD** table. `--cold-pct` chooses the probability of using the COLD table; `--update-pct` independently chooses the probability of an update.
- Supports `logical` (0% cold), `balanced` (50% cold), and `physical` (100% cold) profiles. These names describe workload intent. The default smoke dataset can fit entirely in cache.
- Optionally adjusts the cold mix to approach a **measured buffered physical-read/logical-read ratio** using `--target-physical-pct`.
- Runs operations inside PL/SQL to reduce network round trips, with independent worker sessions and disjoint update row ranges. Reads share the selected table.
- Synchronizes workers after each window's starting snapshots; records client process CPU and worker start skew separately from database metrics.
- Optionally takes AWR boundary snapshots and streams HTML/text reports back to the Linux client's run directory, using a separate reporting connection.
- Writes JSON interval data, complete configuration, database/client identity, operation counts, counter deltas, batch-latency histograms, and a final report. Credentials and DSNs are excluded from reports.
- Has 36 researched deployment labels, including OCI, ExaCS, Exascale, Autonomous, Oracle services in AWS/Azure/Google Cloud, RDS, self-managed clouds, on-premises systems, and `other`.

Read the [deployment catalogue](docs/deployments.md) for the source-backed inventory and the [measurement methodology](docs/methodology.md) for definitions and limitations.

For the implementation comparison, read [BALIO versus SLOB](docs/slob-comparison.md). SLOB already offers remote clients, concurrency, hot spots and AWR reports. BALIO adds a measured-ratio feedback workflow, but its maturity, workload layout and overhead differ; there is no claim of proven performance superiority.

## Install on Linux

Use a 64-bit Python **3.10–3.14** environment and a matching Oracle Instant Client Basic/Basic Light installation for your OS and architecture. The project pins `python-oracledb==3.4.2` for a reproducible starting point that includes older releases in its documented test matrix. Validate any driver upgrade before comparing results.

Oracle's driver documentation lists Client 19 connectivity to Database 11.2 and later, Client 21 to 12.1 and later, and Client 23 to 19 and later. **Use a supported Instant Client 19 installation when one client must reach both 12c and current databases.** Exact support and patch combinations still require the applicable Oracle compatibility matrix. See [driver interoperability](https://python-oracledb.readthedocs.io/en/v3.4.2/user_guide/installation.html#supported-oracle-database-versions) and [Linux client initialization](https://python-oracledb.readthedocs.io/en/v3.4.2/user_guide/initialization.html).

```bash
cd balio
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .

# Use the actual directory installed on this client. Set before starting Python.
export LD_LIBRARY_PATH=/opt/oracle/instantclient_19_XX${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
# Alternative: configure the system dynamic loader using your normal OS process.

balio --help
balio platforms
```

Instant Client binaries and required OS packages such as the matching libaio package are not bundled. Install them using Oracle's instructions for the selected Linux distribution. On Linux, `--lib-dir` is deliberately rejected: use the dynamic loader configuration or `LD_LIBRARY_PATH`. The CLI defaults to **Thick** mode. `--mode thin` is available for compatible connections that do not need Instant Client; run it in a separate process.

## Oracle accounts and credentials

BALIO uses **Oracle database accounts**. These are not Linux users, OCI console users, or wallet passwords.

| Purpose | Which Oracle account to use | Where to enter the username | Where to enter its password |
| :--- | :--- | :--- | :--- |
| Deploy, seed and run the workload | A dedicated benchmark schema, such as `BALIO` | `BALIO_USER` or `--user` | Hidden prompt, or the exported variable `BALIO_PASSWORD` |
| Create the benchmark schema, if needed | An existing administrator authorized to create users and grant privileges | `provision --user YOUR_ADMIN_USER` | Hidden prompt, or a variable selected with `--password-env`, such as `BALIO_ADMIN_PASSWORD` |
| Set the new benchmark schema's password | The new user named by `provision --schema BALIO` | `--schema` | Hidden prompt, or `BALIO_SCHEMA_PASSWORD` selected by `--schema-password-env` |
| Collect AWR reports | An existing account authorized for AWR on the target database, such as a DBA-provided reporting account | `run --awr-user YOUR_REPORTING_USER` | Hidden prompt, or `BALIO_AWR_PASSWORD` |

**`BALIO_AWR_PASSWORD` must contain the database password of the user passed to `--awr-user`.** For example, `--awr-user AWR_REPORTER` means the password must be for `AWR_REPORTER`. `--awr-user ADMIN` means it must be the database password for `ADMIN`.

`AWR_REPORTER` is an example name, not a built-in Oracle user. BALIO does not create an AWR reporting account or grant AWR privileges. Ask the database administrator which existing account and service to use. See [AWR account selection](#1-select-an-awr-reporting-account).

### How password input works

- If the selected password variable is absent, BALIO prompts for the password in an interactive terminal. Input is hidden.
- For repeatable commands, enter the password with the Bash `read` examples below and export the variable. For unattended runs, inject it through your usual secret-management mechanism.
- `--password-env`, `--schema-password-env` and `--awr-password-env` take **environment variable names**, never literal passwords.
- BALIO does not automatically load a `.env` file. Variables must be exported in the shell that starts the CLI.
- `BALIO_DSN` / `--dsn` contains a service alias or connection endpoint, without a username or password.

All shell examples below use **Bash on the Linux client**. Replace example hosts, service aliases, account names, paths and tablespaces with your own values. Type passwords only at the hidden prompts.

## Quick start

### 1. Obtain or create a benchmark schema

Use a dedicated benchmark schema in the target PDB. If the DBA has already supplied one, continue to [configure the workload connection](#2-configure-the-workload-connection).

The schema needs `CREATE SESSION`, `CREATE TABLE`, `CREATE PROCEDURE`, and sufficient quota in an existing tablespace. Session statistics require the additional access described under [metrics and permissions](#metrics-and-permissions).

To create a schema remotely, use an **existing authorized administrative account**. The following uses `ADMIN` and the `DATA` tablespace, as commonly used on Autonomous; substitute the DBA-approved account and tablespace for your deployment. For a wallet connection, configure the [Autonomous wallet](#autonomous-mtls-wallet) first and use its service alias as `BALIO_DSN`.

```bash
export BALIO_DSN='dbhost.example:1521/benchmark_pdb'

# Existing Oracle administrative account's password.
read -r -s -p 'Database password for ADMIN: ' BALIO_ADMIN_PASSWORD
echo
export BALIO_ADMIN_PASSWORD

# Choose a password for the new Oracle benchmark user BALIO.
read -r -s -p 'New database password for BALIO: ' BALIO_SCHEMA_PASSWORD
echo
export BALIO_SCHEMA_PASSWORD

balio provision --user ADMIN --password-env BALIO_ADMIN_PASSWORD \
  --schema BALIO --schema-password-env BALIO_SCHEMA_PASSWORD \
  --tablespace DATA --quota 2G

unset BALIO_ADMIN_PASSWORD BALIO_SCHEMA_PASSWORD
```

On self-managed databases, `USERS` is a common tablespace example. The `2G` quota above is for the small smoke dataset; choose a larger finite quota for a larger test.

`provision` creates a new user; it does not reset an existing user's password or create an AWR account. Oracle DDL commits independently, so a partial failure lists what succeeded for the administrator to finish. The CLI does not support `SYSDBA` connections; do not use `SYS` or `sys as sysdba` in these examples.

### 2. Configure the workload connection

Choose a service/PDB endpoint reachable from the Linux host. A private endpoint needs routing and Oracle Net access from that host; BALIO does not create network paths or provision cloud infrastructure.

```bash
export BALIO_DSN='dbhost.example:1521/benchmark_pdb'
export BALIO_USER='BALIO'  # The Oracle schema that will own the benchmark objects.
read -r -s -p 'Database password for BALIO: ' BALIO_PASSWORD
echo
export BALIO_PASSWORD

balio doctor
```

Enter the existing `BALIO` user's database password at the prompt. If you just ran `provision`, enter the **same password you chose for `BALIO_SCHEMA_PASSWORD`**. Keep these workload credentials selected for `deploy`, `seed` and `run`.

Alternatively, omit the password variable and let each command prompt:

```bash
unset BALIO_PASSWORD
balio doctor --dsn 'dbhost.example:1521/benchmark_pdb' --user BALIO
# Database password: <type the BALIO database password; input is hidden>
```

`--user` and `--dsn` override `BALIO_USER` and `BALIO_DSN` for that command.

#### Autonomous mTLS wallet

For Autonomous mTLS, use the service alias from its downloaded wallet and configure its Oracle Net files:

```bash
export TNS_ADMIN='/secure/path/Wallet_Benchmark'
export BALIO_DSN='benchmark_tp'   # choose an actual alias from tnsnames.ora
balio doctor --mode thick --config-dir "$TNS_ADMIN"
```

Use the wallet's required `sqlnet.ora`/wallet path configuration; some downloads contain a directory placeholder that must be adjusted. An mTLS wallet provides TLS client credentials and normally **still requires the database username/password**. `--external-auth` is for a separately configured external password store/authentication arrangement; it is not a substitute for an Autonomous mTLS wallet's database password. Thin-mode PEM wallets are available through the Python connection API; the CLI's documented wallet path uses Thick mode.

The per-round-trip timeout is `--call-timeout-ms` (default 60000). Initial connection establishment is governed by Oracle Net connection/retry settings in the DSN or `tnsnames.ora`; configure those to avoid lengthy connection hangs.

### 3. Deploy, seed and run a smoke test

The small dataset below is a **functional smoke test**, not a storage-capacity test:

```bash
# Run as BALIO, using the workload credentials configured in step 2.
balio deploy --payload-bytes 512
balio seed --hot-rows 10000 --cold-rows 100000 --payload-bytes 512 --batch-rows 1000
balio doctor
balio run --platform other --workers 2 --duration 15 --warmup 5 \
  --profile balanced --output results/smoke
balio report results/smoke
```

Use a **new output directory for every run**; BALIO refuses to overwrite a previous result. Deployment uses BALIO-prefixed objects and refuses unrecognized collisions.

Seeding uses conventional logged inserts and a transactionally updated progress marker. Re-run the identical seed command after interruption. A different dataset size requires a **new dedicated schema**; BALIO will not silently truncate or resize a populated dataset. Seeding cost includes client payload generation, network transfer, index maintenance, redo and undo. It is not included in workload measurements. Payloads are varied printable ASCII; no claim of perfect incompressibility is made.

## Size the dataset

Keep the total hot table/index working set comfortably within the effective database buffer-cache budget. For a physical-read test, provision a cold working set larger than the usable cache and run long enough to reach a stable observed read rate. A starting experiment might use several times the effective cache, but the correct size is measured, not universal. Shared cache, other workloads, RAC placement, Autonomous resource policy and storage caches change the result.

```bash
# Offline calculation only; does not seed or allocate storage.
balio plan --platform exacs --hot-rows 1000000 --cold-rows 100000000 \
  --payload-bytes 1000 --workers 16 --profile physical
```

That plan describes about **94.1 GiB of row payload**, before table/block overhead, primary-key indexes, undo, redo and headroom. After choosing quota and sizes for the actual database, use those values in a new schema's `seed` command. `dataset_info`/run results report allocated segment bytes when the database exposes them. The maximum row count per table is 2,147,483,646; choose payload size between 64 and 2000 bytes.

## Workload profiles and controls

| Control | Purpose | Default |
| :--- | :--- | :--- |
| `--profile logical` | Select the hot pool for every operation | — |
| `--profile balanced` | Select the cold pool with 50% probability | Default profile |
| `--profile physical` | Select the cold pool for every operation | — |
| `--cold-pct PERCENT` | Set cold-pool selection probability; overrides the profile | From profile |
| `--update-pct PERCENT` | Select updates instead of reads, independently of pool selection | `0` |
| `--target-physical-pct PERCENT` | Adjust the cold mix toward a measured physical/logical ratio | Disabled |
| `--workers COUNT` | Number of concurrent workload sessions | `4` |

```bash
# Cache-focused read baseline.
balio run --platform oci-exadata-dedicated --profile logical \
  --workers 16 --warmup 60 --duration 300 --output results/logical

# Cold-pool read baseline. Verify achieved physical reads in the result.
balio run --platform exacs --profile physical \
  --workers 16 --warmup 60 --duration 300 --output results/physical

# 35% cold selections and 10% updates; these are separate controls.
balio run --platform oci-exadata-exascale --cold-pct 35 --update-pct 10 \
  --commit-every 100 --workers 16 --warmup 60 --duration 300 \
  --output results/mixed

# Feedback: attempt 20 buffered physical blocks per 100 logical block accesses.
balio run --platform oci-autonomous-serverless --cold-pct 50 \
  --target-physical-pct 20 --tolerance 2 --interval 5 \
  --controller-max-step 10 --workers 8 --warmup 60 --duration 300 \
  --output results/target20

# Any other reachable Oracle deployment.
balio run --platform other --deployment-label 'private-rac-19c' \
  --region 'customer-dc' --cold-pct 75 --output results/private
```

`balanced` means a 50/50 selection probability between the two tables. It does **not** promise equal measured logical and physical I/O: a physical read also involves logical block access, and an indexed row lookup commonly accesses several index/table blocks. Updates can add undo and other work. A 50% physical/logical target may be unreachable even with 100% cold selections.

The controller changes cold selection probability after each measured interval using bounded proportional feedback and a tolerance band. It reports adjusting, within tolerance, or saturation with an unmet target. Final assessment includes the achieved aggregate ratio, fraction of intervals within tolerance and saturation streak. Control settling remains in the measured results. Missing required counters stop feedback mode; fixed-mix mode can continue with unavailable counters explicitly recorded as null.

`--batch-operations` defaults to 1000. Larger batches reduce client overhead but delay feedback and cancellation and can overrun the requested interval/duration. A call timeout applies to a network round trip. Each write batch commits after `--commit-every` updates and commits any remainder at its end; read-only batches issue no commits. A failed batch may already have committed some updates, so BALIO does not retry it and marks the run incomplete.

For capacity curves, repeat the same seeded data/configuration at worker counts 1, 2, 4, 8, 16, etc.; repeat each point and retain raw files. Keep database/RU, service, shape, autoscaling, resource limits, client placement and concurrent load documented. This initial release uses one Linux driver host per run; multiple uncoordinated drivers are separate experiments and may contend on the same update ranges.

## Client concurrency

`--workers 32` creates **32 concurrent workload sessions**, each with its own persistent Oracle connection and serial PL/SQL batches. The SQL, memory access, physical I/O, undo and redo execute on the Oracle server. The Linux host generates load and collects measurements; its local disks are not the benchmark target.

Concurrency is essential: too few sessions can leave database CPUs and storage queues underused. Beyond saturation, adding sessions can increase waits and latency without increasing throughput. A managed service may also queue connected workers. Client CPU and network delay can limit how much work reaches the server, so results include `client.measured_phase_cpu_seconds`, `client.cpu_percent_of_one_core`, and start-skew measurements. These are client measurements, not database-host utilization.

See [concurrency and a repeatable sweep](docs/concurrency.md) for an experiment at increasing worker counts and how to identify driver bottlenecks. BALIO does not yet coordinate multiple driver hosts or rotate worker-specific RAC services as SLOB can.

## AWR reports

BALIO can take AWR snapshots immediately before and after the measured workload, then save **HTML and text reports on the Linux client**. AWR is optional and **off by default**.

### 1. Select an AWR reporting account

Keep `BALIO_USER` and `BALIO_PASSWORD` set to the dedicated workload schema. Use `--awr-user` to select a separate reporting account:

| Deployment | Account to pass to `--awr-user` | Password to enter for `BALIO_AWR_PASSWORD` |
| :--- | :--- | :--- |
| Self-managed Oracle, Exadata, ExaCS, Exascale or a similar service | An existing DBA-approved account allowed to create snapshots and generate AWR reports; `AWR_REPORTER` below is an example | That reporting account's Oracle database password |
| Autonomous Database | A DBA-approved account supported by the service; use `ADMIN` only where it is authorized and the required AWR APIs are available | The selected account's Oracle database password; if using `--awr-user ADMIN`, use the `ADMIN` password |

The account needs permission to execute the requested `DBMS_WORKLOAD_REPOSITORY` operations and read the relevant snapshot and instance views. Report generation privileges alone may not permit manual snapshot creation. BALIO does not create this account, grant its permissions, or enable management packs. See the [AWR access requirements](docs/awr.md#access-and-entitlement) and [Oracle's Diagnostics Pack rules](https://docs.oracle.com/en/database/oracle/oracle-database/26/dblic/Licensing-Information.html).

### 2. Enter the reporting password and run

The following example assumes the DBA has supplied an account named **`AWR_REPORTER`**. Replace that username with the actual reporting account before running the command. Enter **that account's password** at the prompt:

```bash
# Existing workload connection:
# BALIO_DSN      = target service/PDB
# BALIO_USER     = BALIO
# BALIO_PASSWORD = password of the Oracle user BALIO

read -r -s -p 'Database password for AWR_REPORTER: ' BALIO_AWR_PASSWORD
echo
export BALIO_AWR_PASSWORD

balio run --platform exacs --workers 32 --cold-pct 70 \
  --update-pct 10 --warmup 60 --duration 300 \
  --awr snapshots --awr-format both --awr-scope instance \
  --awr-user AWR_REPORTER --awr-password-env BALIO_AWR_PASSWORD \
  --output results/exacs-w32

unset BALIO_AWR_PASSWORD
```

For an authorized Autonomous `ADMIN` reporting connection, use `--awr-user ADMIN` instead and enter the **ADMIN database password** into `BALIO_AWR_PASSWORD`. The workload still runs as `BALIO`.

**Credential selection rules:**

- With `--awr-user`, BALIO reads `BALIO_AWR_PASSWORD` by default. The explicit `--awr-password-env BALIO_AWR_PASSWORD` above documents that choice.
- If that variable is absent, BALIO prompts with `AWR reporting password:` in an interactive terminal.
- If both `--awr-user` and `--awr-password-env` are omitted, the reporting connection **reuses the workload username and password**. This works only when that user is also authorized for AWR.
- Exporting `BALIO_AWR_PASSWORD` alone does not select an AWR account or override the workload password. There is no automatically read `BALIO_AWR_USER` variable.

### 3. Find the local reports

Paths are relative to the directory where you start BALIO unless `--output` is absolute. The example above produces a directory shaped like this; angle-bracket fields represent database values:

```text
results/exacs-w32/
├── results.json
├── samples.jsonl
└── awr/
    ├── awr_<dbid>_instance_<number>_<begin_snap>_<end_snap>.html
    └── awr_<dbid>_instance_<number>_<begin_snap>_<end_snap>.txt
```

Instance scope creates reports for each participating instance. Global scope creates a combined RAC report. The `awr` section of `results.json` records the report paths, DBID, container, participating instances, snapshot IDs/times and collection status.

BALIO fetches report content through the Oracle driver using `DBMS_WORKLOAD_REPOSITORY` table functions. **SQL*Plus, SSH, a database-host file copy and an Oracle `DIRECTORY` object are not required.** No real database reports are bundled with the project.

### AWR options and snapshot boundaries

| Option | Behavior |
| :--- | :--- |
| `--awr off` | Default. No AWR collection. |
| `--awr snapshots` | Create a snapshot after warmup and another after all workload workers stop; export reports between them. |
| `--awr existing` | Use existing snapshots. Coverage is explicitly partial; this is not a substitute for a fully bracketed run. |
| `--awr-format both` | Save HTML and text. Alternatives: `html`, `text`. |
| `--awr-scope instance` | Default. Report separately for each participating database instance. |
| `--awr-scope global` | Request a combined RAC report across participating instances. |
| `--awr-dsn SERVICE` | Use another reporting service in the **same database**, such as its CDB root. |
| `--awr-timeout-ms 180000` | Default timeout per reporting connection round trip. |

The lifecycle is **preflight → warmup → beginning snapshot → measured workload → stop all workers → ending snapshot → local report export**. Snapshot creation and report generation are excluded from BALIO's measured workload timer. The broader AWR interval includes snapshot overhead, other sessions and database background activity.

`--awr-dsn` changes only the reporting endpoint. It inherits the selected reporting credentials and the workload's client mode/network configuration. For a **12.1 PDB** workload, use an authorized reporting connection to the matching CDB root; root reports include other containers. PDB-local AWR is available from 12.2, subject to privileges and service restrictions.

Requested AWR failure or partial coverage makes the overall command return **nonzero**, even if the workload completed. Measured results remain available, and `workload_status` distinguishes load completion from AWR collection status. With `--awr existing`, no newer snapshot means failure; a newer available snapshot still produces a partial report because it cannot guarantee coverage through workload stop. There is no silent fallback.

See the [complete AWR guide](docs/awr.md) for permissions, RAC/PDB validation, managed-service restrictions and troubleshooting.

## Metrics and permissions

| Report field | Meaning |
| :--- | :--- |
| `operations_per_s` | Completed row operations per measured worker-window second. |
| `logical_block_accesses_per_s` | Delta of `session logical reads` per second; the usual logical-I/O rate. |
| `physical_read_requests_per_s` | Delta of `physical read IO requests` per second from worker sessions. |
| `physical_read_total_requests_per_s` | The separate `physical read total IO requests` statistic; not silently substituted for the preceding field. |
| `physical_read_blocks_per_s` | Delta of `physical reads` per second; **blocks, not requests**. |
| `physical_to_logical_pct` | `100 × physical reads cache / session logical reads`. |
| `physical_read_bytes_per_s` | Foreground physical-read throughput in bytes/s. |
| `redo_bytes_per_s` | Session redo generation; not storage write IOPS. |
| `session_cpu_seconds` | Session CPU statistic converted from centiseconds. |
| `batch_latency` | Client-observed PL/SQL call latency including network/commit time, with log2-histogram percentile upper bounds; **not per-row latency**. |

Main rates use summed active measured windows. `wall_rates` and `operations_per_wall_second` also include sampling/coordinator gaps in the measured phase. Both exclude setup and warmup. Snapshot SQL has small measurement overhead. Counters missing from any worker or decreasing across snapshots become null rather than misleading zeroes or partial totals.

Each worker reads its own counters from `V$MYSTAT` joined to `V$STATNAME`. On a self-managed database an authorized DBA can grant the minimum required access, in the appropriate PDB:

```sql
GRANT SELECT ON SYS.V_$MYSTAT TO BALIO;
GRANT SELECT ON SYS.V_$STATNAME TO BALIO;
```

`provision --grant-metrics` attempts these grants only when requested. Managed services can prohibit them. Use `doctor` to check actual access. Have the service administrator use only the documented permission path; some Autonomous deployments allow broader catalog roles, which are not automatically granted by BALIO.

Database physical reads may be served by Exadata flash, PMEM/XRMEM or another cache. Session counters do not identify the underlying device's IOPS. Asynchronous DBWR/LGWR writes cannot be attributed to the workload by summing the foreground sessions' write counters. Use optional AWR, the [system telemetry query](balio/sql/system_telemetry.sql), and authorized cloud/Exadata/storage telemetry for a broader infrastructure view; instance-wide observations include other activity. The default workload does not invoke AWR or ASH. AWR is explicitly opt-in and requires the target's applicable entitlement and privileges. See [Oracle counter definitions](https://docs.oracle.com/en/database/oracle/oracle-database/26/refrn/statistics-descriptions-2.html).

## Autonomous, RAC and release considerations

For Autonomous, select a transaction/serial service such as an appropriate `TP` or `LOW` alias and record the exact choice. Resource Manager, session limits, queuing and autoscaling affect measurements. Warehouse/Lakehouse configurations may ignore optimizer hints. The workload asks for indexed serial reads and disables query result caching via hints, but inspect execution plans where privileges permit before calling a run a single-block I/O baseline.

On RAC, a service can spread workers across instances. Results include per-worker instance identity; cache fusion can satisfy a read from another instance without a storage read. Fix service placement for a per-node baseline, or deliberately measure cluster behavior. Writes use disjoint row ranges within one invocation, though adjacent rows can still share a block.

The 26ai product name can correspond to a **23.26.x** numeric build. BALIO records the reported version without requiring a nonexistent major-version 26. A service's supported release range is independent of this tool's SQL compatibility target.

## Results and verification

```bash
python -m unittest discover -s tests -v
python -m compileall -q balio tests
balio run --dry-run --platform exacs --target-physical-pct 20
```

Tests cover pure metric definitions, feedback behavior, failure handling, concurrency coordination, AWR lifecycle/export/scope, a simulated session backend, configuration validation and command behavior. Simulated sessions are **test fixtures**, not measured Oracle performance. A real acceptance run must compile the package, seed/resume, confirm data counts/plans, run read and update mixes, compare session counter deltas, verify unavailable telemetry handling and AWR reports, and repeat against each intended release/service. See [validation status and acceptance checklist](docs/validation.md).

Each run directory contains `results.json`, append-only `samples.jsonl`, and an `awr/` directory when reporting is enabled and preflight succeeds. Files are created with owner-only permissions. Existing run output is never overwritten. An interrupted/failed run retains completed intervals and records the incomplete status; after a successful starting snapshot, the collector attempts an ending diagnostic report even if the workload failed. `balio report PATH` renders the final JSON as text; `--json` prints the complete document. No result files from real Oracle runs are bundled with this project.

When finished, let the DBA retire the dedicated benchmark schema using the normal change process. BALIO deliberately has no automatic destructive cleanup command.

## Documentation

| Guide | Contents |
| :--- | :--- |
| [AWR reports](docs/awr.md) | Reporting credentials, snapshots, local exports, privileges and container scope |
| [Concurrency](docs/concurrency.md) | Worker-count sweeps, saturation and client bottlenecks |
| [Measurement methodology](docs/methodology.md) | Workload design, feedback controls and interpretation of metrics |
| [Deployment catalogue](docs/deployments.md) | OCI, multicloud, on-premises and other Oracle deployment choices |
| [SLOB comparison](docs/slob-comparison.md) | Reviewed SLOB capabilities, BALIO differences and limitations |
| [Validation status](docs/validation.md) | Offline test coverage and live database acceptance checklist |
