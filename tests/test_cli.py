"""Offline CLI contracts and boundaries; no Oracle driver or server is required."""
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from balio import cli
from balio.config import RunConfig, sizing
from balio.db import ConnectionConfig


ROOT = Path(__file__).resolve().parents[1]


def invoke(*argv):
    """Capture one invocation without changing argument-parser exit semantics."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class OfflineCliTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_offline_commands_in_interpreter_without_site_packages(self):
        # -S excludes installed drivers and client packages. A fresh process also
        # prevents an already imported module from hiding an import dependency.
        for argv in (["--version"], ["platforms", "--json"],
                     ["plan", "--platform", "exacs"],
                     ["run", "--dry-run", "--profile", "physical"]):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [sys.executable, "-S", "-m", "balio", *argv],
                    cwd=ROOT, env={"PYTHONPATH": str(ROOT)},
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_every_platform_alias_resolves_to_one_canonical_key(self):
        document = cli.platforms_document()
        all_names = []
        for platform in document["platforms"]:
            for name in [platform["key"], *platform.get("aliases", [])]:
                with self.subTest(name=name):
                    self.assertEqual(cli.canonical_platform(name), platform["key"])
                    all_names.append(name)
        self.assertEqual(len(all_names), len(set(all_names)))
        self.assertEqual(cli.canonical_platform("exacs"), "oci-exadata-dedicated")
        self.assertEqual(cli.canonical_platform("other"), "other")
        with self.assertRaisesRegex(ValueError, "Unknown platform"):
            cli.canonical_platform("made-up-service")

    def test_platforms_json_retains_sources_and_research_date(self):
        code, out, err = invoke("platforms", "--json")
        self.assertEqual((code, err), (0, ""))
        document = json.loads(out)
        self.assertEqual(document, cli.platforms_document())
        self.assertRegex(document["researched_on"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(all("sources" in row for row in document["platforms"]))

    def test_profiles_and_explicit_mix_are_offline_workload_requests(self):
        for profile, expected in (("logical", 0), ("balanced", 50), ("physical", 100)):
            with self.subTest(profile=profile):
                code, out, err = invoke("plan", "--profile", profile, "--platform", "exacs")
                self.assertEqual((code, err), (0, ""))
                document = json.loads(out)
                self.assertEqual(document["status"], "offline_plan_no_measurements")
                self.assertEqual(document["workload"]["cold_pct"], expected)
                self.assertEqual(document["workload"]["platform"], "oci-exadata-dedicated")
                self.assertNotIn("rates", document)
        code, out, err = invoke("run", "--dry-run", "--profile", "physical", "--cold-pct", "27.5")
        self.assertEqual((code, err), (0, ""))
        document = json.loads(out)
        self.assertEqual(document["workload"]["cold_pct"], 27.5)
        self.assertEqual(document["status"], "dry_run_no_connection_no_measurements")

    def test_invalid_workload_rejected_before_connection(self):
        invalid = [
            ("--workers", "0"), ("--workers", "4097"),
            ("--duration", "nan"), ("--duration", "inf"), ("--duration", "0"),
            ("--warmup", "-1"), ("--interval", "0"),
            ("--cold-pct", "-1"), ("--update-pct", "101"),
            ("--batch-operations", "1000001"), ("--commit-every", "0"),
            ("--target-physical-pct", "nan"), ("--target-physical-pct", "101"),
            ("--controller-gain", "0"), ("--controller-max-step", "0"),
            ("--tolerance", "-1"), ("--random-seed", "0"),
            ("--call-timeout-ms", "0"), ("--platform", "unknown"),
        ]
        with mock.patch.object(cli, "connection_config") as connection:
            for option, value in invalid:
                with self.subTest(option=option, value=value):
                    code, out, err = invoke("run", "--dry-run", option, value)
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("BALIO:", err)
                    self.assertNotIn("Traceback", err)
            connection.assert_not_called()

    def test_seed_validation_happens_before_password_or_connection(self):
        invalid = [("--hot-rows", "0"), ("--cold-rows", "-1"),
                   ("--payload-bytes", "63"), ("--payload-bytes", "2001"),
                   ("--batch-rows", "0"), ("--batch-rows", "100001")]
        with mock.patch.object(cli, "connection_config") as connection:
            for option, value in invalid:
                with self.subTest(option=option, value=value):
                    code, out, err = invoke("seed", option, value)
                    self.assertEqual(code, 1)
                    self.assertEqual(out, "")
                    self.assertIn("BALIO:", err)
            connection.assert_not_called()

    def test_run_config_sizing_bounds_and_credential_free_export(self):
        cfg = RunConfig().validate()
        for field in ("password", "dsn", "user", "wallet_password"):
            self.assertNotIn(field, cfg.public_dict())
        estimate = sizing(1024, 2048, 512)
        self.assertEqual(estimate["total_payload_gib"], 3072 * 512 / 2**30)
        self.assertIn("excludes", estimate["note"])
        with self.assertRaises(ValueError):
            sizing(2_147_483_647, 1, 512)
        with self.assertRaises(ValueError):
            replace(cfg, controller_gain=float("nan")).validate()

    def test_report_accepts_directory_and_preserves_unavailable_metrics(self):
        document = {
            "version": "test", "status": "completed", "config": {"platform": "other"},
            "summary": {"measured_seconds": 2, "operations_per_s": 20,
                        "rates": {"physical_read_requests_per_s": None}},
            "target_assessment": {"state": "unavailable"}, "errors": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            code, out, err = invoke("report", directory)
            self.assertEqual((code, err), (0, ""))
            self.assertIn("physical_read_requests_per_s: unavailable", out)
            code, out, err = invoke("report", str(path), "--json")
            self.assertEqual((code, err), (0, ""))
            self.assertEqual(json.loads(out), document)
            path.write_text("not JSON", encoding="utf-8")
            code, out, err = invoke("report", str(path))
            self.assertEqual(code, 1)
            self.assertNotIn("Traceback", err)

    def test_run_status_controls_exit_code(self):
        for status, expected in (("completed", 0), ("failed", 1), ("interrupted", 130)):
            result = {"status": status}
            with self.subTest(status=status), \
                    mock.patch.object(cli, "connection_config", return_value=object()), \
                    mock.patch.object(cli, "run", return_value=result) as runner, \
                    mock.patch.object(cli, "report_text"):
                code, out, err = invoke("run", "--output", "unused-test-output")
                self.assertEqual(code, expected)
                runner.assert_called_once()
                self.assertEqual(err, "")


class ProvisionSecurityTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {"NEW_PASSWORD": "Bench-secret-7!"}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.args = SimpleNamespace(schema="balio", tablespace="users", quota="20g",
                                    schema_password_env="NEW_PASSWORD", grant_metrics=False)
        self.connection = mock.MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value

    def test_identifier_injection_is_rejected_without_executing_sql(self):
        for field in ("schema", "tablespace"):
            for value in ('BALIO; DROP USER APP', '"BALIO"', 'B--comment', 'B\nX', 'éclair', 'A' * 31):
                with self.subTest(field=field, value=value):
                    args = SimpleNamespace(**vars(self.args))
                    setattr(args, field, value)
                    with self.assertRaises(ValueError):
                        cli.provision_user(self.connection, args)
        self.connection.cursor.assert_not_called()

    def test_reserved_schema_and_unbounded_or_injected_quota_are_rejected(self):
        for name in ("SYS", "system", "ADMIN", "PDBADMIN"):
            with self.subTest(name=name):
                self.args.schema = name
                with self.assertRaisesRegex(ValueError, "dedicated benchmark schema"):
                    cli.provision_user(self.connection, self.args)
        self.args.schema = "BALIO"
        for quota in ("UNLIMITED", "0G", "-1G", "2G; GRANT DBA TO BALIO", "1 G", "1G\n"):
            with self.subTest(quota=quota):
                self.args.quota = quota
                with self.assertRaises(ValueError):
                    cli.provision_user(self.connection, self.args)
        self.connection.cursor.assert_not_called()

    def test_password_cannot_escape_or_break_sql_literal(self):
        for password in ('a"b', "a\nb", "a\rb", "a\x00b"):
            with self.subTest(password=repr(password)), mock.patch.object(cli, "get_secret", return_value=password):
                with self.assertRaises(ValueError):
                    cli.provision_user(self.connection, self.args)
        self.connection.cursor.assert_not_called()

    def test_minimum_provision_grants_and_optional_metrics_are_explicit(self):
        result = cli.provision_user(self.connection, self.args)
        statements = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        self.assertIn('DEFAULT TABLESPACE USERS QUOTA 20G ON USERS', statements[0])
        self.assertEqual(statements[1], "GRANT CREATE SESSION, CREATE TABLE, CREATE PROCEDURE TO BALIO")
        self.assertNotIn(os.environ["NEW_PASSWORD"], json.dumps(result))
        self.assertEqual(result["completed"], ["create_user", "grant_schema_privileges"])
        self.cursor.execute.reset_mock()
        self.args.grant_metrics = True
        cli.provision_user(self.connection, self.args)
        statements = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertEqual(statements[2:], ["GRANT SELECT ON SYS.V_$MYSTAT TO BALIO",
                                        "GRANT SELECT ON SYS.V_$STATNAME TO BALIO"])

    def test_partial_ddl_failure_redacts_new_password_and_reports_committed_state(self):
        secret = os.environ["NEW_PASSWORD"]
        self.cursor.execute.side_effect = [None, RuntimeError("driver statement echoed " + secret)]
        with self.assertRaises(RuntimeError) as error:
            cli.provision_user(self.connection, self.args)
        message = str(error.exception)
        self.assertNotIn(secret, message)
        self.assertIn("<redacted>", message)
        self.assertIn("create_user", message)
        self.assertIn("DDL may already be committed", message)
        self.assertEqual(self.cursor.execute.call_count, 2)
        self.assertFalse(any("DROP" in call.args[0] for call in self.cursor.execute.call_args_list))

    def test_cli_redacts_admin_password_dsn_and_new_password_on_provision_failure(self):
        admin_secret = "Admin-secret-9!"
        dsn = "private-db.internal:1521/example"
        cfg = ConnectionConfig(dsn=dsn, user="ADMIN", password=admin_secret)
        self.cursor.execute.side_effect = RuntimeError(
            "failed " + admin_secret + " " + dsn + " " + os.environ["NEW_PASSWORD"])
        with mock.patch.object(cli, "connection_config", return_value=cfg), \
                mock.patch("balio.db.connect", return_value=self.connection):
            code, out, err = invoke("provision", "--schema", "balio", "--tablespace", "users",
                                    "--quota", "20G", "--schema-password-env", "NEW_PASSWORD")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertNotIn(admin_secret, err)
        self.assertNotIn(dsn, err)
        self.assertNotIn(os.environ["NEW_PASSWORD"], err)
        self.assertIn("<redacted>", err)
        self.connection.close.assert_called_once()


class ConnectionValidationTests(unittest.TestCase):
    def setUp(self):
        self.environment = mock.patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_noninteractive_missing_secret_does_not_prompt(self):
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False), \
                mock.patch.object(cli.getpass, "getpass") as prompt:
            with self.assertRaisesRegex(ValueError, "BALIO_PASSWORD"):
                cli.get_secret("BALIO_PASSWORD", "Password: ")
            prompt.assert_not_called()

    def test_external_auth_requires_thick_and_bypasses_password_prompt(self):
        parser = cli.build_parser()
        args = parser.parse_args(["doctor", "--dsn", "wallet_alias", "--external-auth"])
        with mock.patch.object(cli, "get_secret") as password:
            config = cli.connection_config(args)
            self.assertTrue(config.external_auth)
            self.assertIsNone(config.password)
            password.assert_not_called()
            args.mode = "thin"
            with self.assertRaisesRegex(ValueError, "Thick mode"):
                cli.connection_config(args)

    def test_missing_endpoint_user_and_bad_timeout_fail_without_password_prompt(self):
        parser = cli.build_parser()
        invalid = [[], ["--dsn", "db/service"],
                   ["--dsn", "db/service", "--user", "BALIO", "--call-timeout-ms", "0"]]
        with mock.patch.object(cli, "get_secret") as password:
            for options in invalid:
                with self.subTest(options=options):
                    with self.assertRaises(ValueError):
                        cli.connection_config(parser.parse_args(["doctor", *options]))
            password.assert_not_called()


if __name__ == "__main__":
    unittest.main()
