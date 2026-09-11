from __future__ import annotations

import base64
import contextlib
import copy
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import autoarticle_ops as ops
import autoarticle_n8n as n8n
import autoarticle_apply as apply
from autoarticle_progress import Blocked, Progress, read_json, write_json

DATE = "2026-09-10"


class FixtureClient:
    base = "http://127.0.0.1:5678"
    def __init__(self):
        self.calls = []
        self.workflows = {}
        self.rows = []
        self.details = {}
        self.post = None
        self.error = None

    def request(self, path, body=None, **kwargs):
        self.calls.append((path, body))
        if self.error:
            raise Blocked(self.error)
        if body is not None:
            return self.post(path, body)
        if path == "/healthz":
            return {"status": "ok"}
        if path.startswith("/workflows/"):
            return copy.deepcopy(self.workflows[path.split("/")[-1]])
        if path.startswith("/executions/"):
            return copy.deepcopy(self.details[path.split("/")[-1].split("?")[0]])
        raise AssertionError(path)

    def pages(self, path):
        self.calls.append((path, None))
        if self.error:
            raise Blocked(self.error)
        if path.startswith("/workflows?"):
            return list(self.workflows.values())
        return copy.deepcopy(self.rows)


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = FixtureClient()
        self.operator = ops.Operations(self.root, DATE, self.client)
        self.p = self.operator.progress
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        for kind, (stem, _) in n8n.WORKFLOWS.items():
            workflow = {"id": kind, "name": kind, "active": True, "nodes": [], "connections": {}, "settings": {"timezone": "Asia/Tokyo"}}
            self.client.workflows[kind] = workflow
            write_json(self.root / "n8n/workflows" / (stem + ".workflow.json"), workflow)

    def file(self, relative, text="review evidence"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def review(self, step):
        self.file(self.p.generated()["structured-records"], "{}\n")
        for relative in self.p.outputs(step):
            if not (self.root / relative).exists():
                self.file(relative)
        evidence = self.p.outputs(step)[-1] if self.p.outputs(step) else "content/page-check.json"
        if not (self.root / evidence).exists():
            self.file(evidence)
        self.p.checkpoint(step, [evidence], "Reviewed; holds retained")

    def collected(self):
        header = {"recordType": "run", "runDate": DATE, "generatedAt": DATE + "T00:01:00Z", "capturedArticleCount": 0}
        for relative in self.p.generated().values():
            self.file(relative, json.dumps(header) + "\n" if relative.endswith("jsonl") else "generated text " + relative)
        row = {"id": "e1", "workflowId": "collect", "status": "success", "startedAt": DATE + "T00:00:00Z", "stoppedAt": DATE + "T00:02:00Z"}
        self.client.rows = [row]
        items = [{"json": {"relativePath": relative, "date": DATE}, "binary": {"data": {"data": base64.b64encode((self.root / relative).read_bytes()).decode()}}} for relative in self.p.generated().values() if relative.endswith("md")]
        node = {"date": DATE, "capturedArticleCount": 0}
        self.client.details["e1"] = dict(row, data={"resultData": {"runData": {
            "Build Structured Records": [{"data": {"main": [[{"json": node}]]}}],
            "Build Markdown Files": [{"data": {"main": [items]}}]}}})

    def talent_proposal(self):
        value = {"proposalVersion": 1, "proposalDate": DATE, "articles": [], "talents": [], "articleTalents": []}
        write_json(self.root / self.p.outputs("talent-review")[0], value)
        self.review("talent-review")
        return value

    def test_status_is_read_only_short_and_unknown_is_not_zero(self):
        result = self.operator.status()
        self.assertEqual(result["executions"]["counts"], {})
        self.assertEqual(result["artifactVerification"], "existence_only")
        self.assertLess(len(json.dumps(result)), 2500)
        self.assertFalse(self.p.directory.exists())
        self.assertTrue(all(body is None for _, body in self.client.calls))
        self.client.error = "api_key_missing"
        result = self.operator.status()
        self.assertEqual(result["executions"]["status"], "unknown")
        self.assertNotIn("counts", result["executions"])

    def test_workflow_diff_and_wrong_id_block_mutation(self):
        self.client.workflows["collect"]["settings"]["timezone"] = "UTC"
        with self.assertRaisesRegex(Blocked, "definition_differs"):
            self.operator.workflow("collect", require=True)
        with patch.dict(os.environ, {"N8N_WORKFLOW_ID": "talent"}):
            with self.assertRaisesRegex(Blocked, "identity_mismatch"):
                self.operator.workflow("collect")

    def test_inactive_workflow_blocks(self):
        self.client.workflows["collect"]["active"] = False
        with self.assertRaisesRegex(Blocked, "not_active"):
            self.operator.workflow("collect", require=True)

    def test_jst_date_boundary_and_previous_day_running(self):
        self.client.rows = [
            {"id": "1", "workflowId": "collect", "status": "success", "startedAt": "2026-09-09T15:00:00Z"},
            {"id": "2", "workflowId": "collect", "status": "running", "startedAt": "2026-09-09T14:00:00Z"}]
        selected, running = n8n.executions(self.client, "collect", DATE)
        self.assertEqual([r["id"] for r in selected], ["1"])
        self.assertEqual([r["id"] for r in running], ["2"])

    def test_execution_identity_and_missing_time_fail_closed(self):
        self.client.rows = [{"id": "1", "workflowId": "other"}]
        with self.assertRaisesRegex(Blocked, "workflow_mismatch"):
            n8n.executions(self.client, "collect", DATE)
        self.client.rows[0]["workflowId"] = "collect"
        with self.assertRaisesRegex(Blocked, "time_unknown"):
            n8n.executions(self.client, "collect", DATE)

    def test_collect_never_posts_old_date_or_running_or_failed(self):
        with patch.object(ops, "today", return_value="2026-09-11"):
            with self.assertRaisesRegex(Blocked, "only_supports_today"):
                self.operator.collect()
        with patch.object(ops, "today", return_value=DATE):
            for state in ("running", "error", "unknown"):
                self.client.rows = [{"id": "1", "workflowId": "collect", "status": state, "startedAt": DATE + "T00:00:00Z"}]
                with self.assertRaises(Blocked):
                    self.operator.collect()
        self.assertTrue(all(body is None for _, body in self.client.calls))

    def test_collect_timeout_intent_survives_and_blocks_retry(self):
        def timeout(path, body):
            self.assertEqual(self.p.load()["steps"]["collect"]["status"], "submission_unknown")
            raise Blocked("connection_unavailable_or_timeout")
        self.client.post = timeout
        with patch.object(ops, "today", return_value=DATE):
            with self.assertRaises(Blocked):
                self.operator.collect()
            with self.assertRaisesRegex(Blocked, "existing_collection"):
                self.operator.collect()
        self.assertEqual(sum(body is not None for _, body in self.client.calls), 1)

    def test_collect_reuses_success_only_after_execution_and_content_check(self):
        self.collected()
        with patch.object(ops, "today", return_value=DATE):
            result = self.operator.collect()
        self.assertEqual(result["status"], "reused")
        self.assertEqual(self.p.load()["steps"]["collect"]["executionId"], "e1")
        self.assertTrue(all(body is None for _, body in self.client.calls))
        self.file(self.p.generated()["ai-summary-instructions"], "changed")
        self.assertEqual(self.operator.resume()["progress"]["collect"], "stale")
        with self.assertRaisesRegex(Blocked, "markdown_content_mismatch"):
            self.operator.verify_collection("e1")

    def test_collect_new_post_records_completed_after_validation(self):
        def complete(path, body):
            self.collected()
            return {"saved": True, "date": DATE}
        self.client.post = complete
        with patch.object(ops, "today", return_value=DATE):
            self.assertEqual(self.operator.collect()["status"], "completed")

    def test_collect_terminal_branch_response_uses_execution_evidence(self):
        def complete(path, body):
            self.collected()
            return {"fileName": "records.jsonl"}
        self.client.post = complete
        with patch.object(ops, "today", return_value=DATE):
            result = self.operator.collect()
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["webhookResponseVerified"])
        self.assertEqual(sum(body is not None for _, body in self.client.calls), 1)

    def test_collect_terminal_response_without_evidence_remains_unknown(self):
        self.client.post = lambda path, body: {"fileName": "records.jsonl"}
        with patch.object(ops, "today", return_value=DATE):
            with self.assertRaisesRegex(Blocked, "execution_unresolved"):
                self.operator.collect()
        self.assertEqual(self.p.load()["steps"]["collect"]["status"], "submission_unknown")

    def test_collect_terminal_response_rejects_tampered_files(self):
        def complete(path, body):
            self.collected()
            self.file(self.p.generated()["ai-summary-instructions"], "tampered")
            return None
        self.client.post = complete
        with patch.object(ops, "today", return_value=DATE):
            with self.assertRaisesRegex(Blocked, "markdown_content_mismatch"):
                self.operator.collect()
        self.assertEqual(sum(body is not None for _, body in self.client.calls), 1)

    def test_execution_detail_has_separate_bounded_response_limit(self):
        response = Mock()
        response.read.return_value = b"{}"
        opened = Mock()
        opened.__enter__ = Mock(return_value=response)
        opened.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = opened
        with patch.object(n8n.urllib.request, "build_opener", return_value=opener):
            client = n8n.Client("http://localhost:5678", "test")
            client.request("/executions/159?includeData=true", api=True)
            response.read.assert_called_with(256 * 1024 * 1024 + 1)
            client.request("/workflows/test", api=True)
            response.read.assert_called_with(32 * 1024 * 1024 + 1)

    def test_resume_reconciles_unknown_collection_without_post_or_state_write(self):
        self.p.record("collect", "submission_unknown", files={}, workflowId="collect")
        self.collected()
        before = self.p.file.read_bytes()
        self.assertEqual(self.operator.resume()["progress"]["collect"], "completed")
        self.assertEqual(self.p.file.read_bytes(), before)
        self.assertTrue(all(body is None for _, body in self.client.calls))

    def test_resume_unknown_collection_stays_unknown_without_proof(self):
        self.p.record("collect", "submission_unknown", files={}, workflowId="collect")
        self.assertEqual(self.operator.resume()["progress"]["collect"], "submission_unknown")

    def test_resume_reconciles_unknown_apply_without_post(self):
        self.talent_proposal()
        entry = self.p.load()["steps"]["talent-review"]
        self.p.record("apply-talent", "submission_unknown", files=entry["files"])
        before = self.p.file.read_bytes()
        with patch.object(apply, "tables", return_value={}):
            self.assertEqual(self.operator.resume()["progress"]["apply-talent"], "completed")
        self.assertEqual(self.p.file.read_bytes(), before)
        self.assertTrue(all(body is None for _, body in self.client.calls))

    def test_collection_different_archive_time_blocks(self):
        self.collected()
        path = self.root / self.p.generated()["structured-records"]
        header = json.loads(path.read_text())
        header["generatedAt"] = "2026-09-09T00:01:00Z"
        path.write_text(json.dumps(header) + "\n")
        with self.assertRaisesRegex(Blocked, "execution_time"):
            self.operator.verify_collection("e1")

    def test_checkpoint_requires_outputs_and_evidence(self):
        with self.assertRaisesRegex(Blocked, "missing"):
            self.p.checkpoint("summary", ["missing.md"], "checked")
        self.review("summary")
        entry = self.p.load()["steps"]["summary"]
        self.assertEqual(entry["verification"], "operator_attested")
        self.assertTrue(self.p.current(entry))
        self.file(self.p.outputs("summary")[0], "edited")
        self.assertFalse(self.p.current(entry))

    def test_missing_input_added_and_target_changed_invalidate_checkpoint(self):
        self.review("summary")
        entry = self.p.load()["steps"]["summary"]
        self.file("content/article-body-captures/%s.jsonl" % DATE)
        self.assertFalse(self.p.current(entry))
        other = Progress(self.root, DATE, "http://127.0.0.1:5679")
        self.assertFalse(other.current(entry))

    def test_evidence_cannot_escape_project_and_invalid_state_not_reset(self):
        with self.assertRaisesRegex(Blocked, "outside_project"):
            self.p.path("../secret")
        write_json(self.p.file, {"schemaVersion": 9})
        with self.assertRaisesRegex(Blocked, "invalid_progress"):
            self.p.load()

    def test_lock_prevents_concurrent_mutations(self):
        with self.p.lock():
            with self.assertRaisesRegex(Blocked, "another_operation"):
                with self.p.lock():
                    pass

    def test_resume_is_read_only_and_does_not_assume_unrecorded_steps_done(self):
        self.review("summary")
        before = self.p.file.read_bytes()
        result = self.operator.resume()
        self.assertEqual(result["progress"]["summary"], "completed")
        self.assertEqual(result["progress"]["apply-talent"], "not_recorded")
        self.assertEqual(self.p.file.read_bytes(), before)

    def test_page_requires_new_display_check_after_resume(self):
        self.review("page")
        old = self.p.load()["steps"]["page"]
        self.p.record("page", "completed", files=old["files"], url=self.operator.dashboard)
        with patch.object(n8n.Client, "request", return_value=None):
            self.assertEqual(self.operator.resume()["progress"]["page"], "needs_display_recheck")

    def test_recent_display_reused_but_expiry_and_new_apply_require_recheck(self):
        self.review("page")
        with patch.object(n8n.Client,"request",return_value=None):
            self.operator.checkpoint("page",["content/page-check.json"],"Screenshots and DOM checked")
            self.assertEqual(self.operator.resume()["progress"]["page"],"completed")
            entry=self.p.load()["steps"]["page"]
            with patch("autoarticle_progress.now",return_value="2099-01-01T00:00:00+00:00"):
                self.assertFalse(self.p.display_recent(entry))
            self.p.record("dashboard","completed",files={},url=self.operator.dashboard)
            self.assertFalse(self.p.display_recent(entry))

    def test_start_reachable_does_not_launch_or_collect(self):
        with patch.object(ops.subprocess, "Popen") as launch:
            result = self.operator.start("n8n")
        self.assertEqual(result["status"], "already_reachable")
        launch.assert_not_called()
        self.assertEqual(set(self.p.load()["steps"]), {"n8n"})

    def test_start_occupied_unhealthy_port_does_not_spawn(self):
        self.client.error = "http_500"
        with patch.object(ops.socket, "create_connection", return_value=contextlib.nullcontext()), patch.object(ops.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(Blocked, "port_occupied"):
                self.operator.start("n8n")
        launch.assert_not_called()

    def test_start_detaches_logs_and_does_not_inherit_env_file_secrets(self):
        self.operator.startup_env = {"PATH": "/usr/bin"}
        self.client.request = Mock(side_effect=[Blocked("offline"), {"status": "ok"}])
        process = Mock(pid=23456)
        process.poll.return_value = None
        with patch.dict(os.environ, {"N8N_ENCRYPTION_KEY": "do-not-inject"}), patch.object(ops.socket, "create_connection", side_effect=ConnectionRefusedError), patch.object(ops.subprocess, "Popen", return_value=process) as launch:
            result = self.operator.start("n8n")
        self.assertEqual(result["status"], "reachable")
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        self.assertNotIn("N8N_ENCRYPTION_KEY", launch.call_args.kwargs["env"])
        self.assertTrue(Path(result["log"]).is_file())

    def test_slow_start_returns_pending_without_false_failure(self):
        self.client.request=Mock(side_effect=Blocked("offline"))
        process=Mock(pid=23456);process.poll.return_value=None
        with patch.object(ops.socket,"create_connection",side_effect=ConnectionRefusedError), patch.object(ops.subprocess,"Popen",return_value=process) as launch, patch.object(ops.time,"sleep"):
            result=self.operator.start("n8n")
        self.assertEqual(result["status"],"starting")
        self.assertEqual(result["retryAfterSeconds"],30)
        self.assertEqual(self.p.load()["steps"]["n8n"]["status"],"starting")
        launch.assert_called_once()

    def test_apply_requires_current_review(self):
        with self.assertRaisesRegex(Blocked, "review_missing"):
            self.operator.apply("talent")
        self.talent_proposal()
        self.file(self.p.outputs("talent-review")[1], "changed")
        with self.assertRaisesRegex(Blocked, "review_missing"):
            self.operator.apply("talent")

    def test_apply_timeout_never_resubmits_and_can_reconcile_db(self):
        self.talent_proposal()
        self.client.post = Mock(side_effect=Blocked("timeout"))
        with patch.object(apply, "tables", return_value={}):
            with self.assertRaisesRegex(Blocked, "timeout"):
                self.operator.apply("talent")
            self.assertEqual(self.p.load()["steps"]["apply-talent"]["status"], "submission_unknown")
            result = self.operator.apply("talent")
        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(self.client.post.call_count, 1)

    def test_apply_bad_response_does_not_mark_db_complete(self):
        self.talent_proposal()
        self.client.post = Mock(return_value={"accepted": True, "proposalDate": DATE, "counts": {"articles": 999}})
        with patch.object(apply, "tables", return_value={}):
            with self.assertRaisesRegex(Blocked, "response_unverified"):
                self.operator.apply("talent")
        self.assertEqual(self.p.load()["steps"]["apply-talent"]["status"], "submission_unknown")

    def test_apply_all_order_and_partial_failure(self):
        # Bounded dispatch must be talent first, classification second.
        original = ops.Operations.apply
        self.operator.apply = Mock(side_effect=[{"step": "apply-talent"}, {"step": "apply-classification"}])
        self.assertEqual(len(original(self.operator, "all")["steps"]), 2)
        self.assertEqual([c.args[0] for c in self.operator.apply.call_args_list], ["talent", "classification"])
        self.operator.apply = Mock(side_effect=[{"step": "apply-talent"}, Blocked("stop")])
        with self.assertRaisesRegex(Blocked, "stop"):
            original(self.operator, "all")
        self.assertEqual(self.operator.apply.call_count, 2)

    def test_weekly_validation_does_not_accept_arbitrary_zero_exit(self):
        with patch.object(ops.subprocess, "run", return_value=Mock(returncode=0, stdout=b'{}')):
            with self.assertRaisesRegex(Blocked, "response_invalid"):
                self.operator.check_weekly()

    def test_cli_help_is_nonmutating_and_bad_date_is_short_json(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with patch.object(ops, "ROOT", self.root):
                code = ops.main(["--date", "not-date", "status"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())["status"], "blocked")
        self.assertFalse(self.p.directory.exists())


class ClientTests(unittest.TestCase):
    def test_pagination_and_incomplete_history(self):
        client = n8n.Client("http://localhost:5678/api/v1", "secret")
        client.request = Mock(side_effect=[{"data": [{"id": "1"}], "nextCursor": "a/b"}, {"data": [{"id": "2"}]}])
        self.assertEqual(len(client.pages("/executions?limit=100")), 2)
        self.assertIn("cursor=a%2Fb", client.request.call_args.args[0])
        client.request = Mock(return_value={"data": [], "nextCursor": "same"})
        with self.assertRaisesRegex(Blocked, "repeated_cursor"):
            client.pages("/executions?limit=100")

    def test_no_api_key_and_redirects_fail_closed(self):
        client = n8n.Client("http://localhost:5678", "")
        with self.assertRaisesRegex(Blocked, "api_key_missing"):
            client.request("/workflows", api=True)
        self.assertIsNone(n8n.NoRedirect().redirect_request(None, None, 302, "", {}, "http://elsewhere"))
        with self.assertRaisesRegex(Blocked, "invalid_base_url"):
            n8n.Client("http://user:secret@localhost", "")


class DatabaseTests(unittest.TestCase):
    def test_no_automatic_talent_approval_or_search_change(self):
        value = {"articles": [], "articleTalents": [], "talents": [{"talent_id": "t", "display_name": "T", "organization": "", "aliases_json": [], "status": "approved", "search_enabled": False, "auto_discovered": True, "last_seen_at": DATE}]}
        with self.assertRaisesRegex(Blocked, "approval_or_search"):
            apply.preflight("talent", value, {})
        value["talents"][0]["status"] = "pending"
        apply.preflight("talent", value, {})

    def test_classification_requires_registered_article_and_exact_db_content(self):
        row = {"article_url": "https://example.com/a", "article_type": "news_article", "primary_category": "event", "secondary_categories_json": [], "relevance": "in_scope", "confidence": 0.9, "evidence_text": "body evidence", "classification_method": "ai_review", "classified_at": DATE}
        value = {"classifications": [row]}
        with self.assertRaisesRegex(Blocked, "not_registered"):
            apply.expected("classification", value, {})
        current = {"articles": [{"url": row["article_url"], "article_key": "a"}]}
        stored = dict(row, article_key="a", secondary_categories_json="[]")
        stored.pop("article_url")
        current["article_classifications"] = [stored]
        apply.verify("classification", value, current)
        stored["confidence"] = 0.1
        with self.assertRaisesRegex(Blocked, "content_mismatch"):
            apply.verify("classification", value, current)

    def test_sqlite_read_only_and_workflow_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "database.sqlite"
            with sqlite3.connect(str(path)) as db:
                db.execute("CREATE TABLE workflow_entity (id TEXT, name TEXT)")
                db.execute("INSERT INTO workflow_entity VALUES ('w', 'Apply')")
                db.execute("CREATE TABLE data_table (id TEXT, name TEXT)")
                db.execute("INSERT INTO data_table VALUES ('abc', 'articles')")
                db.execute("CREATE TABLE data_table_user_abc (article_key TEXT, url TEXT)")
                db.execute("INSERT INTO data_table_user_abc VALUES ('a', 'url')")
            before = path.read_bytes()
            with patch.dict(os.environ, {"N8N_DATABASE_PATH": str(path)}):
                self.assertEqual(len(apply.tables("http://localhost:5678", {"id": "w", "name": "Apply", "nodes": []})["articles"]), 1)
                with self.assertRaisesRegex(Blocked, "identity_mismatch"):
                    apply.tables("http://localhost:5678", {"id": "other", "name": "Apply", "nodes": []})
                with self.assertRaisesRegex(Blocked, "requires_local"):
                    apply.tables("https://remote.example", {"id": "w", "name": "Apply", "nodes": []})
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
