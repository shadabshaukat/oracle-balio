# Client concurrency and infrastructure testing

BALIO runs on a Linux client and executes its benchmark SQL inside the remote Oracle database. The client is the load generator. Database buffer access, SQL/PLSQL execution, read requests, updates, undo, redo and commits happen on the target database and its infrastructure. The client machine does not need access to the database server filesystem.

## What `--workers` controls

`--workers N` opens **N independent, persistent Oracle connections** and drives them concurrently from a Python thread pool. The default is four. Each worker executes one server-side PL/SQL batch at a time and submits another batch after that call returns. The workers keep their connections throughout warmup and measurement.

At the start of each workload window, workers take their starting session-counter snapshots and wait until every worker is ready. A barrier then releases them together. Scheduling still creates a small start difference; every sample records `worker_start_skew_ms`, and the run records the largest observed difference as `client.max_worker_start_skew_ms`. An unexpected exception while taking a starting snapshot aborts the barrier so peers do not remain waiting for a worker that has failed. Fixed-mix runs can still proceed when the database adapter reports counters as unavailable; feedback mode requires its control counters.

This is a closed-loop workload: the completion time of a batch determines when that worker can submit the next batch. BALIO does not currently offer a separate request-rate limit, think time, open-loop arrival schedule, or a coordinated pool of multiple Linux driver hosts.

There is no one-to-one mapping between a worker and a database CPU. A connected session might execute, wait for storage or a commit, wait for CPU, or be queued by the database service. RAC services can place different workers on different instances. Session counts alone cannot establish how many sessions were actively consuming database CPU. Oracle's [performance measurement guide](https://docs.oracle.com/en/database/oracle/oracle-database/19/tgdba/measuring-database-performance.html) distinguishes CPU time, wait time, and active sessions.

The implementation is in [`runner.py`](../balio/runner.py), with remote batch calls in [`db.py`](../balio/db.py) and database-side SQL in [`schema.sql`](../balio/sql/schema.sql).

## Why concurrency changes the result

A single session often cannot keep all available database CPUs and storage queues busy. Increasing concurrency exposes more independent work and can raise throughput until a resource or contention limit is reached. Beyond that point, additional workers can increase latency and waits without increasing useful throughput.

The limiting resource depends on the workload:

| Workload | Useful observations as workers increase |
|---|---|
| Mostly cached reads | Database CPU, logical reads/s, buffer-related contention, client driving capacity. |
| Mostly physical reads | Oracle physical-read requests/s, read latency, storage queues, storage/cache tier behavior. |
| Updates | Redo generation, commit latency, undo work, DBWR/LGWR activity, block/row contention. |
| RAC | Worker instance placement, global-cache traffic and waits, per-instance CPU and I/O. |
| Managed service | Resource Manager queuing, CPU allocation, service limits and autoscaling behavior. |

Higher worker counts can also change the observed physical/logical balance by changing cache reuse and competition. Repeat the concurrency sweep for each workload profile. For an experiment using feedback, retain the interval history: the controller may change the input mix while concurrency changes the resulting I/O rates.

## Separate client limits from database limits

PL/SQL batching reduces network round trips: with `--batch-operations 1000`, one worker call requests 1000 row operations, rather than making 1000 separate client calls. The SQL still executes in the database.

For a simplified steady-state approximation with equal workers:

```text
row operations per second ≈
    workers × operations per batch
    / (server batch time + network time + client time per batch)
```

This is an explanatory model, not a formula for predicting an Oracle benchmark score. It shows why a remote driver can still limit the load even though the measured SQL runs on the server.

Record the Linux driver's CPU count, CPU utilization, memory pressure and network placement alongside database measurements. Use ordinary host monitoring during a run. For example, on Linux systems with the corresponding tools installed, `pidstat` can monitor the BALIO process and `sar` can monitor host network activity. The pinned [python-oracledb execution guide](https://python-oracledb.readthedocs.io/en/v3.4.2/user_guide/sql_execution.html#parallelizing-queries) confirms that concurrent SQL execution requires multiple connections and identifies Python threading/GIL behavior as a potential limit.

BALIO records the client OS, architecture, Python version, visible logical CPU count, requested workers and connected workers. It also records `client.measured_phase_cpu_seconds` across the client process's threads and `client.cpu_percent_of_one_core`. A value of 100% means an average of one CPU core of process CPU over the measured phase; the value can exceed 100%. These measurements exclude connection setup, warmup and AWR collection, while including the measured phase's sampling and coordination. They do not measure host-wide CPU utilization, memory pressure, network utilization or database server CPU.

If a result plateaus, check database utilization and waits before attributing the plateau to storage or CPU. Compare the same test with a larger batch size or a driver placed closer to the database. If those changes materially improve throughput while the database was underused, the original driver path was limiting the load. Larger batches can also delay cancellation and feedback, increase the duration overrun, and change commit behavior at batch boundaries; retain the batch size in every published configuration.

Client-observed batch latency includes network and client overhead. It is **not** per-row SQL latency, and dividing a batch percentile by its operation count does not produce a valid per-row percentile. Session CPU counters refer to the database sessions; process CPU measurements on the driver refer to the client. Neither substitutes for the other.

## Run a concurrency sweep

Use the same seeded dataset, service, database shape, update percentage, commit interval and batch size at every point. Start with a read-only fixed mix to make the cause of changes easier to interpret:

```bash
# Run from the Linux client after deploy/seed and setting connection variables.
# Use a new output location for every experiment.
for workers in 1 2 4 8 16 32; do
  for repetition in 1 2 3; do
    balio run --platform other --profile logical \
      --workers "$workers" --batch-operations 1000 \
      --warmup 60 --duration 300 \
      --output "results/logical-w${workers}-r${repetition}"
  done
done
```

Repeat with `--profile physical`, and then with the mixed read/update configurations of interest. These are starting values for an experiment, not prescribed safe capacity limits or a guarantee that the physical profile actually produces physical reads.

Use `operations_per_wall_second` and `wall_rates` for end-to-end throughput during the measured phase. They include coordinator and sampling gaps. Compare raw counters, interval variation, observed physical/logical ratio, database waits and client metrics as well as the headline rate. Preserve repetitions and report their spread; the best single score conceals instability.

The sweep is a sequence of separate runs, not an automatic tuning mode. Stop increasing workers when throughput no longer improves meaningfully, latency/waits exceed the experiment's objectives, or resource limits become the subject of the test. Do not run several independent BALIO processes against the same schema for an update test: update key ranges are disjoint only within one invocation.

## What this proves

BALIO can exercise the underlying Oracle database host and storage path remotely. Its session counters show the database work caused by its foreground sessions. They do not identify raw device-media IOPS, all background writer activity, or every competing workload.

Use the optional AWR collection and available cloud, Exadata and storage metrics to explain the result at system scope. AWR may include unrelated activity within its selected database/container and time window. Neither AWR nor a high worker count alone proves that a benchmark reached the maximum capability of the hardware.

The current release has offline tests for overlapping worker calls, coordinated starts after a delayed snapshot, aborting a start barrier after a worker failure, and excluding setup/warmup from client CPU measurement. A live concurrency acceptance test still needs to demonstrate the expected number of sessions, overlapping server activity, correct execution plans, counter reconciliation, repeatable scaling and an adequately provisioned client against the intended Oracle version and deployment.
