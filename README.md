# BALIO

**Balanced Logical and physical I/O for Oracle** — a Linux CLI for driving an Oracle database remotely through Oracle Net and Instant Client.

BALIO is an independent, original implementation inspired by SLOB's focus on database I/O. It uses indexed point reads and small updates, two working sets, server-side PL/SQL batches, concurrent sessions, and measured feedback. No SLOB code is incorporated. The name is a working project name, not a trademark clearance or Oracle endorsement.

**Status: version 0.2.0, implementation and offline tests available. No live Oracle database was available for validation.** The SQL compatibility target is Oracle **12.1/12.2, 18c, 19c, 21c, 23ai and 26ai**. This is not yet a claim of tested or certified compatibility. Service availability and supported database versions differ by provider.

## What BALIO does

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

## Install on a Linux client host

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

## Connect and check capabilities

Choose a service/PDB endpoint reachable from the Linux host. A private endpoint needs routing and Oracle Net access from that host; BALIO does not create network paths or provision cloud infrastructure.

```bash
export BALIO_DSN='dbhost.example:1521/benchmark_pdb'
export BALIO_USER='BALIO'
read -r -s -p 'Benchmark password: ' BALIO_PASSWORD; echo
export BALIO_PASSWORD
balio doctor
```

Passwords may also be entered interactively if the environment variable is absent. Never put a password into `--password-env`: that option takes the **name** of an environment variable. Use your normal secret injection mechanism for unattended execution. Do not embed credentials inside the DSN.

For Autonomous mTLS, use the service alias from its downloaded wallet and configure its Oracle Net files:

```bash
export TNS_ADMIN='/secure/path/Wallet_Benchmark'
export BALIO_DSN='benchmark_tp'   # choose an actual alias from tnsnames.ora
balio doctor --mode thick --config-dir "$TNS_ADMIN"
```

Use the wallet's required `sqlnet.ora`/wallet path configuration; some downloads contain a directory placeholder that must be adjusted. An mTLS wallet provides TLS client credentials and normally **still requires the database username/password**. `--external-auth` is for a separately configured external password store/authentication arrangement; it is not a substitute for an Autonomous mTLS wallet's database password. Thin-mode PEM wallets are available through the Python connection API; the CLI's documented wallet path uses Thick mode.

The per-round-trip timeout is `--call-timeout-ms` (default 60000). Initial connection establishment is governed by Oracle Net connection/retry settings in the DSN or `tnsnames.ora`; configure those to avoid lengthy connection hangs.

## Provision, deploy, seed

Use a dedicated benchmark schema in the target PDB. The required baseline privileges are `CREATE SESSION`, `CREATE TABLE`, `CREATE PROCEDURE`, and sufficient quota in an existing tablespace. The deployment uses only BALIO-prefixed objects and refuses unrecognized collisions. There is no `DROP USER`, buffer-cache flush, SYSDBA dependency, hidden parameter change, or automatic destructive cleanup.

If a DBA has already supplied the schema, skip `provision`. Otherwise connect with an account authorized to create users and grant the required privileges. For example, after securely setting `BALIO_ADMIN_PASSWORD` and `BALIO_SCHEMA_PASSWORD`:

```bash
balio provision --user ADMIN --password-env BALIO_ADMIN_PASSWORD \
  --schema BALIO --schema-password-env BALIO_SCHEMA_PASSWORD \
  --tablespace DATA --quota 2G
```

Use the actual administrative user and tablespace for the target (`DATA` is common on Autonomous; `USERS` is a typical self-managed example). Quota is explicit and finite. Provisioning creates a new user; it does not reset an existing user's password. Oracle DDL commits independently, so a partial provisioning failure lists what succeeded for the administrator to finish.

The small dataset below is a **functional smoke test**, not a storage-capacity test:

```bash
# BALIO_USER/BALIO_PASSWORD now identify the dedicated benchmark user.
balio deploy --payload-bytes 512
balio seed --hot-rows 10000 --cold-rows 100000 --payload-bytes 512 --batch-rows 1000
balio doctor
balio run --platform other --workers 2 --duration 15 --warmup 5 \
  --profile balanced --output results/smoke
balio report results/smoke
```

Seeding uses conventional logged inserts and a transactionally updated progress marker. Re-run the identical seed command after interruption. A different dataset size requires a **new dedicated schema**; BALIO will not silently truncate or resize a populated dataset. Seeding cost includes client payload generation, network transfer, index maintenance, redo and undo. It is not included in workload measurements. Payloads are varied printable ASCII; no claim of perfect incompressibility is made.

## Size for logical and physical pressure

Keep the total hot table/index working set comfortably within the effective database buffer-cache budget. For a physical-read test, provision a cold working set larger than the usable cache and run long enough to reach a stable observed read rate. A starting experiment might use several times the effective cache, but the correct size is measured, not universal. Shared cache, other workloads, RAC placement, Autonomous resource policy and storage caches change the result.

```bash
# Offline calculation only; does not seed or allocate storage.
balio plan --platform exacs --hot-rows 1000000 --cold-rows 100000000 \
  --payload-bytes 1000 --workers 16 --profile physical
```

That plan describes about **94.1 GiB of row payload**, before table/block overhead, primary-key indexes, undo, redo and headroom. After choosing quota and sizes for the actual database, use those values in a new schema's `seed` command. `dataset_info`/run results report allocated segment bytes when the database exposes them. The maximum row count per table is 2,147,483,646; choose payload size between 64 and 2000 bytes.

