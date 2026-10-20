"""Linux-oriented CLI; offline commands do not load Oracle client libraries."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import getpass
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import sys
import time

from . import __version__
from .config import RunConfig, sizing
from .runner import run, safe_error


def platforms_document():
    return json.loads(files("balio").joinpath("platforms.json").read_text(encoding="utf-8"))


def canonical_platform(key):
    for platform in platforms_document()["platforms"]:
        if key == platform["key"] or key in platform.get("aliases", []):
            return platform["key"]
    raise ValueError(f"Unknown platform {key!r}; run 'balio platforms'")


def json_print(value):
    print(json.dumps(value, indent=2, allow_nan=False, default=str))


def connection_args(parser):
    parser.add_argument("--dsn", default=os.getenv("BALIO_DSN"), help="Oracle Net service, Easy Connect, or descriptor; alternatively BALIO_DSN")
    parser.add_argument("--user", default=os.getenv("BALIO_USER"), help="Benchmark schema; alternatively BALIO_USER")
    parser.add_argument("--password-env", default="BALIO_PASSWORD", help="Name of environment variable containing password (never the password itself)")
    parser.add_argument("--mode", choices=("thick", "thin"), default="thick")
    parser.add_argument("--config-dir", default=os.getenv("TNS_ADMIN"), help="Oracle Net configuration/wallet directory; defaults to TNS_ADMIN")
    parser.add_argument("--lib-dir", help="Client library directory on macOS/Windows; Linux uses LD_LIBRARY_PATH or ldconfig")
    parser.add_argument("--external-auth", action="store_true", help="Thick mode external password store/authentication; configure Oracle Net wallet first")
    parser.add_argument("--call-timeout-ms", type=int, default=60000, help="Per database round-trip timeout; does not bound the initial network connection")


def dataset_args(parser):
    parser.add_argument("--hot-rows", type=int, default=10000)
    parser.add_argument("--cold-rows", type=int, default=100000)
    parser.add_argument("--payload-bytes", type=int, default=512)


def workload_args(parser):
    parser.add_argument("--platform", default="other", help="Deployment key or alias from 'balio platforms'")
    parser.add_argument("--deployment-label", default="", help="Free-form environment/shape/config label")
    parser.add_argument("--region", default="")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--duration", type=float, default=60, help="Measured seconds, excluding warmup; a batch may overrun")
    parser.add_argument("--warmup", type=float, default=15, help="Unmeasured seconds; same initial mix")
    parser.add_argument("--interval", type=float, default=5, help="Sampling/controller interval seconds")
    parser.add_argument("--batch-operations", type=int, default=1000, help="Server-side row operations per client call")
    parser.add_argument("--profile", choices=("logical", "balanced", "physical"), default="balanced")
    parser.add_argument("--cold-pct", type=float, help="Probability of choosing the cold pool; overrides profile")
    parser.add_argument("--update-pct", type=float, default=0, help="Probability of update rather than read, independent of hot/cold mix")
    parser.add_argument("--commit-every", type=int, default=100, help="Commit after this many updates; pending updates also commit at batch end")
    parser.add_argument("--random-seed", type=int, default=42)
    parser.add_argument("--target-physical-pct", type=float, help="Feedback target: 100 * physical reads cache / session logical reads; requires counters")
    parser.add_argument("--tolerance", type=float, default=2, help="Target tolerance in percentage points")
    parser.add_argument("--controller-gain", type=float, default=.5)
    parser.add_argument("--controller-max-step", type=float, default=10, help="Maximum cold mix change per interval in percentage points")


def build_parser():
    parser = argparse.ArgumentParser(description="BALIO — Balanced Logical and physical I/O for Oracle")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    platforms = sub.add_parser("platforms", help="List researched deployment choices")
    platforms.add_argument("--json", action="store_true")
    plan = sub.add_parser("plan", help="Offline workload/sizing plan; no database connection")
    workload_args(plan)
    dataset_args(plan)
    plan.add_argument("--call-timeout-ms", type=int, default=60000)
    doctor = sub.add_parser("doctor", help="Read-only connection and session-statistics preflight")
    connection_args(doctor)
    deploy = sub.add_parser("deploy", help="Create BALIO tables and PL/SQL in an existing dedicated schema")
    connection_args(deploy)
    deploy.add_argument("--payload-bytes", type=int, default=512)
    seed = sub.add_parser("seed", help="Seed or resume a dataset over Oracle Net")
    connection_args(seed)
    dataset_args(seed)
    seed.add_argument("--batch-rows", type=int, default=1000)
    run_parser = sub.add_parser("run", help="Run against a READY seeded dataset")
    connection_args(run_parser)
    workload_args(run_parser)
    run_parser.add_argument("--output", type=Path, help="New result directory; never overwrites a prior run")
    run_parser.add_argument("--dry-run", action="store_true", help="Print requested workload only; no connection or measurements")
    run_parser.add_argument("--awr", nargs="?", const="snapshots", default="off", choices=("off", "snapshots", "existing"),
                            help="Opt-in AWR: manual snapshots around measured phase, or explicitly partial existing-snapshot report")
    run_parser.add_argument("--awr-format", choices=("both", "html", "text"), default="both")
    run_parser.add_argument("--awr-scope", choices=("instance", "global"), default="instance",
                            help="Per participating RAC instance, or a global report across participating instances")
    run_parser.add_argument("--awr-user", help="Separate reporting account; workload user is the default")
    run_parser.add_argument("--awr-password-env", help="Reporting password variable name; defaults to BALIO_AWR_PASSWORD when --awr-user is set")
    run_parser.add_argument("--awr-dsn", help="Separate reporting service, e.g. same database CDB root; validated against workload identity")
    run_parser.add_argument("--awr-timeout-ms", type=int, default=180000, help="AWR connection round-trip timeout")
    report = sub.add_parser("report", help="Read a results.json report")
    report.add_argument("path", type=Path)
    report.add_argument("--json", action="store_true")
    provision = sub.add_parser("provision", help="Create a new dedicated database user remotely with an authorized administrative account")
    connection_args(provision)
    provision.add_argument("--schema", required=True, help="New unquoted user, e.g. BALIO")
    provision.add_argument("--schema-password-env", default="BALIO_SCHEMA_PASSWORD")
    provision.add_argument("--tablespace", required=True, help="Existing tablespace, e.g. USERS or DATA on Autonomous")
    provision.add_argument("--quota", required=True, help="Explicit finite quota, e.g. 20G")
    provision.add_argument("--grant-metrics", action="store_true", help="Attempt direct SELECT grants on SYS.V_$MYSTAT and SYS.V_$STATNAME; not available on every managed service")
    return parser


def workload_config(args):
    defaults = {"logical": 0.0, "balanced": 50.0, "physical": 100.0}
    values = {name: getattr(args, name) for name in RunConfig.__dataclass_fields__ if hasattr(args, name)}
    values["cold_pct"] = defaults[args.profile] if args.cold_pct is None else args.cold_pct
    values["platform"] = canonical_platform(args.platform)
    return RunConfig(**values).validate()


def get_secret(variable, prompt):
    value = os.getenv(variable)
    if value:
        return value
    if not sys.stdin.isatty():
        raise ValueError(f"Set {variable} in the environment or use an interactive password prompt")
    return getpass.getpass(prompt)


def connection_config(args):
    from .db import ConnectionConfig
    if not args.dsn:
        raise ValueError("Set BALIO_DSN or --dsn to a remote database service/PDB")
    if args.call_timeout_ms <= 0:
        raise ValueError("call-timeout-ms must be positive")
    if not args.external_auth and not args.user:
        raise ValueError("Set BALIO_USER or --user")
    if args.external_auth and args.mode != "thick":
        raise ValueError("External authentication requires Thick mode")
    password = None if args.external_auth else get_secret(args.password_env, "Database password: ")
    return ConnectionConfig(dsn=args.dsn, user=args.user, password=password, mode=args.mode,
                            lib_dir=args.lib_dir, config_dir=args.config_dir,
                            external_auth=args.external_auth, call_timeout_ms=args.call_timeout_ms)


def awr_options(args):
    if args.awr == "off":
        if args.awr_user or args.awr_password_env or args.awr_dsn:
            raise ValueError("Enable --awr before specifying reporting connection overrides")
        return None
    if not 1 <= args.awr_timeout_ms <= 2147483647:
        raise ValueError("awr-timeout-ms must be between 1 and 2147483647")
    from .awr import AwrConfig
    formats = ("html", "text") if args.awr_format == "both" else (args.awr_format,)
    return AwrConfig(mode=args.awr, formats=formats, scope=args.awr_scope)


def awr_connection(args, workload_connection):
    password = workload_connection.password
    external_auth = workload_connection.external_auth
    if args.awr_user or args.awr_password_env:
        password = get_secret(args.awr_password_env or "BALIO_AWR_PASSWORD", "AWR reporting password: ")
        external_auth = False
    return replace(workload_connection, user=args.awr_user or workload_connection.user,
                   password=password, external_auth=external_auth,
                   dsn=args.awr_dsn or workload_connection.dsn, call_timeout_ms=args.awr_timeout_ms)


def identifier(value):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,29}", value):
        raise ValueError("Identifiers must be 1–30 ASCII letters/digits/underscore and start with a letter")
    return value.upper()


def provision_user(conn, args):
    name, tablespace = identifier(args.schema), identifier(args.tablespace)
    if name in {"SYS", "SYSTEM", "ADMIN", "PUBLIC", "DBSNMP", "OUTLN", "XDB", "PDBADMIN"}:
        raise ValueError("Choose a new dedicated benchmark schema")
    if not re.fullmatch(r"[1-9][0-9]{0,7}[MGT]", args.quota.upper()):
        raise ValueError("quota must be a positive integer followed by M, G, or T")
    password = get_secret(args.schema_password_env, "New benchmark schema password: ")
    if not password or any(c in password for c in ('"', '\n', '\r', '\x00')):
        raise ValueError("Schema password must be nonempty and must not contain quotes, newlines or NUL")
    completed = []
    try:
        with conn.cursor() as cursor:
            cursor.execute(f'CREATE USER {name} IDENTIFIED BY "{password}" DEFAULT TABLESPACE {tablespace} QUOTA {args.quota.upper()} ON {tablespace}')
            completed.append("create_user")
            cursor.execute(f"GRANT CREATE SESSION, CREATE TABLE, CREATE PROCEDURE TO {name}")
            completed.append("grant_schema_privileges")
            if args.grant_metrics:
                for view in ("V_$MYSTAT", "V_$STATNAME"):
                    cursor.execute(f"GRANT SELECT ON SYS.{view} TO {name}")
                    completed.append(f"grant_{view}")
    except Exception as exc:
        # Oracle DDL commits independently: make partial state explicit, never drop it automatically.
        clean_error = str(exc).replace(password, "<redacted>")
        raise RuntimeError(f"Provisioning stopped; completed={completed}. DDL may already be committed. {clean_error}") from None
    return {"schema": name, "tablespace": tablespace, "quota": args.quota.upper(), "completed": completed}


def progress(sample):
    if sample.get("phase") in ("awr_begin", "awr_end"):
        message = "Taking starting AWR snapshot" if sample["phase"] == "awr_begin" else "Taking ending AWR snapshot and downloading reports"
        print(message, file=sys.stderr, flush=True)
        return
    if sample.get("phase") == "warmup":
        print(f"Warmup: {sample['seconds']:g}s (excluded from results)", file=sys.stderr, flush=True)
        return
    ratio = sample["rates"]["physical_to_logical_pct"]
    ratio_text = "unavailable" if ratio is None else f"{ratio:.2f}%"
    print(f"Interval {sample['sample']}: {sample['operations_per_s']:.0f} ops/s; "
          f"cold mix {sample['requested_cold_pct']:.1f}%; physical/logical {ratio_text}",
          file=sys.stderr, flush=True)


def seed_progress():
    last = [0.0]
    def display(info):
        now = time.monotonic()
        if now - last[0] >= 5 or info["status"] == "READY":
            print(f"Seed {info['status']}: hot {info['hot_seeded']:,}/{info['hot_rows']:,}; "
                  f"cold {info['cold_seeded']:,}/{info['cold_rows']:,}", file=sys.stderr, flush=True)
            last[0] = now
    return display


def report_text(document):
    summary = document["summary"]
    print(f"BALIO {document['version']}: {document['status']}")
    print(f"Platform: {document['config']['platform']}; measured {summary['measured_seconds']:.3f}s")
    items = {"Operations/s": summary["operations_per_s"], **summary["rates"]}
    for name, value in items.items():
        print(f"{name}: {'unavailable' if value is None else f'{value:,.3f}'}")
    print(f"Target: {document['target_assessment']['state']}")
    if document.get("awr", {}).get("status", "disabled") != "disabled":
        print(f"Workload status: {document.get('workload_status', document['status'])}; AWR: {document['awr']['status']}")
        print("AWR details and local report paths: see results.json awr section")
    for error in document.get("errors", []):
        print(f"Error: {error}")


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = None
    reporting_cfg = None
    try:
        if args.command == "platforms":
            document = platforms_document()
            if args.json:
                json_print(document)
            else:
                print(f"Researched: {document['researched_on']}; availability varies by service/region/version.")
                for entry in document["platforms"]:
                    aliases = ", ".join(entry.get("aliases", []))
                    print(f"{entry['key']:36} {entry['label']}" + (f" [{aliases}]" if aliases else ""))
            return 0
        if args.command == "plan":
            json_print({"workload": workload_config(args).public_dict(),
                        "dataset": sizing(args.hot_rows, args.cold_rows, args.payload_bytes),
                        "status": "offline_plan_no_measurements"})
            return 0
        if args.command == "report":
            path = args.path / "results.json" if args.path.is_dir() else args.path
            document = json.loads(path.read_text(encoding="utf-8"))
            json_print(document) if args.json else report_text(document)
            return 0
        if args.command == "run":
            workload = workload_config(args)
            awr_config = awr_options(args)
            if args.dry_run:
                json_print({"workload": workload.public_dict(), "awr": {"mode": args.awr, "scope": args.awr_scope, "format": args.awr_format},
                            "status": "dry_run_no_connection_no_measurements"})
                return 0
        if args.command == "seed":
            sizing(args.hot_rows, args.cold_rows, args.payload_bytes)
            if not 1 <= args.batch_rows <= 100000:
                raise ValueError("batch-rows must be in [1, 100000]")
        cfg = connection_config(args)
        if args.command == "run":
            output = args.output or Path("results") / datetime.now(timezone.utc).strftime("balio-%Y%m%dT%H%M%S-%fZ")
            reporting_cfg = awr_connection(args, cfg) if awr_config is not None else None
            result = run(cfg, workload, output, progress=progress,
                         awr_config=awr_config, awr_connection_config=reporting_cfg)
            report_text(result)
            print(f"Results: {output.resolve() / 'results.json'}")
            return 0 if result["status"] == "completed" else (130 if result["status"] == "interrupted" else 1)
        from . import db
        conn = db.connect(cfg)
        try:
            if args.command == "doctor":
                counters, error = db.snapshot(conn)
                json_print({"database": db.database_details(conn), "available_session_counters": sorted(counters or {}),
                            "telemetry_error": safe_error(error, cfg) if error else None,
                            "feedback_capable": counters is not None and all(k in counters for k in ("session logical reads", "physical reads cache"))})
            elif args.command == "provision":
                json_print(provision_user(conn, args))
            elif args.command == "deploy":
                json_print(db.deploy(conn, payload_bytes=args.payload_bytes))
            elif args.command == "seed":
                json_print(db.seed(conn, hot_rows=args.hot_rows, cold_rows=args.cold_rows,
                                   payload_bytes=args.payload_bytes, batch_rows=args.batch_rows,
                                   progress=seed_progress()))
        finally:
            conn.close()
        return 0
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        message = safe_error(exc, cfg) if cfg else str(exc)
        print("BALIO: " + (safe_error(message, reporting_cfg) if reporting_cfg else message), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
