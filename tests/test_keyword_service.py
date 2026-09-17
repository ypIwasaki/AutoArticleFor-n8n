"""Keyword contracts with temporary files/databases and blocked outbound HTTP."""

from __future__ import annotations

from contextlib import closing
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib import error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import keyword_service as keywords


class KeywordServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime.sqlite"
        self.service = keywords.KeywordService(self.root, self.runtime)
        self.config_path = self.root / "config/keywords.json"
        self.config_path.parent.mkdir()
        self.original_config = {
            "manualKeywords": ["Alpha!", "Beta"],
            "excludedKeywords": ["Excluded"],
            "maxAutoKeywords": 30,
            "customSetting": {"keep": True},
        }
        self.config_path.write_text(json.dumps(self.original_config), encoding="utf-8")
        self.http = self.enter_patch(
            keywords.request, "urlopen", side_effect=AssertionError("Unexpected outbound HTTP")
        )
        # Explicit paths must not silently fall back to the real n8n database.
        self.enter_patch(
            keywords.dashboard_data, "database_path",
            side_effect=AssertionError("Unexpected live database lookup"),
        )

    def enter_patch(self, target, name, **kwargs):
        patcher = patch.object(target, name, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def config(self):
        return json.loads(self.config_path.read_text())

    def manage(self, operation, keyword="", previous="", scope="manual"):
        return self.service.manage_keyword({
            "operation": operation,
            "scope": scope,
            "keyword": keyword,
            "previousKeyword": previous,
        })

    def respond(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        response = Mock()
        response.read.return_value = body
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.http.side_effect = None
        self.http.return_value = response

    def create_runtime(self, static_data=None):
        with closing(sqlite3.connect(self.runtime)) as connection:
            connection.execute("CREATE TABLE workflow_entity(name TEXT, staticData TEXT)")
            connection.execute(
                "INSERT INTO workflow_entity VALUES (?, ?)",
                ("Daily Keyword News Summary", json.dumps(static_data or {"global": {"autoKeywords": ["Auto"]}})),
            )
            connection.execute("CREATE TABLE data_table(id TEXT, name TEXT)")
            for name in ("articles", "talents", "article_talents"):
                connection.execute("INSERT INTO data_table VALUES (?, ?)", (name, name))
            connection.execute("CREATE TABLE data_table_user_articles(id INTEGER)")
            connection.execute("CREATE TABLE data_table_user_article_talents(id INTEGER)")
            connection.execute("CREATE TABLE data_table_user_talents(display_name TEXT, status TEXT)")
            connection.executemany(
                "INSERT INTO data_table_user_talents VALUES (?, ?)",
                [("Talent", "pending"), ("talent", "approved"), ("Rejected", "rejected"), ("http://bad", "approved")],
            )
            connection.commit()

    def write_candidates(self, day, rows):
        path = self.root / "content/ai-keyword-candidates" / (day + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "## Candidates\n"
            "| Candidate | Category | Confidence | Add | Reason | Evidence |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
        )
        body = "".join("| " + " | ".join(row) + " |\n" for row in rows)
        path.write_text(header + body + "## Suggested Default Keywords\n", encoding="utf-8")

    def test_manual_add_edit_remove_preserves_other_settings(self):
        self.assertEqual({"status": "added", "keyword": "Gamma"}, self.manage("add", "Gamma"))
        self.assertEqual({"status": "updated", "keyword": "Delta"}, self.manage("edit", "Delta", "Gamma"))
        self.assertEqual({"status": "removed", "keyword": "Delta"}, self.manage("remove", previous="Delta"))
        self.assertEqual(self.original_config, self.config())
        self.http.assert_not_called()
        self.assertFalse(self.config_path.with_suffix(".json.tmp").exists())

    def test_duplicate_and_noop_leave_file_byte_for_byte_unchanged(self):
        before = self.config_path.read_bytes()
        self.assertEqual({"status": "already_added", "keyword": "Alpha!"}, self.manage("add", " alpha！ "))
        self.assertEqual({"status": "unchanged", "keyword": "Alpha!"}, self.manage("edit", "ALPHA!", "Alpha!"))
        self.assertEqual("already_removed", self.manage("remove", previous="Missing")["status"])
        self.assertEqual(before, self.config_path.read_bytes())

    def test_edit_duplicate_and_remove_last_are_rejected(self):
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.manage("edit", "Beta", "Alpha!")
        self.assertEqual(before, self.config_path.read_bytes())
        self.manage("remove", previous="Beta")
        before = self.config_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "At least one"):
            self.manage("remove", previous="Alpha!")
        self.assertEqual(before, self.config_path.read_bytes())

    def test_invalid_requests_never_save_or_send(self):
        invalid = [
            {"operation": "replace", "scope": "manual"},
            {"operation": "add", "scope": "talent", "keyword": "Valid"},
            {"operation": "edit", "scope": "manual", "keyword": "Valid"},
            {"operation": "remove", "scope": "automatic"},
        ]
        invalid.extend(
            {"operation": "add", "scope": "manual", "keyword": term}
            for term in ("x", "x" * 31, "bad|term", "http:term", "bad\nterm", "bad/term")
        )
        before = self.config_path.read_bytes()
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.service.manage_keyword(payload)
        self.assertEqual(before, self.config_path.read_bytes())
        self.http.assert_not_called()

    def test_automatic_webhook_contract_and_manual_config_unchanged(self):
        before = self.config_path.read_bytes()
        self.respond({"status": "updated", "keyword": "New"})
        with patch.dict(keywords.os.environ, {"N8N_KEYWORD_MANAGEMENT_WEBHOOK_URL": "https://n8n.example.test/manage"}):
            result = self.manage("edit", "New", "Old", scope="automatic")
        sent = self.http.call_args.args[0]
        self.assertEqual("https://n8n.example.test/manage", sent.full_url)
        self.assertEqual("POST", sent.get_method())
        self.assertEqual(30, self.http.call_args.kwargs["timeout"])
        self.assertEqual({
            "operation": "edit", "keyword": "New",
            "previousKeyword": "Old", "source": "talent-dashboard",
        }, json.loads(sent.data))
        self.assertEqual("updated", result["status"])
        self.assertEqual(before, self.config_path.read_bytes())

    def test_webhook_rejection_and_malformed_responses_remain_errors(self):
        for response, expected_error in (
            ({"status": "rejected", "reason": "duplicate"}, ValueError),
            (b"not-json", RuntimeError),
            ([], RuntimeError),
        ):
            with self.subTest(response=response):
                self.respond(response)
                with self.assertRaises(expected_error):
                    self.manage("add", "New", scope="automatic")
        self.respond(b"")
        self.assertEqual({}, self.manage("add", "New", scope="automatic"))

    def test_transport_failures_do_not_save_config(self):
        before = self.config_path.read_bytes()
        for failure in (
            error.URLError("offline"),
            error.HTTPError("https://n8n.example.test", 503, "unavailable", {}, io.BytesIO(b"retry later")),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.http.side_effect = failure
                with self.assertRaises(RuntimeError):
                    self.manage("add", "New", scope="automatic")
        self.assertEqual(before, self.config_path.read_bytes())

    def test_explicit_runtime_database_supplies_auto_and_talent_keywords(self):
        self.create_runtime()
        before = self.runtime.read_bytes()
        payload = self.service.management_payload()
        self.assertEqual(["Auto"], payload["automaticKeywords"])
        self.assertEqual(["Talent"], payload["talentKeywords"])
        self.assertEqual("n8n-data-tables", payload["talentKeywordSource"])
        self.assertIsNone(payload["runtimeError"])
        self.assertEqual(before, self.runtime.read_bytes())

    def test_missing_runtime_reports_error_without_creating_database(self):
        automatic, warning = self.service.load_automatic_keywords()
        self.assertEqual([], automatic)
        self.assertIn(str(self.runtime), warning)
        self.assertFalse(self.runtime.exists())

    def test_latest_candidate_states_and_deduplication(self):
        self.create_runtime()
        self.write_candidates("2026-09-16", [("Old", "group", "1", "yes", "reason", "evidence")])
        self.write_candidates("2026-09-17", [
            ("ALPHA！", "group", "1", "yes", "reason", "evidence"),
            ("Talent", "person", "1", "yes", "reason", "evidence"),
            ("New", "group", "bad-number", "yes", "reason", "evidence"),
            ("Skip", "group", "0.2", "no", "reason", "evidence"),
        ])
        payload = self.service.candidate_payload()
        self.assertEqual("2026-09-17", payload["candidateDate"])
        self.assertEqual(["added", "added", "eligible", "not_recommended"], [row["state"] for row in payload["candidates"]])
        self.assertEqual(0.0, payload["candidates"][2]["confidence"])
        self.assertEqual(4, payload["currentKeywordCount"])

    def test_candidate_add_only_sends_latest_recommended_new_term(self):
        self.create_runtime()
        self.write_candidates("2026-09-17", [
            ("Alpha!", "group", "1", "yes", "reason", "evidence"),
            ("New", "group", "1", "yes", "reason", "evidence"),
            ("Skip", "group", "0.1", "no", "reason", "evidence"),
        ])
        self.assertEqual("already_added", self.service.add_candidate("alpha！")["status"])
        for term in ("Missing", "Skip"):
            with self.subTest(term=term), self.assertRaises(ValueError):
                self.service.add_candidate(term)
        self.http.assert_not_called()
        self.respond({"status": "added"})
        self.assertEqual({"status": "added"}, self.service.add_candidate("new"))
        self.assertEqual({
            "keyword": "New", "candidateDate": "2026-09-17", "source": "talent-dashboard",
        }, json.loads(self.http.call_args.args[0].data))

    def test_server_preserves_keyword_status_codes(self):
        spec = importlib.util.spec_from_file_location("keyword_server_test", ROOT / "apps/talent-dashboard/server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        handler = object.__new__(server.DashboardHandler)
        handler.path = "/api/keywords"
        handler.send_json = Mock()
        handler.read_json_body = Mock(return_value={"operation": "add", "scope": "manual", "keyword": "New"})
        with patch.object(server, "keyword_service", return_value=self.service):
            handler.do_POST()
            self.assertEqual(200, handler.send_json.call_args.args[0])
            self.assertEqual("added", handler.send_json.call_args.args[1]["status"])
            handler.read_json_body.return_value["keyword"] = "x"
            handler.do_POST()
            self.assertEqual(400, handler.send_json.call_args.args[0])
            handler.read_json_body.return_value.update(scope="automatic", keyword="New")
            self.http.side_effect = error.URLError("offline")
            handler.do_POST()
            self.assertEqual(502, handler.send_json.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
