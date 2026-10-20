# BALIO measurement methodology

BALIO means **Balanced Logical and physical I/O for Oracle**. It is an independent, SLOB-inspired workload generator. It is not an Oracle product, a SLOB release, a transactional application benchmark, or a certification of a cloud provider's storage specifications.

The starting design uses ordinary SQL and PL/SQL over Oracle Net. A Linux client creates and seeds benchmark objects in an existing, dedicated schema, then drives concurrent sessions remotely. No database-host agent is required. This is a portable way to exercise database CPU, buffer access, Oracle read I/O, and optionally redo-producing updates. The SLOB author's original explanation likewise separates exercising Oracle's I/O and internal concurrency from measuring application scaling. See the [original SLOB introduction](https://kevinclosson.net/wp-content/uploads/2012/05/readme-first.pdf) and [official distribution](https://github.com/therealkevinc/SLOB_2.5.4).

## Two controls, two different meanings

The **cold-operation percentage** chooses how many operations draw from a large key range instead of a small hot range. At 0%, operations repeatedly visit the hot range; at 100%, operations visit the large range. This is an input distribution. Calling a range cold does not make its blocks absent from cache.

The optional **target physical-read fraction** is a feedback target:

```text
buffered physical-read fraction =
    sum(worker delta physical reads cache)
    / sum(worker delta session logical reads)
```

For example, a target of 0.20 asks for approximately 20 buffered physical block reads per 100 logical block reads. It does not ask for 20% of SQL statements to perform storage I/O, nor for physical I/O to consume 20% of elapsed time. Logical work and physical work overlap: a cache miss can incur both.

BALIO uses a bounded proportional adjustment with a deadband: increase the cold-operation percentage when the measured fraction is below target, and decrease it when above target. The actuator remains between 0% and 100%. Each interval records the input mix and observed result. This is a best-effort controller, not a guarantee of a prescribed hardware IOPS rate.

Reaching an actuator limit while missing the target means **target unmet for this run/configuration**. It is not proof that the target can never be attained. A larger dataset, smaller hot range, longer settling period, altered concurrency, or different service resource limits may change the attainable range. Report within-tolerance intervals and sustained saturation separately; do not label an overall average alone as stable convergence.

## What the counters measure

Use deltas of named statistics, never hard-coded statistic numbers. Oracle's names distinguish blocks from requests:

| Reported quantity | Formula over the measured interval |
|---|---|
| Logical block reads/s | `delta session logical reads / elapsed seconds` |
| Oracle application read IOPS | `delta physical read IO requests / elapsed seconds` |
| Physical blocks read/s | `delta physical reads / elapsed seconds` |
| Buffered physical blocks read/s | `delta physical reads cache / elapsed seconds` |
| Oracle application read MiB/s | `delta physical read bytes / elapsed seconds / 1048576` |
| Buffered physical-read fraction | `delta physical reads cache / delta session logical reads` |

`session logical reads` includes consistent gets and current gets. One read request may transfer several blocks. `physical reads` includes direct and private-buffer activity as well as buffer-cache reads; therefore the controller specifically uses `physical reads cache`. Total-instance request statistics include non-application work and should not silently replace application/session counters. See [Oracle statistic definitions](https://docs.oracle.com/en/database/oracle/oracle-database/26/refrn/statistics-descriptions-2.html).

The fraction is a descriptive ratio for this workload, not an exact buffer-cache hit ratio. Prefetch, recursive SQL, index access, block reuse, and sampling overhead affect it. A zero denominator means unavailable, not zero. Unexpected ratios must remain visible with their raw counters rather than being silently clipped.

Collect start/end snapshots in each worker connection from `V$MYSTAT` joined to `V$STATNAME`; [`V$MYSTAT` represents the current session](https://docs.oracle.com/en/database/oracle/oracle-database/19/refrn/V-MYSTAT.html). Aggregate the deltas across workers. Sum counts before calculating a ratio. Do not average worker ratios or add each worker's counts-per-own-duration to create a run-wide rate. Each sample uses the shared interval from its earliest worker start to its latest worker end. The aggregate active-window rate uses the sum of these intervals; whole-measured-phase rates should additionally include coordination gaps between windows. Keep the denominators explicit. Snapshots themselves introduce a small amount of work; a long interval and a no-work calibration quantify that overhead.

If any required worker counter is inaccessible or absent, report the affected metric as unavailable. Fixed-mix operation can still exercise the database, but feedback control must fail explicitly because it cannot verify the requested balance. Do not fabricate zeros. Worker failure, counter reset, or reconnect invalidates the associated interval. Failover/replay should be treated as a separate resilience test, not quietly included in an infrastructure throughput result.

## Physical at the database boundary

An Oracle physical read is not proof of a platter or NAND access. The request can be served by operating-system, hypervisor, storage-controller, Exadata flash, or Exadata RDMA memory caches. Oracle documents flash and XRMEM as storage-side acceleration tiers. See [Exadata Smart Flash Cache](https://www.oracle.com/database/technologies/exadata/software/smartflashcache/).

Consequently, BALIO reports **Oracle-observed read IOPS**. Measuring device-media IOPS, storage replication writes, or the exact tier servicing each read needs separate infrastructure telemetry. Exadata counters such as optimized requests need careful interpretation: they can cover flash-served requests and reads avoided by storage indexes or columnar caches. See [Exadata monitoring definitions](https://docs.oracle.com/en/engineered-systems/exadata-database-machine/sagug/system-software-users-guide-exadata-database-machine-sagug.pdf).

The initial workload is random indexed access. It does not claim to benchmark Smart Scan throughput, full-scan bandwidth, vector search, SQL optimization, bulk direct-path I/O, or storage endurance. Those require distinct workloads and denominators. Do not mix their results into this controller's fraction.

In RAC, blocks can arrive from another instance's cache. Such transfers are neither a local memory hit nor necessarily a storage read. Record service/instance placement and, for a full RAC investigation, independently capture global-cache traffic and waits. Oracle explains this distinction in [RAC performance monitoring](https://docs.oracle.com/en/database/oracle/oracle-database/19/racad/monitoring-performance.html).

## Workload and dataset discipline

1. Seed a dedicated schema using the supplied deployment path. Save row counts, payload size, segment allocation, database block size where visible, and the seed/configuration. Measure actual segment footprint rather than equating payload bytes with allocated storage.
2. Start with a hot working set comfortably below the usable buffer cache and a large working set several times larger than that cache. These are starting assumptions to verify, not universal ratios. A shared or automatically resized cache may be unknowable to the benchmark user.
3. Use repeatable random key selection. Consume a non-index payload column so an index-only plan cannot make the table footprint irrelevant. Avoid highly repetitive payloads when assessing compression/deduplication-sensitive infrastructure; record any unavoidable pattern.
4. Execute many SQL operations per PL/SQL call to amortize network round trips. One remote call per row can benchmark network latency and Python overhead instead of the server. Larger batches improve driving efficiency but reduce stop responsiveness and controller reaction speed.
5. Exclude a declared warmup phase, then inspect interval stability. A timed random warmup does not prove that every hot block was touched or that the storage tiers reached steady state.
6. Begin with read-only runs. Treat update percentage and commit batch size as separate controls; they alter undo, redo, locking and log-file-sync cost. Foreground session write counters do not capture all DBWR/LGWR writes, so update throughput is not total storage write IOPS. Within one run, workers update disjoint contiguous key slices to avoid same-row write contention; rows at slice boundaries can still share blocks, and multiple simultaneous runs are not mutually isolated.
7. Never require cache flushing, hidden parameters, changing buffer-cache size, or turning off database safeguards. If an administrator elects to run special cold-cache experiments, publish them separately and record the intervention.

## Autonomous Database and managed services

The portable path uses an ordinary benchmark schema and SQL access only. ADB's ADMIN account does not have SYSDBA. Granting arbitrary privileges on SYS-owned objects can also be restricted; have the service administrator use the documented permissions available on that service, and capability-test metrics rather than assuming access. Oracle documents `SELECT_CATALOG_ROLE` for dictionary access, but it is broader than a narrowly scoped self-managed grant. See [ADMIN privileges](https://docs.oracle.com/en/cloud/paas/autonomous-database/serverless/adbsb/autonomous-admin-user-roles.html).

Use a serial service such as TP or LOW for the baseline, and record the selected service. HIGH/MEDIUM allow parallel work and queuing. Lakehouse configurations may ignore hints by default. These choices can change what the same SQL measures. Record ECPU/OCPU allocation, autoscaling state, workload type, service and competing activity alongside the result. See [ADB services](https://docs.oracle.com/en/cloud/paas/autonomous-database/serverless/adbsb/predefined-database-services-names.html) and [optimizer hint behavior](https://docs.oracle.com/en/cloud/paas/autonomous-database/serverless/adbsb/manage-optimizer-stats.html).

Database CPU limits, I/O resource management, storage service quotas and network placement are part of the measured environment. A low result does not by itself identify a hardware bottleneck. Run the Linux driver close to the database, record round-trip latency, monitor driver CPU, and increase driver capacity if it saturates before the server.

## Compatibility contract

The SQL/PLSQL design targets Oracle Database 12.1 and 12.2, 18c, 19c, 21c, 23ai and Oracle AI Database 26ai. This is intended source compatibility until live tests pass on each version and service; it is not a statement that every cloud offers every version, or that old database releases retain vendor support.

For the Instant Client path, current python-oracledb documentation states that Client 19 connects to Database 11.2 or later, Client 21 to 12.1 or later, and Client 23 to 19 or later. Client 19 is therefore the conservative common client family for the requested 12c-through-26ai scope, subject to the chosen Linux architecture, patch level, authentication and TLS requirements. Pin a tested stable driver/client combination and consult Oracle's interoperability matrix for certification. The latest documentation's tested-version table is distinct from supported network interoperability. See [driver installation and interoperability](https://python-oracledb.readthedocs.io/en/latest/user_guide/installation.html) and [Oracle Instant Client FAQ](https://www.oracle.com/database/technologies/faq-instant-client.html).

Do not reject 26ai because `Connection.version` begins with `23`. Oracle identifies 26ai builds such as `23.26.0`; release branding and technical version numbering differ. Save the raw version and database banner when available. See the [26ai certification report](https://www.oracle.com/a/ocom/docs/oracle-ai-database-26ai-certification-report.pdf) and [Oracle Database FAQ](https://www.oracle.com/database/faq/).

## Validation before publishing performance claims

The initial implementation is a prototype until tested against a real database. Unit tests cannot validate PL/SQL compilation, execution plans, Oracle counter semantics, managed-service privileges, or the load a target can sustain.

The acceptance campaign should cover:

| Area | Evidence required |
|---|---|
| Versions | Deploy, seed, read, mixed update, stop and cleanup on 12.1, 12.2, 19c and a 26ai build; add 18c/21c/23ai where available. |
| Services | At least self-managed single-instance, RAC/Exadata, ADB serverless and dedicated; each advertised cloud profile needs its own live smoke test. |
| Isolation | Unique objects/schema; cleanup removes only benchmark-owned objects; interrupted seed is detectable and repairable. |
| Execution | Compiled package is VALID; real plan accesses the payload; selected service runs the intended serial workload; no unexpected scans/parallel slaves. |
| Counting | Manually reconcile one worker's start/end counters; reconcile sum-of-workers against an isolated-system observation; verify units and denominators. |
| Control | Repeated hot/cold endpoint runs establish an empirical range; interior targets converge within tolerance; an excessive target remains visibly unmet; missing telemetry cannot yield success. |
| Timing | Warmup excluded; shared interval recorded; no-work sampling overhead quantified; client saturation and network round trips assessed. |
| Updates | Updates commit as configured; no lost operation accounting; row contention and redo/log waits identified independently from read I/O. |
| Failures | Wrong credentials, exhausted quota, missing grants, killed session, interruption, timeout and invalid object produce clear non-success outcomes. |
| Repeatability | At least three measured repetitions per point; publish spread, intervals, errors, raw deltas and exact configuration, not only the best score. |

Keep the measured-results matrix separate from this intended compatibility scope. Do not claim a completed 12c–26ai or multicloud certification until the corresponding live evidence exists.

Research checked 20 October 2026. Provider offerings, grants, branding and support policies can change.
