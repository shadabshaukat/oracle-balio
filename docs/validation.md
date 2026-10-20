# Validation status

Implementation date: 20 October 2026. This is an initial runnable implementation, not a certified or production-proven benchmark. No Oracle endpoint, Instant Client runtime, wallet or database credentials were supplied in this workspace. Offline tests and packaging can validate software paths; they cannot establish Oracle SQL compilation, platform permissions or achieved performance.

## Completed checks

- **89 offline tests passed**, including fake-backend end-to-end runner tests, concurrent overlapping sessions, synchronized window starts, client CPU timing, seeding interruption/resume, feedback and counter semantics, CLI validation, output preservation and secret redaction.
- AWR protocol tests cover boundary lifecycle, separate reporting credentials, local HTML/text files, partial transfers, RAC instance/global selection, container/DBID matching, restart detection, missing APIs and incomplete collection status. These use a fake Oracle protocol; they do not establish that a live service permits the API calls.
- Python compilation passed for application and test sources.
- Built the distributable Python wheel and installed it in an isolated virtual environment with the pinned `oracledb==3.4.2` dependency.
- Verified the installed CLI, deployment catalogue, offline planning, and bundled schema/telemetry SQL resources.
- Reviewed SQL/PLSQL argument order, bind handling and ownership validation; this is code review, not successful compilation on Oracle.

The verification host was **macOS with Python 3.14.7**. The documented Linux/Instant Client execution path and all live database combinations remain pending. No synthetic benchmark score is presented as a database measurement.

## Release acceptance matrix

| Target | SQL compatibility design | Live deployment/seed/run validation |
|---|---|---|
| 12c Release 1 (12.1) | Baseline syntax; Client 19 suggested | Pending |
| 12c Release 2 (12.2) | Baseline syntax | Pending |
| 18c | Baseline syntax | Pending |
| 19c | Baseline syntax | Pending |
| 21c | Baseline syntax | Pending |
| 23ai | Baseline syntax | Pending |
| 26ai (including 23.26.x numeric builds) | Baseline syntax; version label recorded | Pending |
| Autonomous Serverless / Dedicated | Capability detection and telemetry fallback | Pending per service |
| Exadata Dedicated / Exascale / Cloud@Customer | Common Oracle Net path | Pending per deployment |
| Oracle services in AWS / Azure / Google Cloud | Common Oracle Net path | Pending per service/region |
| RDS / self-managed IaaS / on-premises / other | Common Oracle Net path | Pending per exact configuration |

## Live acceptance procedure

Use a disposable, dedicated benchmark schema and a database where this workload is authorized. Keep the exact database RU, client and driver versions in the report.

1. Run `doctor`; confirm the intended PDB/service, client mode and telemetry access. Verify connection timeouts and network routing.
2. Run `deploy`; inspect `USER_ERRORS` for the package and confirm only BALIO objects were created. Re-running deploy should preserve data. Do not deploy while a workload is running.
3. Seed a small dataset. Verify exact row counts, `MIN(id)=1`, `MAX(id)=row_count`, payload length and uniqueness of primary keys. Check indexes are usable and data statistics/plans are suitable for point lookups.
4. Interrupt seeding on a separate fresh schema, resume with the same dimensions, and verify counts. A different-dimension request must fail without truncating data.
5. Run read-only HOT then COLD tests. Confirm `read_operations=operations`, no benchmark update counts, and the reported Oracle counter deltas. Ensure result-cache/parallel behavior does not defeat the intended workload.
6. Run a mixed update test; verify `touch` changes, redo appears, requested probabilities converge over sufficient operations, and transaction boundaries match commit policy. Check undo/redo capacity and checkpoint effects separately.
7. Restrict catalog access on a test account: fixed mix must retain operations with missing counters as null; feedback must refuse to claim a measured target.
8. Make the HOT set resident and enlarge the COLD set beyond effective cache. Establish measured endpoint ratios before selecting an intermediate feedback target. Confirm settled intervals and final target assessment, including unattainable targets.
9. Cancel a run and simulate a failed connection. Verify nonzero exit status, incomplete reporting, preservation of completed intervals and that failed batches are not retried.
10. Repeat across desired Oracle releases and deployment families. Record observed failures and permitted fixes rather than inferring compatibility from one successful 19c run.
11. With AWR explicitly enabled and an authorized reporting account, compare the recorded measurement UTC timestamps with both snapshot boundaries. Open the local HTML/text reports, verify the DBID/container and every participating instance, and independently reconcile their scope. Check 12.1 root versus 12.2+ PDB reporting separately.
12. Confirm AWR failure leaves measured workload results intact but returns nonzero for incomplete requested reporting. A partial existing-snapshot report must not be mistaken for full coverage. Verify the disabled default makes no AWR calls.

For a trustworthy physical-I/O claim, independently compare worker statistics with authorized instance/cloud/Exadata telemetry during an isolated interval. Explain any disparity due to caching, multiblock requests, background writes or other workloads. Never convert a client operation count directly into storage IOPS.
