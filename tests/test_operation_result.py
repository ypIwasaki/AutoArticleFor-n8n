from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import operation_result as operation


class OperationResultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.logs = Path(self.temporary.name)

    def run_code(self, code, **kwargs):
        return operation.run_operation(
            [sys.executable, "-c", code], step="test", log_dir=self.logs, **kwargs)

    def test_success_saves_full_logs_but_prints_no_payload_or_command(self):
        result, code = self.run_code("print('ARTICLE_BODY_PRIVATE' * 20000)")
        self.assertEqual(code, 0)
        self.assertEqual(result["processStatus"], "exited_ok")
        self.assertEqual(result["verification"], "command_result_only")
        self.assertEqual(result["summary"]["resultStatus"], "not_checked")
        self.assertGreater(result["stdoutBytes"], 100000)
        text = json.dumps(result)
        self.assertNotIn("ARTICLE_BODY_PRIVATE", text)
        self.assertNotIn("-c", text)
        self.assertLess(len(text), 4000)
        self.assertIn("ARTICLE_BODY_PRIVATE", Path(result["logs"]["stdout"]).read_text())
        self.assertEqual(json.loads(Path(result["logs"]["result"]).read_text()), result)

    def test_failure_exit_code_preserved_and_stderr_not_echoed(self):
        result, code = self.run_code("import sys; print('SECRET_API_TOKEN', file=sys.stderr); sys.exit(7)")
        self.assertEqual(code, 7)
        self.assertEqual(result["exitCode"], 7)
        self.assertEqual(result["processStatus"], "failed")
        self.assertTrue(result["attentionRequired"])
        self.assertNotIn("SECRET_API_TOKEN", json.dumps(result))
        self.assertIn("SECRET_API_TOKEN", Path(result["logs"]["stderr"]).read_text())

    def test_stderr_warning_is_not_hidden_by_zero_exit(self):
        result, code = self.run_code("import sys; print('warning: sync skipped', file=sys.stderr)")
        self.assertEqual(code, 0)
        self.assertTrue(result["attentionRequired"])
        self.assertGreater(result["stderrBytes"], 0)

    def test_unknown_json_keeps_unverified_and_does_not_guess_counts(self):
        result, code = self.run_code("print('{\"success\": true, \"secret\": \"PRIVATE\"}')", profile="collection")
        self.assertEqual(code, 0)
        self.assertEqual(result["summary"]["resultStatus"], "unrecognized")
        self.assertTrue(result["attentionRequired"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_collection_summary_is_constant_size_and_date_bound(self):
        payload = {"saved": True, "date": "2026-09-10", "articleCount": 800, "candidateCount": 500,
                   "writtenFiles": ["file"] * 10, "candidates": [{"evidence": "PRIVATE" * 200}] * 500,
                   "autoAddedKeywords": ["private-name"] * 3, "autoKeywordCount": 30}
        summary = operation.summarize_payload(payload, "collection", "2026-09-10")
        self.assertLess(len(json.dumps(summary)), 1000)
        self.assertNotIn("PRIVATE", json.dumps(summary))
        self.assertEqual(operation.summarize_payload(payload, "collection", "2026-09-10")["counts"],
                         {"articles": 800, "candidates": 500, "writtenFiles": 10,
                          "autoAddedKeywords": 3, "autoKeywords": 30})
        wrong = operation.summarize_payload(payload, "collection", "2026-09-09")
        self.assertEqual(wrong["resultStatus"], "unrecognized")

    def test_single_array_json_and_last_json_line(self):
        payload = {"saved": True, "date": "2026-09-10", "articleCount": 0, "candidateCount": 0, "writtenFiles": []}
        for literal in (json.dumps(payload), json.dumps([payload]), "progress\n" + json.dumps(payload)):
            result, code = self.run_code("print(" + repr(literal) + ")", profile="collection")
            self.assertEqual(code, 0)
            self.assertEqual(result["summary"]["counts"]["articles"], 0)
            self.assertEqual(result["summary"]["resultStatus"], "saved")

    def test_weekly_provisional_and_warnings_preserved_without_full_warning_text(self):
        payload = {"status": "provisional", "checked": True, "coveredThrough": "2026-09-10",
                   "counts": {name: 0 for name in operation.WEEKLY_COUNTS}, "warnings": ["PRIVATE_WARNING"]}
        payload["counts"]["videoMetadataSummaries"] = None
        result, code = self.run_code("print(" + repr(json.dumps(payload)) + ")", profile="weekly")
        self.assertEqual(code, 0)
        self.assertTrue(result["attentionRequired"])
        self.assertEqual(result["summary"]["resultStatus"], "provisional")
        self.assertTrue(result["summary"]["checked"])
        self.assertEqual(result["summary"]["warningCount"], 1)
        self.assertIsNone(result["summary"]["counts"]["videoMetadataSummaries"])
        self.assertNotIn("PRIVATE_WARNING", json.dumps(result))

    def test_invalid_counts_are_not_cast_or_silently_defaulted_to_zero(self):
        payload = {"saved": True, "date": "2026-09-10", "articleCount": 0, "candidateCount": 0, "writtenFiles": []}
        for value in (True, -1, "100", 1.5, None):
            with self.subTest(value=value):
                self.assertEqual(operation.summarize_payload({**payload, "articleCount": value},
                                 "collection", None)["resultStatus"], "unrecognized")

    def test_missing_executable_has_short_result(self):
        result, code = operation.run_operation([str(self.logs / "missing")], step="missing", log_dir=self.logs)
        self.assertEqual(code, 127)
        self.assertEqual(result["processStatus"], "launch_failed")
        self.assertTrue(Path(result["logs"]["result"]).exists())

    def test_timeout_has_result_and_nonzero_exit(self):
        result, code = self.run_code("import time; time.sleep(30)", timeout=0.05)
        self.assertEqual(code, 124)
        self.assertEqual(result["processStatus"], "timed_out")
        self.assertTrue(result["attentionRequired"])

    def test_retries_do_not_happen_and_same_step_does_not_overwrite(self):
        first, _ = self.run_code("print('first')")
        second, _ = self.run_code("print('second')")
        self.assertNotEqual(first["logs"]["result"], second["logs"]["result"])
        self.assertEqual(Path(first["logs"]["stdout"]).read_text(), "first\n")
        self.assertEqual(Path(second["logs"]["stdout"]).read_text(), "second\n")
        if os.name == "posix":
            self.assertEqual(Path(first["logs"]["result"]).parent.stat().st_mode & 0o077, 0)

    def test_cli_single_line_summary_and_argument_validation(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = operation.main(["--step", "test-cli", "--log-dir", str(self.logs),
                                   "--", sys.executable, "-c", "print('do not echo me')"])
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        self.assertNotIn("do not echo me", stdout.getvalue())
        for options in (["--step", "../bad"], ["--step", "ok", "--timeout", "nan"],
                        ["--step", "ok", "--run-date", "2026-02-30"], ["--step", "ok"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                operation.main(options)
            self.assertEqual(caught.exception.code, 2)

    def test_large_non_json_is_bounded_and_invalid_utf8_does_not_crash(self):
        path = self.logs / "data"
        path.write_bytes(b"x" * (operation.MAX_JSON_BYTES + 20))
        self.assertIsNone(operation.read_payload(path))
        path.write_bytes(b"\xff\xfe")
        self.assertIsNone(operation.read_payload(path))


if __name__ == "__main__":
    unittest.main()
