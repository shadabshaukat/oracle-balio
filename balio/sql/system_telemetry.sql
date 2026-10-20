-- Optional DBA snapshot. Read only; no Diagnostics/Tuning Pack views used.
-- SQL*Plus: connect using an authorized account to the same database/container,
-- then spool this output immediately before and after an isolated BALIO run.
-- Grants on GV_$SYSSTAT/GV_$INSTANCE (or approved equivalent role) are required.
-- Managed services may restrict these views; missing telemetry is not zero I/O.
-- GV$ rows cover all instances and ALL WORKLOADS, including background work.
-- DBWR/LGWR writes cannot be attributed to BALIO from these cumulative counters.
-- Use numeric deltas divided by measured elapsed seconds. Do not sum a statistic
-- across instance restarts: compare startup_time, inst_id and container scope.
-- Requests are Oracle requests, not physical flash/disk/NVMe device IOPS.
-- Exadata flash, RDMA, offload, storage caches, encryption and replication can
-- change downstream work. Correlate authorized storage/cloud metrics separately.
-- Oracle 12.1+ baseline, subject to release/service availability and grants.

SET PAGESIZE 50000
SET LINESIZE 240
SET TRIMSPOOL ON
SET FEEDBACK ON
COLUMN name FORMAT A45
COLUMN instance_name FORMAT A24
COLUMN captured_at FORMAT A36
COLUMN value FORMAT 999999999999999999999999

SELECT TO_CHAR(SYSTIMESTAMP, 'YYYY-MM-DD"T"HH24:MI:SS.FF6TZH:TZM') AS captured_at,
       SYS_CONTEXT('USERENV', 'DB_NAME') AS db_name,
       SYS_CONTEXT('USERENV', 'CON_NAME') AS container_name
  FROM dual;

SELECT inst_id, instance_name, startup_time, version FROM gv$instance ORDER BY inst_id;

SELECT inst_id, con_id, name, value
  FROM gv$sysstat
 WHERE name IN (
    'session logical reads',
    'physical reads',
    'physical reads cache',
    'physical reads direct',
    'physical read IO requests',
    'physical read total IO requests',
    'physical read total bytes',
    'physical writes',
    'physical write IO requests',
    'physical write total IO requests',
    'physical write total bytes',
    'redo size',
    'redo writes',
    'redo blocks written',
    'db block changes',
    'user commits'
 )
 ORDER BY inst_id, con_id, name;