## Run controlled comparisons

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

## Concurrency and server infrastructure

`--workers 32` creates **32 concurrent workload sessions**, each with its own persistent Oracle connection and serial PL/SQL batches. The SQL, memory access, physical I/O, undo and redo execute on the Oracle server. The Linux host generates load and collects measurements; its local disks are not the benchmark target.

Concurrency is essential: too few sessions can leave database CPUs and storage queues underused. Beyond saturation, adding sessions can increase waits and latency without increasing throughput. A managed service may also queue connected workers. Client CPU and network delay can limit how much work reaches the server, so the result now includes `client.measured_phase_cpu_seconds`, `client.cpu_percent_of_one_core`, and start-skew measurements. These are client measurements, not database-host utilization.

See [concurrency and a repeatable sweep](docs/concurrency.md) for an experiment at increasing worker counts and how to identify driver bottlenecks. BALIO does not yet coordinate multiple driver hosts or rotate worker-specific RAC services as SLOB can.

## AWR reports downloaded to the client

After configuring the benchmark connection and securely supplying `BALIO_AWR_PASSWORD` for an authorized reporting account:

```bash
balio run --platform exacs --workers 32 --cold-pct 70 \
  --update-pct 10 --warmup 60 --duration 300 \
  --awr snapshots --awr-format both --awr-scope instance \
  --awr-user AWR_REPORTER --awr-password-env BALIO_AWR_PASSWORD \
  --output results/exacs-w32
```

The lifecycle is: connect/preflight → warmup → starting AWR snapshot → measured workload → wait for every worker to stop → ending AWR snapshot → download reports. Snapshot creation and report generation are excluded from BALIO's measured workload timer. The wider AWR interval includes snapshot overhead, unrelated activity and database background processes.

Reports are saved locally under `results/exacs-w32/awr/`, with separate `.html` and `.txt` files for participating instances. `--awr-scope global` requests a combined RAC report. DBID, container, participating instances, snapshot IDs/times, report paths and collection status are recorded in `results.json`. Report content is fetched through `DBMS_WORKLOAD_REPOSITORY` table functions over the existing Oracle client driver; SQL*Plus, SSH and a database-host filesystem download are not required.

Use `--awr-dsn` when the reporting account needs a different service in the **same database**, such as CDB root for a 12.1 PDB workload. It inherits the workload's client mode/network configuration. Reporting identity is checked against workload identity; root reporting includes other containers. AWR privileges and entitlement must be available for the target deployment; the tool does not grant them or enable management packs. AWR is off by default, and baseline metrics remain usable without it. See [Oracle's Diagnostics Pack rules](https://docs.oracle.com/en/database/oracle/oracle-database/26/dblic/Licensing-Information.html).

`--awr existing` is an explicitly partial diagnostic option: it uses existing common snapshots and cannot guarantee coverage of the run's end. It is never silently substituted for manual snapshots. Requested AWR failure or partial coverage makes the overall run return nonzero, while `workload_status` and measured results preserve whether the load itself completed. See the [AWR guide](docs/awr.md) for Autonomous/PDB restrictions, permissions and exact scope.

## Metrics and permissions

| Report field | Meaning |
|---|---|
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

## Autonomous, RAC and release caveats

For Autonomous, select a transaction/serial service such as an appropriate `TP` or `LOW` alias and record the exact choice. Resource Manager, session limits, queuing and autoscaling affect measurements. Warehouse/Lakehouse configurations may ignore optimizer hints. The workload asks for indexed serial reads and disables query result caching via hints, but inspect execution plans where privileges permit before calling a run a single-block I/O baseline.

On RAC, a service can spread workers across instances. Results include per-worker instance identity; cache fusion can satisfy a read from another instance without a storage read. Fix service placement for a per-node baseline, or deliberately measure cluster behavior. Writes use disjoint row ranges within one invocation, though adjacent rows can still share a block.

The 26ai product name can correspond to a **23.26.x** numeric build. BALIO records the reported version without requiring a nonexistent major-version 26. A service's supported release range is independent of this tool's SQL compatibility target.

## Verification and results

```bash
python -m unittest discover -s tests -v
python -m compileall -q balio tests
balio run --dry-run --platform exacs --target-physical-pct 20
```

Tests cover pure metric definitions, feedback behavior, failure handling, concurrency coordination, AWR lifecycle/export/scope, a simulated session backend, configuration validation and command behavior. Simulated sessions are **test fixtures**, not measured Oracle performance. A real acceptance run must compile the package, seed/resume, confirm data counts/plans, run read and update mixes, compare session counter deltas, verify unavailable telemetry handling and AWR reports, and repeat against each intended release/service. See [validation status and acceptance checklist](docs/validation.md).

Each run directory contains `results.json`, append-only `samples.jsonl`, and an `awr/` directory when reporting is enabled and preflight succeeds. Files are created with owner-only permissions. Existing run output is never overwritten. An interrupted/failed run retains completed intervals and records the incomplete status; after a successful starting snapshot, the collector attempts an ending diagnostic report even if the workload failed. `balio report PATH` renders the final JSON as text; `--json` prints the complete document. No result files from real Oracle runs are bundled with this project.

When finished, let the DBA retire the dedicated benchmark schema using the normal change process. BALIO deliberately has no automatic destructive cleanup command.
