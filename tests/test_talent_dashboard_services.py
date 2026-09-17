"""Dashboard service contracts using temporary databases and mocked HTTP."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import article_feedback_service as feedback
import project_database as database
import project_readers
import talent_dashboard_data as dashboard_data


class DashboardServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.generated_at = datetime(2026, 9, 10, 3, tzinfo=timezone.utc)
        self.article = {
            "article_key": "example-article",
            "url": "https://news.google.com/example",
            "title": "Example title - Publisher",
            "source": "Publisher",
        }

    def create_project_database(self):
        path = self.root / "data/autoarticle.sqlite"
        database.migrate(path)
        with closing(database.connect(path)) as connection:
            connection.execute(
                "UPDATE cutover_state SET read_source='project-db', write_target='project-db'"
            )
            connection.execute("UPDATE compatibility_policy SET mode='on-demand'")
        return path

    def test_project_read_route_does_not_open_legacy_database(self):
        self.create_project_database()
        with patch.object(dashboard_data, "database_path", side_effect=AssertionError("legacy route")):
            records, source = dashboard_data.load_dashboard_records(self.root)
        self.assertEqual("project-db", source)
        self.assertEqual([], records["articles"])

    def test_project_database_errors_remain_visible(self):
        self.create_project_database()
        with patch.object(dashboard_data.project, "reader", side_effect=sqlite3.OperationalError("unavailable")):
            with self.assertRaisesRegex(sqlite3.OperationalError, "unavailable"):
                dashboard_data.load_dashboard_records(self.root)

    def test_legacy_tables_preserve_optional_feedback_availability(self):
        legacy_path = self.root / "legacy.sqlite"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.execute("CREATE TABLE data_table(id TEXT, name TEXT)")
            for name in ("articles", "talents", "article_talents"):
                connection.execute("INSERT INTO data_table VALUES (?, ?)", (name, name))
                connection.execute("CREATE TABLE data_table_user_" + name + "(id INTEGER)")
            connection.commit()
        with patch.object(dashboard_data, "database_path", return_value=legacy_path):
            records, source = dashboard_data.load_dashboard_records(self.root)
        self.assertEqual("n8n-data-tables", source)
        self.assertEqual([], records["article_feedback"])
        self.assertFalse(records["_article_feedback_available"])

    def test_supplied_payload_in_archive_never_uses_live_database(self):
        payload = {"articles": [], "article_feedback": []}
        with patch.object(feedback, "load_dashboard_records", side_effect=AssertionError("unexpected read")), \
             patch.object(feedback.business, "route", side_effect=AssertionError("live DB route")), \
             patch.object(feedback.business, "submit", side_effect=AssertionError("live DB write")):
            path = feedback.write_article_feedback_instruction(self.root, payload, self.generated_at)
        self.assertEqual(self.root, path.parents[2])
        self.assertTrue(path.is_file())
        self.assertTrue(json.loads(path.with_suffix(".json").read_text())["complete"])

    def test_project_writer_uses_explicit_root_and_persists_snapshot(self):
        self.create_project_database()
        payload = {"articles": [], "article_feedback": []}
        with patch.object(feedback.business, "submit", wraps=feedback.business.submit) as submit:
            path = feedback.write_article_feedback_instruction(self.root, payload, self.generated_at)
        self.assertEqual(self.root, submit.call_args.kwargs["root"])
        self.assertEqual(self.root / "data/autoarticle.sqlite", submit.call_args.kwargs["path"])
        self.assertTrue(path.is_file())
        self.assertFalse(path.with_suffix(".json").exists())
        with project_readers.reader(self.root) as reader:
            documents = dict(reader.documents("article-feedback-instructions"))
        self.assertEqual([], documents["content/article-feedback-instructions/2026-09-10.json"].get("feedback", []))

    def evaluate(self, decision, reason="", send_error=None):
        output_path = self.root / "content/article-feedback-instructions/2026-09-10.md"
        sender = Mock(return_value={"accepted": True}, side_effect=send_error)
        writer = Mock(return_value=output_path)
        with patch.object(feedback, "load_dashboard_records", return_value=({"articles": [self.article]}, "project-db")), \
             patch.object(feedback, "load_article_capture_metadata", return_value={
                 self.article["url"]: {"source_host": "www.publisher.test"}
             }), patch.object(feedback, "call_article_feedback_webhook", sender), \
             patch.object(feedback, "write_article_feedback_instruction", writer):
            result = feedback.evaluate_article({
                "articleKey": "example-article",
                "decision": decision,
                "reasonCode": reason,
            }, self.root)
        return result, sender, writer

    def test_rejection_uses_resolved_source_and_returns_relative_output(self):
        result, sender, writer = self.evaluate("rejected", "suspicious_source")
        sent = sender.call_args.args[0]
        self.assertEqual("publisher.test", sent["sourceDomain"])
        self.assertEqual("publisher", sent["publisherLabel"])
        self.assertEqual("", sent["titleSignature"])
        self.assertEqual("talent-dashboard", sent["source"])
        self.assertEqual("content/article-feedback-instructions/2026-09-10.md", result["feedbackInstructionFile"])
        writer.assert_called_once_with(self.root)

    def test_approval_clears_rejection_scope(self):
        _, sender, _ = self.evaluate("approved", "irrelevant")
        sent = sender.call_args.args[0]
        self.assertEqual("approved", sent["reasonCode"])
        self.assertEqual(("", "", ""), (sent["sourceDomain"], sent["publisherLabel"], sent["titleSignature"]))

    def test_invalid_decision_does_not_read_or_write(self):
        with patch.object(feedback, "load_dashboard_records") as load, \
             patch.object(feedback, "call_article_feedback_webhook") as send:
            with self.assertRaisesRegex(ValueError, "decision"):
                feedback.evaluate_article({"articleKey": "example-article", "decision": "unknown"}, self.root)
        load.assert_not_called()
        send.assert_not_called()

    def test_failed_feedback_does_not_generate_success_output(self):
        with patch.object(feedback, "load_dashboard_records", return_value=({"articles": [self.article]}, "project-db")), \
             patch.object(feedback, "load_article_capture_metadata", return_value={}), \
             patch.object(feedback, "call_article_feedback_webhook", side_effect=RuntimeError("send failed")), \
             patch.object(feedback, "write_article_feedback_instruction") as write:
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                feedback.evaluate_article({"articleKey": "example-article", "decision": "approved"}, self.root)
        write.assert_not_called()

    def test_server_delegates_feedback_without_changing_api_result(self):
        path = ROOT / "apps/talent-dashboard/server.py"
        spec = importlib.util.spec_from_file_location("dashboard_service_test_server", path)
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        payload = {"articleKey": "example-article", "decision": "approved"}
        with patch.object(server, "PROJECT_ROOT", self.root), \
             patch.object(feedback, "evaluate_article", return_value={"accepted": True}) as evaluate:
            self.assertEqual({"accepted": True}, server.evaluate_article(payload))
        evaluate.assert_called_once_with(payload, self.root)


if __name__ == "__main__":
    unittest.main()
