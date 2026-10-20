"""Concurrent remote sessions with bounded batches and auditable interval results."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import threading
import time

from . import __version__
from .controller import MixController
from .metrics import COUNTERS, BatchHistogram, aggregate_samples, complete_sum, counter_delta, rates


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_error(error, connection_config):
    message = str(error)
    for secret in (getattr(connection_config, "password", None), getattr(connection_config, "wallet_password", None),
                   getattr(connection_config, "dsn", None)):
        if secret:
            message = message.replace(secret, "<redacted>")
    return message


def redact_document(value, *configs):
    if isinstance(value, dict):
        return {key: redact_document(item, *configs) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_document(item, *configs) for item in value]
    if isinstance(value, str):
        for config in configs:
            value = safe_error(value, config)
    return value


def write_json(path, document):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(document, handle, indent=2, allow_nan=False)
        handle.write("\n")
    temporary.replace(path)


class WindowGate:
    """Release all workers after their starting counter snapshots are ready."""
    def __init__(self, workers, duration, timeout, run_deadline=None):
        self.duration = duration
        self.run_deadline = run_deadline
        self.deadline = None
        self.barrier = threading.Barrier(workers, action=self._start, timeout=timeout)

    def _start(self):
        self.deadline = time.monotonic() + self.duration
        if self.run_deadline is not None:
            self.deadline = min(self.deadline, self.run_deadline)

    def wait(self):
        self.barrier.wait()
        return self.deadline

    def abort(self):
        self.barrier.abort()


class Worker:
    def __init__(self, number, connection_config, run_config, data, api, stop):
        self.number, self.connection_config, self.config = number, connection_config, run_config
        self.data, self.api, self.stop = data, api, stop
        self.connection = None
        self.details = None
        self.random_seed = (run_config.random_seed + 104729 * number - 1) % 2147483646 + 1

    def open(self):
        self.connection = self.api.connect(self.connection_config)
        self.connection.module = "BALIO"
        self.connection.action = f"worker-{self.number}"
        self.details = self.api.database_details(self.connection)
        return self.details

    def close(self):
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass

    def cancel(self):
        if self.connection is not None:
            try:
                self.connection.cancel()
            except Exception:
                pass

    def run_window(self, deadline, cold_pct, gate=None):
        count = {k: 0 for k in ("ops", "hot_ops", "cold_ops", "read_ops", "update_ops", "commits")}
        histogram = BatchHistogram()
        before, stat_error = None, None
        started = time.monotonic()
        error = None
        checksum = 0
        try:
            before, stat_error = self.api.snapshot(self.connection)
            if gate is not None:
                deadline = gate.wait()
            started = time.monotonic()
            while not self.stop.is_set() and time.monotonic() < deadline:
                batch_started = time.monotonic()
                batch = self.api.run_batch(
                    self.connection, operations=self.config.batch_operations,
                    cold_pct=cold_pct, update_pct=self.config.update_pct, seed=self.random_seed,
                    worker_id=self.number, workers=self.config.workers,
                    hot_rows=self.data["hot_rows"], cold_rows=self.data["cold_rows"],
                    commit_every=self.config.commit_every,
                )
                histogram.add(time.monotonic() - batch_started)
                self.random_seed = int(batch["seed"])
                checksum = (checksum + int(batch.get("checksum", 0))) % 2147483647
                for key in count:
                    count[key] += int(batch[key])
        except BaseException as exc:
            self.stop.set()
            if gate is not None:
                gate.abort()
            if not isinstance(exc, Exception):
                raise
            error = safe_error(exc, self.connection_config) or type(exc).__name__
        ended = time.monotonic()
        try:
            after, final_stat_error = self.api.snapshot(self.connection)
        except Exception as exc:
            after, final_stat_error = None, safe_error(exc, self.connection_config)
        return {
            "worker": self.number, "started": started, "ended": ended, **count,
            "counters": counter_delta(before, after), "error": error,
            "telemetry_error": safe_error(stat_error or final_stat_error, self.connection_config)
            if stat_error or final_stat_error else None,
            "batch_latency": histogram.export(), "checksum": checksum,
        }


def summarize_window(results, number, cold_pct):
    seconds = max(r["ended"] for r in results) - min(r["started"] for r in results)
    counters = {name: complete_sum([r["counters"] for r in results], name) for name in COUNTERS}
    mapping = {"operations": "ops", "hot_operations": "hot_ops", "cold_operations": "cold_ops",
               "read_operations": "read_ops", "update_operations": "update_ops", "commits": "commits"}
    counts = {key: sum(r[value] for r in results) for key, value in mapping.items()}
    return {
        "sample": number, "at_utc": utc_now(), "requested_cold_pct": cold_pct,
        "measured_seconds": seconds, **counts,
        "worker_start_skew_ms": 1000 * (max(r["started"] for r in results) - min(r["started"] for r in results)),
        "batches": sum(r["batch_latency"]["count"] for r in results),
        "operations_per_s": counts["operations"] / seconds if seconds else None,
        "counters": counters, "rates": rates(counters, seconds),
        "telemetry_coverage": {name: sum(r["counters"].get(name) is not None for r in results) for name in COUNTERS},
        "workers": [{k: v for k, v in r.items() if k not in ("started", "ended")} for r in results],
    }


def run(connection_config, config, output_dir, progress=None, api=None,
        awr_config=None, awr_connection_config=None, awr_factory=None):
    """Return results even on failure; never fabricate missing DB telemetry."""
    config.validate()
    connection_config = replace(connection_config, call_timeout_ms=config.call_timeout_ms)
    if api is None:
        from . import db as api
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if any((output_dir / name).exists() for name in ("results.json", "samples.jsonl", "awr")):
        raise ValueError("Output directory already contains a run; choose a fresh directory")
    stop = threading.Event()
    workers, samples, errors = [], [], []
    pool = ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix="balio")
    controller = None if config.target_physical_pct is None else MixController(
        config.target_physical_pct, config.cold_pct, config.tolerance,
        config.controller_gain, config.controller_max_step)
    result = {"format_version": 1, "benchmark": "BALIO", "version": __version__,
              "started_at_utc": utc_now(), "config": config.public_dict(),
              "status": "failed", "validation": "Live run; inspect errors and metric coverage.",
              "warnings": [
                  "Physical read counters describe Oracle foreground sessions, not storage device IOPS or DBWR writes.",
                  "Snapshots add a small amount of SQL overhead. Batches may overrun the requested duration.",
                  "Failed batches may have committed partial updates; their operation counts cannot be recovered.",
              ]}
    wall_started = time.monotonic()
    measured_started = None
    measured_ended = None
    client_cpu_started = None
    client_cpu_ended = None
    active_gate = None
    awr_capture = None
    awr_started = False
    awr_connection_config = awr_connection_config or connection_config
    result["awr"] = {"status": "disabled"}
    result["measurement"] = {"started_at_utc": None, "ended_at_utc": None}
    result["client"] = {"os": platform.system(), "architecture": platform.machine(),
                        "python_version": platform.python_version(),
                        "logical_cpus_visible": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count(),
                        "requested_workers": config.workers,
                        "model": "one persistent connection and one in-flight serial PL/SQL batch per worker; closed-loop"}
    try:
        control = api.connect(connection_config)
        try:
            data = api.dataset_info(control)
            if data["status"] != "READY":
                raise ValueError("Dataset is not READY; finish seeding first")
            for active, name in ((controller is not None or config.cold_pct < 100, "hot_rows"),
                                 (controller is not None or config.cold_pct > 0, "cold_rows")):
                if active and data[name] < 1:
                    raise ValueError("Each active table needs at least one row")
                if active and config.update_pct > 0 and data[name] < config.workers:
                    raise ValueError("Each active update table needs at least one row per worker")
            result["dataset"] = data
        finally:
            control.close()
        workers = [Worker(i, connection_config, config, data, api, stop) for i in range(config.workers)]
        openings = [pool.submit(w.open) for w in workers]
        result["sessions"] = [f.result() for f in openings]
        result["client"]["connected_workers"] = len(result["sessions"])
        if awr_config is not None:
            if awr_factory is None:
                from .awr import AwrCapture
                awr_factory = AwrCapture
            awr_capture = awr_factory(awr_connection_config, awr_config, output_dir)
            awr_capture.prepare(result["sessions"])
        if controller:
            for worker in workers:
                stats, error = api.snapshot(worker.connection)
                if stats is None or any(k not in stats for k in ("session logical reads", "physical reads cache")):
                    raise ValueError("Feedback requires session logical reads and physical reads cache on every worker; "
                                     + (safe_error(error, connection_config) if error else "required counters missing"))
        if config.warmup:
            if progress:
                progress({"phase": "warmup", "seconds": config.warmup})
            warm_deadline = time.monotonic() + config.warmup
            active_gate = WindowGate(config.workers, config.warmup, config.call_timeout_ms / 1000 + 5)
            warm = [pool.submit(w.run_window, warm_deadline, config.cold_pct, active_gate) for w in workers]
            warm_results = [f.result() for f in warm]
            warm_errors = [r["error"] for r in warm_results if r["error"]]
            if warm_errors:
                raise RuntimeError("Warmup failed: " + "; ".join(warm_errors))
        if awr_capture is not None:
            if progress:
                progress({"phase": "awr_begin"})
            awr_capture.begin()
            awr_started = True
        measured_started = time.monotonic()
        result["measurement"]["started_at_utc"] = utc_now()
        client_cpu_started = time.process_time()
        deadline = measured_started + config.duration
        with (output_dir / "samples.jsonl").open("x", encoding="utf-8") as log:
            os.chmod(output_dir / "samples.jsonl", 0o600)
            while not stop.is_set() and time.monotonic() < deadline:
                cold_pct = controller.cold_pct if controller else config.cold_pct
                sample_deadline = min(deadline, time.monotonic() + config.interval)
                active_gate = WindowGate(config.workers, config.interval, config.call_timeout_ms / 1000 + 5, deadline)
                futures = [pool.submit(w.run_window, sample_deadline, cold_pct, active_gate) for w in workers]
                rows = [f.result() for f in futures]
                sample = summarize_window(rows, len(samples) + 1, cold_pct)
                errors.extend(r["error"] for r in rows if r["error"])
                if not sample["operations"] and not errors:
                    break  # Starting snapshots can consume the last fraction of the wall-time budget.
                if controller:
                    try:
                        sample["controller"] = controller.step(sample["rates"]["physical_to_logical_pct"])
                    except ValueError as exc:
                        errors.append(str(exc))
                        stop.set()
                samples.append(sample)
                log.write(json.dumps(sample, allow_nan=False) + "\n")
                log.flush()
                if progress:
                    progress(sample)
                if errors:
                    break
        measured_ended = time.monotonic()
        result["measurement"]["ended_at_utc"] = utc_now()
        client_cpu_ended = time.process_time()
        result["status"] = "failed" if errors else "completed"
        if not samples:
            errors.append("No measured interval completed")
            result["status"] = "failed"
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        errors.append("Interrupted by operator; in-flight interval is excluded")
        stop.set()
        if active_gate is not None:
            active_gate.abort()
        for worker in workers:
            worker.cancel()
    except Exception as exc:
        errors.append(safe_error(safe_error(exc, awr_connection_config), connection_config))
        stop.set()
        if active_gate is not None:
            active_gate.abort()
        for worker in workers:
            worker.cancel()
    finally:
        if measured_started is not None and measured_ended is None:
            measured_ended = time.monotonic()
            result["measurement"]["ended_at_utc"] = utc_now()
            client_cpu_ended = time.process_time()
        pool.shutdown(wait=True, cancel_futures=True)
        result["workload_status"] = result["status"]
        if awr_capture is not None:
            try:
                if awr_started:
                    if progress:
                        progress({"phase": "awr_end"})
                    awr_capture.finish(run_status=result["workload_status"])
                result["awr"] = awr_capture.result()
                if result["awr"].get("status") not in ("complete", "completed"):
                    errors.append("Requested AWR collection is incomplete; see awr status and errors")
                    if result["status"] == "completed":
                        result["status"] = "failed"
            except BaseException as exc:
                clean_error = safe_error(safe_error(exc, awr_connection_config), connection_config) or type(exc).__name__
                errors.append("AWR: " + clean_error)
                result["awr"] = {**awr_capture.result(), "status": "failed", "collection_error": clean_error}
                if result["status"] == "completed":
                    result["status"] = "failed"
            finally:
                try:
                    awr_capture.close()
                except Exception as exc:
                    errors.append("AWR close: " + safe_error(safe_error(exc, awr_connection_config), connection_config))
                    if result["status"] == "completed":
                        result["status"] = "failed"
            result["awr"] = redact_document(result["awr"], connection_config, awr_connection_config)
        for worker in workers:
            worker.close()
        result["finished_at_utc"] = utc_now()
        result["wall_seconds_including_setup_and_warmup"] = time.monotonic() - wall_started
        result["summary"] = aggregate_samples(samples)
        phase_seconds = measured_ended - measured_started if measured_started is not None else 0.0
        cpu_seconds = max(0.0, client_cpu_ended - client_cpu_started) if client_cpu_started is not None else None
        result["client"].update(measured_phase_cpu_seconds=cpu_seconds,
            cpu_percent_of_one_core=100 * cpu_seconds / phase_seconds if cpu_seconds is not None and phase_seconds else None,
            max_worker_start_skew_ms=max((s["worker_start_skew_ms"] for s in samples), default=None),
            note="Client process CPU across all threads, not Oracle server CPU. Excludes setup, warmup and AWR calls; includes sampling/coordinator work. Monitor client host and network separately.")
        result["summary"]["measured_phase_wall_seconds"] = phase_seconds
        result["summary"]["operations_per_wall_second"] = result["summary"]["operations"] / phase_seconds if phase_seconds else None
        result["summary"]["wall_rates"] = rates(result["summary"]["counters"], phase_seconds)
        result["summary"]["rate_denominator_note"] = (
            "operations_per_s and rates use summed measured worker windows; wall_rates and "
            "operations_per_wall_second include sampling/coordinator/controller gaps during the measured phase. "
            "Neither denominator includes warmup or connection setup."
        )
        result["sample_count"] = len(samples)
        result["errors"] = errors
        result["telemetry_available"] = bool(samples) and all(
            value is not None for value in result["summary"]["counters"].values())
        result["target_assessment"] = target_assessment(config, samples, result["summary"], result["workload_status"])
        write_json(output_dir / "results.json", result)
    return result


def target_assessment(config, samples, summary, status="completed"):
    if config.target_physical_pct is None:
        return {"state": "fixed_mix", "note": "No measured physical/logical target requested"}
    observed = summary["rates"]["physical_to_logical_pct"]
    if status != "completed":
        state = "incomplete_run"
    elif observed is None:
        state = "unavailable"
    elif abs(observed - config.target_physical_pct) <= config.tolerance:
        state = "aggregate_within_tolerance"
    else:
        state = "target_unmet"
    within = sum(s.get("controller", {}).get("state") == "within_tolerance" for s in samples)
    return {"state": state, "target_pct": config.target_physical_pct,
            "observed_pct": observed, "tolerance_percentage_points": config.tolerance,
            "last_interval_state": samples[-1].get("controller", {}).get("state") if samples else None,
            "intervals_within_tolerance": within,
            "fraction_of_intervals_within_tolerance": within / len(samples) if samples else None,
            "longest_saturation_streak": max((s.get("controller", {}).get("saturated_intervals", 0) for s in samples), default=0),
            "note": "Assesses the complete measured run including controller settling; saturation is evidence of an unmet target for this run, not proof of global unattainability."}
