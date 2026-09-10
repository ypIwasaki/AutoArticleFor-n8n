import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import autoarticle_ops as ops
import autoarticle_diagnostics as diag
import autoarticle_tokens as tokens
from autoarticle_progress import Blocked, write_json
from token_usage_report import NAMES

class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.client=Mock(base="http://127.0.0.1:5678")
        self.client.request.return_value={"status":"ok"}
        self.op=ops.Operations(self.root,"2026-09-10",self.client)
        self.op.workflow=Mock(return_value=({"id":"wf","active":True,"nodes":[]},True))
        write_json(self.root/"config/keywords.json",dict(manualKeywords=["VTuber"],excludedKeywords=[],maxAutoKeywords=30))
    def test_preflight_is_read_only_and_does_not_fetch_large_execution_details(self):
        with patch.object(diag,"today",return_value="2026-09-10"),patch.object(diag.n8n,"executions",return_value=([],[])):
            result=diag.preflight(self.op,"collect")
        self.assertTrue(result["ready"])
        self.client.request.assert_called_once_with("/healthz",timeout=5)
        self.assertFalse(self.op.progress.file.exists())
        self.assertFalse((self.root/".operation-logs").exists())
    def test_unknown_submission_with_no_execution_is_not_ready(self):
        self.op.progress.record("collect","submission_unknown",files={},workflowId="wf")
        before=self.op.progress.file.read_bytes()
        with patch.object(diag,"today",return_value="2026-09-10"),patch.object(diag.n8n,"executions",return_value=([],[])):
            result=diag.preflight(self.op,"collect")
        self.assertFalse(result["ready"])
        self.assertIn("never_resubmit",result["nextAction"])
        self.assertEqual(before,self.op.progress.file.read_bytes())
    def test_invalid_configuration_is_reported(self):
        write_json(self.root/"config/keywords.json",dict(manualKeywords=[]))
        with patch.object(diag.n8n,"executions",return_value=([],[])):
            result=diag.preflight(self.op,"collect")
        self.assertIn("invalid_manual_keywords",[c.get("reason") for c in result["checks"]])
    def test_stale_review_and_previous_submission_block_apply_preflight(self):
        self.op.progress.record("talent-review","completed",files={"missing":"old"})
        self.op.progress.record("apply-talent","submission_unknown",files={"missing":"old"})
        with patch.object(diag.n8n,"executions",return_value=([],[])),patch.object(diag.db,"proposal",return_value={"articles":[],"talents":[],"articleTalents":[]}),patch.object(diag.db,"tables",return_value={}):
            result=diag.preflight(self.op,"talent")
        self.assertFalse(result["ready"])
        codes=[c.get("reason") for c in result["checks"]]
        self.assertIn("proposal_review_missing_or_stale",codes)
        self.assertIn("previous_apply_inputs_changed_requires_review",codes)
    def test_snapshot_excludes_notes_bodies_and_retains_execution_id(self):
        self.op.progress.record("collect","submission_unknown",files={},workflowId="wf",executionId="123",note="SECRET_BODY_AND_TOKEN")
        before=self.op.progress.file.read_bytes()
        path=self.root/diag.snapshot(self.op,"collect","network_failure")
        data=json.loads(path.read_text())
        self.assertNotIn("SECRET_BODY_AND_TOKEN",path.read_text())
        self.assertEqual(data["failedStepEvidence"]["executionId"],"123")
        self.assertFalse(data["automaticRetry"])
        self.assertEqual(before,self.op.progress.file.read_bytes())
        self.client.request.assert_not_called()
    def test_database_difference_is_bounded_and_values_are_omitted(self):
        rows=[{"key":str(i),"evidence_text":"SECRET_WANTED"} for i in range(30)]
        current={"table":[{"key":str(i),"evidence_text":"SECRET_ACTUAL"} for i in range(30)]}
        with patch.object(diag.db,"expected",return_value=[("table","key",rows)]):
            result=diag.mismatch_summary("talent",{},current)
        self.assertEqual(result["mismatchedRows"],30)
        self.assertEqual(len(result["sample"]),10)
        self.assertNotIn("SECRET",json.dumps(result))
    def test_cli_failure_saves_diagnostic_without_clearing_unknown_submission(self):
        def fail(operation):
            operation.active_step="collect"
            operation.progress.record("collect","submission_unknown",files={},workflowId="wf")
            raise Blocked("response_unverified")
        output=io.StringIO()
        with patch.object(ops,"ROOT",self.root),patch.object(ops,"load_env_file"),patch.object(ops.Operations,"collect",fail),contextlib.redirect_stdout(output):
            code=ops.main(["--date","2026-09-10","collect"])
        result=json.loads(output.getvalue())
        self.assertEqual(code,2)
        self.assertTrue((self.root/result["diagnosticFile"]).is_file())
        self.assertEqual(self.op.progress.load()["steps"]["collect"]["status"],"submission_unknown")
    def test_failed_diagnostic_write_keeps_original_failure(self):
        output=io.StringIO()
        with patch.object(ops,"ROOT",self.root),patch.object(ops,"load_env_file"),patch.object(ops.Operations,"collect",side_effect=Blocked("original_failure")),patch.object(diag,"snapshot",side_effect=OSError("SECRET")),contextlib.redirect_stdout(output):
            self.assertEqual(ops.main(["--date","2026-09-10","collect"]),2)
        result=json.loads(output.getvalue())
        self.assertEqual(result["reason"],"original_failure")
        self.assertEqual(result["diagnosticSaveError"],"OSError")
    def test_investigation_token_step_is_accepted_and_labeled(self):
        output=io.StringIO()
        with patch.object(ops,"load_env_file"),patch.object(tokens,"execute",return_value={"status":"measuring"}) as execute,contextlib.redirect_stdout(output):
            self.assertEqual(ops.main(["--date","2026-09-10","tokens","begin","investigation"]),0)
        self.assertEqual(execute.call_args.args[0].step,"investigation")
        self.assertEqual(NAMES["investigation"],"障害調査")

    def test_resume_explains_failure_without_resubmitting(self):
        self.op.progress.record("collect","completed",files={},executionId="123")
        before=self.op.progress.file.read_bytes()
        with patch.object(self.op,"status",return_value={}),patch.object(self.op,"verify_collection",side_effect=Blocked("collection_content_changed")):
            result=self.op.resume()
        self.assertEqual(result["progress"]["collect"],"unverified")
        self.assertEqual(result["issues"]["collect"],"collection_content_changed")
        self.assertIn("collect",result["nextSteps"])
        self.assertEqual(before,self.op.progress.file.read_bytes())
        self.client.request.assert_not_called()
