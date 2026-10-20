# BALIO and SLOB: scope, concurrency, and validation

Reviewed on 20 October 2026. This comparison is based on the actual **SLOB
2.5.4.0** distribution, its author's documentation, and BALIO's source. It is a
feature and implementation review, **not measured evidence that BALIO is faster,
more accurate, or more scalable than SLOB**. BALIO still requires live acceptance
testing against the intended Oracle versions and services.

SLOB is an established Oracle infrastructure workload generator with substantial
field use. Its author originally distinguished it from an application benchmark:
it generates Oracle I/O to investigate platform behavior. Its current distribution
calls it the Simple Database I/O Testing Toolkit for Oracle Database. Both SLOB
and BALIO are Oracle-specific and can be hardware/cloud neutral; neither provides
cross-database-engine comparability or a TPC-style standardized score.
[Original author explanation](https://kevinclosson.net/2012/02/06/),
[official distribution](https://github.com/therealkevinc/SLOB_2.5.4).

## Client concurrency is fundamental

`balio run --workers N` establishes **N independent workload connections and
Oracle sessions**. Each worker sends PL/SQL batches to the database. SQL, buffer
gets, physical reads, updates, undo, and redo execute on the database server;
the Linux client coordinates work and receives counts and measurements. The
workload is not a disk benchmark of the Linux driver's local storage.

These are concurrent database sessions, not Oracle Parallel Execution servers.
Queries request serial execution. Workloads with more sessions can expose CPU
scaling, memory bandwidth, latch/mutex contention, storage queueing, redo/commit
limits, and RAC cache-fusion behavior. Too few sessions can leave infrastructure
underused; beyond saturation, additional sessions may increase latency with
little throughput gain. Connected sessions can also be queued or throttled by a
managed service; a requested worker count is not a guarantee that all workers
are simultaneously executing SQL.

BALIO uses a Python thread pool with a distinct connection per worker. It
synchronizes the start of each measurement window after the initial session
snapshots, and records worker start skew and client process CPU separately.
Synchronization reduces startup skew; it cannot make server scheduling,
network delivery, or resource-manager admission simultaneous.

For a capacity curve, run the same dataset and fixed workload at **1, 2, 4, 8,
16, 32, ... workers**, stopping at the useful saturation point or the service
limit. Repeat each point, retain variability, and compare throughput with
database wait latency, DB CPU, and client CPU. Current BALIO runs one selected
worker count per invocation; the CLI does not automatically orchestrate a
concurrency sweep or coordinate multiple driver hosts.

```bash
# Run separately at each selected worker count; use a new output path each time.
balio run --platform other --workers 16 --profile physical \
  --update-pct 0 --warmup 60 --duration 300 \
  --output results/physical-w16-repeat1
```

Client CPU, round-trip latency, batching, and network throughput still matter:
an underpowered or distant driver may fail to feed the server. Repeat a
representative point with a larger batch and/or a better-provisioned nearby
driver. If throughput changes substantially, investigate the driver before
claiming server saturation. Client process CPU is not database host CPU, and
batch latency is not single-row or single-I/O latency.

## Features compared

SLOB's documented concurrency model is schemas multiplied by threads per
schema: `runit.sh -s 16 -t 4` starts 64 workload sessions. The positional form
`runit.sh 16` uses `THREADS_PER_SCHEMA` from `slob.conf`. There is no need to
invent a `NUM_SCHEMAS` configuration parameter for this interface. The author
reports testing 12,288 sessions on an eight-node RAC cluster with think time;
that is evidence of experience at scale, not a guarantee for every deployment.
[SLOB 2.5.4 announcement](https://kevinclosson.net/2021/05/14/announcing-slob-2-5-4/).

| Area | SLOB 2.5.4.0 | BALIO |
|---|---|---|
| Concurrency | Multiple SQL*Plus processes; one or many schemas, threads per schema, synchronized trigger. | `--workers`; one benchmark schema, independent sessions, synchronized measurement windows. |
| Work location | Server-side PL/SQL workload loop; Linux starts and monitors clients. | Server-side PL/SQL batches; Linux schedules batches, samples counters, and changes mix. |
| Remote access | SQL*Net is already supported through administrative and workload services. | Oracle Net through python-oracledb, Thick/Instant Client by default; optional Thin mode. |
| RAC placement | Numbered service rotation using `SQLNET_SERVICE_BASE`/`SQLNET_SERVICE_MAX`. | One selected DSN per run; records session instance identity. No equivalent per-worker service rotation. |
| Hot/cold behavior | Working-set scale, cache configuration, hot spots, and hot-schema access. | Separate HOT/COLD tables; explicit probability of choosing COLD. |
| Adaptive physical mix | No feedback controller targeting an observed physical/logical ratio was found in the reviewed distribution. | Optional bounded feedback adjusts cold selection from measured session counters. |
| Read/write work | Range reads/updates, `UPDATE_PCT`, `WORK_UNIT`, and `REDO_STRESS`. | Indexed point reads and small numeric updates; update probability and commit frequency. |
| Additional workload modes | Think time, shared hot-schema access, configurable short table scans. | These equivalent modes are not implemented. |
| Database reports | Before/after snapshots; AWR text/HTML including RAC reports, or Statspack. | Optional AWR capture/export to local files; baseline interval counters work without AWR. No Statspack automation. |
| Results | AWR/Statspack, session statistics, OS collection and external-script hooks; helper scripts can extract tuples. | JSON/JSONL intervals, measured mix, null telemetry coverage, failure status, and batch histograms. |
| Seeding | Parallel/concurrent loading and optional obfuscated data. | Array-bound conventional inserts with deterministic payloads and committed resume markers. |
| Maturity | Established methodology and extensive reported field use. | Initial implementation with offline tests; no published real-database performance results. |

The SLOB feature descriptions above were checked in `slob.conf`, `runit.sh`,
`slob.sql`, `setup.sh`, and `misc/sql_net/README` inside the
[official archive](https://github.com/therealkevinc/SLOB_2.5.4/blob/main/2021.05.12.slob_2.5.4.0.tar.gz).
The author's [SLOB 2.3 announcement](https://kevinclosson.net/2015/07/12/announcing-slob-2-3-tarry-not-get-it-while-its-hot/)
describes single/multiple schema threads and both forms of hot spot;
the [release notes](https://github.com/therealkevinc/SLOB_2.5.4/blob/main/RELEASE_NOTES.txt)
describe scans, Statspack, external scripts, and redo changes.

SLOB already supports remote execution and local report generation. Those are
not inventions of BALIO. The reviewed SLOB scripts also use `tnsping` and Oracle
home report scripts such as `@?/rdbms/admin/awrrpti`; account for those client-side
dependencies when assessing a minimal Instant Client deployment. BALIO's AWR
path queries the reporting APIs and writes the returned content locally. See
[AWR setup and scope](awr.md).

## What BALIO adds, and what remains unproven

The most useful addition is a measured control loop. For example,
`--target-physical-pct 20` attempts to achieve:

```text
100 × delta("physical reads cache") / delta("session logical reads") = 20
```

This is a buffered block-read ratio, not a percentage of operations going
directly to SSD, a read/write ratio, or an IOPS rate. The controller can identify
an unmet target at its cold-mix limit. That makes a desired experiment explicit
and its achieved behavior auditable. It cannot guarantee an arbitrary target:
index accesses, cache size, cache fusion, write-related reads, and service policy
all affect the result.

JSON interval records, explicit missing counters, resumable seeding, optional
use of a pre-created schema, and a separate reporting connection are practical
workflow benefits. Deployment labels help organize results; they do not change
the workload or certify a provider.

These benefits come with tradeoffs. SLOB minimizes CPU overhead per unit of I/O
and keeps its main loop inside the server for the duration of the run. BALIO
re-enters PL/SQL for each batch, consumes payloads into a checksum, collects
session statistics, and coordinates interval boundaries. The additional cost
may matter most in logical-I/O and very low-latency tests. This is an inference
from the implementations and must be measured. Feedback also changes the
workload during the measured phase, so settling and sampling time must remain
visible. Use fixed-mix runs for primary platform capacity comparisons; examine
adaptive runs separately for achieved target, stability, and saturation.

## Why equal settings do not produce equal workloads

SLOB's loader deliberately produces sparse blocks and randomizes key placement.
Its workload accesses key ranges controlled by `WORK_UNIT`. BALIO uses ordinary
packed heap tables with sequentially seeded keys and one row per workload
operation. SLOB's updates modify character columns, with different redo payloads
for LITE/HEAVY, and commit per update statement. BALIO increments a numeric
column and commits after the configured number of updates or at batch end.
These facts were verified in the archive's `setup.sh`, `slob.sql`, and
`misc/procedure.sql`.

Consequently:

- Equal row counts do not mean equal allocated bytes or cache footprint.
- Equal update percentages do not mean equal redo bytes, commits, dirty blocks,
  or storage write IOPS.
- SLOB `WORK_UNIT` and BALIO `--batch-operations` are not equivalent controls.
- SQL executions/second and application operations/second cannot be treated as
  interchangeable scores across the tools.

Use Oracle's measured logical block accesses, physical read requests, bytes,
CPU, waits, and redo to describe each experiment. Even then, identify the
different workload shapes rather than presenting one number as a drop-in SLOB
score. Distinguishing blocks from requests is also critical for multiblock I/O;
the author illustrates this issue in
[Interpreting RAC IOPS with AWR](https://kevinclosson.net/2018/10/13/accurately-interpreting-real-application-clusters-iops-with-automatic-workload-repository-so-easy-to-get-so-wrong/).

## Acceptance protocol before claiming an improvement

1. **Validate execution first.** Compile/deploy on each intended database
   version/service; verify seed counts, resume behavior, SQL plans, actual
   session count and RAC placement, metrics permissions, and AWR scope. Record
   client, database/RU, and tool versions. Offline tests are not substitutes.
2. **Keep infrastructure fixed.** Use the same database shape, CPU allocation,
   memory, storage configuration, client location, network, service policy,
   autoscaling setting, and controlled competing load. Alternate tool/run order
   to reduce time-related bias.
3. **Measure the working set.** Compare actual table/index segment allocation
   and buffer-cache exposure, not row counts alone. Establish separate cached
   read, sustained physical-read, and update/redo experiments. Verify steady
   state and disclose compression and cache effects.
4. **Run concurrency curves.** Repeat fixed-mix measurements at increasing
   session counts using adequate warmup and duration. Report medians and spread,
   start skew, active/queued sessions, DB CPU, physical requests/s, logical
   blocks/s, bytes/s, redo, commits, wait latency, and driver utilization.
   Inspect the throughput/latency knee, not only maximum throughput.
5. **Quantify overhead.** Test batch-size sensitivity; compare matched intervals
   with and without optional reporting; inspect CPU per logical block access
   and per physical read. Use database latency measurements for I/O, because
   BALIO's batch histogram includes many operations and network/commit time.
6. **Reconcile reports.** Compare worker counters with the overlapping AWR
   interval and available server/cloud/storage metrics. Explain differences
   caused by other sessions, background writers, snapshot overhead, container
   scope, and caches. AWR is not exclusive to the benchmark.
7. **Test feedback separately.** Choose reachable and deliberately unreachable
   targets; retain all intervals and record convergence, oscillation, time
   within tolerance, and saturation. Compare repeated runs at different cache
   sizes and concurrency levels.
8. **Publish exact workload differences.** Keep full configurations, segment
   sizes, plans, errors, snapshot IDs and raw reports. Claim only improvements
   demonstrated by those measurements; retain SLOB as an external reference.

For the unmodified reviewed SLOB archive, `setup.sh` defaults `BLOCK_SZ` to 8192
while `runit.sh` defaults it to 16384. The author acknowledges this mismatch in
the comments on the [2.5.4 announcement](https://kevinclosson.net/2021/05/14/announcing-slob-2-5-4/).
For an 8 KiB test database, explicitly set `BLOCK_SZ=8192` and verify the effective
scale. Avoid comparing an accidentally reduced SLOB working set with BALIO.

The downloaded SLOB archive was used only for review and is not included in
BALIO's deliverables; no SLOB source was incorporated. Review artifact SHA-256:
`4a6df26e8ad19f0d8f0786bac710b311efbd55799bbbe1a68f792844c5df3c6b`.
