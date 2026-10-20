"""AWR command routing tests without Oracle connections or report generation."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from balio import cli
from balio.db import ConnectionConfig


class AwrCliTests(unittest.TestCase):
    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli.main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_with_reporting_account_does_not_ask_for_secrets(self):
        with patch.object(cli, "get_secret", side_effect=AssertionError("must not prompt")):
            code, output, error = self.invoke([
                "run", "--dry-run", "--awr", "--awr-user", "REPORTER", "--awr-scope", "global"])
        self.assertEqual(code, 0, error)
        document = json.loads(output)
        self.assertEqual(document["awr"], {"mode": "snapshots", "scope": "global", "format": "both"})
        self.assertNotIn("password", output)

    def test_reporting_override_without_opt_in_fails_before_connecting(self):
        with patch.object(cli, "connection_config", side_effect=AssertionError("must not connect")):
            code, _, error = self.invoke(["run", "--awr-user", "REPORTER"])
        self.assertEqual(code, 1)
        self.assertIn("Enable --awr", error)

    def test_bad_reporting_timeout_fails_before_connecting(self):
        with patch.object(cli, "connection_config", side_effect=AssertionError("must not connect")):
            code, _, error = self.invoke(["run", "--awr", "--awr-timeout-ms", "0"])
        self.assertEqual(code, 1)
        self.assertIn("awr-timeout-ms", error)

    def test_default_reporting_connection_reuses_credentials_but_separate_timeout(self):
        args = cli.build_parser().parse_args(["run", "--awr"])
        workload = ConnectionConfig("host/pdb", user="BALIO", password="workload-secret")
        reporting = cli.awr_connection(args, workload)
        self.assertIsNot(workload, reporting)
        self.assertEqual(reporting.password, workload.password)
        self.assertEqual(reporting.call_timeout_ms, 180000)
        self.assertEqual(workload.call_timeout_ms, 60000)

    def test_run_routes_reporting_options_without_changing_workload_account(self):
        workload = ConnectionConfig("host/pdb", user="BALIO", password="workload-secret")
        calls = []
        def fake_run(connection, config, output, **kwargs):
            calls.append((connection, kwargs))
            return {"status": "completed"}
        with tempfile.TemporaryDirectory() as output:
            with patch.object(cli, "connection_config", return_value=workload), \
                 patch.object(cli, "run", side_effect=fake_run), \
                 patch.object(cli, "report_text"), \
                 patch.dict(os.environ, {"TEST_AWR_PASSWORD": "reporting-secret"}):
                code, stdout, stderr = self.invoke([
                    "run", "--awr", "snapshots", "--awr-format", "text",
                    "--awr-scope", "global", "--awr-user", "REPORTER",
                    "--awr-password-env", "TEST_AWR_PASSWORD", "--awr-dsn", "host/root",
                    "--output", output])
        self.assertEqual(code, 0, stderr)
        connection, kwargs = calls[0]
        self.assertIs(connection, workload)
        self.assertEqual(asdict(kwargs["awr_config"]), {"mode": "snapshots", "formats": ("text",), "scope": "global"})
        reporting = kwargs["awr_connection_config"]
        self.assertEqual((reporting.user, reporting.password, reporting.dsn), ("REPORTER", "reporting-secret", "host/root"))
        self.assertNotIn("reporting-secret", stdout + stderr)
        self.assertNotIn("workload-secret", stdout + stderr)


if __name__ == "__main__":
    unittest.main()
